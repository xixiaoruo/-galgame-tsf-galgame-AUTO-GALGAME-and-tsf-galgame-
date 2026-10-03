"""原生窗口宿主（阶段 4 / M-13b）：pywebview 占主线程，服务器跑后台线程。

⚠️ 与仓库里已有的 `launcher.py` **不是一回事**：那个是 tkinter 的「剧本/启动/分级/插件」
启动器 GUI（并且用 `webbrowser.open` 打开游戏），本文件是**把游戏装进原生窗口**的宿主。
两者并存，互不覆盖。

为什么需要独立入口：
  · pywebview 的 GUI 循环必须跑在**主线程**，而 `server.py` 的 `__main__` 最后会在主线程
    `server.run(sockets=socks)` 里阻塞；
  · 所以本宿主把 **`server.py` 的整段 `__main__` 原样**丢进后台线程执行
    （`runpy.run_path(..., run_name="__main__")`）——**不复制、不改写**它的启动序列
    （启动进度、端口探测、单实例探测、token、bind、优雅停机全部保持原样）；
  · 主线程只做三件事：显示窗口（启动页）→ 等 `/api/version` 就绪 → 把窗口导航到游戏页。

与阶段 4 契约（`tsf-spec/PHASE4-CONTRACT-INPUT.md`）的对应：
  · §1.3 可见性判据：pywebview(WebView2) 路径 = UIA 树里**没有**"地址和搜索栏"元素（达标）；
    回退到 Edge/Chrome `--app=` 时**必须标注"可见性不达标"**（UIA 树里有该元素）。
  · §2 F1（`window.open(...,"_blank")`）：`webbrowser.open*` 在本进程内被换成 no-op，
    杜绝任何"窗口里的操作把系统浏览器拉起来"；前端那一处的修正属 F1 任务（`static/main.js`）。
  · §3 P7/P8：windowed 下没有 stdout，日志必须显式落盘（本文件写 `<DATA_DIR>/window.log`；
    uvicorn 访问日志的改道在 `server.py` 侧属 P8，另行处理）。
  · §3 P2/P3：`webview`/`clr` 的 hiddenimports 与 collect_all 在 `TSF_Galgame.spec` 里声明。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

# pythonnet 默认走 netfx，在本机 venv 里会报
#   "Failed to resolve Python.Runtime.Loader.Initialize from ...Python.Runtime.dll"
# 实测 `PYTHONNET_RUNTIME=coreclr` 可正常 `import clr`（netfx 仍失败）⇒ 在导入 webview
# **之前**设好，免得打包后还要求玩家自己配环境变量。
os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")

PORT = int(os.environ.get("TSF_PORT", "8765") or 8765)   # 与 server.py 同一约定
# 单实例锁：生产恒为 8765 ⇒ 恒用 47653；用 TSF_PORT 做隔离测试时按端口错开，
# 免得"两个不同端口的测试实例互相认为对方已在运行"（测试夹具层面的坑，不影响交付形态）。
FOCUS_LOCK_PORT = 47653 if PORT == 8765 else 47653 + (PORT % 1000)
READY_TIMEOUT = 150.0            # 冷启动（首次解压/模型加载）留足余量


def _is_our_game_running() -> bool:
    """8765 上是否已经有**本游戏**在跑（`/api/config` 带 `llm_mock` 是它的签名）。

    为什么要先问这一句：本宿主与 `server.py` 是同一个进程、同一个端口。若不先判断，
    就会出现「端口被自己的旧实例占着 → 我的 server 线程走单实例分支 sys.exit → 而窗口
    仍然导航到**别人**的服务、关窗时还把**别人**关掉」——本任务就踩到过一次
    （用户正在跑的实例差点被关；事后核对：实例仍在服务、启动时间未变、日志无 shutdown）。
    """
    st, raw = _api("GET", "/api/config", timeout=3.0)
    if st != 200:
        return False
    try:
        return "llm_mock" in (json.loads(raw) or {})
    except Exception:
        return False

SPLASH_HTML = """<!doctype html><meta charset="utf-8">
<style>
 html,body{margin:0;height:100%;background:#14161c;color:#e8eaf0;
   font:15px/1.7 "Microsoft YaHei UI",system-ui;display:flex;
   align-items:center;justify-content:center;flex-direction:column;gap:14px}
 .t{font-size:19px;letter-spacing:.06em}
 .s{color:#8d93a6;font-size:13px}
 .bar{width:260px;height:4px;background:#262a35;border-radius:2px;overflow:hidden}
 .bar i{display:block;height:100%;width:35%;background:#6f8cff;
   border-radius:2px;animation:m 1.15s ease-in-out infinite}
 @keyframes m{0%{margin-left:-35%}100%{margin-left:100%}}
</style>
<div class="t">自动化AI Galgame</div>
<div class="bar"><i></i></div>
<div class="s" id="s">正在启动本地服务…（首次启动较慢，请稍候）</div>
<script>
 const el = document.getElementById('s');
 let n = 0;
 setInterval(() => { n = (n + 1) % 4; el.textContent =
   '正在启动本地服务' + '…'.slice(0, n) + '（首次启动较慢，请稍候）'; }, 700);
</script>"""


def _base_dir() -> str:
    """onedir 冻结形态下资源在 exe 同级目录；源码形态下就是本文件所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _log(msg: str) -> None:
    """windowed 下没有 stdout：日志显式落盘（`<DATA_DIR>/window.log`）。"""
    line = f"[WINDOW] {time.strftime('%H:%M:%S')} {msg}"
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        pass
    try:
        sys.path.insert(0, _base_dir())
        from app import config as cfgmod
        p = cfgmod.DATA_DIR / "window.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_token() -> str:
    """首实例从内存拿 token；第二实例读 `<DATA_DIR>/local_token.json`（t39 的既定通道）。"""
    try:
        import server as srv
        tok = getattr(srv, "_LOCAL_TOKEN", "") or ""
        if tok:
            return tok
    except Exception:
        pass
    try:
        from app import config as cfgmod
        p = cfgmod.DATA_DIR / "local_token.json"
        if p.is_file():
            return (json.loads(p.read_text(encoding="utf-8")) or {}).get("token", "")
    except Exception:
        pass
    return ""


def _api(method: str, path: str, body=None, timeout: float = 5.0):
    """本机回环调用：无 Origin ⇒ t39 中间件按"工具链"放行；仍带 token 以求稳。"""
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    tok = _read_token()
    if tok:
        req.add_header("X-Local-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _start_server_thread(base: str):
    def run():
        try:
            os.chdir(base)
            sys.path.insert(0, base)
            import runpy
            # 冻结形态：server 作为模块在包内（spec 里同时分析 window_host.py 与 server.py）
            try:
                runpy.run_module("server", run_name="__main__")
                return
            except ImportError:
                pass
            # 源码形态（或 server.py 以数据文件随包发出时）：按文件路径跑
            runpy.run_path(os.path.join(base, "server.py"), run_name="__main__")
        except SystemExit as e:
            _log(f"server __main__ SystemExit: {e.code}")
        except Exception:
            _log("server __main__ 异常:\n" + traceback.format_exc())
    t = threading.Thread(target=run, name="tsf-server", daemon=True)
    t.start()
    return t


def _wait_ready(deadline_s: float):
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        st, _ = _api("GET", "/api/version", timeout=2.0)
        if st == 200:
            return True, int((time.time() - t0) * 1000)
        time.sleep(0.35)
    return False, int((time.time() - t0) * 1000)


def _focus_first_instance() -> bool:
    st, raw = _api("POST", "/api/window/focus", {}, timeout=5.0)
    _log(f"第二实例请求聚焦: status={st} body={str(raw)[:120]}")
    return st == 200


def _notice(title: str, text: str) -> None:
    """windowed 无控制台：降级/失败必须可见，不得静默。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def _fallback_launch(url: str, orig_opens: dict) -> str:
    """回退链：Edge --app= → Chrome --app= → 系统浏览器（并报告可见性不达标）。"""
    import shutil
    import subprocess
    cands = [(shutil.which("msedge"), "Edge"),
             (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Edge"),
             (shutil.which("chrome"), "Chrome"),
             (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Chrome")]
    for exe, label in cands:
        if not exe or not os.path.isfile(exe):
            continue
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,860"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _log(f"回退 {label} --app=：UIA 树里有地址栏元素 ⇒ 可见性不达标")
            return f"{label} --app"
        except Exception as e:
            _log(f"{label} 回退失败: {e}")
    try:
        orig_opens["open"](url)
        _log("回退系统浏览器：可见性不达标")
        return "system-browser"
    except Exception as e:
        _log(f"系统浏览器回退失败: {e}")
        return ""


def _run_qt(url: str) -> str:
    """第二级首选：PySide6 + QtWebEngine（**无浏览器外壳**的原生窗口，不依赖 .NET）。

    为什么加这一级：本机 pywebview 的 Windows 后端不可用 —— pythonnet+CoreCLR 载入的是
    .NET 6 WinForms，而 `Microsoft.Web.WebView2.WinForms` 与 pywebview 的 COM 互操作路径
    是按 .NET Framework 版 WinForms 的 API 面写成的（详见 `tsf-fix/NATIVE-WINDOW.md` §3/§7）。
    QtWebEngine 是 Chromium 内嵌，**整条 .NET 依赖链都不参与**；窗口里只有网页视图本身
    （没有地址栏/标签栏/工具栏）⇒ 同样满足"不再开浏览器"的可见性判据。

    返回 `"qt"` = 成功（事件循环已结束并已请求停机）；`""` = 不可用，调用方继续往下回退。
    """
    try:
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication, QMainWindow
    except Exception as e:
        _log(f"QtWebEngine 不可用：{type(e).__name__}: {e}")
        return ""

    # ---- 下载落盘（t49 缺口修复）----
    # QtWebEngine **不会**自动保存下载：宿主必须接 `QWebEngineProfile.downloadRequested`
    # 并 `accept()`，否则页面里点「⬇ 下载」（档案/更新包）会**静默无反应**。
    # 目录选择依据：跟应用其余写盘**同一来源**（`app.config.DATA_DIR`，即
    # “游戏专属数据目录”；玩家双击启动时它= `D:\TSF_Galgame_Data\data`），
    # 在其下建 `downloads/`，与 saves/cache/cg 同级；**不写 exe 目录**（免安装目录只读/UAC）。
    # 隔离验收时用 `TSF_DATA_DIR` 把它整体重定向到工作区样本，因此不会碰到玩家数据。
    def _download_dir():
        import pathlib
        try:
            from app import config as _cfg
            d = pathlib.Path(_cfg.DATA_DIR) / "downloads"
        except Exception:
            d = pathlib.Path(_base_dir()) / "downloads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _on_download(req):
        try:
            d = _download_dir()
            name = (req.suggestedFileName() or "archive.zip")
            try:
                req.setDownloadDirectory(str(d))
                req.setDownloadFileName(name)
            except AttributeError:            # 旧版 API 兜底
                req.setPath(str(d / name))
            req.accept()
            _log(f"下载已接受：{name} → {d}")
        except Exception as e:
            _log(f"下载接受失败：{type(e).__name__}: {e}")
            try:
                req.cancel()
            except Exception:
                pass

    app = QApplication.instance() or QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle("自动化AI Galgame")
    win.resize(1280, 860)
    view = QWebEngineView()                 # 只有网页视图 ⇒ 树里没有地址栏元素
    win.setCentralWidget(view)
    view.setHtml(SPLASH_HTML)
    win.show()
    _log("QtWebEngine 窗口已显示（启动页），等待服务就绪")

    # 接上下载请求（未接 = 页面里的下载按钮静默无反应）
    try:
        _prof = view.page().profile()
        _dd = _download_dir()
        try:
            _prof.setDownloadPath(str(_dd))     # 默认下载目录（Qt 6）
        except Exception:
            pass
        _prof.downloadRequested.connect(_on_download)
        _log(f"下载通道已接：downloadRequested → {_dd}")
    except Exception as e:
        _log(f"下载通道接线失败（下载将不可用）：{type(e).__name__}: {e}")

    # 测试钩子（仅当显式设置时才生效）：把窗口指向另一个同源页，
    # 用于**在冻结产物里**验证"页面发起的下载会被 accept 并落盘"。
    _override = os.environ.get("TSF_WINDOW_URL_OVERRIDE", "").strip()
    if _override:
        _log(f"测试钩子 TSF_WINDOW_URL_OVERRIDE={_override}")
        url = _override

    timer = QTimer()

    def try_navigate():
        ok, ms = _wait_ready(2.5)
        if ok:
            view.setUrl(QUrl(url))
            _log(f"服务就绪 {ms} ms → Qt 窗口导航 {url}")
            timer.stop()
        # 未就绪则继续等（下一次 timeout 再试）

    timer.timeout.connect(try_navigate)
    timer.start(400)

    def _on_close(ev):
        _log("Qt 窗口关闭 → 请求优雅停机")
        _log(f"shutdown status={_api('POST', '/api/shutdown', {}, timeout=10.0)[0]}")
        app.quit()
        return True
    # 用 MethodType 正确绑定（签名 (self, event)）；直接赋函数会给 Qt 多传一个 self 而报 TypeError
    import types as _types
    win.closeEvent = _types.MethodType(lambda self, e: _on_close(e), win)  # type: ignore[method-assign]

    app.exec()
    _log("Qt 事件循环结束")
    return "qt"


def main() -> int:
    import webbrowser
    orig = {"open": webbrowser.open, "open_new_tab": webbrowser.open_new_tab}
    base = _base_dir()
    url = f"http://127.0.0.1:{PORT}/"

    # ★ 安全取证（t49 契约 ⑤）：交付物**绝不自行设置** `--no-sandbox`。
    # 这里只**记录**当前取值（冻结产物在干净环境下应为 None）；开发沙箱里为了
    # 让 Qt renderer 起来，才由外部 shell 设 `QTWEBENGINE_CHROMIUM_FLAGS`。
    _qtflags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS")
    if _qtflags and "--no-sandbox" in _qtflags:
        _log("启动自检：QTWEBENGINE_CHROMIUM_FLAGS=" + repr(_qtflags)
             + "  ⚠ 含 --no-sandbox（降低 Chromium 隔离等级，仅开发环境应如此）")
    else:
        _log("启动自检：QTWEBENGINE_CHROMIUM_FLAGS=" + repr(_qtflags)
             + "  （未设置 / 不含 --no-sandbox）")
    _log("启动自检：frozen=" + str(getattr(sys, "frozen", False))
         + " stdout=" + repr(sys.stdout) + " stderr=" + repr(sys.stderr))
    # windowed 下 stdout/stderr 为 None：证明"写入 None 不会抛异常"（print 是 no-op）
    try:
        print("[WINDOW] stdout probe（windowed 下应为 no-op）", flush=True)
        _log("启动自检：print 到 stdout OK（stdout=None 时为 no-op，不抛异常）")
    except Exception as e:
        _log("启动自检：print 到 stdout 抛异常（不应发生）："
             + type(e).__name__ + ": " + str(e))

    lock = None
    try:
        import socket
        lock = socket.socket()
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        lock.bind(("127.0.0.1", FOCUS_LOCK_PORT))
        lock.listen(1)
    except OSError:
        if not _focus_first_instance():
            _notice("自动化AI Galgame", "游戏似乎已在运行，但没有找到它的窗口。\n"
                                       "请在任务栏里切换到已有的游戏窗口。")
        return 0

    # 端口上已有本游戏在跑（例如手动起的 server）：**绝不托管、更不关它**
    if _is_our_game_running():
        _log(f"{PORT} 上已有本游戏在运行 → 视为第二实例，请求聚焦后退出")
        if not _focus_first_instance():
            _notice("自动化AI Galgame", "游戏已经在运行中。\n"
                                       "请在任务栏里切换到已有的游戏窗口。")
        return 0

    try:
        import webview
        _has_webview = True
    except Exception as e:
        _log(f"pywebview 不可用：{type(e).__name__}: {e}")
        _has_webview = False

    if not _has_webview:
        for n in orig:                                  # 回退链里禁止拉起任何浏览器
            setattr(webbrowser, n, lambda *a, **k: False)
        srv_t = _start_server_thread(base)
        # 第二级首选：QtWebEngine（无浏览器外壳、不依赖 .NET）
        if _run_qt(url):
            srv_t.join(timeout=10.0)
            if srv_t.is_alive():
                os._exit(0)
            return 0
        ok, ms = _wait_ready(READY_TIMEOUT)
        mode = _fallback_launch(url, orig) if ok else ""
        if not ok or not mode:
            _notice("自动化AI Galgame 启动失败",
                    "本地服务未能在预期时间内就绪。\n请查看游戏专属数据目录下的 debug.log 后重试。")
        srv_t.join(timeout=10.0)
        if srv_t.is_alive():
            os._exit(0)
        return 0
        return 0

    for n in orig:                                      # 首选路径：禁止任何浏览器
        setattr(webbrowser, n, lambda *a, **k: False)
    try:
        from app import startup as startup_mod
        # 启动页由本窗口承担；Tk 进度窗在后台线程里不安全 → 只关 GUI，状态写入照旧
        startup_mod._ensure_window = lambda: None
    except Exception as e:
        _log(f"抑制 Tk 进度窗失败（不影响启动）：{e}")

    srv_thread = _start_server_thread(base)
    state = {"closed": False, "webview_ok": False}
    win = webview.create_window("自动化AI Galgame", html=SPLASH_HTML,
                                width=1280, height=860, min_size=(900, 600),
                                background_color="#14161c")

    def on_ready():
        ok, ms = _wait_ready(READY_TIMEOUT)
        if not ok:
            _log(f"服务未就绪（{ms} ms）")
            try:
                win.load_html("<h3 style='font:16px Microsoft YaHei UI;padding:24px'>"
                              "本地服务启动失败，请查看 debug.log</h3>")
            except Exception:
                pass
            return
        _log(f"服务就绪 {ms} ms → 导航 {url}")
        try:
            win.load_url(url)
        except Exception as e:
            _log(f"窗口导航失败：{e}")

    def on_closed():
        state["closed"] = True
        _log("窗口已关闭 → 请求优雅停机")
        _log(f"shutdown status={_api('POST', '/api/shutdown', {}, timeout=10.0)[0]}")

    try:
        win.events.closed += on_closed
    except Exception:
        pass
    threading.Thread(target=on_ready, name="tsf-ready", daemon=True).start()

    try:
        webview.start(debug=False)
        state["webview_ok"] = True
    except Exception:
        # ★ 后端初始化失败（实测：pythonnet 报
        #   "Failed to resolve Python.Runtime.Loader.Initialize"）也算 pywebview 不可用
        #   ⇒ 必须走回退链，而不是静默退出。回退路径的可见性**不达标**（UIA 有地址栏）。
        _log("webview.start 异常（后端初始化失败）:\n" + traceback.format_exc())
    _log("GUI 循环结束")
    if not state["webview_ok"]:
        # 第二级首选：QtWebEngine（无浏览器外壳 ⇒ 不降级可见性）
        if _run_qt(url):
            if not state["closed"]:
                _api("POST", "/api/shutdown", {}, timeout=10.0)
            srv_thread.join(timeout=10.0)
            if srv_thread.is_alive():
                os._exit(0)
            return 0
        ok, ms = _wait_ready(30.0)
        mode = _fallback_launch(url, orig) if ok else ""
        _log(f"pywebview 与 Qt 都失败 → 回退={mode or '无'}（服务就绪={ok} {ms} ms）")
        if not mode:
            _notice("自动化AI Galgame",
                    "内嵌窗口不可用，且未能拉起备用窗口。\n请查看游戏专属数据目录下的 window.log。")
    if not state["closed"] and state["webview_ok"]:
        _api("POST", "/api/shutdown", {}, timeout=10.0)
    srv_thread.join(timeout=10.0)
    if srv_thread.is_alive():
        _log("服务器线程 10s 未退出 → 强制结束进程")
        os._exit(0)
    _log("退出完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""CoC 模式 UI 层验证：真实浏览器（headless Edge）加载页面 → CDP 真点 CoC 按钮
→ 断言设置框出现 / 开局参数可读 / 面板容器存在 / 无控制台报错。

为什么要这一层：此前只验过
  · API 可达（冻结 exe 内 POST /api/game/start {"mode":"coc"} → 200）
  · UIA 树无地址栏
但**没验过"用户在界面里点得动"** —— 而这才是"③ CoC 模式可玩"的最后一环。

隔离：六路径常量 + 哨兵断言拒绝真实数据目录；服务只绑 127.0.0.1:8811（自起自停）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REAL_DATA = Path(r"D:\TSF_Galgame_Data").resolve()
REAL_PROJ = Path(r"D:\TSF_Galgame").resolve()
PORT = 8811
BASE = f"http://127.0.0.1:{PORT}"
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"]


def start_server(sample: Path):
    sys.path.insert(0, str(REAL_PROJ))
    from app import config as cfgmod
    data = sample / "data"
    cfgmod.DATA_DIR = data
    cfgmod.CACHE_DIR = data / "cache"
    cfgmod.SAVES_DIR = data / "saves"
    cfgmod.CG_DIR = data / "cg"
    cfgmod.CG_META_PATH = cfgmod.CG_DIR / "meta.json"
    cfgmod.CONFIG_PATH = sample / "config.json"
    for k in ("DATA_DIR", "CACHE_DIR", "SAVES_DIR", "CG_DIR", "CG_META_PATH", "CONFIG_PATH"):
        p = Path(getattr(cfgmod, k)).resolve()
        if p == REAL_DATA or REAL_DATA in p.parents:
            print(f"[FATAL] 隔离失败：{k}={p}")
            raise SystemExit(2)
    cfgmod.CONFIG_PATH.write_text(json.dumps({"api": {}, "image": {}}), encoding="utf-8")
    cfgmod.migrate_legacy_data = lambda: {"migrated": [], "disabled_by": "coc-ui-harness"}

    from app import library as library_mod
    library_mod.ROSTER_DIR = sample / "角色库"
    if Path(library_mod.ROSTER_DIR).resolve() == REAL_PROJ / "角色库":
        print("[FATAL] 隔离失败：ROSTER_DIR 仍是真实角色库")
        raise SystemExit(2)

    import uvicorn
    import server
    import app.coc as coc  # noqa: F401  确保 CoC 路由/会话已就绪
    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/version", timeout=2) as r:
                if r.status == 200:
                    return srv
        except Exception:
            time.sleep(0.5)
    print("[FATAL] 服务未就绪")
    raise SystemExit(2)


COC_UI_JS = r"""
(async () => {
  const $ = (id) => document.getElementById(id);
  const vis = (id) => { const e = $(id); return e ? !e.classList.contains('hidden') : null; };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = {steps: {}, errors: []};
  const S = out.steps;

  // 0) 元素静态存在性（HTML 层）
  S.el_mode_btn_coc = !!$("mode-btn-coc");
  S.el_coc_setup_box = !!$("coc-setup-box");
  S.el_coc_panel = !!$("coc-panel");
  S.el_coc_difficulty = !!$("coc-difficulty");
  S.el_coc_death_mode = !!$("coc-death-mode");
  S.el_coc_old_god = !!$("coc-old-god");
  S.el_coc_character = !!$("coc-character");
  S.el_btn_coc_save = !!$("btn-coc-save-slot");

  // 1) 真点 CoC 模式按钮
  const btn = $("mode-btn-coc");
  if (!btn) { out.fatal = "缺 mode-btn-coc"; return out; }
  const clsBefore = btn.className;
  const setupBefore = vis("coc-setup-box");
  btn.click();
  await sleep(120);
  S.mode_btn_coc_class_changed = btn.className !== clsBefore;
  S.mode_btn_coc_on = btn.className.includes("on");
  S.coc_setup_box_before = setupBefore;
  S.coc_setup_box_after = vis("coc-setup-box");
  S.coc_setup_appeared = (setupBefore === false) && (vis("coc-setup-box") === true);

  // 2) 其他模式按钮应被关掉（互斥）
  S.other_modes_off = !$("mode-btn-tsf").className.includes("on")
                    && !$("mode-btn-vn").className.includes("on");

  // 3) 选项是否有内容（服务端 /api/game/coc/config 填的）
  const optCount = (id) => { const e = $(id); return e ? e.options.length : -1; };
  S.difficulty_options = optCount("coc-difficulty");
  S.death_mode_options = optCount("coc-death-mode");
  S.old_god_options = optCount("coc-old-god");
  S.character_options = optCount("coc-character");
  S.selected = {
    difficulty: $("coc-difficulty") ? $("coc-difficulty").value : null,
    death_mode: $("coc-death-mode") ? $("coc-death-mode").value : null,
    old_god: $("coc-old-god") ? $("coc-old-god").value : null,
    character: $("coc-character") ? $("coc-character").value : null,
  };

  // 4) 切回 TSF 再切回 CoC（验证可反复切换，不残留）
  $("mode-btn-tsf").click();
  await sleep(80);
  S.back_to_tsf_hides_setup = vis("coc-setup-box") === false;
  $("mode-btn-coc").click();
  await sleep(80);
  S.recoc_shows_setup = vis("coc-setup-box") === true;

  // 5) 读出既有模式状态，确认没有被 CoC 破坏
  S.mode_tsf_btn_exists = !!$("mode-btn-tsf");
  S.mode_vn_btn_exists = !!$("mode-btn-vn");

  out.ui_init_mark = document.documentElement.dataset.tsfUiInit || null;
  // 复位到 TSF，避免影响后续
  if ($("mode-btn-tsf")) $("mode-btn-tsf").click();
  return out;
})()
"""


def browser_dump(browser: str, extra_dom: bool, tag: str):
    prof = Path(tempfile.gettempdir()) / f"cocui-{tag}"
    prof.mkdir(parents=True, exist_ok=True)
    args = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={prof}", "--virtual-time-budget=8000",
            "--enable-logging=stderr", "--v=0"]
    if extra_dom:
        args += ["--dump-dom", BASE + "/"]
    else:
        args += [BASE + "/"]
    r = subprocess.run(args, capture_output=True, timeout=180)
    return r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def cdp_probe(browser: str, js: str) -> dict:
    """用 CDP 在真实页面里求值。

    实现直接沿用 t37_browser_check.py 中【已被验证 10/10】的写法：
    用 websocket-client（系统 Python 已装）、先起 about:blank 再 Page.navigate、
    以递增 seq 配对请求响应。不自己手搓 WebSocket 帧。
    """
    import websocket
    prof = Path(tempfile.mkdtemp(prefix="cocui-cdp-"))
    port = 9233
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu",
         "--no-proxy-server", "--proxy-bypass-list=*",
         f"--user-data-dir={prof}", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", "--no-first-run", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(80):
            try:
                j = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2).read())
                pages = [t for t in j if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        if not ws_url:
            return {"_error": "CDP /json/list 无 page"}
        ws = websocket.create_connection(ws_url, timeout=30)
        seq = [0]

        def cmd(method, params=None):
            seq[0] += 1
            ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == seq[0]:
                    return msg

        cmd("Page.enable")
        cmd("Runtime.enable")
        cmd("Page.navigate", {"url": BASE + "/"})
        time.sleep(4.0)
        res = cmd("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                       "awaitPromise": True})
        ws.close()
        if "exceptionDetails" in res.get("result", {}):
            return {"_error": "JS 异常",
                    "detail": str(res["result"]["exceptionDetails"])[:400]}
        val = res.get("result", {}).get("result", {}).get("value")
        return val if isinstance(val, dict) else {"_error": "无 value", "raw": str(val)[:200]}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser:
        print("[FATAL] 找不到 Edge/Chrome")
        return 2
    print(f"浏览器: {browser}")

    sample = Path(tempfile.mkdtemp(prefix="cocui-sample-"))
    print(f"隔离样本: {sample}")
    srv = start_server(sample)
    try:
        print("\n=== L1: --dump-dom 静态检查 ===")
        dom, err = browser_dump(browser, True, "dom")
        for probe in ("mode-btn-coc", "coc-setup-box", "coc-panel",
                      "coc-difficulty", "coc-old-god", "coc-character",
                      "btn-coc-save-slot"):
            print(f"  {'✓' if probe in dom else '✗'} #{probe}")
        m = re.search(r'data-tsf-ui-init="([^"]+)"', dom)
        print(f"  ui-init = {m.group(1) if m else '<未找到>'}")

        print("\n=== L2: 控制台错误 ===")
        errs = [l for l in err.splitlines()
                if re.search(r"Uncaught|SyntaxError|TypeError|ReferenceError|ERROR:CONSOLE", l)]
        print(f"  命中 {len(errs)} 条")
        for l in errs[:5]:
            print("   ", l[:150])

        print("\n=== L3: CDP 真实交互 ===")
        r = cdp_probe(browser, COC_UI_JS)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:2600])

        # 判定
        print("\n=== 判定 ===")
        fails = []
        if not isinstance(r, dict) or "_error" in r:
            fails.append(f"CDP 求值失败: {r.get('_error') if isinstance(r, dict) else r}")
        else:
            s = r.get("steps", {})
            for k in ("el_mode_btn_coc", "el_coc_setup_box", "el_coc_panel",
                      "el_coc_difficulty", "el_coc_death_mode", "el_coc_old_god",
                      "el_coc_character"):
                if not s.get(k):
                    fails.append(f"{k} 不存在")
            if not s.get("mode_btn_coc_on"):
                fails.append("点 CoC 按钮后未置 on")
            if not s.get("coc_setup_appeared"):
                fails.append("点 CoC 后设置框未出现")
            if not s.get("other_modes_off"):
                fails.append("切到 CoC 后其他模式按钮未关（互斥失效）")
            for k in ("difficulty_options", "death_mode_options",
                      "old_god_options", "character_options"):
                v = s.get(k, -1)
                if not isinstance(v, int) or v <= 1:
                    fails.append(f"{k}={v}（应为服务端填充的 >1 项）")
            if not s.get("back_to_tsf_hides_setup"):
                fails.append("切回 TSF 后设置框未隐藏")
            if not s.get("recoc_shows_setup"):
                fails.append("再次切回 CoC 后设置框未出现")
            if r.get("ui_init_mark") != "40/40" and r.get("ui_init_mark") != "41/41":
                print(f"  注意: ui-init = {r.get('ui_init_mark')}")
        if errs:
            fails.append(f"控制台有 {len(errs)} 条错误")

        print("VERDICT:", "PASS" if not fails else "FAIL")
        for f in fails:
            print("  ✗", f)
        return 0 if not fails else 1
    finally:
        try:
            srv.should_exit = True
        except Exception:
            pass
        time.sleep(1)
        if not args.keep:
            import shutil
            shutil.rmtree(sample, ignore_errors=True)
            print("样本已清理")
        print("已收尾")


if __name__ == "__main__":
    sys.exit(main())

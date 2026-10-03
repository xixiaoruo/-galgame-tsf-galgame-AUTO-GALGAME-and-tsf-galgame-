"""AI Galgame 服务入口。运行：python server.py → http://127.0.0.1:8765"""
import json
import logging
import os
import sys
import time
from pathlib import Path

# 控制台可能为 GBK：统一 UTF-8 输出，避免特殊字符 print 崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 启动进度：在导入重依赖（rembg/onnxruntime）之前就显示（控制台行 + Tk 进度窗）
from app import config as cfgmod
from app import startup as startup_mod

startup_mod.set_status_file(cfgmod.DATA_DIR / "startup.json")
startup_mod.mark(1, "loading runtime environment...", "加载运行环境…")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import archive as archive_mod
from app import cgbook
from app import exporter
from app import game
from app import image as image_mod
from app import inject
from app import library
from app import plugins as plugins_mod
from app import safe as safemod
from app import story
from app import updater
from app import vn
# P18-A（t51）：CoC 作为**第三个独立模式**。只导入会话层，不合并既有分发分支。
# 位置在既有导入之后 + 仅新增一行 → 与 t37/t38/t39 的改动不构成块级冲突。
from app.coc import session as coc
# C×T4（t56）：第四模式 coc_tsf —— 面板进度**复用 t54 的接口取值**（前端不重算阶段）。
from app.coc import tsf as coc_tsf
from app.llm import LLMError

import sys as _sys


# ---------------------------------------------------------------------------
# C×T4（t56）：CoC×TSF（第四模式）的**服务端**接线
#
# 职责边界（重要）：
#   · 引擎与镜像的**构造/落盘**在 `app/game.py`（t55：`make_coc_tsf_mirror` /
#     `coc_tsf_persist` / `coc_tsf_load` / `start_coc_tsf_portrait`）与
#     `app/coc/tsf.py`（t54：`tsf_progress_of` / `mechanical_effects`）——本文件**只调用**。
#   · 本文件只做三件事：① 记住"哪些 coc 会话是 coc_tsf"② 每回合把镜像进度刷新/落盘
#     ③ 把进度与立绘资产**塞进既有的** `GET /api/game/{sid}` 响应（不新增端点、
#     因此不需要新的鉴权面：沿用同一套中间件）。
#   · 一切失败都不影响开局/回合：镜像只是"面板与立绘"的附加物。
# ---------------------------------------------------------------------------

#: 本进程内已知的 coc_tsf 会话 sid（内存注册表，与 game.SESSIONS / coc 的会话表同生命周期）
_COC_TSF_SIDS: set[str] = set()
#: 镜像缓存：sid → mirror（含 assets，供面板取立绘；进程内，重启后由 coc_tsf_load 复原）
_COC_TSF_MIRRORS: dict[str, dict] = {}


def _coc_tsf_mirror(sid: str) -> dict | None:
    """取该会话的 CoC×TSF 镜像；没有就返回 None（调用方据此走原路径）。

    先查进程内缓存，再查 t55 的**自有落盘**（`<DATA_DIR>/coc_tsf/<sid>.json`）——
    后者让"读档后重新进入 coc_tsf"仍能恢复进度与立绘资产，而不必依赖内存。
    """
    if not sid:
        return None
    hit = _COC_TSF_MIRRORS.get(sid)
    if hit is not None:
        return hit
    if sid not in _COC_TSF_SIDS:
        try:
            loaded = game.coc_tsf_load(sid)
        except Exception:            # noqa: BLE001  —— 镜像缺失/损坏都不该影响主流程
            loaded = None
        if not isinstance(loaded, dict):
            return None
        _COC_TSF_SIDS.add(sid)
        _COC_TSF_MIRRORS[sid] = loaded
        return loaded
    return None


def _coc_tsf_state(sid: str) -> dict:
    """取 CoC 会话的原始 state（`coc.get(sid)` 是**会话 dict**，run 在其 `["run"]` 下）。"""
    return coc.get(sid)["run"].state


def _coc_tsf_progress(sid: str) -> int:
    """当前 CoC×TSF 进度（0–100）——**只走 t54 的接口**，不在此处重算。"""
    try:
        return int(coc_tsf.tsf_progress_of(_coc_tsf_state(sid)))
    except Exception:                # noqa: BLE001
        return 0


def _coc_tsf_start(payload, started: dict) -> None:
    """开局时建镜像 + 落盘 + 起首张立绘。**任何异常都不影响开局结果**。"""
    sid = str((started or {}).get("sid") or "")
    if not sid:
        return
    try:
        p = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload or {})
        prog = _coc_tsf_progress(sid)
        mirror = game.make_coc_tsf_mirror(
            {"sid": sid, "created": time.time(),
             "run": {"state": _coc_tsf_state(sid)},
             "content_rating": p.get("content_rating", "18"),
             "r18_enabled": bool(p.get("r18_enabled", True)),
             "allow_forced": bool(p.get("allow_forced", True))},
            progress=prog,
            anchor=str(p.get("anchor") or ""),
            character=str(p.get("character") or ""),
        )
        _COC_TSF_SIDS.add(sid)
        _COC_TSF_MIRRORS[sid] = mirror
        game.coc_tsf_persist(mirror)
        # 首张立绘：后台排队（`_queue_asset` 内部处理失败回退，不空屏、不抛到请求里）
        game.start_coc_tsf_portrait(mirror, progress=prog,
                                    target_en=str(p.get("tsf_target_en") or ""))
    except Exception as e:           # noqa: BLE001
        logging.warning("coc_tsf 开局附加流程失败（不影响开局）：%s", e)


def _coc_tsf_refresh(sid: str) -> None:
    """回合结束后同步镜像进度；**段号变化**时才排新立绘（阶段跃迁=重新生成）。"""
    mirror = _coc_tsf_mirror(sid)
    if mirror is None:
        return
    try:
        prog = _coc_tsf_progress(sid)
        prot = mirror.setdefault("protagonist", {})
        stats = prot.setdefault("stats", {})
        before_stage = int(mirror.get("_last_stage_idx", -1))
        stats["progress"] = prog
        tbl = mirror.get("stages") or coc_tsf.default_stages()
        idx = coc_tsf.stage_index(prog, tbl)
        game.coc_tsf_persist(mirror)
        if idx != before_stage:
            mirror["_last_stage_idx"] = idx
            keys = (mirror.get("assets") or {}).get("portraits") or {}
            busy = any(v.get("status") == "pending" for v in keys.values())
            if not busy:             # 已在生成就不重复排队
                game.start_coc_tsf_portrait(mirror, progress=prog)
    except Exception as e:           # noqa: BLE001
        logging.warning("coc_tsf 进度刷新失败（不影响回合）：%s", e)


def _coc_tsf_view(sid: str) -> dict | None:
    """给面板的 TSF 视图（进度值 + 阶段名 + 机制档位/修正），值全部来自 t54 的接口。"""
    mirror = _coc_tsf_mirror(sid)
    if mirror is None:
        return None
    try:
        eff = coc_tsf.mechanical_effects(_coc_tsf_state(sid))
    except Exception:                # noqa: BLE001
        return None
    eff["target_en"] = str(((mirror.get("protagonist") or {}).get("anchor")) or "")
    return eff


def _coc_tsf_decorate(d, sid: str):
    """把 coc_tsf 的附加字段挂到**既有响应**上（读状态与推进回合共用一处，避免两处漂移）。

    纯 coc 会话取不到镜像 → 原样返回，t51 的行为逐字段不变。
    """
    view = _coc_tsf_view(sid)
    if view is None or not isinstance(d, dict):
        return d
    mirror = _coc_tsf_mirror(sid) or {}
    d["mode"] = "coc_tsf"
    d["coc_tsf"] = view
    d["assets"] = mirror.get("assets") or {"portraits": {}, "backgrounds": {}}
    d["tsf"] = {"stage": int(view.get("stage", 0))}
    return d


debug_level = logging.DEBUG if "--debug" in _sys.argv else logging.INFO
logging.basicConfig(level=debug_level,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
# 调试日志落盘：data/debug.log（含 API 请求与生成错误，便于排查）
cfgmod.DATA_DIR.mkdir(parents=True, exist_ok=True)  # 先建目录：冷启动时 data/ 可能不存在
_debug_fh = logging.FileHandler(cfgmod.DATA_DIR / "debug.log", encoding="utf-8")
_debug_fh.setLevel(logging.INFO)
_debug_fh.setFormatter(logging.Formatter(
    "%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_debug_fh)
logging.getLogger("httpx").setLevel(logging.WARNING)

# 写入区根目录（exe 所在目录 / 源码项目目录）；冻结模式 __file__ 在临时解压区
ROOT = cfgmod.ROOT
# 显式引用 AI 抠图引擎：让 PyInstaller 完整收集 rembg（抠图质量保障），无环境时静默
try:
    import rembg  # noqa: F401
    import onnxruntime  # noqa: F401
except Exception:
    pass
startup_mod.mark(2, "runtime ready, preparing data dirs...", "运行环境就绪，准备数据目录…")
cfgmod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
cfgmod.SAVES_DIR.mkdir(parents=True, exist_ok=True)
cfgmod.CG_DIR.mkdir(parents=True, exist_ok=True)
startup_mod.mark(3, "migrating legacy data...", "迁移旧版本数据…")
_migrated = cfgmod.migrate_legacy_data()
if _migrated.get("migrated"):
    logging.info("已迁移旧数据到统一数据目录：%s", "、".join(_migrated["migrated"][:20]))
startup_mod.mark(4, "loading sessions...", "加载对局…")
game.load_all()
startup_mod.mark(5, "relocating portraits into session dirs...", "立绘归位到对局目录…")
_cache_migrated = game.migrate_cache_to_session_dirs()
if _cache_migrated.get("migrated"):
    logging.info("历史立绘已归位到各自对局文件夹：%d 张", _cache_migrated["migrated"])
startup_mod.mark(6, "cleaning up orphan assets...", "清理过期立绘…")
_cleaned = game.cleanup_orphan_cache()
if _cleaned.get("deleted"):
    logging.info("已清理无引用的过期立绘：%d 张", _cleaned["deleted"])
startup_mod.mark(7, "naming session dirs...", "对局目录可读化命名…")
_renamed = game.rename_session_dirs_to_readable()
if _renamed.get("renamed"):
    logging.info("对局立绘目录已改为可读命名：%d 个", _renamed["renamed"])

app = FastAPI(title="自动化AI Galgame")


# 1.7.34 全局 500 兜底：未捕获异常落盘 debug.log + 返回可读错误（调试与排障）


@app.exception_handler(Exception)
async def _unhandled_exception(request, exc):
    logging.exception("unhandled %s %s: %r",
                      request.method, request.url.path, exc)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={
        "detail": f"服务器内部错误：{type(exc).__name__}: {exc}"})

# ---------- 版本号：单一真源（唯一权威）----------
# `GAME_VERSION` 是版本号的唯一真源；`static/index.html` 里的两处 `?v=` 是**派生物**。
# 改完版本号必须跑构建期闸门（不一致即非零退出并指出 文件:行）：
#     python 测试工具/check_version_sync.py
#     python 测试工具/check_version_sync.py --fix     # 用本常量回写 index.html 两处
# 注意：不要用中间件注入 HTML 去改 `?v=`——StaticFiles 是流式响应、没有 body 可改。
GAME_VERSION = "1.7.52"


# ---------- 请求参数安全校验（B-01：路径穿越 / id 边界）----------
# 统一连接器在 app/safe.py；这里只做「拒绝 → 4xx」的翻译。
# 约定：detail 一律是拒绝原因，**不含服务器绝对路径**。

def _req_id(value, what: str = "id") -> str:
    """请求来的 id：白名单校验（默认 `[A-Za-z0-9_-]{1,32}`），非法直接 400。"""
    try:
        return safemod.safe_id(value, what=what)
    except ValueError as e:
        raise HTTPException(400, f"非法请求参数：{e}")


def _req_name(value, what: str = "名称") -> str:
    """请求来的文件名/目录名：逐段校验（拒绝分隔符/`..`/同形字符等），非法 400。"""
    try:
        return safemod.safe_name(value, what=what)
    except ValueError as e:
        raise HTTPException(400, f"非法请求参数：{e}")


def _req_file(root, *parts, what: str = "文件路径", max_parts: int | None = None):
    """把已校验的段安全拼到 root 下：越界（含链接/junction 逃逸）直接 400。"""
    try:
        return safemod.safe_join(root, *parts, what=what, max_parts=max_parts)
    except ValueError as e:
        raise HTTPException(400, f"非法请求参数：{e}")


@app.get("/api/version")
def version():
    return {"version": GAME_VERSION}


# ---------- 本地访问安全基线（B-10 / t39）----------
# 审计缺陷：原先绑 0.0.0.0 + ::，仅 3 个端点查 127.0.0.1（**对浏览器 CSRF 无效**，
# 因为请求就是本机浏览器发出的、源 IP 也是回环），wipe 的确认词是固定串 "WIPE"，
# /api/open-folder 无鉴权 → 局域网内任意网页可抹数据 + 关机。
#
# 本基线四件事：
#   ① 默认只绑回环（见文件末尾 __main__；局域网需显式 TSF_ALLOW_LAN=1）
#   ② 写操作（非 GET/HEAD/OPTIONS 的 /api/*）必须携带**本轮随机 token**
#   ③ Origin / Sec-Fetch-Site 校验（跨源写一律拒；Host 必须是回环，兼防 DNS rebinding）
#   ④ wipe 改为服务端随机下发确认词（单次、5 分钟过期）
#
# token 的生成与存放：启动时 `secrets.token_urlsafe(32)` 随机生成一次（**不写死在源码里**），
# 原子写入 `DATA_DIR/local_token.json`（进程生命周期内稳定、不轮换）。本机工具链
# （launcher.py / 测试工具/acceptance_e2e.py / 未来的第二实例聚焦）读该文件并带
# `X-Local-Token` 头即可；同源浏览器页面则靠 `SameSite=Strict` cookie 自动携带，
# 前端**无需改一行代码**。
_LOCAL_TOKEN = ""
_TOKEN_HEADER = "X-Local-Token"
_TOKEN_COOKIE = "tsf_token"
_WIPE_TTL = 300.0
_WIPE_CHALLENGE: dict = {}      # {"word": str, "exp": float}，单次使用
_LAN_MODE = False               # 由 __main__ 的 opt-in 决定；影响 Host 白名单
_LAN_HOSTS: set = set()         # opt-in 时的本机局域网 IP


def _init_local_token() -> str:
    """生成本轮 token 并原子落盘（DATA_DIR/local_token.json）。幂等。"""
    global _LOCAL_TOKEN
    import secrets as _secrets
    if _LOCAL_TOKEN:
        return _LOCAL_TOKEN
    tok = _secrets.token_urlsafe(32)
    _LOCAL_TOKEN = tok
    try:
        cfgmod.atomic_write_json(cfgmod.DATA_DIR / "local_token.json",
                                 {"token": tok, "issued_at": time.time()})
    except OSError as e:                # 落盘失败不致命：内存里的 token 依然有效
        logging.warning("local_token.json 写入失败（工具链将无法读取 token）：%s", e)
    return tok


def _allowed_hosts() -> set:
    """允许的 Host 主机名：回环三种写法，opt-in 时外加本机局域网 IP。"""
    hosts = {"127.0.0.1", "localhost", "::1"}
    if _LAN_MODE:
        hosts |= _LAN_HOSTS
    return hosts


def _host_of(host_header: str) -> str:
    """从 Host 头取出主机名（兼容 `127.0.0.1:8765`、`[::1]:8765`）。"""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h.split("]")[0].lstrip("[")
    return h.rsplit(":", 1)[0] if ":" in h else h


def _host_allowed(host_header: str) -> bool:
    return _host_of(host_header) in _allowed_hosts()


def _origin_allowed(origin: str) -> bool:
    """同源判定：仅接受 http://<允许主机>[:端口]。"""
    if not origin:
        return True                     # 无 Origin = 非浏览器调用方（凭据在 token 上）
    if origin == "null":
        return False                    # sandboxed iframe / file:// 等
    try:
        import urllib.parse as _up
        p = _up.urlparse(origin.strip())
    except ValueError:
        return False
    return p.scheme == "http" and _host_of(p.netloc) in _allowed_hosts()


def _presented_token(request: Request) -> str:
    """从 头 / cookie / 查询串 里取 token（三者等价）。"""
    tok = request.headers.get(_TOKEN_HEADER, "")
    if not tok:
        tok = request.cookies.get(_TOKEN_COOKIE, "")
    if not tok:
        tok = request.query_params.get("token", "")
    return tok.strip()


def _deny(status_code: int, detail: str, reason: str):
    """统一的拒绝响应（含 no-store + 反内嵌头，与其它响应口径一致）。"""
    from fastapi.responses import JSONResponse
    logging.warning("local-guard 拒绝请求：%s（%s）", detail, reason)
    resp = JSONResponse(status_code=status_code, content={"detail": detail},
                        headers={"Cache-Control": "no-store, max-age=0",
                                 "X-Local-Guard": reason})
    return _apply_security_headers(resp)


def _looks_like_browser(request: Request) -> bool:
    """判定"浏览器发来的请求"：任一 Fetch 元数据头或 Origin 出现即算。

    浏览器对**同源 POST 也会带** `Origin` 与 `Sec-Fetch-Site`；而非浏览器
    （curl / urllib / launcher / acceptance_e2e / 第二实例聚焦）两者都没有。
    因此"两个头都缺失"是**非浏览器调用方**的可靠签名（队长裁决：回环 + 无 Origin
    ⇒ 视为本机工具链）。注意：这只用于**区分调用方类别**，
    「无 Origin」**不是**"已验证为安全"，真正的凭据是 token（浏览器侧）。
    """
    for h in ("origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"):
        if request.headers.get(h):
            return True
    return False


#: 反内嵌：`frame-ancestors` 只能通过**响应头**生效（放在 `<meta>` 里会被规范忽略——
#: t38 曾把它放在 meta，浏览器控制台每次加载都报一条 security/error 且**毫无保护效果**；
#: 真实浏览器实测：当时任意页面都能用 iframe 内嵌本服务）。
#: 现由本中间件对**所有**响应下发；`X-Frame-Options` 作旧引擎兜底
#: （现代 Chromium 以 CSP 为准并忽略 XFO；WebView2 也是 Chromium，但 IE 模式/老内嵌壳只认 XFO）。
_SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}


def _apply_security_headers(resp):
    """给任意响应补反内嵌头（已存在同名头则不覆盖）。"""
    for k, v in _SECURITY_HEADERS.items():
        if k not in resp.headers:
            resp.headers[k] = v
    return resp


@app.middleware("http")
async def local_access_guard(request: Request, call_next):
    """B-10：Host 必须回环；写操作需同源 + 本地 token；所有响应带反内嵌头。

    中间件注册顺序：后注册的在外层 → 本函数最先看到请求、最早拒绝；
    响应头（no-store）仍由先注册的 no_cache_middleware 负责，两者互不干扰。
    """
    path = request.url.path
    if path.startswith("/api/"):
        if not _host_allowed(request.headers.get("host", "")):
            return _deny(403, "仅允许本机访问：Host 校验失败（防 DNS rebinding）", "host")
        if request.method.upper() not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return _deny(403, "跨站写请求被拒绝", "fetch-site")
            if not _origin_allowed(request.headers.get("origin", "")):
                return _deny(403, "跨源写请求被拒绝（Origin 校验失败）", "origin")
            if _looks_like_browser(request):
                # 浏览器发来的写请求：必须有本轮 token（同源页面由 cookie 自动携带，
                # 也可用 X-Local-Token 头 / ?token=）。缺 token 一律 403。
                import secrets as _secrets
                tok = _presented_token(request)
                if not _LOCAL_TOKEN or not tok \
                        or not _secrets.compare_digest(tok, _LOCAL_TOKEN):
                    return _deny(403, "缺少或错误的本地 token：写操作需携带 X-Local-Token"
                                      "（同源浏览器由 cookie 自动携带）", "token")
    response = await call_next(request)
    _apply_security_headers(response)          # 页面/静态/API 全响应都带反内嵌头
    # 同源页面附带 SameSite=Strict 的 token cookie：前端零改动即可通过写操作校验。
    # 跨站请求不会带上它（CSRF 在 cookie 层就断了）。
    if path in ("/", "/index.html") and _LOCAL_TOKEN and request.method.upper() == "GET":
        response.set_cookie(_TOKEN_COOKIE, _LOCAL_TOKEN, path="/", httponly=True,
                            samesite="strict")
    return response


@app.get("/api/local-token")
def local_token(request: Request):
    """同源页面读取本轮 token（跨源读不到：Host 必须回环，且拒绝跨站读取）。

    JSON 对象字面量不会被 `<script src>` 之类的手段读走；跨源真读还会被
    `Sec-Fetch-Site` 与 Origin 两道挡下。
    """
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site" \
            or not _origin_allowed(request.headers.get("origin", "")):
        raise HTTPException(403, "跨源不可读取本地 token")
    if not _LOCAL_TOKEN:
        _init_local_token()
    return {"token": _LOCAL_TOKEN}


@app.get("/api/update-state")
def update_state():
    """是否有待安装的更新包（update/ 目录存在新 exe）。"""
    return {"update_ready": bool((ROOT / "update" / "TSF_Galgame.exe").is_file()),
            "version": GAME_VERSION}


@app.post("/api/window/focus")
def window_focus(request: Request):
    """把**首实例的原生窗口**提到前台（第二实例双击图标时的正确反馈）。

    为什么需要它：`window_host.py` 在检测到"本游戏已在运行"时会请求本端点；此前
    该端点不存在 ⇒ 永久 405 ⇒ 第二实例只能退回"静默退出"，用户双击图标后
    **看不到任何反应**（与 t36 契约 §2 F6 的要求不符）。

    鉴权（按 t39 的既定口径）：
      · **仅回环**调用方（`request.client.host` ∈ {127.0.0.1, ::1}）；
      · **必须带本轮 token**（`X-Local-Token` 头 / `tsf_token` cookie / `?token=`），
        用 `compare_digest` 常数时间比较——本端点属于**写操作**，不走"无 Origin 即放行"。

    返回：`{"ok": true, "focused": <bool>}`。`focused=False` 表示本进程**没有原生窗口**
    （例如直接 `python server.py` 起的服务）或窗口钩子未就绪，此时**调用方要给出可见提示**。
    """
    import secrets as _secrets
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅允许本机调用")
    tok = _presented_token(request)
    if not _LOCAL_TOKEN or not tok or not _secrets.compare_digest(tok, _LOCAL_TOKEN):
        raise HTTPException(403, "缺少或错误的本地 token（聚焦需要 X-Local-Token）")
    # 窗口钩子由 window_host.py 注册在 `__main__` 上（冻结与源码形态都是它当 __main__）；
    # 服务单独起（python server.py）时没有钩子 ⇒ focused=False，由调用方提示用户。
    hook = getattr(sys.modules.get("__main__"), "WINDOW_FOCUS_HOOK", None)
    if not callable(hook):
        return {"ok": True, "focused": False, "reason": "本进程没有原生窗口"}
    try:
        focused = bool(hook())
    except Exception as e:
        logging.warning("window focus 钩子失败：%s", e)
        return {"ok": True, "focused": False, "reason": f"聚焦钩子异常：{type(e).__name__}"}
    return {"ok": True, "focused": focused}


@app.middleware("http")
async def no_cache_middleware(request, call_next):
    """禁用浏览器缓存：页面与脚本始终取最新版，避免升级后新旧文件混搭报错。"""
    # B-02：任何 HTTP 处理都跑在服务端主事件循环上，顺手把循环登记给 game
    # （幂等）——同步端点（线程池 worker）里的后台生成任务靠它投递，
    # 否则 asyncio.create_task 会抛 "no running event loop"。
    game.set_main_loop()
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# ---------- 配置 ----------

class ConfigPayload(BaseModel):
    config: dict


@app.get("/api/config")
def get_config():
    cfg = cfgmod.load_config()
    model_detected = None
    if cfg["image"].get("provider", "openai") in ("sdwebui", "comfyui"):
        try:
            import httpx as _httpx
            base = (cfg["image"].get("base_url") or "").rstrip("/")
            with _httpx.Client(timeout=3) as c:
                if cfg["image"].get("provider") == "comfyui":
                    r = c.get(f"{base}/system_stats")
                    ck = json.loads(r.text).get("system", {})\
                        .get("comfyui_version", "")
                    model_detected = {"checkpoint": f"ComfyUI {ck}", "preset_name": "ComfyUI"}
                else:
                    r = c.get(f"{base}/sdapi/v1/options")
                    ck = r.json().get("sd_model_checkpoint", "") or ""
                    from app.image import match_model_preset
                    preset = match_model_preset(ck)
                    model_detected = {
                        "checkpoint": ck,
                        "preset_name": preset["name"],
                "steps": preset["steps"],
                "cfg": preset["cfg"],
                "sampler": preset["sampler"],
                "hr_fix": preset["enable_hr"],
                "hr_scale": preset.get("hr_scale", 2),
            }
        except Exception:
            model_detected = None
    from app.llm import LLMClient
    llm_client = LLMClient(cfg)
    return {
        "config": cfgmod.masked(cfg),
        "llm_ready": cfgmod.is_llm_configured(cfg),
        "llm_mock": llm_client.mock,
        "image_ready": cfgmod.is_image_configured(cfg),
        "model_detected": model_detected,
    }


@app.post("/api/config")
def update_config(payload: ConfigPayload):
    incoming = payload.config
    current = cfgmod.load_config()
    # key 安全合并：打码值不覆盖真实 key；空 key 视为"未修改"也不覆盖
    # （否则一次无关的设置保存会把已填好的 key 清空 → 玩家被永久锁在演示模式）
    for section in ("llm", "image"):
        if section in incoming and isinstance(incoming[section], dict):
            key = str(incoming[section].get("api_key", ""))
            if not key:
                incoming[section].pop("api_key", None)
            elif "*" in key or "填入" in key:
                incoming[section].pop("api_key", None)
    merged = dict(current)
    for k, v in incoming.items():
        if isinstance(v, dict):
            merged[k] = {**merged.get(k, {}), **v}
        else:
            merged[k] = v
    cfgmod.save_config(merged)
    return {"ok": True}


# ---------- 设定辅助 ----------

@app.get("/api/game/coc/config")
def coc_config():
    """P18-A（t51）：CoC 开局可选值（难度/死法/古神/角色/双轴档位/白名单）。

    纯读接口，**不注册会话、不触发任何生成**。
    路径含三段，不会与 `GET /api/game/{sid}` 冲突。
    """
    try:
        view = coc.config_view()
    except Exception as e:      # 数据缺失时也要给出可读错误，而不是 500 堆栈
        raise HTTPException(500, f"CoC 配置不可用：{e}")
    # C×T4（t56）：告知前端本进程支持哪些模式（纯增字段，老前端忽略即可）。
    view["modes"] = ["tsf", "vn", "coc", "coc_tsf"]
    view["coc_tsf"] = {"available": True, "label": "CoC×TSF",
                       "note": "CoC 跑团 + TSF 转变进度/立绘（第四模式）"}
    return view


@app.post("/api/setup/random")
async def random_setup(mode: str = "tsf"):
    try:
        if mode == "vn":
            data = await vn.random_setup()
        else:
            data = await game.random_setup()
    except LLMError as e:
        raise HTTPException(500, str(e))
    if not isinstance(data, dict) or not data.get("characters"):
        raise HTTPException(500, "生成的设定格式异常，请重试")
    return data


# ---------- 游戏 ----------

class StartPayload(BaseModel):
    mode: str = "tsf"
    title: str = ""
    world: str = ""
    characters: list[dict] = []
    protagonist: dict = {}
    outline: str = ""
    lorebook: list[dict] = []
    directives: list[dict] = []
    catchphrases: list[str] = []
    content_rating: str = "18"
    r18_enabled: bool = True
    adult_intensity: str = "浓烈"
    allow_forced: bool = True
    library_imports: list[dict] = []
    stat_config: list[dict] = []
    uniform_en: str = ""
    uniform_neg: str = ""
    # 1.7.33 主角最终变化样式（身材/胸/瞳色/发色/气质/最终衣物）
    tsf_target: dict = {}


class OutlinePayload(BaseModel):
    world: str = ""
    protagonist: str = ""
    characters: list[dict] = []


class AdvancePayload(BaseModel):
    choice_index: int


class DirectivesPayload(BaseModel):
    directives: list[dict] = []


class RegeneratePayload(BaseModel):
    target: str = "protagonist"
    note: str = ""
    use_initial: bool = False
    # 1.7.31：force_fullgen=True 时重画=全量精美重画（内建 3 种子轮换+质检，
    # 跨性别变化只有全量做得到）；False 走链式保守（重画需求的温和路径）
    force_fullgen: bool = False

class DebugPayload(BaseModel):
    action: str = ""
    stat: str = ""
    value: int = 0
    character: str = ""
    outfit: str = ""





@app.post("/api/game/{sid}/debug")
async def debug_action(sid: str, payload: DebugPayload, request: Request):
    """隐藏调试面板接口：仅接受本机（127.0.0.1）请求。"""
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅限本机调试")
    try:
        s = game.get_session(sid)
        if s.get("mode") == "vn":
            # 普通模式仅支持重画/换装类调试动作
            if payload.action == "regen_all":
                return vn.regenerate(sid, "all")
            if payload.action == "force_outfit":
                return vn.apply_outfit(sid, payload.character or
                                       s["protagonist"]["name"], payload.outfit)
            raise game.GameError("普通模式不支持该调试动作")
        return game.debug_action(sid, payload.action, payload.stat,
                                 payload.value, payload.character,
                                 payload.outfit)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))



    target: str = "protagonist"


class SpeakPayload(BaseModel):
    text: str = ""


@app.post("/api/game/start")
async def start_game(payload: StartPayload):
    try:
        if payload.mode == "coc_tsf":
            # C×T4（t56）：第四模式。**新建独立分支**放在既有 coc 分支之前 ——
            # coc 那一块（t51）一行未动，既有三模式的路径完全不变。
            # 开局仍是 CoC 的开局（同一引擎），只是额外建一个 TSF 镜像用于面板/立绘。
            started = coc.start(payload.model_dump())
            _coc_tsf_start(payload, started)
            # 开局响应也要带 mode/coc_tsf/assets：前端 enterGame 直接拿这份 state，
            # 不装饰的话它会以为当前是纯 coc 局（实测：S.mode 回落成 "coc"）。
            sid0 = str((started or {}).get("sid") or "")
            _coc_tsf_refresh(sid0)
            return _coc_tsf_decorate(started, sid0)
        if payload.mode == "coc":
            # P18-A（t51）：第三模式的**早返回**分支。
            # 早返回而不是插在 else 链里，是为了让既有两分支一个字符都不动。
            return coc.start(payload.model_dump())
        if payload.mode == "vn":
            return await vn.start(payload.model_dump())
        return await game.start_game(payload.model_dump())
    except game.GameError as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


class NewGamePlusPayload(BaseModel):
    slot_id: str = ""
    sid: str = ""
    title: str = ""


@app.post("/api/game/newgame-plus")
async def newgame_plus(payload: NewGamePlusPayload):
    """二周目（继承角色/场景 + 前情已被压缩），从存档槽或当前对局创建新局。"""
    try:
        if payload.slot_id:
            src = game.load_slot_raw(payload.slot_id)
        elif payload.sid:
            src = game.get_session(payload.sid)
        else:
            raise HTTPException(400, "请提供 slot_id 或 sid")
        return await game.new_game_plus(src, payload.title)
    except game.GameError as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        logging.exception("newgame-plus failed: %r", e)
        raise HTTPException(500, f"二周目失败：{e}")


@app.post("/api/outline")
async def generate_outline(payload: OutlinePayload):
    try:
        return await game.generate_outline(
            payload.world, payload.protagonist, payload.characters)
    except (game.GameError, LLMError) as e:
        raise HTTPException(502, str(e))




# ---------- 剧本管理 ----------

class StoryPayload(BaseModel):
    id: str = ""
    mode: str = "tsf"
    name: str = ""
    title: str = ""
    world: str = ""
    protagonist: dict = {}
    characters: list[dict] = []
    outline: str = ""
    lorebook: list[dict] = []
    directives: list[dict] = []
    catchphrases: list[str] = []
    content_rating: str = "all"
    r18_enabled: bool = False
    allow_forced: bool = False
    stat_config: list[dict] = []
    uniform_en: str = ""
    uniform_neg: str = ""
    tsf_target: dict = {}


@app.get("/api/storylines")
def story_list():
    return {"storylines": story.list_all()}


@app.get("/api/storylines/{sid}")
def story_get(sid: str):
    st = story.get(_req_id(sid, "剧本 id"))
    if not st:
        raise HTTPException(404, "剧本不存在")
    return st


@app.post("/api/storylines")
def story_save(payload: StoryPayload):
    return story.save(payload.model_dump())


@app.delete("/api/storylines/{sid}")
def story_delete(sid: str):
    if not story.delete(_req_id(sid, "剧本 id")):
        raise HTTPException(404, "剧本不存在")
    return {"ok": True}


# ---------- 插件管理 ----------

class PluginPayload(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    kind: str = "story"
    directives: list[dict] = []


@app.get("/api/plugins")
def plugin_list():
    return {"plugins": plugins_mod.list_all()}


@app.post("/api/plugins")
def plugin_save(payload: PluginPayload):
    return plugins_mod.save(payload.model_dump())


@app.delete("/api/plugins/{pid}")
def plugin_delete(pid: str):
    if not plugins_mod.delete(_req_id(pid, "插件 id")):
        raise HTTPException(404, "插件不存在")
    return {"ok": True}


# ---------- API 前置注入 ----------

class InjectPayload(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    text: str = ""
    enabled: bool = False


@app.get("/api/injects")
def inject_list():
    return {"injects": inject.list_all()}


@app.post("/api/injects")
def inject_save(payload: InjectPayload):
    return inject.save(payload.model_dump())


@app.delete("/api/injects/{pid}")
def inject_delete(pid: str):
    if not inject.delete(_req_id(pid, "注入 id")):
        raise HTTPException(404, "注入不存在")
    return {"ok": True}



class DeleteByIdPayload(BaseModel):
    id: str = ""


@app.post("/api/admin/wipe-challenge")
def wipe_challenge(request: Request):
    """B-10：下发**服务端随机**确认词（单次使用、5 分钟过期）。

    固定串 "WIPE" 已失效——攻击者无法预知随机词；且写操作本身还需本地 token。
    """
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅限本机调试")
    import secrets as _secrets
    word = _secrets.token_hex(3).upper()          # 6 位十六进制，如 9F3A1C
    _WIPE_CHALLENGE.clear()
    _WIPE_CHALLENGE.update({"word": word, "exp": time.time() + _WIPE_TTL})
    logging.info("wipe 确认词已下发（有效期 %.0fs）", _WIPE_TTL)
    return {"word": word, "expires_in": int(_WIPE_TTL)}


@app.post("/api/admin/wipe")
async def wipe_data(request: Request):
    """清空全部游戏历史数据（立绘缓存/存档/全局存档/世界观档案/插件），
    保留 API 配置（config.json）。

    B-10 加固：确认词改为**服务端随机下发**（见 /api/admin/wipe-challenge），
    单次使用 + 5 分钟过期；写操作另需本地 token（中间件）；本机来源再查一次。
    """
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅限本机调试")
    import secrets as _secrets
    import shutil as _sh
    payload = await request.json()
    word = str(payload.get("confirm", "")).strip().upper()
    ch = _WIPE_CHALLENGE
    if not ch or time.time() > float(ch.get("exp", 0)):
        raise HTTPException(
            400, "确认词已过期或未下发：请先 POST /api/admin/wipe-challenge 获取随机确认词"
                 "（旧固定词 WIPE 已失效）")
    if not word or not _secrets.compare_digest(word, str(ch.get("word", ""))):
        raise HTTPException(400, "确认词不正确（旧固定词 WIPE 已失效）")
    _WIPE_CHALLENGE.clear()                        # 单次使用
    import glob as _g
    cleared = {}
    for name, pattern in (
        ("cache", "data/cache"),
        ("saves", "data/saves"),
        ("slots", "data/slots"),
        ("storylines", "data/storylines"),
    ):
        d = cfgmod.DATA_DIR / name if name != "slots" else cfgmod.DATA_DIR / "slots"
        n = 0
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        n += 1
                    except OSError:
                        pass
        cleared[name] = n
    pf = cfgmod.DATA_DIR / "plugins.json"
    if pf.is_file():
        pf.unlink()
        cleared["plugins"] = 1
    game.SESSIONS.clear()
    for sf in cfgmod.SAVES_DIR.glob("*_slots"):
        _sh.rmtree(sf, ignore_errors=True)
    return {"ok": True, "cleared": cleared}


#: 正在运行的 uvicorn Server（由 __main__ 赋值），/api/shutdown 靠它优雅停机
_UVICORN_SERVER = None


def _graceful_shutdown() -> None:
    """B-04：先落盘、再优雅停机。

    旧实现是 `threading.Timer(0.6, os._exit(0))`——0.6 秒后无条件强杀进程，
    若此刻正在写存档/配置，文件会被截断成半个 JSON（整局存档被 load_all
    静默跳过 / config.json 静默回退默认值、API Key 丢失）。现在：
    ① 把内存里的会话全部原子落盘；② 置 uvicorn `should_exit` 走正常关闭；
    ③ 只在 3 秒宽限内没退干净时才强退兜底（此时关键状态已落盘）。
    """
    import os
    time.sleep(0.5)          # 让 /api/shutdown 的响应先发回前端
    try:
        n = game.persist_all()
        logging.info("shutdown: persisted %d session(s) before exit", n)
    except Exception as e:   # 落盘失败也要能退出，但必须留痕
        logging.warning("shutdown: persist_all failed: %s", e)
    srv = _UVICORN_SERVER
    if srv is None:          # 非 uvicorn 托管（测试/嵌入）时才直接退
        os._exit(0)
        return
    srv.should_exit = True   # 关监听 → 等在途请求收尾 → serve() 正常返回
    deadline = time.time() + 3.0
    while time.time() < deadline:
        time.sleep(0.1)
    os._exit(0)              # 宽限期内未退干净：数据已落盘，强退是安全的


@app.post("/api/shutdown")
async def shutdown_server(request: Request):
    """关闭游戏：仅限本机调用；只结束游戏自身进程（不影响绘图服务等其它程序）。"""
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅限本机")
    import socket as _sock
    sd_running = False
    try:
        with _sock.create_connection(("127.0.0.1", 7860), timeout=0.8):
            sd_running = True
    except OSError:
        pass
    import threading
    threading.Thread(target=_graceful_shutdown, daemon=True).start()
    return {"ok": True, "sd_running": sd_running}


@app.post("/api/storylines/delete")
def story_delete_by_id(payload: DeleteByIdPayload):
    # B-01：id 来自请求体，先过白名单再进 story.delete（无校验 = 可删任意 .json）
    if not story.delete(_req_id(payload.id, "剧本 id")):
        raise HTTPException(404, "剧本不存在")
    return {"ok": True}


@app.post("/api/plugins/delete")
def plugin_delete_by_id(payload: DeleteByIdPayload):
    if not plugins_mod.delete(_req_id(payload.id, "插件 id")):
        raise HTTPException(404, "插件不存在")
    return {"ok": True}


# ---------- 会话列表（启动器分级调整用） ----------

@app.get("/api/game/sessions")
def session_list():
    out = []
    for s in game.SESSIONS.values():
        out.append({
            "sid": s["sid"],
            "title": s.get("title", ""),
            "content_rating": s.get("content_rating", "all"),
            "r18_enabled": bool(s.get("r18_enabled")),
            "saved_at": s.get("saved_at", 0),
        })
    out.sort(key=lambda x: x.get("saved_at", 0), reverse=True)
    return {"sessions": out[:10]}


@app.get("/api/directives/presets")
def directive_presets():
    return {"presets": plugins_mod.merged_presets()}


@app.get("/api/game/{sid}/directives")
def get_directives(sid: str):
    try:
        return {"directives": game.get_directives(sid)}
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/game/{sid}/directives")
def set_directives(sid: str, payload: DirectivesPayload):
    try:
        return {"directives": game.set_directives(sid, payload.directives)}
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class PolicyPayload(BaseModel):
    content_rating: str = "all"
    r18_enabled: bool = False
    adult_intensity: str = "成熟"
    allow_forced: bool | None = None   # None = 保留当前值（启动器等旧调用不会误关）
    sid: str = ""


# ---------- 命令 / 换装 / CG 收藏 ----------

class CommandPayload(BaseModel):
    character: str = ""
    action: str = ""


class OutfitPayload(BaseModel):
    character: str = ""
    outfit: str = ""


class CgPayload(BaseModel):
    type: str = "manual"
    note: str = ""


class PortraitDeletePayload(BaseModel):
    label: str = ""
    file: str = ""


@app.post("/api/game/{sid}/portrait-delete")
async def portrait_delete(sid: str, payload: PortraitDeletePayload):
    """立绘历程「删除此立绘」（1.7.38）：移除历史条目+删除文件+解除固定。"""
    try:
        return game.delete_portrait_version(sid, payload.label, payload.file)
    except game.GameError as e:
        raise HTTPException(400, str(e))


@app.post("/api/game/{sid}/context-reset")
async def context_reset(sid: str):
    """压缩上下文·重置 AI（1.7.36）：长局剧情压缩为摘要只留近 6 幕。"""
    try:
        return await game.context_reset(sid)
    except game.GameError as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        logging.exception("context-reset failed: %r", e)
        raise HTTPException(500, f"压缩上下文失败：{e}")


@app.post("/api/game/{sid}/command")
async def command(sid: str, payload: CommandPayload):
    try:
        if game.get_session(sid).get("mode") == "vn":
            return await vn.apply_command(sid, payload.character, payload.action)
        return await game.apply_command(sid, payload.character, payload.action)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/game/{sid}/outfit")
async def change_outfit(sid: str, payload: OutfitPayload):
    # async：换装会启动后台立绘生成任务（asyncio.create_task），必须有事件循环
    try:
        if game.get_session(sid).get("mode") == "vn":
            return vn.apply_outfit(sid, payload.character, payload.outfit)
        return game.apply_outfit(sid, payload.character, payload.outfit)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class CgRegenPayload(BaseModel):
    cg_id: str = ""


@app.post("/api/game/{sid}/cg-regen")
async def cg_regen(sid: str, payload: CgRegenPayload):
    """「刷新 CG / 重绘 CG」：按已记录的桥段画面描述重新生成（旧图保留）。
    async：CG 任务创建需要事件循环。"""
    try:
        return game.regen_cg(sid, payload.cg_id)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/cg")
def save_cg(sid: str, payload: CgPayload):
    try:
        return game.save_cg(sid, payload.type, payload.note)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.get("/api/cg")
def cg_gallery():
    return {"items": cgbook.list_entries(game.resolve_cg_file)}


# ---------- 更新：从 GitHub Releases 检查 / 下载新版本 ----------

@app.get("/api/update/check")
def update_check():
    """查询仓库最新版本与当前版本对比（不下载）。"""
    return updater.check(GAME_VERSION)


@app.get("/api/update/status")
def update_status():
    """下载进度（前端轮询）。"""
    return updater.status()


@app.post("/api/update/download")
async def update_download():
    """下载最新整合包到 update/staging，随后由更新脚本应用。"""
    if updater.status().get("status") in ("checking", "downloading", "extracting"):
        return updater.status()
    import asyncio
    asyncio.get_running_loop().run_in_executor(None, updater.download_and_stage)
    return {"status": "started", "msg": "已开始下载…"}


@app.delete("/api/cg/{cg_id}")
def cg_delete(cg_id: str):
    # B-01 同批：cg_id 用于拼 CG 目录下的文件名，先过白名单/越界断言
    if not cgbook.delete(_req_id(cg_id, "CG id")):
        raise HTTPException(404, "CG 不存在")
    return {"ok": True}


@app.get("/cg/{filename}")
def serve_cg(filename: str):
    # 文件名由服务端生成，仍做白名单校验 + safe_join（resolve 越界断言）防目录穿越
    path = _req_file(cfgmod.CG_DIR, filename, what="CG 文件名")
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)



@app.post("/api/game/sessions/policy")
def session_policy(payload: PolicyPayload):
    """按 body 中的 sid 应用内容分级（启动器用，避免动态 URL）。"""
    if not payload.sid:
        raise HTTPException(400, "缺少会话 id")
    try:
        return game.update_policy(payload.sid,
                                  payload.content_rating, payload.r18_enabled,
                                  payload.adult_intensity, payload.allow_forced)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/policy")
def update_policy(sid: str, payload: PolicyPayload):
    """切换内容分级（下一幕生效）。"""
    try:
        return game.update_policy(sid, payload.content_rating, payload.r18_enabled,
                                  payload.adult_intensity, payload.allow_forced)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/speak")
async def speak(sid: str, payload: SpeakPayload):
    """玩家固定话语：直接作为本轮行动注入，不进选项列表。"""
    try:
        if game.get_session(sid).get("mode") == "vn":
            return await vn.speak(sid, payload.text)
        return await game.speak(sid, payload.text)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


class RecutPayload(BaseModel):
    character: str = ""


@app.post("/api/game/{sid}/reload-assets")
def reload_assets(sid: str):
    """手动校正：重新读取本局全部立绘（历史路径自适应），修复后刷新显示。"""
    try:
        return game.reload_assets(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.get("/api/game/{sid}/portrait-history")
def portrait_history(sid: str):
    """该局全部角色立绘的版本历程（含归档旧版与当前版）。"""
    try:
        return game.portrait_history_view(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/game/{sid}/recut")
async def recut_portraits(sid: str, payload: RecutPayload):
    """按当前抠图模式，对全部（或指定角色）立绘的原始图重新抠图。"""
    try:
        return game.recut_portraits(sid, payload.character)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class CutPreviewPayload(BaseModel):
    character: str = ""


class CutApplyPayload(BaseModel):
    character: str = ""
    mode: str = "ai"


@app.post("/api/game/{sid}/cutpreviews")
def cut_previews(sid: str, payload: CutPreviewPayload):
    """生成指定角色的 4 种抠图方案预览图，供玩家选择。"""
    try:
        return game.cut_previews(sid, payload.character)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/cutapply")
def cut_apply(sid: str, payload: CutApplyPayload):
    """把选定的抠图方案应用到该角色全部立绘。"""
    try:
        return game.apply_cut(sid, payload.character, payload.mode)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class RegenLabelPayload(BaseModel):
    label: str = ""


@app.post("/api/game/{sid}/regenerate-label")
async def regenerate_label(sid: str, payload: RegenLabelPayload):
    """立绘历程「重绘差分」：只重画指定的一张表情差分（旧版归档）。
    非 neutral 表情走「以 neutral 为底的图生图只变表情」管线。
    async：需要事件循环创建后台生图任务。"""
    try:
        return game.regenerate_label(sid, payload.label)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/restore-raw")
async def restore_raw(sid: str):
    """立绘还原为去除背景之前的原始图。"""
    try:
        return game.restore_raw_portraits(sid)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/regenerate")
async def regenerate_portrait(sid: str, payload: RegeneratePayload):
    """针对性重新生成某个角色的立绘（防畸形/不满意时只重画该角色）。

    async 端点保证在事件循环内执行，从而能创建后台生图任务。
    """
    try:
        if game.get_session(sid).get("mode") == "vn":
            return vn.regenerate(sid, payload.target, payload.note)
        return game.regenerate_portrait(sid, payload.target, payload.note,
                                        use_initial=payload.use_initial,
                                        force_fullgen=payload.force_fullgen)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.get("/api/game/{sid}/scenes")
def scene_list(sid: str):
    try:
        return {"scenes": game.scene_summary(sid)}
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.get("/api/slots")
def list_all_slots():
    """全局存档列表（固定位置，与会话无关）。"""
    return {"slots": game.list_slots()}


@app.get("/api/game/{sid}/slots")
def list_slots(sid: str):
    # 兼容旧前端：同样返回全局存档列表
    return {"slots": game.list_slots()}


class SaveSlotPayload(BaseModel):
    name: str = "存档"


@app.post("/api/game/{sid}/slots")
def save_slot(sid: str, payload: SaveSlotPayload):
    # P18-A（t51）：coc 早返回（coc 会话不在 game.SESSIONS 里）。
    if coc.is_coc(sid):
        try:
            return coc.save_slot(sid, payload.name)
        except coc.CocError as e:
            raise HTTPException(404, str(e))
    try:
        return game.save_slot(sid, payload.name)
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/slots/{slot_id}/load")
async def load_slot(slot_id: str):
    slot_id = _req_id(slot_id, "存档槽 id")   # 同批：id 会拼 slots/{id}.json
    # P18-A（t51）：先看 envelope 的 mode（**只读，不注册会话、无副作用**）。
    # 必须在 game.load_slot 之前判：它会顺带 ensure_scene_bg()，对 coc 存档没有意义。
    if coc.peek_slot_mode(slot_id) == "coc":
        try:
            return coc.load_slot(slot_id)
        except coc.CocError as e:
            raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    try:
        r = game.load_slot(slot_id)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    s = game.get_session(r.get("sid", ""))
    if s.get("mode") == "vn":
        return vn.public_state(s)
    return r


@app.post("/api/game/{sid}/slots/{slot_id}/load")
async def load_slot_compat(sid: str, slot_id: str):
    # 兼容旧前端：读档改用全局存档库
    slot_id = _req_id(slot_id, "存档槽 id")
    if coc.peek_slot_mode(slot_id) == "coc":
        try:
            return coc.load_slot(slot_id)
        except coc.CocError as e:
            raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    try:
        r = game.load_slot(slot_id)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    s = game.get_session(r.get("sid", ""))
    if s.get("mode") == "vn":
        return vn.public_state(s)
    return r


@app.delete("/api/slots/{slot_id}")
def remove_slot(slot_id: str):
    try:
        slot_id = safemod.safe_id(slot_id, what="存档槽 id")
    except ValueError:
        return {"ok": False}   # 非法 id 与 game.delete_slot 的旧行为一致：ok=false
    return {"ok": game.delete_slot(slot_id)}





@app.post("/api/game/{sid}/library")
def export_library(sid: str):
    """导出全部角色到「角色库/」文件夹（立绘 + 设定 + 角色卡），并打开文件夹。"""
    import os
    import sys
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    result = library.export_roster(s)
    if result["portraits"] == 0:
        raise HTTPException(400, "本局还没有已生成的立绘可导出")
    try:
        path = result["path"]
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass
    return result


@app.get("/api/library")
def list_library():
    """角色库清单：开局页可选角色（设定+已有立绘变体），供导入复用。"""
    return {"characters": library.list_roster()}


@app.get("/api/library/{name}/preview")
def library_preview(name: str, dir: str = ""):
    """角色库角色缩略图（取第一张变体立绘，只读库内已存在文件）。
    同名多来源时用 ?dir= 指定具体那一份。

    B-01 同批：name / dir 都来自请求，先过 safe 原语（library.variant_path
    另有 safe_join 越界断言，被篡改的「角色设定.json」读不到库外文件）。"""
    info = library.get_roster_char(_req_name(name, "角色名"),
                                   _req_name(dir, "角色库目录名") if dir else "")
    if not info or not info.get("variants"):
        raise HTTPException(404, "该角色没有立绘")
    p = library.variant_path(info, str(info["variants"][0]["variant"]))
    if not p:
        raise HTTPException(404, "立绘文件不存在")
    from fastapi.responses import FileResponse
    return FileResponse(p)


class ComfyCheckPayload(BaseModel):
    base_url: str = "http://127.0.0.1:8188"


class ComfyLaunchPayload(BaseModel):
    path: str = ""


@app.post("/api/image/comfy/scan")
async def comfy_scan():
    """探测本机 ComfyUI 服务与桌面端程序（连接助手用）。"""
    return {"services": await image_mod.scan_local_comfy(),
            "desktop_apps": image_mod.desktop_comfy_paths()}


@app.post("/api/image/comfy/check")
async def comfy_check(payload: ComfyCheckPayload):
    """校验 ComfyUI 地址并回传可用模型列表。"""
    return await image_mod.comfy_checkpoints(payload.base_url)


@app.post("/api/image/comfy/launch")
async def comfy_launch(payload: ComfyLaunchPayload):
    """一键启动本机 ComfyUI 桌面端（仅白名单已知路径，防止任意执行）。"""
    known = set(image_mod.desktop_comfy_paths())
    if payload.path not in known:
        raise HTTPException(400, "只允许启动本机探测到的 ComfyUI 桌面端")
    try:
        import os as _os
        _os.startfile(payload.path)  # noqa: S606
    except OSError as e:
        raise HTTPException(500, f"启动失败：{e}")
    return {"ok": True, "path": payload.path}


class RestorePayload(BaseModel):
    label: str = ""
    file: str = ""
    pin: bool = False


@app.post("/api/game/{sid}/portrait-restore")
def restore_portrait(sid: str, payload: RestorePayload):
    """立绘历程「一键还原」：把归档旧版恢复为当前立绘；pin=True 表示
    「立刻使用此立绘」——该角色舞台显示固定为此版本（直到重画或取消固定）。"""
    try:
        return game.restore_portrait(sid, payload.label, payload.file,
                                     pin=payload.pin)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) or "归档" in str(e) else 400, str(e))


class UnpinPayload(BaseModel):
    name: str = ""


@app.post("/api/game/{sid}/portrait-unpin")
def unpin_portrait(sid: str, payload: UnpinPayload):
    """立绘历程「取消固定」：恢复该角色按当前阶段/表情/服装自动取图。"""
    try:
        return game.unpin_portrait(sid, payload.name)
    except game.GameError as e:
        raise HTTPException(404, str(e))


class PinLabelPayload(BaseModel):
    label: str = ""


@app.post("/api/game/{sid}/portrait-pin")
def pin_portrait(sid: str, payload: PinLabelPayload):
    """立绘历程「替换为此立绘」：指定标签即刻固定为该角色当前显示
    （不动文件；标「当前」但未生效的版本也能直接使用）。"""
    try:
        return game.pin_portrait(sid, payload.label)
    except game.GameError as e:
        raise HTTPException(404, str(e))


class TsfTargetPayload(BaseModel):
    hair: str = ""
    build: str = ""
    aura: str = ""
    hair_color: str = ""
    keep_hair_color: bool = True


@app.post("/api/game/{sid}/tsf-target")
async def set_tsf_target(sid: str, payload: TsfTargetPayload):
    """主角「转变目标设定」（仅 TSF 主角）：保存并立即按新目标重画当前阶段。
    必须 async——内部 create_task 需要事件循环（同 regenerate-label 教训）。"""
    try:
        return game.set_tsf_target(sid, payload.model_dump())
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/game/{sid}/archive")
def export_archive(sid: str):
    """把整局打包为 data/archives/ 下的 zip 档案（可分享/可再导入）。"""
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    try:
        return {"ok": True, **archive_mod.export_session(s)}
    except archive_mod.ArchiveError as e:
        raise HTTPException(400, str(e))


@app.get("/api/archives")
def list_archives():
    """档案库列表（标题/轮数/大小/时间）。"""
    return {"archives": archive_mod.list_archives()}


@app.post("/api/archives/import")
async def import_archive(request: Request):
    """导入对局档案：请求体为 zip 原始字节（Content-Type: application/zip）。"""
    data = await request.body()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > 500 * 1024 * 1024:
        raise HTTPException(413, "档案超过 500MB")
    try:
        return archive_mod.import_archive(data)
    except archive_mod.ArchiveError as e:
        raise HTTPException(400, str(e))


@app.get("/api/archives/{name}/download")
def download_archive(name: str):
    """下载档案文件（分享给他人）。"""
    import re as _re
    name = _req_name(name, "档案文件名")
    if not _re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9_\-（）() ·——]{1,90}\.zip", name):
        raise HTTPException(400, "非法文件名")
    p = _req_file(archive_mod._archives_dir(), name, what="档案文件")
    if not p.is_file():
        raise HTTPException(404, "档案不存在")
    from fastapi.responses import FileResponse
    return FileResponse(p, filename=name, media_type="application/zip")


@app.post("/api/archives/{name}/import")
def import_local_archive(name: str):
    """导入本地档案库中的一份 zip（继续玩/另存为独立对局）。"""
    import re as _re
    name = _req_name(name, "档案文件名")
    if not _re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9_\-（）() ·——]{1,90}\.zip", name):
        raise HTTPException(400, "非法文件名")
    p = _req_file(archive_mod._archives_dir(), name, what="档案文件")
    if not p.is_file():
        raise HTTPException(404, "档案不存在")
    try:
        return archive_mod.import_archive(str(p))
    except archive_mod.ArchiveError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/archives/{name}")
def delete_archive(name: str):
    # B-01 同批：name 来自请求路径，先过 safe_name（分隔符/`..`/同形字符一律 400），
    # 再交给 archive.delete_archive（内含同一套 safe_join 越界断言）
    name = _req_name(name, "档案文件名")
    if archive_mod.delete_archive(name):
        return {"ok": True}
    raise HTTPException(404, "档案不存在")


@app.post("/api/game/{sid}/export")
def export_sprites(sid: str):
    """把本局立绘导出为修图软件友好的目录（干净命名+白底版+manifest）并打开。"""
    import os
    import sys
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    result = exporter.export_session(s)
    if result["files"] == 0:
        raise HTTPException(400, "本局还没有已就绪的立绘可导出")
    try:
        if sys.platform == "win32":
            os.startfile(result["path"])  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", result["path"]])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", result["path"]])
    except OSError:
        pass
    return result


class SceneSetPayload(BaseModel):
    name: str = ""
    action: str = "enter"   # enter = 进入/更换场景；rerender = 重生成当前场景背景


@app.post("/api/game/{sid}/scene-set")
async def scene_set(sid: str, payload: SceneSetPayload):
    """「读取场景」：进入已有场景（复用固定背景）/ 更换到新场景 / 重生成当前背景。

    B-03：必须是 async——进入新场景/重绘背景会在 _assign_scene_bg →
    _queue_asset 里排后台生成任务，同步端点跑在线程池里没有事件循环。"""
    try:
        return game.scene_set(sid, payload.name, payload.action)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class OpenFolderPayload(BaseModel):
    sid: str = ""


@app.post("/api/open-folder")
def open_folder(payload: OpenFolderPayload = OpenFolderPayload()):
    """在系统资源管理器中打开「当前对局的立绘目录」（portraits 等子目录），
    未进入对局时打开立绘缓存根目录。

    B-10 加固：本端点会**在本机启动进程/打开窗口**，因此必须
      ① 写操作鉴权（中间件：token + Origin + 回环 Host）；
      ② 打开目标必须是 `CACHE_DIR` **内部已存在**的目录（resolve 后包含断言，
         挡 junction/软链接逃逸），绝不允许请求方指定任意路径。
    """
    import os
    import subprocess
    import sys
    base = cfgmod.CACHE_DIR.resolve()
    path = str(base)
    sid = (payload.sid or "").strip()
    if sid:
        try:
            session = game.get_session(_req_id(sid, "会话 id"))
            # 目录名由会话数据（标题/文档名）拼成，仍走 safe_join 越界断言
            path = str(_req_file(cfgmod.CACHE_DIR,
                                 game._session_dir_name(session), "portraits",
                                 what="对局立绘目录"))
        except game.GameError:
            pass
    target = Path(path).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(403, "只能打开本机立绘缓存目录内的文件夹")
    if not target.is_dir():
        raise HTTPException(404, "目标目录不存在（还没有生成过立绘？）")
    path = str(target)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"ok": True, "path": path}
    except OSError as e:
        raise HTTPException(500, f"无法打开文件夹：{e}")


@app.post("/api/game/{sid}/advance")
async def advance(sid: str, payload: AdvancePayload):
    try:
        # P18-A（t51）：coc 早返回。**不**走 game.get_session（coc 会话不在那本表里，
        # 走那条会抛"会话不存在"）。
        if coc.is_coc(sid):
            # C×T4（t56）：coc_tsf 走同一条推进路径，回合结束后同步镜像进度
            # （`_coc_tsf_refresh` 对纯 coc 会话是空操作：取不到镜像就直接返回）。
            done = await coc.advance_async(coc.get(sid), payload.choice_index)
            _coc_tsf_refresh(sid)
            return _coc_tsf_decorate(done, sid)
        if game.get_session(sid).get("mode") == "vn":
            return await vn.advance(sid, payload.choice_index)
        return await game.advance(sid, payload.choice_index)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    except coc.CocError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/api/game/{sid}")
async def game_state(sid: str):
    """进入对局/刷新页面：当前幕背景不可用时立即补生成。

    B-02：必须是 async——ensure_scene_bg 会排后台生成任务，同步端点跑在
    线程池里没有事件循环（旧行为：RuntimeError 被吞掉、背景永不补生成，
    每次刷新还泄漏一个永久 pending 资产）。"""
    # P18-A（t51）：coc 必须在 game.get_session 之前早返回 ——
    # coc 会话不在 game.SESSIONS 里；而且即使塞进去，下面的
    # game.ensure_scene_bg() / game.public_state() 会读 prot["stats"]（TSF 字段）而炸。
    if coc.is_coc(sid):
        d = coc.public_state(coc.get(sid))
        d["pinned"] = {}
        d["tsf_target"] = {}
        # C×T4（t56）：coc_tsf 的附加字段（纯 coc 会话不加，行为与 t51 完全一致）。
        # 值全部来自 t54 的接口（`mechanical_effects`）——前端**不重算**阶段。
        return _coc_tsf_decorate(d, sid)
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    game.ensure_scene_bg(s)   # 进入对局：当前幕背景不可用时立即补生成
    if s.get("mode") == "vn":
        d = vn.public_state(s)
    else:
        d = game.public_state(s)
    d["pinned"] = s.get("pinned_portraits", {})
    d["tsf_target"] = s.get("tsf_target", {})
    return d


@app.get("/api/game/{sid}/assets")
def game_assets(sid: str):
    # C×T4（t56）：coc 会话不在 game.SESSIONS 里，前端 2 秒轮询这条时会拿到 404
    # （控制台报错）。这里在 `game.get_session` **之前**早返回：
    #   · coc_tsf → 返回镜像自己的资产（新立绘生成完就能被轮询刷出来，不必刷新页面）
    #   · 纯 coc  → 返回空资产表（保持 t51 行为：无立绘管线）
    # 只新增早返回分支，既有 TSF/VN 路径一行未动。
    if coc.is_coc(sid):
        mirror = _coc_tsf_mirror(sid)
        assets = (mirror or {}).get("assets") or {"portraits": {}, "backgrounds": {}}
        return {"assets": assets, "bg_map": {}}
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    return {"assets": s["assets"], "bg_map": s["bg_map"]}


@app.get("/api/game/{sid}/tasks")
async def game_tasks(sid: str):
    """M-07（后端部分）：当前对局的生成队列与 SD 串行通道状态。

    解决「一直在跑但没新图」的可观测性：返回正在生成/正在排队的资产
    （含各自 elapsed）、资产计数，以及 SD 闸门当前持有者与等待者。
    **只读**：不触发任何生成、不改任何状态。"""
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    return game.tasks_view(s)


@app.get("/api/sd/observe")
def sd_observe(reset: int = 0):
    """M-06：导出「生成 / 打标（interrogate/tagger）/ 探测」时间窗与重叠区间报告。

    `reset=1` 时导出后清空观测缓冲（便于分次采样）。**只读观测**：
    本端点不改变任何互斥/并发行为（Semaphore(1) 的容量与获取顺序未动）。"""
    return image_mod.sd_report(reset=bool(reset))


@app.get("/api/game/{sid}/history")
def game_history(sid: str):
    try:
        return {"history": game.history_view(game.get_session(sid))}
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/game/{sid}")
def remove_game(sid: str):
    # B-01 同批：sid 会拼成 saves/{sid}.json（delete_session），先过白名单
    game.delete_session(_req_id(sid, "会话 id"))
    return {"ok": True}


@app.get("/img/{filename:path}")
def serve_image(filename: str):
    # 路径通配：支持 三层（sid/{portraits|backgrounds|raw|preview}/文件）、
    # 两层（sid/文件）与根级（旧哈希）；白名单防目录穿越
    import re as _re
    if ".." in filename or "\\" in filename or filename.startswith("/"):
        raise HTTPException(400, "非法文件名")
    # 文件名白名单：允许空格（历史场景名「星冠学园 · 走廊」会进入文件名，
    # 此前白名单缺空格 → /img 400 → 背景 img onerror → 旧档背景永远黑屏）
    _FN = r"[A-Za-z0-9_.\-~ \u4e00-\u9fa5（）()·——]+\.(png|jpg|jpeg|webp)"
    _DIR = r"[\u4e00-\u9fa5A-Za-z0-9_\-]{1,32}"
    ok = bool(_re.fullmatch(_FN, filename))
    parts = filename.split("/")
    if len(parts) == 2 and _re.fullmatch(_DIR, parts[0]):
        ok = ok or bool(_re.fullmatch(_FN, parts[1]))
    if len(parts) == 3 and _re.fullmatch(_DIR, parts[0]) \
            and parts[1] in ("portraits", "backgrounds", "raw", "preview", "versions"):
        ok = ok or bool(_re.fullmatch(_FN, parts[2]))
    if not ok:
        raise HTTPException(400, "非法文件名")
    # B-01 同批加固：白名单之外再断言 resolve 后仍在立绘缓存目录内
    # （三层语义不变：sid/{portraits|backgrounds|raw|preview|versions}/文件）
    try:
        path = safemod.safe_relpath(filename, cfgmod.CACHE_DIR, max_parts=3,
                                    what="图片路径")
    except ValueError as e:
        raise HTTPException(400, f"非法请求参数：{e}")
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """旧缓存页面可能仍请求 /favicon.ico，返回 SVG 图标避免 404。"""
    from fastapi.responses import Response
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
           "<text y='26' font-size='26'>✦</text></svg>")
    return Response(content=svg, media_type="image/svg+xml")


# 冻结（exe）模式下 static 在包内只读区；源码运行用项目 static/
import sys as _sys
_static_dir = (cfgmod.BUNDLE / "static" if getattr(cfgmod, "BUNDLE", None)
               else ROOT / "static")
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


if __name__ == "__main__":
    import socket

    import uvicorn

    # 端口可用 TSF_PORT 覆盖（默认 8765 不变）：窗口化宿主/隔离验收要在**不碰
    # 用户正在跑的 8765 实例**的前提下起自己的服务。绑定语义、单实例探测分支、
    # `server.run(sockets=socks)` 调用形态都不受影响（只是这个常量的取值来源变了）。
    PORT = int(os.environ.get("TSF_PORT", "8765") or 8765)
    # ---- 更新检测：update/ 有新包时引导一键 bat 更新（数据固定，替换安全）----
    import os as _os
    if (_os.path.basename(_sys.executable).lower().endswith(".exe")
            and "python" not in _os.path.basename(_sys.executable).lower()):
        try:
            _upd = ROOT / "update" / "TSF_Galgame.exe"
            if _upd.is_file():
                # 1.7.52 起改名为「开发者更新游戏状态.bat」；旧名仍兼容
                _bat = ROOT / "开发者更新游戏状态.bat"
                if not _bat.is_file():
                    _bat = ROOT / "安装更新.bat"
                print("=" * 46)
                print("  [UPDATE] 检测到新版本更新包 (update\\TSF_Galgame.exe)")
                if _bat.is_file():
                    print(f"  [UPDATE] 在文件夹中双击「{_bat.name}」即可自动完成更新；")
                    print("  [UPDATE] 或者：先点 [X 关闭游戏]，把 update\\ 里的新 exe 覆盖即可")
                print("  [UPDATE] 进度/档案不会丢失（数据在 D:\\TSF_Galgame_Data）")
                print("=" * 46)
        except OSError:
            pass
    # B-10 / t39：**默认只绑回环**（原先 0.0.0.0 + :: 会让局域网内任意网页/设备
    # 访问到本服务，配合无鉴权的写端点可被抹数据 + 关机）。
    # 如需局域网访问（如手机），显式设 TSF_ALLOW_LAN=1 —— 此时写操作仍需本地 token，
    # 且 Host/Origin 白名单会自动把本机局域网 IP 加进来。
    lan_opt_in = str(os.environ.get("TSF_ALLOW_LAN", "")).strip().lower() in ("1", "true", "yes", "on")
    bind_v4 = "0.0.0.0" if lan_opt_in else "127.0.0.1"
    bind_v6 = "::" if lan_opt_in else "::1"
    # 注意：`if __name__ == "__main__":` 里仍是模块作用域，直接赋值即可（不能写 global）
    _LAN_MODE = lan_opt_in
    socks = []
    # Windows 上 SO_REUSEADDR 会允许多实例静默共存同一端口，导致请求被旧进程
    # 接走；仅在非 Windows 平台设置，让重复启动直接报"端口被占用"
    import sys
    reuse = socket.SO_REUSEADDR if sys.platform != "win32" else 0
    # IPv6（设 v6only 避免与 IPv4 抢占），系统不支持时自动跳过
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        if reuse:
            s6.setsockopt(socket.SOL_SOCKET, reuse, 1)
        s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s6.bind((bind_v6, PORT))
        s6.listen(128)
        socks.append(s6)
    except OSError as e:
        print(f"IPv6 监听未启用（不影响 IPv4 使用）：{e}")
    # IPv4 必须可用
    s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuse:
        s4.setsockopt(socket.SOL_SOCKET, reuse, 1)
    try:
        s4.bind((bind_v4, PORT))
        s4.listen(128)
    except OSError:
        for s in socks:
            s.close()
        # 复用旧实例：端口被占时先探测是不是本游戏服务，是则直接打开浏览器退出
        try:
            import json as _json
            import urllib.request as _ureq
            with _ureq.urlopen(f"http://127.0.0.1:{PORT}/api/config", timeout=2) as _r:
                _probe = _json.loads(_r.read().decode("utf-8"))
            if isinstance(_probe, dict) and "llm_mock" in _probe:
                print(f"检测到游戏已在运行（端口 {PORT}），直接为你打开浏览器…")
                import webbrowser
                webbrowser.open(f"http://127.0.0.1:{PORT}")
                sys.exit(0)
        except Exception:
            pass
        print(f"端口 {PORT} 已被其他进程占用，无法启动（不是本游戏服务）。")
        print("· 最简单方式：双击项目根目录的 start.bat（会按端口结束后再启动）")
        print("· 或手动结束：taskkill /PID <占用进程PID> /F  后重新运行 python server.py")
        sys.exit(1)
    socks.append(s4)

    lan_ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
    except OSError:
        pass
    # B-10：opt-in 局域网时，把本机所有局域网地址加入 Host/Origin 白名单
    if lan_opt_in:
        try:
            import socket as _s2
            for _info in _s2.getaddrinfo(_s2.gethostname(), None, _s2.AF_INET):
                _LAN_HOSTS.add(_info[4][0])
        except OSError:
            pass
        if lan_ip:
            _LAN_HOSTS.add(lan_ip)

    # B-10：生成本轮随机 token（写操作鉴权；工具链可读 DATA_DIR/local_token.json）
    _init_local_token()

    print()
    print("=" * 52)
    print("  自动化AI Galgame 已启动，请用浏览器打开：")
    print("  · http://127.0.0.1:8765")
    print("  · http://localhost:8765")
    if lan_opt_in:
        print("  ⚠ 已开启局域网监听（TSF_ALLOW_LAN=1）：")
        if lan_ip:
            print(f"    · 局域网设备（如手机）: http://{lan_ip}:8765")
        print("    · 写操作仍需本地 token；只在你信任的网络里这么做")
    else:
        print("  （默认仅本机可访问；如需手机/局域网访问：设 TSF_ALLOW_LAN=1 再启动）")
    print("  （保持本窗口开启；关闭窗口即停止服务）")
    print("=" * 52)
    print()
    if getattr(sys, "frozen", False):
        # 分享版 exe：启动后自动打开浏览器，双击即玩
        import webbrowser
        webbrowser.open("http://127.0.0.1:8765")
    startup_mod.mark(8, "starting web service...", "启动服务（打开浏览器）…")

    # 服务端口实际可连时关闭启动进度窗（避免窗口盖住游戏页面）
    def _close_splash_online():
        import socket as _sock
        try:
            for _ in range(150):
                try:
                    with _sock.create_connection(("127.0.0.1", PORT), timeout=0.3):
                        startup_mod.finish()
                        return
                except OSError:
                    time.sleep(0.2)
        except Exception:
            pass
        startup_mod.finish()

    import threading as _threading
    _threading.Thread(target=_close_splash_online, daemon=True).start()

    # ★ console=False（windowed）日志改道（t49 / 契约 §3.2 P8、U1）：
    # uvicorn 默认 LOGGING_CONFIG 给 `uvicorn` / `uvicorn.error` / `uvicorn.access`
    # 挂的是 **stderr** handler 且 `propagate=False` ⇒ 冻结成 windowed 后
    # `sys.stderr is None`，访问日志会**静默丢失**。这里：
    #   · `log_config=None` 不让 uvicorn 覆盖我们的 logging 配置；
    #   · 给三个 uvicorn logger 显式挂**文件 handler**（data/uvicorn.log），
    #     并保留 propagate 关闭（避免与 debug.log 重复），
    #   · 只有在**确实存在** stderr 时才额外加控制台 handler。
    _uv_handlers = []
    try:
        _uv_fh = logging.FileHandler(cfgmod.DATA_DIR / "uvicorn.log", encoding="utf-8")
        _uv_fh.setLevel(logging.INFO)
        _uv_fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        _uv_handlers.append(_uv_fh)
    except OSError as e:            # 落盘失败不致命：退回根 logger 的 debug.log
        logging.warning("uvicorn.log 创建失败，将只写 debug.log：%s", e)
    if _sys.stderr is not None:     # 有控制台时保持可见（非 windowed 形态）
        _uv_sh = logging.StreamHandler(_sys.stderr)
        _uv_sh.setLevel(logging.INFO)
        _uv_sh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        _uv_handlers.append(_uv_sh)
    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _lg = logging.getLogger(_name)
        _lg.setLevel(logging.INFO)
        _lg.handlers = list(_uv_handlers)   # 顶掉 uvicorn 默认的 stderr handler
        _lg.propagate = False if _uv_handlers else True

    server = uvicorn.Server(uvicorn.Config(app, log_level="info", log_config=None))
    _UVICORN_SERVER = server   # B-04：/api/shutdown 靠它优雅停机（should_exit）
    server.run(sockets=socks)

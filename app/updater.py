"""从 GitHub Releases 检查 / 下载新版本（游戏内「检查更新」用）。

链路：
1. 检查：GET api.github.com/.../releases/latest（国内可直连），与 GAME_VERSION 比对；
2. 下载：release 资产 zip（github.com / objects.githubusercontent.com，国内常需代理）
   —— 自动读取 Windows 系统代理，也可在 config.json 里写 game.update_proxy 指定；
3. 落盘：解压到 update/staging/，随后由「开发者更新游戏状态.bat」应用并重启。

数据（存档 / CG / 配置）在独立的数据目录，更新不会影响。
"""
import json
import logging
import re
import time
import zipfile
from pathlib import Path

import httpx

from . import config as cfgmod

log = logging.getLogger("galgame.updater")

REPO = "xixiaoruo/-galgame-tsf-galgame-AUTO-GALGAME-and-tsf-galgame-"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
STAGING = cfgmod.ROOT / "update" / "staging"
UPDATE_DIR = cfgmod.ROOT / "update"

# 后台下载状态（前端轮询 /api/update/status）
STATE: dict = {"status": "idle", "progress": 0, "msg": "", "result": None}


def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3] or [0])


def system_proxy() -> str:
    """Windows 系统代理（VP N/加速器常用）；config 里的 game.update_proxy 优先。"""
    try:
        cfg = cfgmod.load_config()
        forced = str((cfg.get("game") or {}).get("update_proxy", "") or "").strip()
        if forced:
            return forced
    except Exception:
        pass
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
        if enabled and server:
            server = str(server).strip()
            if not server.startswith("http"):
                server = "http://" + server
            return server
    except Exception:
        pass
    return ""


def current_version() -> str:
    """当前版本号：GAME_VERSION 定义在 server.py（以 __main__ 运行），
    这里按实际加载的模块名去找，取不到就返回空串（界面显示「未知」）。"""
    import sys
    for name in ("__main__", "server", "app.server"):
        mod = sys.modules.get(name)
        v = getattr(mod, "GAME_VERSION", "") if mod is not None else ""
        if v:
            return str(v)
    return ""


def check(current: str = "") -> dict:
    """查询最新版本；返回与当前版本的比对结果（不下载）。"""
    cur = str(current or current_version())
    out = {"current": cur, "latest": "", "has_update": False, "notes": "",
           "published_at": "", "asset": None, "proxy": system_proxy(), "error": ""}
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r = c.get(API_LATEST, headers={"Accept": "application/vnd.github+json",
                                           "User-Agent": "tsf-galgame-updater"})
        if r.status_code == 404:
            out["error"] = "仓库还没有发布任何 Release"
            return out
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        out["error"] = f"查询失败：{type(e).__name__}: {e}"
        return out
    out["latest"] = str(data.get("tag_name", "")).lstrip("v")
    out["notes"] = str(data.get("body", ""))[:4000]
    out["published_at"] = str(data.get("published_at", ""))
    for a in data.get("assets", []):
        if str(a.get("name", "")).endswith(".zip"):
            out["asset"] = {"name": a["name"], "size": int(a.get("size", 0)),
                            "url": a.get("browser_download_url", "")}
            break
    out["has_update"] = bool(out["latest"] and _ver_tuple(out["latest"]) > _ver_tuple(cur))
    return out


def download_and_stage() -> dict:
    """下载最新 Release 的 zip 并解压到 update/staging/（供更新脚本应用）。"""
    global STATE
    STATE = {"status": "checking", "progress": 0, "msg": "正在检查最新版本…", "result": None}
    info = check()
    if info.get("error"):
        STATE = {"status": "error", "progress": 0, "msg": info["error"], "result": None}
        return STATE
    asset = info.get("asset")
    if not asset:
        STATE = {"status": "error", "progress": 0,
                 "msg": "最新版本没有可下载的整合包附件", "result": None}
        return STATE
    proxy = info.get("proxy") or None
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPDATE_DIR / "_download.zip"
    total = asset["size"] or 0
    got = 0
    STATE = {"status": "downloading", "progress": 0,
             "msg": f"正在下载 {asset['name']}（{total / 1024 / 1024:.1f} MB）…", "result": None}
    try:
        kw = {"timeout": httpx.Timeout(30.0, read=120.0), "follow_redirects": True}
        if proxy:
            kw["proxy"] = proxy
        with httpx.Client(**kw) as c:
            with c.stream("GET", asset["url"],
                          headers={"User-Agent": "tsf-galgame-updater"}) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            STATE["progress"] = min(99, int(got * 100 / total))
                            STATE["msg"] = (f"正在下载… {got / 1024 / 1024:.0f} /"
                                            f" {total / 1024 / 1024:.0f} MB")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        hint = "" if proxy else "（未检测到系统代理，可在 config.json 里设 game.update_proxy）"
        STATE = {"status": "error", "progress": 0,
                 "msg": f"下载失败：{type(e).__name__}: {e} {hint}".strip(), "result": None}
        return STATE

    STATE = {"status": "extracting", "progress": 99, "msg": "正在解压…", "result": None}
    try:
        if STAGING.exists():
            import shutil
            shutil.rmtree(STAGING, ignore_errors=True)
        STAGING.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp) as z:
            base = STAGING.resolve()
            for member in z.infolist():
                name = member.filename
                # 防 Zip Slip：拒绝绝对路径、盘符与 .. 越界条目
                if (name.startswith(("/", "\\")) or ".." in Path(name).parts
                        or re.match(r"^[A-Za-z]:", name)):
                    raise ValueError(f"压缩包内含非法路径：{name}")
                if not str((base / name).resolve()).startswith(str(base)):
                    raise ValueError(f"压缩包内含越界路径：{name}")
            z.extractall(STAGING)
        # 包内若是单层目录，把内容提到 staging 根，方便脚本整体覆盖
        kids = [p for p in STAGING.iterdir()]
        if len(kids) == 1 and kids[0].is_dir():
            inner = kids[0]
            for p in list(inner.iterdir()):
                p.rename(STAGING / p.name)
            inner.rmdir()
    except Exception as e:
        STATE = {"status": "error", "progress": 0, "msg": f"解压失败：{e}", "result": None}
        return STATE
    finally:
        tmp.unlink(missing_ok=True)

    bat = cfgmod.ROOT / "开发者更新游戏状态.bat"
    STATE = {"status": "done", "progress": 100,
             "msg": f"已下载并解压到 update\\staging（版本 {info['latest']}）。"
                    f"运行「{bat.name}」即可应用并重启。",
             "result": {"version": info["latest"],
                        "bat": bat.name if bat.is_file() else "开发者更新游戏状态.bat",
                        "staged": str(STAGING)}}
    log.info("update staged: %s", STATE["result"])
    return STATE


def status() -> dict:
    return dict(STATE)


def staged_ready() -> bool:
    """staging 里是否已有一份可直接应用的更新。"""
    return (STAGING / "TSF_Galgame.exe").is_file()

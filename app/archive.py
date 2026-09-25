"""对局归档：把一整局（剧情 log + 立绘 + 背景 + raw + 版本归档）打包成单个
zip 档案，可导出/分享，也可再导入本机继续玩。

数据只在 data/archives/ 与 data/cache/ 之间流动：包内只有对局内容，
绝不包含 config.json / API Key。导入时逐成员严格校验（白名单目录、
路径穿越拒绝、大小上限），重名目录自动加后缀、绝不覆盖已有对局。
"""
import io
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from . import config as cfgmod

MAX_ARCHIVE_BYTES = 500 * 1024 * 1024  # 500MB 上限（本地游戏，够用且防呆）
CN = "\u4e00-\u9fa5"
_MEMBER_RE = re.compile(
    rf"^[{CN}A-Za-z0-9_\-]{{1,32}}"
    rf"/(portraits|backgrounds|raw|preview|versions)"
    rf"/[A-Za-z0-9_.~{CN}（）()·——]{{1,64}}\.(png|jpg|jpeg|webp)$")
_INFO_RE = re.compile(rf"^[{CN}A-Za-z0-9_\-]{{1,32}}/info\.json$")
_DIR_RE = re.compile(rf"^[{CN}A-Za-z0-9_\-]{{1,32}}$")
_ZIP_NAME_RE = re.compile(rf"^[{CN}A-Za-z0-9_\-（）() ·——]{{1,90}}\.zip$")


class ArchiveError(Exception):
    pass


def _archives_dir() -> Path:
    d = cfgmod.DATA_DIR / "archives"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _zip_name(session: dict) -> str:
    title = "".join(c for c in str(session.get("title", ""))
                    if c.isalnum() or c in "_- ")[:20] or "对局"
    handle = session.get("prot_handle") or str(session.get("sid", ""))
    safe = "".join(c for c in str(handle) if c.isalnum() or c in "_-") or "arch"
    return f"{title}_{safe}.zip"


def export_session(session: dict) -> dict:
    """打包整局为 data/archives/{标题}_{文档名}.zip，返回 {file, size}。"""
    from . import game
    dir_name = game._session_dir_name(session)
    src_dir = cfgmod.CACHE_DIR / dir_name
    out = _archives_dir() / _zip_name(session)
    tmp = out.with_suffix(".tmp.zip")
    total = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=7) as z:
        if src_dir.is_dir():
            for p in sorted(src_dir.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(src_dir).as_posix()
                member = f"{dir_name}/{rel}"
                total += p.stat().st_size
                if total > MAX_ARCHIVE_BYTES:
                    raise ArchiveError("本局数据超过 500MB，无法打包归档")
                z.write(p, member)
        snap = json.loads(json.dumps(session, ensure_ascii=False))
        snap["archived_at"] = time.time()
        z.writestr("session.json", json.dumps(snap, ensure_ascii=False))
    tmp.replace(out)
    return {"file": out.name, "size": out.stat().st_size, "dir": dir_name}


def check_members(names: list[str]) -> str:
    """白名单校验全部 zip 成员：返回对局目录名；任何非法成员直接拒绝
    （正则不含 "/" 之外的分隔符、不允许绝对路径与 .. 成分，故名即安全）。"""
    if "session.json" not in names:
        raise ArchiveError("不是有效的对局档案（缺少 session.json）")
    dir_name = None
    for n in names:
        if n == "session.json":
            continue
        if not (_MEMBER_RE.fullmatch(n) or _INFO_RE.fullmatch(n)):
            raise ArchiveError(f"档案包含非法路径：{n}")
        d = n.split("/", 1)[0]
        if not _DIR_RE.fullmatch(d):
            raise ArchiveError(f"非法对局目录名：{d}")
        if dir_name is None:
            dir_name = d
        elif d != dir_name:
            raise ArchiveError("档案包含多个对局目录")
    if not dir_name:
        raise ArchiveError("档案缺少对局目录")
    return dir_name


def list_archives() -> list[dict]:
    """data/archives/ 下的档案列表（含标题/轮数/大小，供档案库显示）。"""
    out = []
    for z in _archives_dir().glob("*.zip"):
        try:
            with zipfile.ZipFile(z) as zf:
                sip = zf.getinfo("session.json")
                if sip.file_size > 8 * 1024 * 1024:
                    continue
                snap = json.loads(zf.read("session.json").decode("utf-8"))
            out.append({
                "file": z.name,
                "size": z.stat().st_size,
                "mtime": int(z.stat().st_mtime),
                "title": str(snap.get("title", "")),
                "mode": str(snap.get("mode", "tsf")),
                "protagonist": str((snap.get("protagonist") or {}).get("name", "")),
                "turn_no": len(snap.get("log") or []),
                "sid": str(snap.get("sid", "")),
            })
        except (zipfile.BadZipFile, json.JSONDecodeError, OSError, KeyError):
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _pick_target_dir(dir_name: str, session_title: str) -> tuple[str, str, bool]:
    """决定导入目标目录；已存在时加「_导入1/2」后缀并返回新文档名。
    返回 (target_dir, new_handle, 是否需要改写引用)。"""
    from . import game
    base = dir_name
    if not (cfgmod.CACHE_DIR / base).exists():
        return base, "", False
    i = 1
    while True:
        suffix = f"导入{i}"
        max_base = 32 - len(suffix) - 1
        candidate = f"{base[:max_base]}_{suffix}"
        if not (cfgmod.CACHE_DIR / candidate).exists():
            break
        i += 1
    safe_title = game._safe_title(session_title)
    handle = candidate[len(safe_title) + 1:] if candidate.startswith(safe_title + "_") \
        else candidate
    return candidate, handle, True


def import_archive(path_or_bytes) -> dict:
    """导入对局档案：校验 → 提取 → 引用改写 → 注册会话 → 返回 public_state。
    返回 dict 含 ok/sid/state；异常抛 ArchiveError。"""
    from . import game
    zf = zipfile.ZipFile(path_or_bytes if isinstance(path_or_bytes, (str, Path))
                         else io.BytesIO(path_or_bytes))
    names = zf.namelist()
    total = sum(0 if n == "session.json" else zf.getinfo(n).file_size for n in names)
    if total > MAX_ARCHIVE_BYTES:
        raise ArchiveError("档案超过 500MB，拒绝导入")
    dir_name = check_members(names)
    try:
        snap = json.loads(zf.read("session.json").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ArchiveError(f"session.json 损坏：{e}")
    if not isinstance(snap, dict) or "sid" not in snap or "assets" not in snap \
            or ("protagonist" not in snap and "mode" not in snap):
        raise ArchiveError("session.json 结构异常，拒绝导入")
    title = str(snap.get("title", ""))
    target, new_handle, need_rewrite = _pick_target_dir(dir_name, title)

    # 1) 提取：成员已全量白名单校验（名内无 ..、无绝对路径），先落到
    #    临时目录，再把对局目录整体归位到最终目标（目标名经 _pick_target_dir
    #    保证不存在，os.replace 同卷重命名安全）
    tmp_root = cfgmod.CACHE_DIR / f"@tmp_{uuid.uuid4().hex[:8]}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        zf.extractall(tmp_root, members=[n for n in names if n != "session.json"])
        inner = tmp_root / dir_name
        if not inner.is_dir():
            raise ArchiveError("档案缺少对局目录数据")
        os.replace(inner, cfgmod.CACHE_DIR / target)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    # 2) 会话身份：sid 冲突时换新（避免同名对局互相覆盖）
    sid = str(snap["sid"])
    if sid in game.SESSIONS or (cfgmod.SAVES_DIR / f"{sid}.json").is_file():
        sid = uuid.uuid4().hex[:12]
        snap["sid"] = sid
    if need_rewrite:
        if new_handle:
            snap["prot_handle"] = new_handle
        # 3) 改写全部文件引用：旧对局前缀 → 目标目录
        for group in snap.get("assets", {}).values():
            for item in group.values():
                for k in ("file", "raw_file"):
                    v = str(item.get(k) or "")
                    if v.startswith(dir_name + "/"):
                        item[k] = target + v[len(dir_name):]
        for e in snap.get("portrait_history") or []:
            for k in ("file", "raw_file"):
                v = str(e.get(k) or "")
                if v.startswith(dir_name + "/"):
                    e[k] = target + v[len(dir_name):]
    game._ensure_asset_paths(snap)
    game.SESSIONS[sid] = snap
    game.persist(snap)
    if snap.get("mode") == "vn":
        from . import vn as _vn
        return {"ok": True, "sid": sid, "state": _vn.public_state(snap)}
    return {"ok": True, "sid": sid, "state": game.public_state(snap)}


def delete_archive(name: str) -> bool:
    """删除归档（仅限 archives 目录内的 .zip，文件名白名单校验）。"""
    if not _ZIP_NAME_RE.fullmatch(name):
        return False
    f = _archives_dir() / name
    if not f.is_file():
        return False
    try:
        f.unlink()
        return True
    except OSError:
        return False

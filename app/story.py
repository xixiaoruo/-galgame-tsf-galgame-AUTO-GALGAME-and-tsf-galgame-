"""剧本存储：data/storylines/ 下的 JSON CRUD（启动器与网页共用）。"""
import json
import time
import uuid
from pathlib import Path

from . import config as cfgmod


def _dir() -> Path:
    d = cfgmod.DATA_DIR / "storylines"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(sid: str) -> Path:
    return _dir() / f"{sid}.json"


import re as _re

_ID_OK = _re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _clean(st: dict) -> dict:
    """规范化剧本字段（防脏数据）；id 非白名单则重新生成（防路径穿越）。"""
    chars = []
    for c in st.get("characters") or []:
        if isinstance(c, dict) and str(c.get("name", "")).strip():
            chars.append({
                "name": str(c["name"]).strip()[:12],
                "appearance": str(c.get("appearance", "")).strip()[:300],
                "personality": str(c.get("personality", "")).strip()[:200],
            })
    catch = [str(x).strip()[:60] for x in (st.get("catchphrases") or [])
             if str(x).strip()][:5]
    rating = str(st.get("content_rating", "18")).strip()[:8]
    if rating not in ("all", "16", "18"):
        rating = "18"
    prot = st.get("protagonist") or {}
    raw_id = str(st.get("id") or "")
    if not _ID_OK.fullmatch(raw_id):
        raw_id = uuid.uuid4().hex[:10]
    return {
        "id": raw_id,
        "mode": "vn" if str(st.get("mode", "tsf")).strip() == "vn" else "tsf",
        "name": str(st.get("name", "")).strip()[:30] or "未命名档案",
        "title": str(st.get("title", "")).strip()[:30] or "未命名剧本",
        "world": str(st.get("world", "")).strip()[:2000],
        "protagonist": {
            "name": str(prot.get("name", "")).strip()[:12] or "主角",
            "anchor": str(prot.get("anchor", "")).strip()[:200],
        },
        "characters": chars[:4],
        "outline": str(st.get("outline", "")).strip()[:2000],
        "lorebook": [
            {"keys": str(e.get("keys", ""))[:100],
             "content": str(e.get("content", ""))[:500],
             "always": bool(e.get("always"))}
            for e in (st.get("lorebook") or [])
            if str(e.get("content", "")).strip()
        ][:8],
        "directives": (st.get("directives") or [])[:12],
        "catchphrases": catch,
        "content_rating": rating,
        "r18_enabled": bool(st.get("r18_enabled", True)) and rating == "18",
        "allow_forced": bool(st.get("allow_forced", True)) and rating == "18"
        and bool(st.get("r18_enabled", True)),
        "stat_config": _clean_stat_config(st.get("stat_config")),
        "uniform_en": str(st.get("uniform_en", "") or "").strip()[:200],
        "uniform_neg": str(st.get("uniform_neg", "") or "").strip()[:200],
        # 1.7.33 主角「最终变化样式」（开局面板：身材/胸/瞳/发/气质/衣物）
        # ——随设定档案保存/读取，开局后由 game 规范化
        "tsf_target": st.get("tsf_target") if isinstance(st.get("tsf_target"), dict) else {},
        "updated_at": time.time(),
    }


def _clean_stat_config(raw) -> list[dict]:
    """状态栏自定义配置透传（轻校验：key 由开局端规范化，此处只防脏数据）。"""
    out = []
    for e in (raw if isinstance(raw, list) else [])[:16]:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()[:10]
        if not name:
            continue
        try:
            start = max(0, min(100, int(round(float(e.get("start", 0))))))
        except (TypeError, ValueError):
            start = 0
        out.append({"key": str(e.get("key", "")).strip()[:32],
                    "name": name, "start": start})
    return out


def list_all() -> list[dict]:
    out = []
    for p in _dir().glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return [_clean(s) for s in out]


def get(sid: str) -> dict | None:
    p = _path(sid)
    if not p.is_file():
        return None
    try:
        return _clean(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return None


def save(st: dict) -> dict:
    st = _clean(st)
    _path(st["id"]).write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return st


def delete(sid: str) -> bool:
    """删除档案：软删除——先移入 trash 备份目录（data/storylines_trash/），
    防止任何误删/数据丢失，需要时可直接从备份恢复。"""
    p = _path(sid)
    if not p.is_file():
        return False
    trash = cfgmod.DATA_DIR / "storylines_trash"
    trash.mkdir(parents=True, exist_ok=True)
    try:
        (trash / f"{sid}.json").write_text(p.read_text(encoding="utf-8"),
                                           encoding="utf-8")
        p.unlink()
        return True
    except OSError:
        return False

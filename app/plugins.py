"""插件存储：data/plugins.json 的 CRUD（插件 = 一组预设指令，可一键注入游戏）。"""
import json
import uuid
from pathlib import Path

from . import config as cfgmod


def _path() -> Path:
    p = cfgmod.DATA_DIR / "plugins.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


DEFAULT_PLUGINS = [
    {"id": "builtin_drama", "name": "癫狂叙事（戏剧化）", "kind": "story",
     "description": "夸张戏剧化的叙事风格（不含露骨内容，内置）",
     "directives": [{"text": "叙事风格戏剧化夸张，多用巨响、光影、内心风暴般的比喻，"
                              "情绪张力拉满"},
                    {"text": "环境描写充满压迫感与悬念，角色反应强烈"}]},
    {"id": "builtin_cinema", "name": "电影风格", "kind": "image_style",
     "description": "固定立绘风格为电影质感（内置）",
     "directives": [{"text": "cinematic film still, cinematic lighting, "
                              "dramatic light and shadow, film grain"}]},
    {"id": "builtin_solo", "name": "严格单人", "kind": "image_negative",
     "description": "强力压制多角色/分镜错误（内置）",
     "directives": [{"text": "multiple people, multiple characters, 2girls, "
                              "2boys, twins, couple, group, crowd, comic panels, "
                              "split image"}]},
    {"id": "builtin_edge", "name": "边缘干净", "kind": "image_negative",
     "description": "减少抠图困难：禁渐变背景/白边/模糊边缘（内置）",
     "directives": [{"text": "gradient background, complex background, blurry, "
                              "white outline, glowing edges, watermark, text"}]},
    {"id": "builtin_tf_progress", "name": "转变渐进叙事（内置）", "kind": "story",
     "description": "性别转变过程的剧情节奏（内置，牢牢伴随游戏）："
                    "变化随同化率循序渐进、细节日常化、无突兀跳跃",
     "directives": [
         {"text": "主角身体与气质的转变必须与主角的转变同化率数值严格同步，"
                  "逐步发生、无突兀跳跃"},
         {"text": "转变细节从日常生活出发（发丝光泽、嗓音、体感、衣着的贴合度），"
                  "描写具体渐进、无突兀跳跃"},
     ]},
]


def _merge_builtin(items: list[dict]) -> list[dict]:
    """内置插件防删回补：数据文件缺失/被删的内置项自动补回（牢牢放入）。"""
    have = {str(p.get("id", "")) for p in items}
    for pl in DEFAULT_PLUGINS:
        if pl["id"] not in have:
            items.append(pl)
    return items


def _load_raw() -> list[dict]:
    p = _path()
    if not p.is_file():
        items = json.loads(json.dumps(DEFAULT_PLUGINS))
        _save_raw(items)
        return items
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        items = []
    merged = _merge_builtin(items)
    if len(merged) != len(items):
        _save_raw(merged)
    return merged


def _save_raw(items: list[dict]) -> None:
    _path().write_text(json.dumps(items, ensure_ascii=False, indent=2),
                       encoding="utf-8")


import re as _re

_ID_OK = _re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _clean(pl: dict) -> dict:
    directives = []
    for d in pl.get("directives") or []:
        text = str(d.get("text", "")).strip()[:200]
        if text:
            directives.append({"text": text})
    raw_id = str(pl.get("id") or "")
    if not _ID_OK.fullmatch(raw_id):
        raw_id = uuid.uuid4().hex[:10]
    kind = str(pl.get("kind", "story")).strip()
    if kind not in ("story", "image_style", "image_negative"):
        kind = "story"
    return {
        "id": raw_id,
        "name": str(pl.get("name", "")).strip()[:20] or "未命名插件",
        "description": str(pl.get("description", "")).strip()[:120],
        "kind": kind,
        "directives": directives[:8],
    }


def list_all() -> list[dict]:
    return [_clean(p) for p in _load_raw()]


def save(pl: dict) -> dict:
    pl = _clean(pl)
    items = _load_raw()
    items = [x for x in items if x.get("id") != pl["id"]]
    items.append(pl)
    _save_raw(items)
    return pl


def delete(pid: str) -> bool:
    items = _load_raw()
    keep = [x for x in items if x.get("id") != pid]
    if len(keep) == len(items):
        return False
    _save_raw(keep)
    return True


def merged_presets() -> list[dict]:
    """预置指令 + 全部插件指令（供 /api/directives/presets 合并返回）。"""
    from .game import PRESET_DIRECTIVES
    presets = [dict(p) for p in PRESET_DIRECTIVES]
    for pl in list_all():
        if pl.get("kind", "story") != "story":
            continue
        for i, d in enumerate(pl.get("directives", [])):
            presets.append({
                "id": f"plugin_{pl['id']}_{i}",
                "group": f"插件·{pl['name']}",
                "text": d.get("text", ""),
            })
    return presets


def image_style_extras() -> list[str]:
    """image_style 类插件的全部指令（追加到立绘风格后缀）。"""
    out = []
    for pl in list_all():
        if pl.get("kind") == "image_style":
            out += [d["text"] for d in pl.get("directives", [])]
    return out


def image_negative_extras() -> list[str]:
    """image_negative 类插件的全部指令（追加到立绘负面词，减少错误生成）。"""
    out = []
    for pl in list_all():
        if pl.get("kind") == "image_negative":
            out += [d["text"] for d in pl.get("directives", [])]
    return out

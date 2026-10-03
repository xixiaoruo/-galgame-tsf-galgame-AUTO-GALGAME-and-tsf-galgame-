"""内容层数据：谜团 / 地点 / 物品 / 敌人 / 城镇状态 / 古神 / 结局 / 演员表。

全部为**结构化骨架**，最小可用即可（MECHANICS.md §10.2 #10/#16 的转化方案：
"JSON 卡池 + 按 location/requirements/weight 抽取；代码负责抽取与结算"）。

计数依据（COC-DESIGN §6.1「MVP 数据量」表）：
  - 谜团 2（上游 20 或 21，§12 #2 未查实）
  - 古神 2（上游 9，§1.4）
  - 城镇状态 6（上游 15，§1.4；MVP 不含会改写谜团内容的 rewrites 钩子）
  - 诅咒 6（覆盖 Mental/Mutation/Curse/Status 四型，§2.3）
  - 伤势 6（Minor/Major 各 3，上游约 23 种，§2.3）
  - 难度 3 开放（上游 5 档，§2.4）
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import paths


class ContentError(ValueError):
    """内容层缺文件或结构非法。"""


def _load(path: str, what: str) -> Any:
    if not os.path.isfile(path):
        raise ContentError(f"缺少内容文件 {path}（{what}）")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _unwrap(data: Any, key: str) -> list:
    """内容文件统一形态：`{key: [...]}` 或裸 list。"""
    items = data.get(key, data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ContentError(f"{key} 结构非法：期望 list，实际 {type(items).__name__}")
    return list(items)


def locations() -> list[dict]:
    return _unwrap(_load(paths.LOCATIONS_PATH, "地点表"), "locations")


def mysteries() -> list[dict]:
    return _unwrap(_load(paths.MYSTERIES_PATH, "谜团表"), "mysteries")


def cards() -> list[dict]:
    """卡池：`data/coc/cards/*.json`，每个文件形如 `{"cards": [ ... ]}`。"""
    out: list[dict] = []
    if not os.path.isdir(paths.CARDS_DIR):
        raise ContentError(f"缺少卡池目录 {paths.CARDS_DIR}")
    for name in sorted(os.listdir(paths.CARDS_DIR)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(paths.CARDS_DIR, name), "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        out.extend(_unwrap(blob, "cards"))
    return out


def enemies() -> dict:
    data = _load(paths.ENEMIES_PATH, "敌人表")
    return data.get("enemies", data)


def old_gods() -> list[dict]:
    data = _load(paths.OLD_GODS_PATH, "古神表")
    return _unwrap(data, "old_gods")


def endings() -> list[dict]:
    data = _load(paths.ENDINGS_PATH, "结局表")
    return _unwrap(data, "endings")


def items() -> list[dict]:
    data = _load(paths.ITEMS_PATH, "物品表")
    return _unwrap(data, "items")


def town_dict() -> dict:
    """城镇状态 / 诅咒 / 伤势 / 治疗副作用（同一个文件的多段）。"""
    data = _load(paths.TOWN_STATUS_PATH, "城镇状态表")
    if not isinstance(data, dict):
        raise ContentError("town_status.json 结构非法：期望 dict")
    return data


def town_status() -> list[dict]:
    return _unwrap(town_dict(), "town_status")


def curses() -> list[dict]:
    return _unwrap(town_dict(), "curses")


def injuries() -> list[dict]:
    return _unwrap(town_dict(), "injuries")


def treatments() -> list[dict]:
    return _unwrap(town_dict(), "treatments")


def cast() -> dict:
    """成年角色白名单（COC-DESIGN §5.3 G1：不在此表内即拒）。"""
    data = _load(paths.CAST_PATH, "演员表")
    return {str(c["name"]): c for c in _unwrap(data, "cast")}

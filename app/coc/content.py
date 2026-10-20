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


# ------------------------------------------------------------- 战斗动作层（t62）

#: 战斗动作表里必须带溯源元数据的两种节点
_CA_REQUIRED = ("_source", "_unverified")


def _require_meta(node: Any, at: str) -> None:
    """校验一个节点带 `_source`（非空 str）与 `_unverified`（bool）。

    与 `provenance.validate_provenance` 是**同一条铁律的两种形状**：那里走 tuning.json 的
    嵌套 dict，这里走 `combat_actions.json` 的 dict/list 混排。为什么不复用：
    `validate_provenance` 见到 list 会直接判「结构非法」，而动作表天然是 list。
    """
    if not isinstance(node, dict):
        raise ContentError(f"combat_actions.json: {at} 期望 dict，实际 {type(node).__name__}")
    for key in _CA_REQUIRED:
        if key not in node:
            raise ContentError(
                f"combat_actions.json: {at} 缺 {key!r}；"
                f"COC-DESIGN §0.2 要求所有数值带 _source 与 _unverified"
            )
    src = node["_source"]
    if not isinstance(src, str) or not src.strip():
        raise ContentError(f"combat_actions.json: {at}._source 必须是非空字符串（现在 {src!r}）")
    unv = node["_unverified"]
    if not isinstance(unv, bool):
        raise ContentError(
            f"combat_actions.json: {at}._unverified 必须是 bool（现在 {unv!r}）；"
            f"字符串 'true' / 0 / 1 都不算已标注"
        )


def combat_actions_doc() -> dict:
    """读**并逐节点校验** `data/coc/combat_actions.json`（缺来源即加载失败）。"""
    data = _load(paths.COMBAT_ACTIONS_PATH, "战斗动作表")
    if not isinstance(data, dict):
        raise ContentError("combat_actions.json 结构非法：期望 dict")
    _require_meta(data, "$")
    for group in ("hit", "time", "enemy_counter", "exorcism"):
        node = data.get(group)
        if not isinstance(node, dict):
            raise ContentError(f"combat_actions.json: 缺少分组 {group!r}")
        for k, v in node.items():
            if str(k).startswith("_"):
                continue
            _require_meta(v, f"{group}.{k}")
    weapons = data.get("weapons")
    if not isinstance(weapons, dict):
        raise ContentError("combat_actions.json: 缺少 weapons 分组")
    for wid, w in weapons.items():
        if str(wid).startswith("_"):
            continue
        _require_meta(w, f"weapons.{wid}")
    acts = data.get("actions")
    if not isinstance(acts, list) or not acts:
        raise ContentError("combat_actions.json: actions 必须是非空 list")
    seen: set[str] = set()
    for i, a in enumerate(acts):
        _require_meta(a, f"actions[{i}]")
        aid = a.get("id")
        if not isinstance(aid, str) or not aid:
            raise ContentError(f"combat_actions.json: actions[{i}].id 必须是非空字符串")
        if aid in seen:
            raise ContentError(f"combat_actions.json: 动作 id 重复 {aid!r}")
        seen.add(aid)
    return data


def combat_actions() -> list[dict]:
    """战斗动作表（list，顺序即数据表顺序；运行期按 id 建索引）。"""
    return list(combat_actions_doc()["actions"])


def combat_field(dotted: str) -> Any:
    """取动作表里的一个非动作字段（点号路径），如 `combat_field("exorcism.steps")`。

    与 `provenance.t` 同形（自动脱 `value` 外壳），但作用于 `combat_actions.json`。
    """
    node: Any = combat_actions_doc()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"combat_actions.json 里没有路径 {dotted!r}（在 {part!r} 处断了）")
        node = node[part]
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


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

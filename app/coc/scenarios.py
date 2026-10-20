"""CoC 剧本预设：把 `data/coc/scenarios.json` 变成可开局选择的短篇模块。

设计边界（与 `scenario.py` 完全一致）：
  · 预设只替换**内容层**（地点 / 谜团 / 古神 / 卡池 / 叙述），不改变 DOOM/STA/REA/SAN
    与 d12 检定的任何数值；
  · `default` 预设返回 `None`，即完全使用随包原版内容（零影响）；
  · 玩家仍可在开局界面粘贴自己的 JSON 剧本；自定义字段会覆盖预设的同名字段，
    没覆盖的字段继续用预设 —— 这样「选一个模板再改两行」也可行。

本模块不导入 LLM、不写状态、不掷骰；只做读取、校验与合并。
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import paths
from . import scenario as _scn


class ScenarioPresetError(_scn.ScenarioError):
    """预设文件缺失或结构非法（继承 ScenarioError，HTTP 层按玩家输入错误回 400）。"""


_CACHE: list[dict] | None = None


def _load_raw() -> list[dict]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not os.path.isfile(paths.SCENARIOS_PATH):
        _CACHE = []
        return _CACHE
    try:
        with open(paths.SCENARIOS_PATH, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioPresetError(f"剧本预设读取失败：{exc}") from exc
    rows = blob.get("scenarios", blob) if isinstance(blob, dict) else blob
    if not isinstance(rows, list):
        raise ScenarioPresetError("scenarios.json 结构非法：期望 scenarios 数组")
    out: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("id"):
            raise ScenarioPresetError(f"scenarios[{i}] 缺 id")
        out.append(dict(row))
    _CACHE = out
    return out


def summaries() -> list[dict]:
    """开局下拉用的轻量清单（不含 cards，避免前端传输整包卡池）。"""
    out = []
    for row in _load_raw():
        sc = row.get("scenario") or {}
        out.append({
            "id": str(row["id"]),
            "title": str(row.get("title") or sc.get("title") or row["id"]),
            "summary": str(row.get("summary") or ""),
            "era": str(row.get("era") or ""),
            "tone": str(row.get("tone") or ""),
            "players": str(row.get("players") or ""),
            "locations": len(sc.get("locations") or []),
            "cards": len(sc.get("cards") or []),
            "mystery_len": int(sc.get("mystery_len") or 0),
            "mystery_count": int(sc.get("mystery_count") or 0),
            "is_default": row["id"] == "default",
        })
    return out


def get(preset_id: str) -> dict | None:
    """取一个预设的原始条目（含 title/summary，不是 normalize 后的 scenario）。"""
    pid = str(preset_id or "").strip()
    if not pid:
        return None
    for row in _load_raw():
        if str(row["id"]) == pid:
            return row
    raise ScenarioPresetError(f"未知剧本预设 {pid!r}；可选 "
                              f"{[x['id'] for x in _load_raw()]}")


def resolve(preset_id: str = "", custom: Any = None) -> dict | None:
    """把「预设 + 玩家自定义 JSON」合并成一个 normalize 后的剧本对象。

    · 只选 default / 不选 ⇒ `None`（随包内容，零影响）；
    · 选预设 + 无自定义 ⇒ 预设本身；
    · 选预设 + 自定义 ⇒ 自定义字段覆盖预设字段（深一层合并，数组整体替换）；
    · 未知预设 id ⇒ 抛 `ScenarioPresetError`（绝不静默回落，否则玩家以为剧本生效了）。
    """
    row = get(preset_id) if str(preset_id or "").strip() else None
    base = dict((row or {}).get("scenario") or {})
    pid = str((row or {}).get("id") or "")
    custom_n = _scn.normalize(custom) if custom else None
    if custom_n:
        merged = dict(base)
        merged.update(custom_n)
        return _scn.normalize(merged)
    if not base:
        return None if (not pid or pid == "default") else _scn.normalize(base) or None
    return _scn.normalize(base)


def prompt_hint(preset_id: str) -> str:
    """给开局界面的一句提示（不暴露 Keeper-only 信息）。"""
    row = get(preset_id)
    if not row:
        return ""
    return " · ".join(x for x in (row.get("era"), row.get("tone")) if x)

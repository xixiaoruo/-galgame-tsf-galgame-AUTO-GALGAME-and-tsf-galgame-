"""CoC 模式的数值清洗与**唯一写入点**。

对应设计：`COC-DESIGN.md` §3.2 E2/E3/E4 与 §8.4 的白名单表。

三件事，按强制顺序排列：

1. E2 **键白名单**（`sanitize_llm_delta`）：LLM 提议的 delta 只认
   `{exp, funds, item_grant, item_take, stat}`。
   **`doom` / `sta` / `rea` / `tags_grant` / `tags_remove` / `flag_set` / `ending`
   根本不在白名单里** —— LLM 连提议权都没有（不是"提议了再校验"，是"没有这个入口"）。
   不在白名单的键**静默丢弃**并记 `dropped`，不抛错、不进状态。
2. E3 **两级钳制**（`clamp_turn_delta`）：level-1（`doom`/`sta`/`rea`）单轮 ±3；
   level-2（`exp`/`funds`/`stat`）单轮 ±5。上限来自 tuning.json。
3. E4 **跨字段不变式**（`invariants`）：属性 ≤0 → `doom` 立即 100
   （`MECHANICS.md` §2.1，"廉价但极有效的隐性绝境"）；`doom` 钳在 0~100；
   `sta`/`rea` 钳在 `death_mode` 的地板之上；不超各自上限。

`apply_delta` 是本包**唯一**写权威状态数字的函数（`test_state_write_point.py` 用
静态扫描证明这点：除本文件外无人赋值 `state["doom"]` 等）。

本模块**不导入** LLM、prompt、网络相关任何东西。
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import paths
from .provenance import t

#: LLM 提议 delta 的键白名单（= tuning.json 的 `llm_delta_whitelist`）
#: 这里只是"常量便于静态检查"，**取值仍以 tuning.json 为准**（加载时断言一致）。
LLM_DELTA_WHITELIST: tuple[str, ...] = ("exp", "funds", "item_grant", "item_take", "stat")

#: **明确不在**白名单里的键 —— 这些由确定性层独占（用于测试与审计输出）
DETERMINISTIC_ONLY_KEYS: tuple[str, ...] = (
    "doom", "sta", "rea", "sta_max", "rea_max",
    "tags_grant", "tags_remove", "flag_set", "ending",
)

#: 权威数值字段（`apply_delta` 是唯一写入者）
STATE_NUMERIC_FIELDS: tuple[str, ...] = ("doom", "sta", "rea", "exp", "funds")


class SchemaViolation(RuntimeError):
    """白名单常量与 tuning.json 不一致（说明有人改了其中一处）。"""


def assert_whitelist_matches_tuning(cfg: dict) -> None:
    """白名单是"没有入口"，所以它必须与 tuning.json 严格一致。"""
    want = list(t(cfg, "llm_delta_whitelist"))
    if sorted(want) != sorted(LLM_DELTA_WHITELIST):
        raise SchemaViolation(
            f"tuning.llm_delta_whitelist={want} 与本模块 LLM_DELTA_WHITELIST="
            f"{list(LLM_DELTA_WHITELIST)} 不一致；两处必须同时改"
        )
    for bad in DETERMINISTIC_ONLY_KEYS:
        if bad in want:
            raise SchemaViolation(
                f"{bad!r} 出现在 LLM 白名单里！COC-DESIGN §8.4 明确要求 LLM "
                f"对这些字段连提议权都没有"
            )


def _as_int(v: Any) -> int | None:
    """宽松地把 LLM 给的值转成 int；转不了返回 None（调用方丢弃）。"""
    if isinstance(v, bool):
        return None              # bool 是 int 的子类，但语义上不该出现在这里
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def valid_item_ids() -> set[str]:
    """`items.json` 里登记的物品 id 集合（不存在则返回空集 = 一切物品都被丢弃）。"""
    if not os.path.isfile(paths.ITEMS_PATH):
        return set()
    with open(paths.ITEMS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    items = data.get("items", data) if isinstance(data, dict) else data
    out: set[str] = set()
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("id"):
                out.add(str(it["id"]))
            elif isinstance(it, str):
                out.add(it)
    elif isinstance(items, dict):
        out.update(str(k) for k in items)
    return out


def sanitize_llm_delta(delta: Any, cfg: dict) -> tuple[dict, list[str]]:
    """E2 + E3（LLM 提案部分）：返回 `(清洗后的 delta, 被丢弃的键列表)`。

    - 非 dict → 空提案；
    - 键不在白名单 → 丢弃并记入 `dropped`；
    - 值不可转 int → 丢弃并记入 `dropped`；
    - `item_grant`/`item_take` → 必须是**已登记**的物品 id，超出数量上限截断；
    - `stat` → 必须是合法属性名（STR/DEX/PER/KNW/CHA）且值可转 int。
    """
    dropped: list[str] = []
    out: dict[str, Any] = {}
    if not isinstance(delta, dict):
        return out, ["<非 dict 的整体提案>"]

    items_ok = valid_item_ids()
    stat_names = {"STR", "DEX", "PER", "KNW", "CHA"}
    max_item_ops = int(t(cfg, "sanitize.max_item_ops_per_turn"))

    for key, raw in delta.items():
        if key not in LLM_DELTA_WHITELIST:
            dropped.append(str(key))
            continue
        if key in ("exp", "funds"):
            v = _as_int(raw)
            if v is None:
                dropped.append(key)
                continue
            out[key] = v
            continue
        if key in ("item_grant", "item_take"):
            if not isinstance(raw, (list, tuple)):
                dropped.append(key)
                continue
            kept = [str(x) for x in raw if str(x) in items_ok][:max_item_ops]
            if len(kept) != len(list(raw)):
                dropped.append(f"{key}[未登记或超量]")
            if kept:
                out[key] = kept
            continue
        if key == "stat":
            if not isinstance(raw, dict):
                dropped.append(key)
                continue
            sub: dict[str, int] = {}
            for sname, sval in raw.items():
                if str(sname) not in stat_names:
                    dropped.append(f"stat.{sname}")
                    continue
                v = _as_int(sval)
                if v is None:
                    dropped.append(f"stat.{sname}")
                    continue
                sub[str(sname)] = v
            if sub:
                out["stat"] = sub
            continue
        dropped.append(str(key))

    return out, dropped


def _budgets(cfg: dict) -> tuple[int, int]:
    return int(t(cfg, "sanitize.turn_budget_level1")), int(t(cfg, "sanitize.turn_budget_level2"))


def clamp_turn_delta(delta: Any, cfg: dict) -> dict:
    """E3：按 level-1 / level-2 预算钳制**一个回合**的总变化量。

    对合并后的 delta（确定性结算 + LLM 提案）再钳一次 —— 这样即使
    事件卡 `outcome` 写了 `doom: 10`，单轮也只会落地 ±level-1 预算。
    """
    if not isinstance(delta, dict):
        return {}
    b1, b2 = _budgets(cfg)
    out: dict[str, Any] = {}
    for key, raw in delta.items():
        if key in ("doom", "sta", "rea"):
            v = _as_int(raw)
            if v is None:
                continue
            out[key] = max(-b1, min(b1, v))
        elif key in ("exp", "funds"):
            v = _as_int(raw)
            if v is None:
                continue
            out[key] = max(-b2, min(b2, v))
        elif key == "stat":
            if not isinstance(raw, dict):
                continue
            sub: dict[str, int] = {}
            for sname, sval in raw.items():
                v = _as_int(sval)
                if v is None:
                    continue
                sub[str(sname)] = max(-b2, min(b2, v))
            if sub:
                out["stat"] = sub
        else:
            # 非数值键（item_*、tags_*、flag_set、ending…）原样透传：
            # 它们不是"数值变更"，由 apply_delta 各自处理。
            out[key] = raw
    return out


def invariants(state: dict, cfg: dict) -> list[str]:
    """E4：应用数值后强制成立的不变式。返回被触发的修正说明（供日志/测试）。"""
    notes: list[str] = []
    stats = state.get("stats") or {}

    # ① 任一技能属性 ≤0 → DOOM 立即 100（MECHANICS.md §2.1）
    if any(int(v) <= 0 for v in stats.values()):
        if int(state.get("doom", 0)) != 100:
            state["doom"] = 100
            notes.append("属性≤0 → DOOM=100（MECHANICS.md §2.1 隐性绝境）")

    # ② doom 是 0~100 的仪表（§2.1："到 100% 时数字改为显示 END"）
    d = int(state.get("doom", 0))
    if d > 100:
        state["doom"] = 100
        notes.append("doom 上限钳到 100")
    elif d < 0:
        state["doom"] = 0
        notes.append("doom 下限钳到 0")

    # ③ 属性 1~12（§8.4）
    smin = int(t(cfg, "sanitize.stat_value_min"))
    smax = int(t(cfg, "sanitize.stat_value_max"))
    for k, v in list(stats.items()):
        nv = max(smin, min(smax, int(v)))
        if nv != int(v):
            stats[k] = nv
            notes.append(f"stats.{k} 钳到 {nv}")

    # ④ sta/rea 地板随后四种死亡模式；上限不超过 sta_max/rea_max
    floor = int(t(cfg, "sanitize.last_stand_floor")) if state.get("death_mode") == "last_stand" \
        else int(t(cfg, "sanitize.sudden_death_floor"))
    f = floor if state.get("death_mode") == "last_stand" else 0
    for key, max_key in (("sta", "sta_max"), ("rea", "rea_max")):
        v = int(state.get(key, 0))
        cap = int(state.get(max_key, v))
        if v > cap:
            state[key] = cap
            notes.append(f"{key} 上限钳到 {max_key}={cap}")
        elif v < f:
            state[key] = f
            notes.append(f"{key} 地板钳到 {f}（death_mode={state.get('death_mode')}）")

    # ⑤ exp / funds 非负且有合理上限（§2.2 EXP 兼作货币，可为 0）
    if int(state.get("exp", 0)) < 0:
        state["exp"] = 0
        notes.append("exp 下限钳到 0")
    if int(state.get("funds", 0)) < 0:
        state["funds"] = 0
        notes.append("funds 下限钳到 0")

    return notes


def apply_delta(state: dict, delta: Any, cfg: dict, *,
                clamp: bool = True) -> dict:
    """**唯一写权威状态数值的函数**。返回 `{applied, dropped, notes}`。

    `delta` 可含：数值键（doom/sta/rea/exp/funds/stat）+ 非数值键
    （item_grant/item_take/tags_grant/tags_remove/flag_set/ending）。
    后者**只接受确定性层的输入**——LLM 提案在 `sanitize_llm_delta` 里已经把
    这些键丢干净了。
    """
    if not isinstance(delta, dict):
        return {"applied": {}, "dropped": ["<非 dict>"], "notes": []}

    d = clamp_turn_delta(delta, cfg) if clamp else dict(delta)
    applied: dict[str, Any] = {}
    dropped: list[str] = []

    for key, raw in d.items():
        if key in STATE_NUMERIC_FIELDS:
            v = _as_int(raw)
            if v is None:
                dropped.append(key)
                continue
            state[key] = int(state.get(key, 0)) + v
            applied[key] = v
        elif key == "stat":
            subs = raw if isinstance(raw, dict) else {}
            changed: dict[str, int] = {}
            stats = state.setdefault("stats", {})
            for sname, sval in subs.items():
                v = _as_int(sval)
                if v is None:
                    continue
                stats[sname] = int(stats.get(sname, 0)) + v
                changed[sname] = v
            if changed:
                applied["stat"] = changed
        elif key in ("item_grant", "item_take"):
            inv = state.setdefault("inventory", {"backpack": [], "storage": []})
            bag = inv.setdefault("backpack", [])
            lst = raw if isinstance(raw, (list, tuple)) else [raw]
            for iid in lst:
                iid = str(iid)
                if key == "item_grant":
                    if iid not in bag:
                        bag.append(iid)
                    applied.setdefault("item_grant", []).append(iid)
                else:
                    if iid in bag:
                        bag.remove(iid)
                        applied.setdefault("item_take", []).append(iid)
                    else:
                        dropped.append(f"item_take:{iid}(不在背包)")
        elif key == "tags_grant":
            tags = state.setdefault("tags", [])
            for tg in (raw if isinstance(raw, (list, tuple)) else [raw]):
                if isinstance(tg, dict) and tg.get("id"):
                    tags.append(dict(tg))
                    applied.setdefault("tags_grant", []).append(tg["id"])
                else:
                    dropped.append(f"tags_grant:{tg!r}")
        elif key == "tags_remove":
            tags = state.setdefault("tags", [])
            for tid in (raw if isinstance(raw, (list, tuple)) else [raw]):
                tid = str(tid)
                before = len(tags)
                # 城镇状态不可移除（MECHANICS.md §1.4）；诅咒全局仅 3 种手段（§2.3）
                state["tags"] = [
                    tg for tg in tags
                    if not (tg.get("id") == tid and tg.get("kind") != "town")
                ]
                if len(state["tags"]) != before:
                    applied.setdefault("tags_remove", []).append(tid)
                else:
                    dropped.append(f"tags_remove:{tid}(不可移除或不存在)")
        elif key == "flag_set":
            flags = state.setdefault("flags", {})
            for fk, fv in (raw if isinstance(raw, dict) else {}).items():
                flags[str(fk)] = fv
                applied.setdefault("flag_set", {})[str(fk)] = fv
        elif key == "ending":
            if raw:
                state["ending"] = str(raw)
                applied["ending"] = str(raw)
        else:
            dropped.append(str(key))

    notes = invariants(state, cfg)
    return {"applied": applied, "dropped": dropped, "notes": notes}

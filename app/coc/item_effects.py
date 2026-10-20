"""TSF×CoC 融合模式：**转变进度对物品 / 装备 / 背包的机制后果**。

## 这个文件为什么存在

融合模式此前只有**属性层**的机制后果：`coc/tsf.py` 的 `_MECH_TABLE` 给出五档
`shift`，由 `dice.check(tsf_shift=…)` 用在**属性检定**上。而**战斗命中不走那条路**
（`combat.action_stat` 直接读 `state["stats"]` 的原始值），所以"身体重构了却照样抡得动消防斧"；
衣物、装备槽、背包物品的效果也**完全没有**转变相关的机制。

用户要求（2026-10-05）：
  ① 武器/装备要吃 TSF 修正  ② 服装随阶段演化  ③ 背包与所得物品的效果真正生效。

本模块是这三件事的**唯一实现点**，全部数值来自 `tuning.json` 的 `coc_tsf_items` 段
（代码里没有任何兜底常量 —— 与 `dice.py` / `state.py` 同一纪律）。

## 只读纪律

本模块**不写状态**、**不掷骰**、**不调 LLM**。它只回答三类问题：
「这个属性现在是多少」「这件装备现在能不能穿/用」「背包里现在哪些可用、不可用的原因是什么」。
真正的写入仍然只发生在 `state.sanitize.apply_delta`（唯一写入点）。
"""
from __future__ import annotations

from typing import Any

from . import tsf as _tsf
from .provenance import t


def _seg(cfg: dict | None = None) -> dict:
    """`coc_tsf_items` 段的**裸值**（已拆掉 `{"value":…, "_source":…}` 包装）。

    `tuning.json` 的强制格式是「每个数值都带 `_source`/`_unverified`」，所以
    `provenance.t(cfg, "coc_tsf_items")` 返回的是**包装体**（本段每个键各自一个包装）。
    本函数负责统一拆包，让下面的读取代码看到的是纯数据。
    `cfg=None` ⇒ 自己加载（避免在每个调用点都传 cfg，也避免 `state.py` ↔ 本模块 的环形导入）。
    """
    if cfg is None:
        from .provenance import load_tuning
        cfg = load_tuning()
    raw = t(cfg, "coc_tsf_items") or {}
    out: dict = {}
    for k, v in raw.items():
        if str(k).startswith("_"):
            continue                      # 跳过 _note 之类的说明键
        if isinstance(v, dict) and "value" in v:
            out[k] = v["value"]
        else:
            out[k] = v
    return out


def tier_of(prog: int) -> int:
    """转变进度 → 机制档位（0..5）。与 `tsf.tier_of` 同口径，便于两处一致。"""
    return _tsf.tier_of(int(prog))


def progress_of(state: dict) -> int:
    return _tsf.tsf_progress_of(state)


# ---------------------------------------------------------------- ① 属性修正


def stat_with_tsf(state: dict, stat_name: str) -> int:
    """属性**经 TSF 修正后**的当前值（战斗与检定都用它取属性，别读原始 `state["stats"]`）。

    纯函数：`原始值 + shift_for(档位)`，下限 1（属性域 1~12 的左端，见 COC-DESIGN §8.4）。
    未接入/无该属性 ⇒ 返回原始值（**默认零影响**）。
    """
    raw = int((state.get("stats") or {}).get(str(stat_name), 0) or 0)
    prog = progress_of(state)
    shift = int(_tsf.shift_for(str(stat_name), prog))
    return max(1, raw + shift) if raw > 0 else raw


def tsf_shift_for(state: dict, stat_name: str) -> int:
    """本次该属性因转变得到的修正（正/负；0 = 无影响）。"""
    return int(_tsf.shift_for(str(stat_name), progress_of(state)))


# ---------------------------------------------------------------- ② 装备合法性


def _wear_ok(slot: str, tier: int, cfg: dict) -> bool:
    table = _seg(cfg).get("wear_table") or {}
    row = table.get(str(slot))
    if not isinstance(row, list) or not row:
        return True                     # 未登记该槽 ⇒ 不设限（保守）
    i = max(0, min(int(tier), len(row) - 1))
    return bool(row[i])


def _hand_limit(item_id: str, cfg: dict | None, *, items: dict | None = None) -> int | None:
    """双手类物品的档位上限；无限制 ⇒ `None`。"""
    tbl = _seg(cfg).get("item_hand_tier_max") or {}
    meta = (items or {}).get(item_id) if isinstance(items, dict) else None
    if meta is None:
        from .state import item_table
        meta = item_table().get(str(item_id)) or {}
    hand = str((meta or {}).get("hand") or "")
    v = tbl.get(hand)
    return None if v is None else int(v)


def wearable(state: dict, item_id: str, *, cfg: dict | None = None,
             items: dict | None = None) -> tuple[bool, str]:
    """这件物品**能不能装备**。返回 `(可装备, 不可装备原因码)`。

    原因码（供 UI 显示中文，`reason` 与既有 `equip_item` 的码风格一致）：
      · `""`                  → 可以
      · `tsf_slot_unfit`      → 转变到该档位后，此槽位的旧装备不再合身
      · `tsf_hand_unfit`      → 双手类物品在此档位无法使用（握持半径/体格变了）
    """
    tier = tier_of(progress_of(state))
    meta = (items or {}).get(item_id) if isinstance(items, dict) else None
    if meta is None:
        from .state import item_table
        meta = item_table().get(str(item_id)) or {}
    slot = (meta or {}).get("equip_slot")
    if slot and not _wear_ok(str(slot), tier, cfg):
        return False, "tsf_slot_unfit"
    lim = _hand_limit(str(item_id), cfg, items=meta)
    if lim is not None and tier >= lim:
        return False, "tsf_hand_unfit"
    return True, ""


def use_penalty(state: dict, cfg: dict | None = None) -> int:
    """当前档位下**使用/服用类**操作的额外惩罚（0 = 无）。供叙述与后续战斗接入读。"""
    row = _seg(cfg).get("use_penalty") or []
    if not isinstance(row, list) or not row:
        return 0
    i = max(0, min(tier_of(progress_of(state)), len(row) - 1))
    return int(row[i])


# ---------------------------------------------------------------- ③ 背包可用性


def inventory_effects(state: dict, *, cfg: dict | None = None, which: str = "backpack") -> list[dict]:
    """背包（或仓库）每件物品的**在融合模式下的实际可用性**。

    在 `state.inventory_list()` 的元数据之上补三个键：
      · `wearable`   bool   能不能装备（消耗品恒 True —— 它们不占装备槽）
      · `block_reason` str  不可装备的原因码（可装备时为空串）
      · `use_penalty`  int  当前档位下使用它的额外惩罚
    这样前端与 prompt 都能"看见机制"，而不是只看到一件件的名字。
    """
    from .state import inventory_list
    pen = use_penalty(state, cfg)
    out = []
    for meta in inventory_list(state, which):
        m = dict(meta)
        iid = str(m.get("id") or "")
        ok, why = wearable(state, iid, cfg=cfg)
        m["wearable"] = bool(ok)
        m["block_reason"] = str(why)
        m["use_penalty"] = int(pen)
        m["tsf_tier"] = int(tier_of(progress_of(state)))
        out.append(m)
    return out


def wardrobe_state(state: dict, cfg: dict | None = None) -> dict:
    """衣物状态档（叙述用）：`{tier, state, label}`。"""
    rows = _seg(cfg).get("wardrobe_states") or []
    tier = tier_of(progress_of(state))
    if not isinstance(rows, list) or not rows:
        return {"tier": tier, "state": "unknown", "label": ""}
    pick = rows[0]
    for r in rows:
        if int(r.get("tier", 0)) <= tier:
            pick = r
    return {"tier": tier, "state": str(pick.get("state") or ""),
            "label": str(pick.get("label") or "")}


def mech_view(state: dict, cfg: dict | None = None) -> dict:
    """融合模式物品机制的**一次性汇总**（面板 / prompt / 探针都用它，避免多处各算一遍）。"""
    prog = progress_of(state)
    tier = tier_of(prog)
    return {
        "progress": int(prog),
        "tier": int(tier),
        "shifts": {k: tsf_shift_for(state, k)
                   for k in ("STR", "DEX", "PER", "CHA", "KNW")},
        "wardrobe": wardrobe_state(state, cfg),
        "use_penalty": use_penalty(state, cfg),
    }

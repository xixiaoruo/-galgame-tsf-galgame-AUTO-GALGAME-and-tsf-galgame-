"""CoC 模式的权威状态：构造 / 序列化 / 视图 / 提示词状态块。

与 `app/vn.py` 的**范式复用**（COC-DESIGN §2.1 对应表；本包不 import vn.py）：

| vn.py | 本模块 | 差异 |
|---|---|---|
| `initial_states(config)` (vn.py:47) | `initial_states(payload, cfg)` | 本模式初始值是**难度表 × 角色 × 背景 × 古神**的叠加结果，不是一张固定状态表 |
| `sanitize_delta(delta, keys)` (vn.py:66) | `sanitize_delta(delta, cfg)` | 本模式**分两级钳制**（level-1 ±3 / level-2 ±5），且白名单里没有数值字段 |
| `apply_delta(states, delta)` (vn.py:80) | `apply_delta(state, delta, cfg)` | 本模式应用后立即跑 `invariants()`（属性≤0 → DOOM=100 等） |
| `panel_view(states)` (vn.py:87) | `panel_view(state)` | 本模式面板是**多资源**（DOOM + 双血条 + 5 属性 + 标签计数），不是单组 0–100 |
| `states_block(states)` (vn.py:105) | `states_block(state)` | 本模式**只暴露数值与"感觉"，不暴露标签名 / 敌人弱点 / 本次检定属性**（信息遮蔽，§2.10 手法 2） |

状态**独立存放**在 `session["coc"]`，不是 `session["states"]` / `session["char_stats"]` /
`prot["stats"]`（COC-DESIGN §7.1 B3/B4）。

`rng_state` 也保存在状态里：`random.Random` 的 `getstate()` 结果转成可 JSON 化的形式，
读档后经 `restore_rng()` 逐字节还原 —— 这是 V-08 A7"存档读档后状态一致"的对象。

本模块**不导入** app/tsf.py、app/vn.py、app/game.py，也不导入 LLM 客户端。
"""
from __future__ import annotations

import json
import os
import random
import uuid
from typing import Any

from . import paths
from . import sanitize as _san
from .provenance import t

#: 参与检定的五项技能属性（MECHANICS.md §6.2，可信度高）
STAT_KEYS: tuple[str, ...] = ("STR", "DEX", "PER", "KNW", "CHA")

#: 局阶段
PHASES = ("town", "rest", "shop", "combat", "lighthouse", "ending")

#: 存档格式版本（读档迁移用）
SCHEMA_VERSION = 1


class StateError(RuntimeError):
    """状态非法（缺字段 / 阶段非法 / 属性名非法）。"""


# ---------------------------------------------------------------- 初始构造

def initial_states(payload: dict | None = None, cfg: dict | None = None) -> dict:
    """构造一局的初始权威状态（**唯一**的初始值来源：难度表 + 角色 + 背景）。"""
    if cfg is None:
        raise StateError("initial_states 需要已校验的 tuning 数据（数值不得有代码内兜底）")
    p = dict(payload or {})

    difficulty = str(p.get("difficulty") or "Initiate")
    diff_all = t(cfg, "difficulty")
    aliases = diff_all.get("_aliases", {}) if isinstance(diff_all, dict) else {}
    difficulty = aliases.get(difficulty, difficulty)
    if difficulty not in diff_all or difficulty.startswith("_"):
        raise StateError(
            f"未知难度 {difficulty!r}；合法值 "
            f"{[k for k in diff_all if not k.startswith('_')]}（MECHANICS.md §2.4）"
        )
    d = diff_all[difficulty]

    death_mode = str(p.get("death_mode") or "last_stand")
    if death_mode not in ("last_stand", "sudden_death"):
        raise StateError(f"未知死亡模式 {death_mode!r}（MECHANICS.md §1.3 两种）")

    stats = {k: int((p.get("stats") or {}).get(k, 5)) for k in STAT_KEYS}
    for k, v in stats.items():
        if not 1 <= v <= 12:
            raise StateError(f"属性 {k}={v} 超出 1~12（COC-DESIGN §8.4）")

    sta = int(d["sta"])
    rea = int(d["rea"])
    # SAN 初始值由 tuning 决定（KNW 越高，知道得越多，初始 SAN 越低）。
    # 在构造 dict 之前算好，避免"先写 0 再改"这种绕过唯一写点的形态。
    from . import sanity as _sanity
    san0 = _sanity.initial_san(stats, cfg)
    san_cap = _sanity.san_max(cfg)
    state: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "mode": "coc",
        "sid": str(p.get("sid") or uuid.uuid4().hex[:12]),
        "difficulty": difficulty,
        "death_mode": death_mode,
        "old_god": str(p.get("old_god") or "Goizo"),
        "character": str(p.get("character") or "Kirie"),
        "background": str(p.get("background") or "Commoner"),

        "doom": 0,
        "sta": sta, "sta_max": sta,
        "rea": rea, "rea_max": rea,
        "funds": int(d["funds"]),
        "exp": 0,
        "level": 1,
        "stats": stats,
        "luck": int(p.get("luck", 6)),

        #: 1.9.19：CoC 式 SAN（0–99）。与 REA 同时存在，归零走 END_SAN_ZERO。
        "san": san0,
        "san_max": san_cap,
        "san_checks": 0,
        "san_loss_total": 0,
        "temp_insanity": False,
        "bout_until_turn": 0,

        "mystery_count": int(d["mystery_count"]),
        "mystery_idx": 0,
        "keys": 0,
        "investigations_done": 0,
        "investigations_total": 0,
        "mystery_len": int(t(cfg, "mystery.len_default")),
        "lighthouse_events": [],
        "phase": "town",
        "ending": None,

        "tags": [],
        "inventory": {"backpack": [], "storage": []},
        #: 已装备槽位（t63）：`{槽位: item_id}`。空 = 什么都没装备。
        #: 旧档缺此字段时由 `equipped()` 惰性补空 dict，**不抛**（与 `inventory` 同策略）。
        "equipped": {},
        #: 1.9.19：固定商店的每局库存/到访记录；`searched` 记录本回合搜刮过的地点。
        "shops": {},
        "searched": [],
        "allies": [],
        "flags": {},
        "seen_cards": [],

        "log": [],
        "turn": 0,
        "rng_state": None,
        # 1.9.20：AI 控制台（每条主剧情调用一条）与玩家导演指令。
        # 放在权威状态里，才能随 CoC 存档往返；内容由 app/ai_console.py 截断。
        "ai_console": [],
        "ai_directive": "",

        # ---- t54：CoC 内的 TSF 子系统（`app/coc/tsf.py`）新增的两个字段 ----
        # **刻意没有** `tsf_progress` / `tsf_stage`：它们是**派生量**
        # （`tsf.tsf_progress_of` 从账本投影、`tsf.stage_index` 从段表现算），
        # 落盘反而制造"存档与账本不一致"的可能（t53 §9 的结论，captain 已采纳）。
        # 下面两个字段不可推导，所以必须落盘：
        #   · tsf_nudge_total —— 模型被允许提议的、有界的推动累计（账本不记录它）
        #   · tsf_symptoms    —— 已叙述过的症状档位（同一进度下"看过/没看过"是两种历史）
        "tsf_nudge_total": 0,
        "tsf_symptoms": [],

        # ---- t62：战斗子系统（`app/coc/combat.py`）新增的唯一字段 ----
        #: 本局**固定不变**的驱魔答案（`MECHANICS.md` §5.4：正确序列在本局内固定不变）
        #: 与它的提交计数。**刻意只存这一处是"战斗内状态"**：敌人 HP / 玩家 STA / 回合时间
        #: 都在 `combat.Fight` 的**工作副本**里结算，不落进权威状态（战斗结束前不写）。
        #: `target` 一旦抽定就不再变（同 seed 同序 ⇒ 同答案）；`target=[]` 表示"还没遇到幽灵"。
        #: 该字段**不外泄**：`combat.exorcism_public()` 只给 tries/solved/last_score。
        "exorcism": {"target": [], "tries": 0, "solved": False,
                     "last_score": None, "steps": 0},
    }
    if p.get("seed") is not None:
        state["rng_state"] = rng_to_json(random.Random(int(p["seed"])))
    return state


def new_rng(seed: int | None = None, state: dict | None = None) -> random.Random:
    """新建 rng；若 state 里已有 `rng_state` 则精确还原（读档一致性）。"""
    if state is not None and state.get("rng_state") is not None:
        return restore_rng(state["rng_state"])
    return random.Random(seed)


# ------------------------------------------------------- rng 的可 JSON 化

def rng_to_json(rng: random.Random) -> list:
    """`random.Random.getstate()` → 可 JSON 化的嵌套 list。

    `getstate()` 返回 `(version, tuple_of_625_ints, gauss_next_or_None)`，
    其中 ints 是 32 位无符号；直接 json.dumps 会因 tuple 变 list 而**能**工作，
    但 `gauss_next` 可能是 `None` 或 float —— 这里显式规整，保证往返稳定。
    """
    version, internal, gauss = rng.getstate()
    return [int(version), [int(x) for x in internal],
            None if gauss is None else float(gauss)]


def restore_rng(payload: list) -> random.Random:
    """`rng_to_json` 的逆操作（逐字段还原，保证后续随机序列完全一致）。"""
    if not isinstance(payload, (list, tuple)) or len(payload) != 3:
        raise StateError("rng_state 结构非法，无法还原")
    version, internal, gauss = payload
    rng = random.Random()
    rng.setstate((int(version), tuple(int(x) for x in internal),
                  None if gauss is None else float(gauss)))
    return rng


# ---------------------------------------------------------------- 序列化

def to_json_obj(state: dict) -> dict:
    """导出为可 JSON 落盘的纯数据（键序稳定，便于逐字节比较）。"""
    return json.loads(json.dumps(state, ensure_ascii=False, sort_keys=True))


def dumps(state: dict) -> str:
    return json.dumps(to_json_obj(state), ensure_ascii=False, sort_keys=True, indent=1)


def loads(text: str) -> dict:
    obj = json.loads(text)
    if not isinstance(obj, dict) or obj.get("mode") != "coc":
        raise StateError("这不是一个 CoC 存档")
    from_json_obj(obj)
    return obj


def from_json_obj(obj: dict) -> dict:
    """读档后的字段补齐（老存档迁移位）。"""
    obj.setdefault("schema", SCHEMA_VERSION)
    obj.setdefault("tags", [])
    obj.setdefault("flags", {})
    obj.setdefault("allies", [])
    obj.setdefault("seen_cards", [])
    obj.setdefault("log", [])
    obj.setdefault("inventory", {"backpack": [], "storage": []})
    #: 1.9.19：旧档没有 SAN / 商店 / 搜刮字段 ⇒ 按当前 KNW 补一个合理初始值，
    #: 而不是补 0（补 0 会让旧档一打开就触发 END_SAN_ZERO）。
    if "san" not in obj or not isinstance(obj.get("san"), int):
        try:
            from .provenance import load_tuning
            from . import sanity as _sanity
            _cfg = load_tuning()
            obj["san"] = _sanity.initial_san(obj.get("stats") or {}, _cfg)
            obj["san_max"] = _sanity.san_max(_cfg)
        except Exception:
            obj["san"] = 50
            obj["san_max"] = 99
    if "san_max" not in obj or not isinstance(obj.get("san_max"), int):
        obj["san_max"] = 99
    obj.setdefault("san_checks", 0)
    obj.setdefault("san_loss_total", 0)
    obj.setdefault("temp_insanity", False)
    obj.setdefault("bout_until_turn", 0)
    obj.setdefault("shops", {})
    obj.setdefault("searched", [])
    obj.setdefault("ai_console", [])
    obj.setdefault("ai_directive", "")
    #: t63：旧档没有装备槽位 ⇒ 补空 dict。物品仍留在背包里，不会因此丢失。
    eq = obj.get("equipped")
    if not isinstance(eq, dict):
        obj["equipped"] = {}
    #: t62：旧档没有驱魔字段 ⇒ 补空结构（**不抽答案** —— 首次遇到幽灵时才由
    #: `combat.exorcism_target()` 用当局 rng 抽，见 `combat.ensure_fields`）。
    ex = obj.get("exorcism")
    if not isinstance(ex, dict) or not isinstance(ex.get("target"), list):
        obj["exorcism"] = {"target": [], "tries": 0, "solved": False,
                           "last_score": None, "steps": 0}
    obj.setdefault("stats", {k: 5 for k in STAT_KEYS})
    return obj


def save(state: dict) -> str:
    """落盘到 `<DATA_DIR>/saves/<sid>.json`，返回路径。

    路径**跟随 `app.config.DATA_DIR`**（含 `TSF_DATA_DIR` 逃逸舱），不再由包内 `__file__` 推 ——
    后者会写进安装目录，更新替换目录时连带丢档（详见 `paths.py` 顶部的两类路径说明）。
    这里调 `paths.saves_dir()` 取**当下值**而不是读顶层常量，避免进程内切换数据根后拿到旧路径。
    """
    target = paths.saves_dir()
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, f"{state['sid']}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps(state))
    return path


def load(sid: str) -> dict:
    path = os.path.join(paths.saves_dir(), f"{sid}.json")
    with open(path, "r", encoding="utf-8") as fh:
        return loads(fh.read())


def state_equal(a: dict, b: dict) -> tuple[bool, str]:
    """逐字段深比较（V-08 A7 用）。返回 `(相等?, 不同点说明)`。"""
    ja, jb = dumps(a), dumps(b)
    if ja == jb:
        return True, ""
    la, lb = ja.splitlines(), jb.splitlines()
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<缺行>"
        y = lb[i] if i < len(lb) else "<缺行>"
        if x != y:
            return False, f"第 {i + 1} 行不同：\n  A: {x}\n  B: {y}"
    return False, "长度不同但无逐行差异（不应发生）"


# ------------------------------------------------- 数值清洗（vn.py 范式）

def sanitize_delta(delta: Any, cfg: dict) -> dict:
    """vn.py:66 `sanitize_delta` 的 CoC 版：**只要 LLM 提案**（服务端结算走 apply_delta）。

    注意语义差异：vn.py 返回清洗后的 dict；本模式同时需要"丢了什么"的审计信息，
    故审计走 `sanitize_llm_delta`（返回二元组），此处保持 vn.py 的单返回值形态。
    """
    cleaned, _dropped = _san.sanitize_llm_delta(delta, cfg)
    return cleaned


def apply_delta(state: dict, delta: Any, cfg: dict, *, trust: str = "llm") -> dict:
    """vn.py:80 `apply_delta` 的 CoC 版：委托到唯一写入点。

    `trust` 默认 `"llm"`（**最不信任**的那一层）—— 与 `sanitize.apply_delta` 一致。
    引擎结算请用 `sanitize.apply_delta_engine()`（第二轮复核后新增的显式信任层）。
    """
    return _san.apply_delta(state, delta, cfg, trust=trust)


# ---------------------------------------------------------------- 物品栏与装备（t63）

#: 可装备槽位（`data/coc/items.json` 的 `_equip_slots`）：R 右手 / C 携带物 / B 身体
EQUIP_SLOT_KEYS: tuple[str, ...] = ("R", "C", "B")

#: items.json 的元数据缓存（进程内只读一次；路径随 DATA_DIR 无关，是随包数据）
_ITEM_TABLE_CACHE: dict[str, dict] | None = None


def item_table() -> dict[str, dict]:
    """`{item_id: 元数据}`（来自 `data/coc/items.json`）。

    读不到时返回**空表**（与 `sanitize.valid_item_ids()` 同一降级策略：
    空表 ⇒ 一切物品都被丢弃，而不是放行未登记物品）。
    """
    global _ITEM_TABLE_CACHE
    if _ITEM_TABLE_CACHE is not None:
        return _ITEM_TABLE_CACHE
    out: dict[str, dict] = {}
    try:
        if os.path.isfile(paths.ITEMS_PATH):
            with open(paths.ITEMS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("items", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("id"):
                        out[str(it["id"])] = {
                            k: v for k, v in it.items() if not k.startswith("_")
                        }
    except (OSError, json.JSONDecodeError):
        out = {}
    _ITEM_TABLE_CACHE = out
    return out


def item_meta(item_id: str) -> dict:
    """单件物品的元数据（未登记 ⇒ 最小兜底，**不抛**）。

    兜底形状保证前端永远拿到 `kind` 字段，不必判空。
    """
    meta = item_table().get(str(item_id))
    if meta is None:
        return {"id": str(item_id), "name": str(item_id), "kind": "unknown"}
    return dict(meta)


def equipped(state: dict) -> dict:
    """已装备槽位 `{slot: item_id}`（旧档无此字段 ⇒ 空 dict，**不抛**）。"""
    eq = state.get("equipped")
    if not isinstance(eq, dict):
        eq = {}
        state["equipped"] = eq
    return eq


def wielded_weapon(state: dict) -> dict | None:
    """当前**右手**装备的武器元数据；没装备或不是武器 ⇒ `None`。

    这是"武器属性真的被用于判定"的唯一取值入口（`loop.py` 的武器路由用它）。
    校验 `kind == "weapon"`：防止把图腾之类塞进 R 槽就当成武器。
    """
    iid = equipped(state).get("R")
    if not iid:
        return None
    meta = item_table().get(str(iid))
    if not meta or meta.get("kind") != "weapon":
        return None
    return dict(meta)


def inventory_list(state: dict, which: str = "backpack") -> list[dict]:
    """背包/仓库的物品视图列表（每件含元数据 + 是否已装备）。"""
    inv = state.get("inventory") or {}
    ids = inv.get(which) or []
    eq = equipped(state)
    worn = {v for v in eq.values() if v}
    out: list[dict] = []
    for iid in ids:
        meta = item_meta(str(iid))
        meta["equipped"] = str(iid) in worn
        out.append(meta)
    return out


def equip_item(state: dict, item_id: str) -> dict:
    """把背包里的物品装备到它的 `equip_slot`。返回 `{ok, slot, item, reason}`。

    规则（都在返回值里说明原因，**不抛异常** —— 它会被 HTTP 端点与测试调用）：
      · 物品必须在**背包**里（仓库里的不算）；
      · 必须有 `equip_slot`（消耗品/可用品不能装备）；
      · 槽位已被占用 ⇒ 顶掉旧的（旧件回背包，不丢失）；
      · `hand == "two"` 的双手武器会同时占用 R（本模式只有 R 槽放武器，
        所以"双手"的含义是**不能再装备第二把武器**，由 R 槽的单一性天然保证）。
    """
    iid = str(item_id or "")
    inv = state.setdefault("inventory", {"backpack": [], "storage": []})
    bag = inv.setdefault("backpack", [])
    if iid not in bag:
        return {"ok": False, "slot": None, "item": iid, "reason": "not_in_backpack"}
    meta = item_table().get(iid)
    if not meta:
        return {"ok": False, "slot": None, "item": iid, "reason": "unregistered"}
    slot = meta.get("equip_slot")
    if not slot or slot not in EQUIP_SLOT_KEYS:
        return {"ok": False, "slot": None, "item": iid, "reason": "not_equippable"}
    # t71（TSF×CoC 融合）：转变会导致**旧装备不再合身**、或**双手物品握不住**。
    # 与既有规则同形：拒绝但不抛，给出可读 reason 交前端显示中文。
    try:
        from .item_effects import wearable
        ok, why = wearable(state, iid)
        if not ok:
            return {"ok": False, "slot": str(slot), "item": iid, "reason": str(why)}
    except Exception:
        pass          # 转变数据缺失 ⇒ 不因此拦住装备（保守）
    eq = equipped(state)
    prev = eq.get(slot)
    eq[slot] = iid
    return {"ok": True, "slot": str(slot), "item": iid,
            "replaced": str(prev) if prev else None, "reason": "equipped"}


def unequip_item(state: dict, slot: str) -> dict:
    """卸下某槽位（物品回背包）。返回 `{ok, slot, item, reason}`。"""
    sl = str(slot or "")
    eq = equipped(state)
    if sl not in eq:
        return {"ok": False, "slot": sl, "item": None, "reason": "slot_empty"}
    iid = eq.pop(sl)
    return {"ok": True, "slot": sl, "item": str(iid), "reason": "unequipped"}


def inventory_view(state: dict) -> dict:
    """物品栏视图（给 `session.public_state` 的 `coc` 段）。

    形状（**前端契约**之一，见报告 §5）：

        {
          "backpack": [ {id, name, kind, stat?, equip_slot?, hand?, usable?, equipped} ],
          "storage":  [ ...同形... ],
          "equipped": { "R": {"id","name","kind","stat",...} | null, "C": ..., "B": ... },
          "slots":    {"R": "右手（武器）", ...},
          "weapon":   {"id","name","stat"} | null,     # 当前右手武器（判定用）
          "counts":   {"backpack": n, "storage": m}
        }
    """
    eq = equipped(state)
    eq_view: dict[str, Any] = {}
    for sl in EQUIP_SLOT_KEYS:
        iid = eq.get(sl)
        eq_view[sl] = item_meta(str(iid)) if iid else None
    w = wielded_weapon(state)
    bag = inventory_list(state, "backpack")
    sto = inventory_list(state, "storage")
    # t71（TSF×CoC 融合）：给每件物品补"在融合模式下的真实可用性"——
    # 前端与 prompt 因此能看见机制（不可装备的原因、使用惩罚、当前衣物档），
    # 而不是只看到一串名字。**默认（无转变）时 block_reason 恒空、penalty 恒 0。**
    tsf_mech: dict[str, Any] | None = None
    try:
        from . import item_effects as _ie
        bag = _ie.inventory_effects(state, which="backpack")
        sto = _ie.inventory_effects(state, which="storage")
        tsf_mech = _ie.mech_view(state)
    except Exception:
        pass          # 转变数据缺失 ⇒ 退回基础视图，绝不因此让面板打不开
    out = {
        "backpack": bag,
        "storage": sto,
        "equipped": eq_view,
        "slots": {k: v for k, v in _EQUIP_SLOT_LABELS.items()},
        "weapon": ({"id": w["id"], "name": w.get("name", w["id"]),
                    "stat": w.get("stat", "")} if w else None),
        "counts": {"backpack": len(bag), "storage": len(sto)},
    }
    if tsf_mech is not None:
        out["tsf_mech"] = tsf_mech
    return out


#: 槽位显示名（与 items.json 的 `_equip_slots` 保持一致；这里内联一份是为了
#: 前端即使在物品表读不到时也能渲染槽位名）
_EQUIP_SLOT_LABELS: dict[str, str] = {
    "R": "右手（武器）",
    "C": "携带物 / 护符",
    "B": "身体 / 护甲",
}


def panel_view(state: dict) -> dict:
    """前端面板视图（vn.py:87 的 CoC 版：多资源而非单组 0–100）。

    `severity` 是 sta/rea 阈值的**机读形态**（MECHANICS.md §3.2：≤9 变样子、
    ≤0 被黑污覆盖）——本模式不做立绘，故转成字段交给前端。
    """
    sta, rea = int(state.get("sta", 0)), int(state.get("rea", 0))
    severity = "normal"
    if sta <= 0 or rea <= 0:
        severity = "collapsed"
    elif sta <= 9 or rea <= 9:
        severity = "hurt" if sta <= 9 else "haunted"
    tags = state.get("tags") or []
    # 1.9.19：SAN 面板状态（bout / 低 SAN）。独立于 REA，前端可单独渲染。
    san = int(state.get("san", 0))
    san_cap = int(state.get("san_max", 99))
    try:
        from . import sanity as _sanity
        san_state = _sanity.san_state(state)
    except Exception:
        san_state = {"state": "unknown", "label": "", "bout": False}
    # t71（TSF×CoC 融合）：面板要能显示**融合模式下的物品机制** ——
    # 否则玩家看得见属性却看不见"为什么这把斧头不能用了"。
    # 只带轻量字段（装备/计数/衣物档/机制汇总），完整背包列表走 session 的 coc.inventory。
    tsf_mech: dict | None = None
    eq_light: dict = {}
    inv_counts = {"backpack": 0, "storage": 0}
    try:
        from . import item_effects as _ie
        tsf_mech = _ie.mech_view(state)
        eq = equipped(state)
        for sl in EQUIP_SLOT_KEYS:
            iid = eq.get(sl)
            eq_light[sl] = item_meta(str(iid)) if iid else None
        inv = state.get("inventory") or {}
        inv_counts = {"backpack": len(inv.get("backpack") or []),
                      "storage": len(inv.get("storage") or [])}
    except Exception:
        pass          # 转变数据缺失 ⇒ 面板照常渲染（不因新功能打不开）
    out = {
        "mode": "coc",
        "sid": state.get("sid"),
        "doom": int(state.get("doom", 0)),
        "doom_line": doom_line(state),
        "sta": sta, "sta_max": int(state.get("sta_max", sta)),
        "rea": rea, "rea_max": int(state.get("rea_max", rea)),
        "san": san, "san_max": san_cap,
        "san_state": san_state,
        "san_checks": int(state.get("san_checks", 0)),
        "san_loss_total": int(state.get("san_loss_total", 0)),
        "funds": int(state.get("funds", 0)),
        "exp": int(state.get("exp", 0)),
        "level": int(state.get("level", 1)),
        "stats": {k: int(v) for k, v in (state.get("stats") or {}).items()},
        "keys": int(state.get("keys", 0)),
        "mystery_idx": int(state.get("mystery_idx", 0)),
        "mystery_len": int(state.get("mystery_len", 0)),
        "investigations_done": int(state.get("investigations_done", 0)),
        "phase": state.get("phase"),
        "severity": severity,
        # 标签**只给计数与类型**，不给 id/name —— 信息遮蔽（§2.10 手法 2）
        "tag_counts": {
            "injury": sum(1 for x in tags if x.get("kind") == "injury"),
            "curse": sum(1 for x in tags if x.get("kind") == "curse"),
            "town": sum(1 for x in tags if x.get("kind") == "town"),
        },
        "ending": state.get("ending"),
        "turn": int(state.get("turn", 0)),
        # ---- t71 新增（默认无转变时 tsf_mech 仍返回全 0/空，前端可无脑渲染）----
        "equipped": eq_light,
        "inv_counts": inv_counts,
    }
    if tsf_mech is not None:
        out["tsf_mech"] = tsf_mech
    return out


def states_block(state: dict) -> str:
    """注入 LLM 的**数值块**（vn.py:105 的 CoC 版）。

    与 `vn.py.char_states_block` 的**故意差异**：这里**不输出标签名**、
    **不输出敌人弱点**、**不输出本次检定属性** —— 信息遮蔽是恐怖感来源
    （MECHANICS.md §9.4 手法表、COC-DESIGN §8.2 禁忌）。
    """
    return "\n".join([
        doom_line(state),
        f"体力 {int(state.get('sta', 0))}/{int(state.get('sta_max', 0))}"
        f"　意志 {int(state.get('rea', 0))}/{int(state.get('rea_max', 0))}"
        f"　SAN {int(state.get('san', 0))}/{int(state.get('san_max', 99))}",
        "属性 " + " ".join(f"{k}{int(v)}" for k, v in (state.get("stats") or {}).items()),
        f"身体状态：{severity_phrase(state)}",
        f"理智状态：{_san_phrase(state)}",
        f"异常感：{unease_phrase(state)}",
    ])


def _san_phrase(state: dict) -> str:
    try:
        from . import sanity as _sanity
        return str(_sanity.san_state(state).get("label") or "SAN 稳定")
    except Exception:
        return "SAN 状态未知"


def doom_line(state: dict) -> str:
    """固定输出行（MECHANICS.md §8.3 设计建议 4 / §10.2 #3 / COC-DESIGN §6.1 第 11 项）。

    DOOM 到 100 时按上游 §2.1 显示 `END`（"数字改为显示 END"）。
    """
    d = int(state.get("doom", 0))
    return "[DOOM END]" if d >= 100 else f"[DOOM {d}%]"


def severity_phrase(state: dict) -> str:
    """把 sta/rea 阈值转成**自然语言**（替代立绘变化；§10.2 #12 的转化方案）。"""
    sta, rea = int(state.get("sta", 0)), int(state.get("rea", 0))
    parts: list[str] = []
    if sta <= 0:
        parts.append("身体已经撑不住了")
    elif sta <= 9:
        parts.append("身上有伤，呼吸不稳")
    if rea <= 0:
        parts.append("精神已经崩了")
    elif rea <= 9:
        parts.append("神经绷得太紧，注意力开始断线")
    return "；".join(parts) if parts else "暂时没有明显异样"


def unease_phrase(state: dict) -> str:
    """把 doom/rea 转成"不安感"措辞（**不含城镇状态名**，只需玩家能感到不对劲）。"""
    d = int(state.get("doom", 0))
    rea = int(state.get("rea", 0))
    if d >= 75 or rea <= 9:
        return "空气里有种说不上来的错位，像有什么正在被翻开"
    if d >= 40:
        return "街上比平时安静，安静得不太对"
    return "暂时还算正常"


def extra_dimensions(state: dict) -> list[dict]:
    """额外面板维度（周目 / 次数 / 等级）。

    `COC-DESIGN.md` §9.1 的验收要求"存档读档后**周目**一致"，
    这里把周目相关的计数量显式暴露出来，好让面板与报告都能看到它们。
    """
    return [
        {"key": "mystery_idx", "name": "已过谜团", "value": int(state.get("mystery_idx", 0))},
        {"key": "keys", "name": "钥匙", "value": int(state.get("keys", 0))},
        {"key": "investigations_total", "name": "累计调查",
         "value": int(state.get("investigations_total", 0))},
        {"key": "level", "name": "等级", "value": int(state.get("level", 1))},
    ]


# -*- coding: utf-8 -*-
"""CoC 战斗：**200 时间单位 / ≤8 动作的「有序动作队列」**结算。

## 这个模块实现的是哪一条上游规则

| 规则 | 出处 | 本模块的落点 |
|---|---|---|
| 每回合 200 时间单位、队列最多 8 个动作 | `MECHANICS.md` §5.1（L2，§12 #6 未二次确认） | `plan_queue()`：`Σ time_cost ≤ tuning.combat.time_budget_turn` 且 `len ≤ tuning.combat.max_actions_per_turn`；**越界给可读理由**（`QueueRejected.reasons`），**绝不静默截断** |
| 玩家编排一串动作 → 一次性提交 → 系统结算 → 敌人伤害 | §5.1 | `submit()`（一个回合） / `resolve()`（自动多回合） |
| 动作耗时 = 基础耗时 × 通用速度修正 − 缩放系数 × 属性；向玩家有利取整；多个修正相乘 | §5.3 | `time_cost()`；`speed_multiplier()` 用**乘法**累乘；`floor` |
| 命中率 = 武器属性 + 10%×等级 − 敌人强度 | §5.3 + `tuning.combat._hit_formula` | `hit_pct()` |
| 武器走不同属性（Steak Knife → DEX、双手武器 → STR） | §5.2、§4.3 | `weapon_profile()`：`stat` **只从 `items.json` 读**（单一真源） |
| 幽灵免疫普通进攻动作，唯一例外是装备 `Bronze Crucifix`；Spiritual 动作 / 法术 / 投掷有效 | §5.4 | `is_blocked_by_incorporeal()` + `damage_channel` 白名单 |
| 5 步「拍手/鞠躬」驱魔序列，本局固定，返回「有几步正确」，成功 +15 EXP | §5.4、§10.2 #14 | `exorcism_target()` / `score_exorcism()` / `note_exorcism_try()` |
| 逃跑 +5% DOOM；Boss / `SwollenJoint` / 古神 `Cthac-Atorasu` 不可逃 | §5.2 | `can_flee()` |
| 法术与投掷**不占队列、不耗时间**，只付 REA/DOOM | §5.1、§5.6 | `free_actions` 参数；`free: true` 的动作若排进主队列会被拒并给出理由 |
| 普通遭遇应能在 1–2 回合内解决 | §9.4 坑 2 / `MECHANISM-MAP` S-04 | `default_sequence()` 优先「少而重」的动作；非 Boss 敌人 `min_turns_to_kill ≤ 2` 的数据约束不变 |

## 三条硬不变量（这个模块为什么这么写）

1. **本模块不改权威状态。** 它把 `sta`/`rea`/`exp`/`funds`/背包/HP 全部放进 `Fight` 的**工作副本**里结算，
   最后以 `sta`/`rea`/`doom`/`exp`/`funds`/`item_*`/`tags_remove` 的形式**交还**调用方
   （`Fight.deltas()`），由 `loop.Run.apply()`（唯一写点）落账。
   ⇒ 同一次结算在「读→算→写」三段里没有半途可见的中间态。
   *唯一例外*：`state["exorcism"]`（本局固定的驱魔答案）由本模块自己写 —— 它不可由账本推导，
   且与 `tsf.apply_nudge` 写 `state["log"]` 是同一类「子系统自有的、带命名空间的字段」。
   **答案本身永不外泄**：`Outcome` 只带 `tries/solved/last_score`，不带 `target`。
2. **随机只走调用方给的 rng。** `submit()`/`resolve()` 只用传入的 `random.Random` 掷骰
   （命中 d100、闪避 miss d100、驱魔答案的首次抽取）。⇒ **同 seed + 同队列 ⇒ 逐字节同结果**。
3. **每一步都进账本。** 每个动作写一条 `kind="combat_step"`；凡是走了命中判定的动作**额外**再写一条
   `kind="check"`（`phase="combat"`，字段名与调查检定对齐：`stat/stat_value/roll/target/success`）。
   敌人反击写在同一条 `combat_step` 的 `enemy_counter` 里，因此「我为什么掉了 2 点体力」是可逐条回溯的。

## 与调查检定的关系（**刻意不合并**）

调查检定用 `dice.check`（`stat + threat_mod vs randint(1,12)`，§4.2，可信度高）；
战斗命中用 §5.3 的**百分比**公式（`clamp(0,100, …) vs randint(1,100)`）。
上游把这两者放在不同的节里、给出不同的域，本模块**不发明二者的换算**，各自按原文实现。
`tsf_shift`（t54 的转变修正）在两边都生效：这里以 `pct` 为单位直接相加，
因为 §5.3 的命中本身就是百分数；进度 0 时 shift=0 ⇒ 行为与本模块接入前逐字相同。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import content
from . import state as _state
from . import tsf as _tsf
from .provenance import t

# ---------------------------------------------------------------- 常量

#: 四个动作分组（§5.1 的 Offensive / Defensive / Support / Spiritual）
GROUPS: tuple[str, ...] = ("offensive", "support", "defensive", "spiritual")

#: 幽灵特性（§5.4）
TRAIT_INCORPOREAL = "incorporeal"

#: 幽灵战斗里普通攻击的**唯一**例外（§5.4）
CRUCIFIX_ID = "BronzeCrucifix"

#: 不允许逃跑的伤势（§5.2）
NO_FLEE_INJURY = "SwollenJoint"

#: 不允许逃跑的古神（§5.2）
NO_FLEE_OLD_GOD = "Cthac-Atorasu"

#: 驱魔进度的落点（本模块自有命名空间，见模块 docstring 不变量 1）
EXORCISM_KEY = "exorcism"

#: 与既有事件账本同形的四种事件名
EVENT_STEP = "combat_step"
EVENT_CHECK = "check"
EVENT_TURN = "combat_turn"
EVENT_COMBAT = "combat"

#: 能伤到幽灵的伤害通道（§5.4：4 个 Spiritual 动作 + 所有进攻性法术与投掷物品）
INCORPOREAL_OK_CHANNELS: tuple[str, ...] = ("spiritual", "spell", "thrown")


class CombatError(RuntimeError):
    """战斗模块被非法调用（敌人表缺项 / 动作表坏了）。"""


class QueueRejected(CombatError):
    """动作队列越界或前置不满足。

    `reasons` 是**全部**拒绝理由（不是只报第一条）：一次提交里同时"超步数 + 超时间 + 缺武器"
    是很常见的，只报一条会逼玩家改一次跑一次。
    """

    def __init__(self, reasons: Iterable[str], *, plan: "QueuePlan | None" = None):
        self.reasons: list[str] = [str(r) for r in reasons]
        self.plan = plan
        super().__init__("；".join(self.reasons) or "动作队列被拒（无具体理由）")


# ---------------------------------------------------------------- 小工具

def _as_int(v: Any, default: int = 0) -> int:
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 数据访问

def action_list(table: dict | None = None) -> list[dict]:
    """动作表（list，顺序即数据表顺序）。"""
    doc = table if isinstance(table, dict) else content.combat_actions_doc()
    return list(doc.get("actions") or [])


def action_index(table: dict | None = None) -> dict[str, dict]:
    """`{id: action}`。缺 `id` 的条目被跳过（加载期已校验，这里只防注入的坏表）。"""
    out: dict[str, dict] = {}
    for a in action_list(table):
        if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"]:
            out[a["id"]] = a
    return out


def weapon_table(table: dict | None = None) -> dict:
    """`{item_id: {dmg, _source, ...}}`（**只含伤害**；`stat` 在 items.json 里）。"""
    doc = table if isinstance(table, dict) else content.combat_actions_doc()
    return {k: v for k, v in (doc.get("weapons") or {}).items() if not str(k).startswith("_")}


def tuned(dotted: str, table: dict | None = None) -> Any:
    """取动作表里的非动作字段（点号路径，自动脱 `value` 外壳）。

    ⚠️ **名字不能叫 `field`**：本模块同时 `from dataclasses import field`，
    模块级的同名函数会**遮蔽** stdlib 的 `field`，于是所有
    `field(default_factory=list)` 会在 **import 期**抛
    `TypeError: field() got an unexpected keyword argument 'default_factory'`
    —— 整个模块直接不可导入（不是某条分支才出错）。
    `tuned` = "读 tuning/动作表里的某个点路径"，语义上也更贴。
    （`t` 已被 `provenance.t` 占用，故不叫 `t`。）
    """
    node: Any = table if isinstance(table, dict) else content.combat_actions_doc()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"combat_actions.json 里没有路径 {dotted!r}（在 {part!r} 处断了）")
        node = node[part]
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


def item_index() -> dict[str, dict]:
    """`{item_id: item}` —— **委托给 `state.item_table()`**（t63 建的进程内缓存）。

    为什么不自己读 json：物品元数据只能有一个来源，否则"UI 上能装备"与"战斗算得出属性"
    会因为两份缓存/两条解析路径而不一致。
    """
    return _state.item_table()


def budget_of(cfg: dict) -> int:
    """回合时间预算（来自 tuning，代码零硬编码）。"""
    return int(t(cfg, "combat.time_budget_turn"))


def max_actions_of(cfg: dict) -> int:
    return int(t(cfg, "combat.max_actions_per_turn"))


def hit_conf(cfg: dict, table: dict | None = None) -> dict:
    """命中参数的**唯一读取点**（全部来自 tuning + 动作表，代码零硬编码）。"""
    return {
        "kick_fixed": int(t(cfg, "combat.hit_kick_fixed")),
        "level_bonus": int(t(cfg, "combat.hit_level_bonus_per_level")),
        "unarmed_no_scale": bool(t(cfg, "combat.hit_unarmed_no_scale")),
        "base_hit": int(tuned("hit.base_hit_default", table)),
        "stat_coef": int(tuned("hit.stat_coef", table)),
        "roll_min": int(tuned("hit.roll_min", table)),
        "roll_max": int(tuned("hit.roll_max", table)),
        "kick_fixed_hit": bool(tuned("hit.kick_uses_fixed_hit", table)),
    }


# ---------------------------------------------------------------- 装备与武器

def equip_map(state: dict) -> dict:
    """装备槽 `{槽位: item_id}`。

    **单一真源是 `state["equipped"]`** —— 那是 t63 的物品栏任务建立的约定
    （`app/coc/state.py::equipped()` / `equip_item()` / `EQUIP_SLOT_KEYS = ("R","C","B")`，
    武器占 `R` 槽）。这里**不另立一套**：`state["equip"]` 只作为旧档别名容忍。
    """
    eq = state.get("equipped")
    if isinstance(eq, dict):
        return {str(k): str(v) for k, v in eq.items() if v}
    legacy = state.get("equip")
    return {str(k): str(v) for k, v in legacy.items() if v} if isinstance(legacy, dict) else {}


def equipped_ids(state: dict) -> list[str]:
    """当前**已装备**的物品 id（按槽位名排序，保证确定性）。"""
    return [v for _, v in sorted(equip_map(state).items()) if v]


def held_ids(state: dict) -> list[str]:
    """已装备 + 背包（"身上有没有这个物品"的宽口径；仅用于展示与提示）。"""
    out = equipped_ids(state)
    inv = state.get("inventory")
    if isinstance(inv, dict) and isinstance(inv.get("backpack"), list):
        for v in inv["backpack"]:
            if isinstance(v, str) and v and v not in out:
                out.append(v)
    return out


def has_equipped(state: dict, item_id: str) -> bool:
    """**已装备**（不是"只是在背包里"）判定 —— §5.2「需装备 Camera」/ §5.4「装备十字架」
    都写的是"装备"，故这里按字面实现：只用 `state["equipped"]`。"""
    return str(item_id) in equipped_ids(state)


def has_item(state: dict, item_id: str) -> bool:
    return str(item_id) in held_ids(state)


def weapon_profile(state: dict, *, table: dict | None = None,
                   items: dict | None = None) -> dict | None:
    """解析"当前用哪把武器、走哪个属性、打多少"。

    **只认已装备的右手武器**：走 `state.wielded_weapon(state)`（t63 的
    「武器属性真的被用于判定」唯一取值入口，读 `R` 槽并校验 `kind == "weapon"`）。
    背包里有武器但没装备 ⇒ **不算**（任务书的措辞是"已装备武器的 stat"；
    要让玩家能武器攻击，先把它装到 `equip_slot`）。

    返回 `{"id", "stat", "dmg", "kind", "usable", "note"}`；没装备武器返回 `None`。
    `stat` **只从 `items.json` 读**（单一真源）。
    若该武器在 `combat_actions.json.weapons` 里没有伤害条目 ⇒ `usable=False`，
    使用它的动作会被拒并给出可读理由（而不是默默按 0 伤害打）。
    """
    meta = items.get("__wielded__") if isinstance(items, dict) else None
    if meta is None:
        meta = _state.wielded_weapon(state)
    if not isinstance(meta, dict) or not meta.get("id"):
        return None
    wid = str(meta["id"])
    spec = weapon_table(table).get(wid)
    out = {"id": wid, "kind": "weapon", "name": str(meta.get("name") or wid),
           "stat": meta.get("stat"), "dmg": None if not isinstance(spec, dict) else spec.get("dmg"),
           "slot": "R"}
    if not isinstance(spec, dict) or spec.get("dmg") is None:
        out["usable"] = False
        out["note"] = (f"武器 {wid!r} 在 items.json 里登记了、但 combat_actions.json.weapons "
                       f"里没有伤害条目 ⇒ 拒绝按 0 伤害结算")
    else:
        out["usable"] = True
        out["note"] = ""
    return out


# ---------------------------------------------------------------- 通用速度修正

def speed_multiplier(state: dict, *, table: dict | None = None) -> tuple[float, list[dict]]:
    """当前生效的**通用速度修正**（§5.3：多个修正**相乘**）。

    数据只在 `combat_actions.json.speed_mods` 里（`Aiko` 0.9 / `perk:FastSwimmer` 0.85 是上游
    逐字给出的两个倍率；其余来源上游未给倍率，**不臆造**）。触发规则：
      - 键 `== state["character"]` ⇒ 角色固有加成；
      - 键以 `perk:` 开头 ⇒ 看 `state["flags"]` 里同名旗标。
    没有任何来源命中 ⇒ 返回 `(1.0, [])`，即**默认完全惰性**（本机制不改变既有平衡）。
    """
    doc = table if isinstance(table, dict) else content.combat_actions_doc()
    raw = doc.get("speed_mods") or {}
    flags = state.get("flags") if isinstance(state.get("flags"), dict) else {}
    char = str(state.get("character") or "")
    mult = 1.0
    hits: list[dict] = []
    for key, node in raw.items():
        if str(key).startswith("_") or not isinstance(node, dict):
            continue
        on = (key == char) or (str(key).startswith("perk:") and bool(flags.get(key)))
        if not on:
            continue
        v = _as_float(node.get("value"), 1.0)
        if v <= 0:
            continue
        mult *= v
        hits.append({"key": str(key), "mult": v, "_source": node.get("_source", "")})
    return max(0.1, mult), hits


# ---------------------------------------------------------------- 属性 / 耗时

def stat_of(state: dict, name: str | None) -> int:
    """属性的**当前有效值**（含 TSF 转变修正）。

    t71：原实现直接返回 `state["stats"][name]` 的**原始值** —— 于是融合模式里
    `coc/tsf.py` 的五档属性修正只作用于 `dice.check(tsf_shift=…)` 那条属性检定路，
    **战斗命中完全不受转变影响**（"身体重构了却照样抡得动消防斧"）。
    改为经 `item_effects.stat_with_tsf()` 取值：该函数是纯函数，未接转变时
    `shift=0` ⇒ 返回值与改动前**逐字相同**（零影响），因此既有 263 条断言不受影响。
    """
    if not name:
        return 0
    raw = _as_int((state.get("stats") or {}).get(name), 0)
    if raw <= 0:
        return raw
    try:
        from .item_effects import stat_with_tsf
        return int(stat_with_tsf(state, str(name)))
    except Exception:      # 转变数据缺失/旧档 ⇒ 退化为原始值，绝不因此中断战斗结算
        return raw


def action_stat(state: dict, action: dict, *, weapon: dict | None = None,
                table: dict | None = None) -> tuple[str | None, int]:
    """该动作**走哪个属性 + 该属性的当前值**（任务书："是否走属性检定及哪个属性"）。

    `scale_stat == "weapon"` ⇒ 用**已装备武器**的 `stat`（Steak Knife → DEX、Fire Axe → STR），
    再取 `state["stats"]` 里那项属性的**当前值**。

    ## `weapon=None` 时**自己解析武器**（不是返回 `(None, 0)`）

    这一点是被复核逼出来的：`AttackWeapon`/`StrongAttack` 走哪项属性**完全由武器决定**，
    所以"没传 weapon"绝不该退化成 `(None, 0)` —— 那会让 `stat_coef × stat` 恒为 0，
    于是**两把不同属性的武器算出同一个命中率/耗时**：
    能力看着实现了、实际是死的（而所有测试只要都显式传参就抓不到）。

    修正前的实测（captain 的证否判据）：
        action_stat(state, AttackWeapon)              → (None, 0)   ← 错的
        action_stat(state, AttackWeapon, weapon=wp)   → ('DEX', 10) ← 对的
        hit_pct(state_dex10, AttackWeapon, …)         → 77%（stat_term=0，与 dex5 相同）
    修正后 `hit_pct(state_dex10, …)`（不传 weapon、不传 stat_value）必须给出更高命中率。
    """
    name = action.get("scale_stat")
    if name == "weapon":
        wp = weapon if weapon is not None else weapon_profile(state, table=table)
        if not wp or not wp.get("stat"):
            return None, 0
        return str(wp["stat"]), stat_of(state, str(wp["stat"]))
    if isinstance(name, str) and name:
        return name, stat_of(state, name)
    return None, 0


def time_cost(action: dict, state: dict, *, cfg: dict | None = None,
              table: dict | None = None, speed: float | None = None,
              weapon: dict | None = None, stat_value: int | None = None) -> int:
    """动作耗时（`MECHANICS.md` §5.3）。

    逐字对应上游公式 `耗时 = 基础耗时 × 通用速度修正 − 缩放系数 × 对应属性`：

        cost = floor(base_time × speed_mult + scale_coef × stat_value)

    为什么把 `scale_coef` 存成**带符号**的负数：§5.3 的表逐字写的是「−3 × Stat」「−10 × DEX」，
    即该系数本身就是负的；存成 `-3` 时上面的式子与 §5.3 的 `− |系数| × 属性` 完全等价，
    且不必在代码里替数据"补一个正负号"。

    取整：`floor`（§5.3「所有计算都向玩家有利的方向取整」——耗时越小对玩家越有利），
    下限 `time.min_cost`（=1，同一句取整规则的下界）。

    `free: true` 的动作耗时恒为 0（§5.1：法术与投掷不消耗时间单位）。
    """
    if bool(action.get("free")):
        return 0
    base = _as_int(action.get("base_time"), 0)
    mult = speed if speed is not None else speed_multiplier(state, table=table)[0]
    if stat_value is None:
        _, sv = action_stat(state, action, weapon=weapon, table=table)
    else:
        sv = _as_int(stat_value, 0)
    coef = _as_int(action.get("scale_coef"), 0)
    cost = math.floor(base * mult + coef * sv)
    # t71（TSF×CoC 融合）：身体变化会让**动作变慢**（手部精细度与协调基线改变）。
    # 这是 `coc_tsf_items.use_penalty[档位]` 的落点 —— 此前该值只作为事实显示，
    # 不参与结算。**只在 state 可读时叠加**；无转变/数据缺失 ⇒ +0，与改动前逐字相同。
    if isinstance(state, dict):
        try:
            from .item_effects import use_penalty as _up
            cost += int(_up(state, cfg))
        except Exception:
            pass
    return max(int(tuned("time.min_cost", table)), cost)


# ---------------------------------------------------------------- 命中与伤害

def hit_pct(state: dict, action: dict, enemy: dict, *, cfg: dict,
            weapon: dict | None = None, stat_value: int | None = None,
            autohit: bool = False, hit_bonus: int = 0,
            tsf_shift: int | None = None, table: dict | None = None) -> tuple[int, dict]:
    """命中率（`MECHANICS.md` §5.3 + `tuning.combat._hit_formula`）。

        clamp(0, 100, 武器固有命中 + 每级加成 × (等级−1) + 属性系数 × 属性 − 敌人强度)

    - **Kick 特例**（§5.2）：`命中率不受敌人 PWR 影响` + `不随等级变化`
      （`tuning.combat.hit_unarmed_no_scale`）⇒ 基线取 `combat.hit_kick_fixed`(=80)，
      并**忽略敌人 PWR 与等级项**。`Prepare Your Attack` 的必中与 `Boost Hit` 的 +10%
      是你自己的增益，仍然生效（§5.2 的"固定 80%"约束的是"不受敌人 PWR 影响"）。
    - `tsf_shift`（t54）：转变修正按逐属性表取值，以**百分点**相加；进度 0 时为 0。
    - `enemy_power` 取敌人的 `power`（`CameraFlash` 的 −10 以 `power_delta_in_turn` 叠加，
      不改 `enemies.json`）。

    ## `stat_value=None` 是**默认**（不是 0）

    不传时**自己**用 `action_stat(state, action, weapon=weapon)` 取当前属性值。
    为什么这对：本函数是公开入口，`weapon_profile()` 已经把 `stat`（DEX/STR）解析好了，
    但"属性值"必须从 `state["stats"]` 现取。若把默认值定成 `0`，
    **任何忘记传参的调用点都会静默得到 `stat_term = 0`** ⇒ 两把不同属性的武器算出同一个命中率，
    而"武器 stat 联动"这项能力就**看着实现了、实际是死的**（captain 的复核正是这样抓到的）。
    显式传 `stat_value`（含 0）仍然优先。

    ## `weapon=None` 时**也自己解析**

    第一版只做到了"属性值自己取"，但"走哪项属性"仍依赖调用方把 `weapon` 递进来
    ⇒ `hit_pct(state_dex10, AttackWeapon, …)` 仍然给出与 `dex5` 相同的命中率
    （内部 `action_stat(state, action, weapon=None)` → `(None, 0)`）。
    ⇒ 效果与"默认值写成 0"**一模一样**，注释只描述了一半的修复。
    现在 `weapon` 缺省时由本函数自己 `weapon_profile(state)` 解析，
    并把用到的武器写进 `detail["weapon"]`（账本里能看到"这次算的是哪把武器"）。

    返回 `(pct, detail)`；`detail` 原样进账本（§5.6：「跑团玩家喜欢看到自己的数字」）。
    """
    conf = hit_conf(cfg, table)
    enemy_power = _as_int(enemy.get("power"), 0) + _as_int(enemy.get("power_delta_in_turn"), 0)
    wp = weapon if weapon is not None else weapon_profile(state, table=table)
    stat_name, resolved_val = action_stat(state, action, weapon=wp, table=table)
    if stat_value is None:
        stat_value = resolved_val
    if tsf_shift is None:
        tsf_shift = _tsf.check_shift(state, stat_name) if stat_name else 0
    shift = _as_int(tsf_shift)
    bonus = _as_int(hit_bonus)
    weapon_id = str((wp or {}).get("id") or "")
    kind = action.get("hit")
    if kind == "fixed_kick" and conf["kick_fixed_hit"]:
        pct = conf["kick_fixed"] + bonus
        if autohit:
            pct = 100
        pct = max(0, min(100, pct))
        return pct, {
            "formula": "kick_fixed",
            "note": "Kick 固定命中率，不受敌人 PWR 影响、不随等级变化（§5.2）；"
                    "你自己的必中/蓄力增益仍生效",
            "base_hit": conf["kick_fixed"], "enemy_power_ignored": enemy_power,
            "hit_bonus": bonus, "autohit": bool(autohit), "tsf_shift": 0,
            "weapon": weapon_id, "stat": stat_name, "stat_value": _as_int(stat_value, 0),
            "level": max(1, _as_int(state.get("level"), 1)), "raw": pct, "pct": pct,
        }
    base = conf["base_hit"]
    level = max(1, _as_int(state.get("level"), 1))
    level_term = conf["level_bonus"] * (level - 1)
    stat_term = conf["stat_coef"] * _as_int(stat_value, 0)
    raw = base + level_term + stat_term - enemy_power + shift + bonus
    pct = 100 if autohit else max(0, min(100, raw))
    return pct, {
        "formula": "base_hit + level_bonus*(level-1) + stat_coef*stat - enemy_power "
                   "+ tsf_shift + hit_bonus",
        "weapon": weapon_id, "stat": stat_name, "stat_value": _as_int(stat_value, 0),
        "base_hit": base, "level": level, "level_term": level_term,
        "stat_coef": conf["stat_coef"], "stat_term": stat_term,
        "enemy_power": enemy_power, "tsf_shift": shift, "hit_bonus": bonus,
        "autohit": bool(autohit), "raw": raw, "pct": pct,
    }


def damage_value(action: dict, state: dict, *, weapon: dict | None = None,
                 cfg: dict | None = None, table: dict | None = None) -> tuple[int, dict]:
    """动作伤害（`MECHANICS.md` §5.2 逐条）。

    | `dmg.kind` | 规则 | 出处 |
    |---|---|---|
    | `none` | 0（纯效果动作） | §5.2 |
    | `flat` | 固定值（Kick 2 / Bite 3） | §5.2 |
    | `weapon` | `floor(武器伤害 × mult)`（普攻 ×1、强攻 ×1.5、垂死挣扎 ×2） | §5.2 |
    | `stat` | `= 属性值`（Leave an Offering = KNW） | §5.2 |
    | `allies` | `= 盟友数 × 2`（有小概率少 2） | §5.2 |
    | `sever_ties` | `= floor(武器伤害 / 2) + floor(KNW / 4)` | §5.2 |
    | `firearm_second` / `spell` / `thrown` | 无内容数据 ⇒ 0，并在 detail 里说明 | §5.1 / §5.2 |

    返回 `(damage, detail)`。
    """
    spec = action.get("dmg") or {}
    kind = str(spec.get("kind") or "none")
    mult = _as_float(spec.get("mult"), 1.0)
    rnd = str(spec.get("round") or "")
    # `weapon=None` 时**自己解析**（同 `action_stat` / `hit_pct` 的理由：
    # 否则 `weapon` 类伤害会静默算成 0，而"武器伤害联动"就变成了死代码）
    wp = weapon if weapon is not None else weapon_profile(state, table=table)
    wdmg = _as_int((wp or {}).get("dmg"), 0)
    if kind == "none":
        return 0, {"kind": kind, "value": 0, "note": "无伤害动作（纯效果）", "source": "§5.2"}
    if kind == "flat":
        v = _as_int(spec.get("value"), 0)
        return max(0, v), {"kind": kind, "value": v, "source": "§5.2 逐字"}
    if kind == "weapon":
        raw = wdmg * mult
        v = math.floor(raw) if rnd == "floor" else int(round(raw))
        return max(0, v), {"kind": kind, "weapon_dmg": wdmg, "mult": mult,
                           "raw": raw, "value": v, "source": "§5.2（+50% 向下取整）"}
    if kind == "stat":
        name = str(spec.get("stat") or "")
        v = stat_of(state, name)
        return max(0, v), {"kind": kind, "stat": name, "value": v, "source": "§5.2"}
    if kind == "allies":
        n = len(state.get("allies") or [])
        per = _as_int(spec.get("per_ally"), 2)
        v = n * per
        return max(0, v), {"kind": kind, "allies": n, "per_ally": per, "value": v,
                           "low_roll_delta": _as_int(spec.get("low_roll_delta"), -2),
                           "source": "§5.2（平均伤害 = 盟友数 × 2）"}
    if kind == "sever_ties":
        div = max(1, _as_int(spec.get("weapon_div"), 2))
        per = max(1, _as_int(spec.get("knw_per"), 4))
        a = wdmg // div
        b = stat_of(state, "KNW") // per
        return max(0, a + b), {"kind": kind, "weapon_part": a, "knw_part": b,
                               "value": a + b, "source": "§5.2"}
    return 0, {"kind": kind, "value": 0,
               "note": "该伤害通道在本模式的 MVP 数据里没有内容（不臆造数值）",
               "source": "§5.1 / §5.2"}


def damage_channel(action: dict) -> str:
    """动作的伤害通道：`physical`（默认） / `spiritual` / `spell` / `thrown`。"""
    ch = action.get("damage_channel")
    if isinstance(ch, str) and ch:
        return ch
    return "spiritual" if action.get("group") == "spiritual" else "physical"


def is_attack(action: dict) -> bool:
    """这个动作是不是"会打出去"的动作（有命中判定或伤害载体）。

    Dodge/Brace/Meditate/Pray/Clap/Bow/Prepare/Boost/Assess/UseItem/Reload 都是**纯效果**动作，
    不掷命中、也不吃 §5.4 的幽灵豁免（它们本来就不是"打"。Run Away 同理）。
    """
    if action.get("hit") is not None:
        return True
    return str((action.get("dmg") or {}).get("kind") or "none") != "none"


def skipped_counter(sta: int = 0) -> dict:
    """**被跳过的**敌人反击的账本形状。

    为什么要给"跳过"也一份完整形状：第一版只写 `{"skipped": True, "damage": 0}`，
    于是"本次没有反击"与"反击了 0 点"在账本里长成两个不同的形状
    （缺 `sta_after` / `roll` / `brace_halved`）⇒ 任何按字段消费账本的代码
    都要为"最后一步打死敌人"这个**最常见**的情形写特例。
    现在两种形状**完全同构**，只有 `skipped` 与 `damage` 不同。
    """
    return {"base": 0, "brace_halved": False, "dodge_miss_pct": 0, "roll": None,
            "missed": False, "damage": 0, "skipped": True, "sta_after": int(sta)}


# ---------------------------------------------------------------- 幽灵豁免（§5.4）

def is_blocked_by_incorporeal(action: dict, enemy: dict, *, has_crucifix: bool = False,
                              state: dict | None = None) -> tuple[bool, str]:
    """普通进攻动作 vs `incorporeal` 敌人 ⇒ **被豁免**（`MECHANICS.md` §5.4）。

    上游原文："所有进攻动作提示 `This attack can't hurt an incorporeal enemy`（唯一例外：
    Yashiro 装备 `Bronze Crucifix`）"；可行手段为"4 个 Spiritual 动作；所有进攻性法术与投掷物品"。

    本函数实现的规则（**逐条可查**）：

    1. 敌人没有 `incorporeal` 特性 ⇒ 从不豁免（返回 `(False, "")`）；
    2. 该动作不是"打出去"的动作（`is_attack` 为假）⇒ 不豁免（它不打人，谈不上打不打得动）；
    3. 该动作的伤害通道 ∈ {spiritual, spell, thrown} ⇒ 不豁免；
    4. 否则（physical 通道）⇒ 豁免，**除非**持有/装备 `Bronze Crucifix`（§5.4 的唯一例外）。

    返回 `(blocked?, 可读理由)`。理由直接进账本（"你的攻击穿过了它的身体"）。
    """
    if TRAIT_INCORPOREAL not in (enemy.get("traits") or []):
        return False, ""
    if not is_attack(action):
        return False, ""
    ch = damage_channel(action)
    if ch in INCORPOREAL_OK_CHANNELS:
        return False, ""
    if has_crucifix:
        return False, (f"{CRUCIFIX_ID} 是普通攻击对幽灵生效的**唯一**例外（§5.4）："
                       f"本动作走 {ch} 通道，放行")
    return True, (f"这是 {ch} 通道的进攻动作，而 {enemy.get('id') or '敌人'} 是 incorporeal 幽灵 "
                  f"⇒ 攻击穿过它的身体（§5.4）。可行手段：4 个 Spiritual 动作 / 进攻性法术 / "
                  f"投掷物品；普通攻击的唯一例外是装备 {CRUCIFIX_ID}")


# ---------------------------------------------------------------- 逃跑（§5.2）

def can_flee(state: dict, enemy: dict, *, is_boss: bool = False) -> tuple[bool, str]:
    """能否逃跑（`MECHANICS.md` §5.2 的三个否决条件，逐条都给理由）。

    ① Boss 战不可逃；② 中了 `SwollenJoint`（关节肿胀）不可逃；③ 古神为 `Cthac-Atorasu` 不可逃。
    （`Cthac-Atorasu` 不在 MVP 的 `old_gods.json`（上游 9 位 / MVP 2 位）里，
    所以第 ③ 条目前**惰性**——但它按 `state["old_god"]` 数据驱动，该古神一进数据表即自动生效。）
    """
    if is_boss or bool(enemy.get("boss")):
        return False, "Boss 战不可逃（§5.2）"
    tag_ids = {str(x.get("id")) for x in (state.get("tags") or []) if isinstance(x, dict)}
    if NO_FLEE_INJURY in tag_ids:
        return False, f"中了 {NO_FLEE_INJURY}（关节肿胀）不可逃（§5.2）"
    if str(state.get("old_god") or "") == NO_FLEE_OLD_GOD:
        return False, f"古神为 {NO_FLEE_OLD_GOD} 时不可逃（§5.2）"
    return True, ""


# ---------------------------------------------------------------- 驱魔序列（§5.4 / §10.2 #14）

def exorcism_conf(table: dict | None = None) -> dict:
    return {
        "steps": int(tuned("exorcism.steps", table)),
        "tokens": list(tuned("exorcism.tokens", table)),
        "exp": int(tuned("exorcism.exp_reward", table)),
        "hint_steps": int(tuned("exorcism.hint_steps", table)),
    }


def exorcism_target(state: dict, rng, table: dict | None = None) -> list[str]:
    """本局**固定不变**的驱魔答案（5 步 clap/bow）。

    首次需要时从 `rng` 抽（因此同 seed 同序 ⇒ 同答案），落 `state["exorcism"]`。
    之后的每次调用都读回同一个答案 —— §5.4「正确序列在本局内固定不变（攻略甚至建议"记下来"）」。
    """
    conf = exorcism_conf(table)
    node = state.get(EXORCISM_KEY)
    if (isinstance(node, dict) and isinstance(node.get("target"), list)
            and len(node["target"]) == conf["steps"]):
        return [str(x) for x in node["target"]]
    target = [str(rng.choice(conf["tokens"])) for _ in range(conf["steps"])]
    state[EXORCISM_KEY] = {"target": target, "tries": 0, "solved": False,
                           "last_score": None, "steps": conf["steps"]}
    return target


def score_exorcism(submitted: Sequence[str], target: Sequence[str]) -> int:
    """有几步正确（§5.4「每次提交 5 步会告诉你**有几步正确**」）。"""
    return sum(1 for a, b in zip([str(x) for x in submitted], [str(x) for x in target]) if a == b)


def note_exorcism_try(state: dict, scores: list[int], *, steps: int) -> dict:
    """记一次驱魔提交，返回**不含答案**的账本信息（`tries` / `score` / `solved`）。"""
    node = state.setdefault(EXORCISM_KEY, {"target": [], "tries": 0, "solved": False,
                                          "last_score": None, "steps": steps})
    node["tries"] = _as_int(node.get("tries"), 0) + 1
    best = max(scores) if scores else 0
    node["last_score"] = best
    node["steps"] = steps
    solved = bool(scores) and best >= steps
    if solved:
        node["solved"] = True
    return {"tries": node["tries"], "score": best, "steps": steps, "solved": solved}


def exorcism_public(state: dict) -> dict:
    """给外部（`Outcome`/UI/prompt）的驱魔视图：**绝不含答案 `target`**。"""
    node = state.get(EXORCISM_KEY)
    if not isinstance(node, dict):
        return {"tries": 0, "solved": False, "last_score": None, "steps": 0,
                "hint": None}
    return {"tries": _as_int(node.get("tries"), 0), "solved": bool(node.get("solved")),
            "last_score": node.get("last_score"), "steps": _as_int(node.get("steps"), 0),
            "hint": None}


# ---------------------------------------------------------------- 队列校验

@dataclass
class QueuePlan:
    """一次提交的**已校验**队列（可直接展示给玩家看每步的耗时/属性）。"""

    steps: list[dict]
    time_total: int
    count: int
    budget: int
    max_actions: int

    def to_json(self) -> dict:
        return {
            "count": self.count, "time_total": self.time_total,
            "budget": self.budget, "max_actions": self.max_actions,
            "time_left": self.budget - self.time_total,
            "steps": [dict(s) for s in self.steps],
        }


def queue_context(state: dict, enemy: dict | None = None, *, is_boss: bool = False,
                  used_once: Iterable[str] = (), weapon: dict | None = None,
                  items: dict | None = None, table: dict | None = None) -> dict:
    """队列校验/结算共用的**只读上下文**（把所有要在拒绝理由里讲的现状收在一处）。"""
    wp = weapon if weapon is not None else weapon_profile(state, table=table, items=items)
    en = enemy or {}
    traits = en.get("traits") or []
    eq = equip_map(state)
    return {
        "state": state,
        "weapon": wp,
        "has_weapon": bool(wp),
        "weapon_usable": bool(wp and wp.get("usable")),
        "has_ally": bool(state.get("allies")),
        "flags": state.get("flags") if isinstance(state.get("flags"), dict) else {},
        "backpack": list(((state.get("inventory") or {}).get("backpack")) or []),
        "equip": eq,
        "equipped": [v for _, v in sorted(eq.items())],
        "held": held_ids(state),
        "facing_incorporeal": TRAIT_INCORPOREAL in traits,
        "incorporeal_id": str(en.get("id") or "") if TRAIT_INCORPOREAL in traits else "",
        "has_crucifix": has_equipped(state, CRUCIFIX_ID),
        "is_boss": bool(is_boss or en.get("boss")),
        "sta": _as_int(state.get("sta"), 0),
        "rea": _as_int(state.get("rea"), 0),
        "funds": _as_int(state.get("funds"), 0),
        "used_once": set(str(x) for x in used_once),
    }


def prereq_reasons(action: dict, ctx: dict, at: int = 0) -> list[str]:
    """一个动作的前置检查，返回**可读**理由列表（空 = 通过）。"""
    aid = str(action.get("id"))
    label = f"第{at}步 {aid!r}（{action.get('name', '')}）" if at > 0 \
        else f"{aid!r}（{action.get('name', '')}）"
    req = action.get("requires") or {}
    eff = action.get("effects") or {}
    out: list[str] = []
    if req.get("weapon") and not ctx["has_weapon"]:
        bag_weapons = [i for i in ctx["backpack"]
                       if item_index().get(i, {}).get("kind") == "weapon"]
        if bag_weapons:
            # ⚠️ 这条分支曾经不存在，于是"背包里有武器但没装备"也被报成
            # "items.json 里一件武器都没有" —— 与同一进程里加载好的数据**直接矛盾**，
            # 而且把玩家指向了错误的方向（去找武器，而不是去装备）。两种情形的
            # 可操作指引完全不同，必须分开报。
            out.append(f"{label} 需要**装备**一把武器（§5.2）；背包里有 {bag_weapons}，"
                       f"但 R 槽是空的 —— 先把它装到 `equip_slot=\"R\"`"
                       f"（`state.equip_item()`）再排这个动作")
        else:
            out.append(f"{label} 需要装备一把武器（§5.2）；你目前**没有任何** "
                       f"`kind=\"weapon\"` 的物品（背包与 R 槽都是空的）。"
                       f"无武器时只能排 `Kick`（§5.2）")
    elif req.get("weapon") and not ctx["weapon_usable"]:
        out.append(f"{label} 的武器 {ctx['weapon'].get('id')!r} 没有可用伤害数据："
                   f"combat_actions.json.weapons 里缺该武器的 dmg 条目（§5.2 要求伤害有出处）")
    if req.get("weapon_kind"):
        want = str(req["weapon_kind"])
        meta = item_index().get(str((ctx["weapon"] or {}).get("id") or ""), {})
        if meta.get("kind") != want:
            out.append(f"{label} 需要 `kind=\"{want}\"` 的武器；本模式的 items.json 里"
                       f"没有任何该类别武器 ⇒ 数据位就绪但 MVP 无法满足（§5.2 Fire Weapon）")
    if req.get("item"):
        want_item = str(req["item"])
        if want_item not in ctx["equipped"]:
            if want_item in ctx["held"]:
                out.append(f"{label} 需要**装备** {want_item!r}（§5.2 写的是「需装备」）；"
                           f"它现在只在背包里 —— 请先装到它的 `equip_slot`")
            else:
                out.append(f"{label} 需要装备 {want_item!r}（§5.2）；当前装备 "
                           f"{ctx['equipped'] or '（空）'}")
    if req.get("ally") and not ctx["has_ally"]:
        out.append(f"{label} 需要有盟友（§5.2）；`state['allies']` 为空")
    if req.get("flag") and not ctx["flags"].get(str(req["flag"])):
        out.append(f"{label} 需要旗标 {req['flag']!r} 为真（§5.2）；当前为假")
    if req.get("consumable"):
        cons = [i for i in ctx["backpack"] if item_index().get(i, {}).get("kind") == "consumable"]
        if not cons:
            out.append(f"{label} 需要背包里有消耗品（§2.3 Bandage / Medical Kit）；当前没有")
    if "sta_or_rea_at_most" in req:
        lim = _as_int(req["sta_or_rea_at_most"], 0)
        if not (ctx["sta"] <= lim or ctx["rea"] <= lim):
            out.append(f"{label} 需要 STA 或 REA ≤{lim}（§5.2 Desperate Struggle）；"
                       f"当前 STA={ctx['sta']} REA={ctx['rea']}")
    if req.get("facing_incorporeal") and not ctx["facing_incorporeal"]:
        out.append(f"{label} 只对幽灵（`traits` 含 `{TRAIT_INCORPOREAL}`）可用（§5.2/§5.4）；"
                   f"当前敌人不是幽灵")
    if eff.get("once_per_combat") and aid in ctx["used_once"]:
        out.append(f"{label} 每场战斗只能用一次（§5.2）；本场已经用过")
    if eff.get("funds_cost") is not None and ctx["funds"] < _as_int(eff["funds_cost"], 0):
        out.append(f"{label} 需要 {_as_int(eff['funds_cost'], 0)} FND（§5.2 Leave an Offering）；"
                   f"当前 FUNDS={ctx['funds']}")
    if action.get("free"):
        out.append(f"{label} 是**自由动作**（法术/投掷不占队列、不耗时间 —— §5.1 / §5.6）；"
                   f"请放进 `free_actions`，不要排进主队列")
    return out


def plan_queue(sequence: Any, *, cfg: dict, state: dict, enemy: dict | None = None,
               table: dict | None = None, is_boss: bool = False,
               used_once: Iterable[str] = (), weapon: dict | None = None,
               items: dict | None = None) -> tuple[QueuePlan | None, list[str]]:
    """校验一个有序动作队列。

    返回 `(plan, reasons)`：`reasons` 非空即**队列非法**（`plan` 为 `None`）。
    本函数**从不截断**：越界只报错，不"悄悄丢掉最后一个动作"。

    校验项（全部给可读理由）：
      1. 队列不是 list/tuple、为空、元素不是字符串、动作 id 不在动作表里；
      2. `len > max_actions`（§5.1 的 8）；
      3. `Σ time_cost > time_budget`（§5.1 的 200）—— 逐项累计，报出"排到第几步、超了多少"；
      4. 每个动作的前置（武器 / 枪械 / 物品 / 盟友 / 旗标 / 消耗品 / STA·REA 门槛 /
         幽灵限定 / 每战一次 / FND 代价）；
      5. 自由动作被排进主队列（§5.1）。
    """
    if not isinstance(sequence, (list, tuple)):
        raise QueueRejected([f"队列必须是 list/tuple，实际 {type(sequence).__name__}"])
    idx = action_index(table)
    ctx = queue_context(state, enemy, is_boss=is_boss, used_once=used_once,
                        weapon=weapon, items=items, table=table)
    reasons: list[str] = []
    seq = list(sequence)
    budget = budget_of(cfg)
    cap = max_actions_of(cfg)
    if not seq:
        reasons.append("动作队列为空：至少排一个动作（§5.1 的编排至少要提交一件事）")
    if len(seq) > cap:
        reasons.append(f"动作数 {len(seq)} 超过每回合上限 {cap}（MECHANICS.md §5.1；"
                       f"tuning.combat.max_actions_per_turn={cap}）")
    mult, _hits = speed_multiplier(state, table=table)
    steps: list[dict] = []
    total = 0
    over_budget_at: list[int] = []
    for i, aid in enumerate(seq, start=1):
        if not isinstance(aid, str) or not aid:
            reasons.append(f"第{i}个元素不是动作 id 字符串（实际 {aid!r}）")
            continue
        act = idx.get(aid)
        if act is None:
            reasons.append(f"第{i}步 {aid!r} 不在动作表里（合法 id：{sorted(idx)}）")
            continue
        stat_name, stat_val = action_stat(state, act, weapon=ctx["weapon"])
        cost = time_cost(act, state, cfg=cfg, table=table, speed=mult, stat_value=stat_val)
        reasons.extend(prereq_reasons(act, ctx, i))
        before = total
        total += cost
        if total > budget:
            over_budget_at.append(i)
            # **只详细报第一个越界点**：9 个 Kick 会越界 7 次，逐条重复同一句话
            # 会把真正可读的那条理由淹掉（读者要的是"从哪一步开始超、超多少"）。
            if len(over_budget_at) == 1:
                reasons.append(
                    f"总耗时超过回合预算 {budget}（MECHANICS.md §5.1；"
                    f"tuning.combat.time_budget_turn={budget}）：从第{i}步 {aid!r}"
                    f"（耗时 {cost}）起越界 —— 排到它时已累计 {before}，合计 {total}"
                    f"，超出 {total - budget}。**队列不会被截断**，请自行删减动作重排")
        steps.append({
            "index": i, "action": aid, "name": str(act.get("name", aid)),
            "group": str(act.get("group", "")), "time_cost": cost,
            "cumulative_time": total, "stat": stat_name, "stat_value": stat_val,
            "free": bool(act.get("free")), "is_attack": is_attack(act),
        })
    if len(over_budget_at) > 1:
        reasons.append(f"另有 {len(over_budget_at) - 1} 步（第 "
                       f"{'、'.join(str(x) for x in over_budget_at[1:])} 步）同样在预算之外；"
                       f"整条队列的合计耗时 {total}")
    if reasons:
        return None, reasons
    return QueuePlan(steps=steps, time_total=total, count=len(steps),
                     budget=budget, max_actions=cap), []


def validate_queue(sequence: Any, **kw) -> QueuePlan:
    """`plan_queue` 的抛异常版本（越界 ⇒ `QueueRejected`，理由在 `.reasons`）。"""
    plan, reasons = plan_queue(sequence, **kw)
    if reasons:
        raise QueueRejected(reasons, plan=plan)
    return plan


# ---------------------------------------------------------------- 默认（自动）队列

def default_sequence(state: dict, enemy: dict, *, cfg: dict, is_boss: bool = False,
                     table: dict | None = None, items: dict | None = None) -> tuple[list[str], str]:
    """**自动路径**用的确定性队列（无玩家输入时）。

    `loop.resolve_combat` 的既有签名没有"队列"参数（t48 的电路），所以必须有一个默认策略。
    这个策略**不是**"聪明的 AI"，只是把 §9.4 坑 2 / S-04 的约束翻译成编排：

    - 选每回合伤害最大、且每回合消耗动作最少的可用动作，然后按预算与 8 步上限填满
      （少而重 ⇒ 少挨打：敌人是**每个玩家动作**反击一次）；
    - 对幽灵：优先 `SeverTies`（需武器），否则 `LeaveOffering`（需 1 FND）；
      两者都不可用 ⇒ **逃跑**（若允许），否则只排无伤害的精神动作并在账本里说明"打不动它"。

    返回 `(sequence, why)`，`why` 进账本便于解释"这局为什么这么打"。
    """
    idx = action_index(table)
    ctx = queue_context(state, enemy, is_boss=is_boss, table=table, items=items)
    budget = budget_of(cfg)
    cap = max_actions_of(cfg)
    mult, _ = speed_multiplier(state, table=table)

    def usable(aid: str) -> bool:
        act = idx.get(aid)
        if not act:
            return False
        return not prereq_reasons(act, ctx, 1)

    if ctx["facing_incorporeal"] and not ctx["has_crucifix"]:
        if usable("SeverTies"):
            primary, why = "SeverTies", "幽灵：SeverTies 走 spiritual 通道（§5.4），伤害 = 武器/2 + KNW/4"
        elif usable("LeaveOffering"):
            primary, why = "LeaveOffering", "幽灵：LeaveOffering 走 spiritual 通道（§5.4），伤害 = KNW"
        elif usable("RunAway"):
            return ["RunAway"], ("幽灵且没有任何能伤到它的手段（无武器 / 无 FND）⇒ "
                                 "按 §5.2 逃跑（+5% DOOM），而不是站着被打死")
        else:
            return ["Pray"], ("幽灵、没有可伤它的手段、且不可逃（Boss/伤势/古神）⇒ "
                              "只能排无伤害的精神动作，账本会记下『打不动它』")
    else:
        primary, why = None, ""
        extra = "（持有/装备 Bronze Crucifix：§5.4 的幽灵例外打开，普通攻击对幽灵同样有效）" \
            if (ctx["facing_incorporeal"] and ctx["has_crucifix"]) else ""
        for aid in ("StrongAttack", "AttackWeapon", "Kick"):
            if usable(aid):
                primary, why = aid, (f"普通战斗：优先少而重的动作（§9.4 坑 2 / S-04）⇒ {aid}{extra}")
                break
        if primary is None:
            return [], "没有任何动作可用（数据不全）"
    act = idx[primary]
    stat_name, stat_val = action_stat(state, act, weapon=ctx["weapon"])
    cost = time_cost(act, state, cfg=cfg, table=table, speed=mult, stat_value=stat_val)
    if cost <= 0:                       # 防「零耗时动作无限填」把队列填爆
        return [], f"{primary} 耗时为 0，自动路径拒绝编排它（防死循环）"
    seq: list[str] = []
    while len(seq) < cap and (len(seq) + 1) * cost <= budget:
        seq.append(primary)
    if not seq:
        return [], f"{primary} 单次耗时 {cost} 已超回合预算 {budget}（数据不自洽）"
    return seq, f"{why}；填满 {len(seq)} 步（{len(seq)} × {cost} ≤ {budget}）"


# ---------------------------------------------------------------- 结算（核心）

@dataclass
class TurnResult:
    """一个回合（一次 `submit`）的结果。"""

    turn: int
    plan: dict
    steps: list[dict]
    enemy_hp_before: int = 0
    enemy_hp_after: int = 0
    damage_dealt: int = 0
    damage_taken: int = 0
    sta_delta: int = 0
    rea_delta: int = 0
    time_spent: int = 0
    ended: str | None = None      # enemy_down / player_down / fled / exorcised / None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "turn": self.turn, "plan": self.plan, "steps": self.steps,
            "enemy_hp_before": self.enemy_hp_before, "enemy_hp_after": self.enemy_hp_after,
            "damage_dealt": self.damage_dealt, "damage_taken": self.damage_taken,
            "sta_delta": self.sta_delta, "rea_delta": self.rea_delta,
            "time_spent": self.time_spent, "ended": self.ended, "notes": list(self.notes),
        }


@dataclass
class Outcome:
    """整场战斗的结果（可能跨多个回合）。"""

    enemy: str
    enemy_name: str
    is_boss: bool
    won: bool
    fled: bool
    exorcised: bool
    hit_cap: bool
    incorporeal: bool
    turns: int
    time_spent: int
    time_budget: int
    player_damage: int
    damage_taken: int
    sta_delta: int = 0
    rea_delta: int = 0
    doom_delta: int = 0
    exp_delta: int = 0
    funds_delta: int = 0
    item_take: list[str] = field(default_factory=list)
    item_grant: list[str] = field(default_factory=list)
    tags_remove: list[str] = field(default_factory=list)
    sequence: list[str] = field(default_factory=list)
    free_actions: list[str] = field(default_factory=list)
    exorcism: dict | None = None
    turns_detail: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: 完整事件账本（交给 `loop.Run.log` 落账；**不进** `to_json`，避免与 `state["log"]` 重复）
    events: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        """兼容既有 `resolve_combat` 的返回键（`rounds` 作为 `turns` 的旧名保留）。"""
        return {
            # ---- 既有键（t48 电路，形状不变）----
            "enemy": self.enemy, "is_boss": self.is_boss, "rounds": self.turns,
            "player_damage": self.player_damage, "damage_taken": self.damage_taken,
            "incorporeal": self.incorporeal, "won": self.won, "hit_cap": self.hit_cap,
            # ---- t62 新增键 ----
            "enemy_name": self.enemy_name,
            "turns": self.turns,
            # ⚠️ 名字必须自解释：`time_spent` 是**全场累计**（跨多个回合相加，
            #    可能 > 200），`time_budget_per_turn` 才是 §5.1 的每回合 200。
            #    把 400 配一个叫 `time_budget` 的 200 很容易被读成"这一回合花了 400"。
            "time_spent_total": self.time_spent,
            "time_spent": self.time_spent,          # 兼容别名 = 全场累计
            "time_budget_per_turn": self.time_budget,
            "time_budget": self.time_budget,        # 兼容别名 = 每回合预算
            "fled": self.fled, "exorcised": self.exorcised,
            "sequence": list(self.sequence), "free_actions": list(self.free_actions),
            "sta_delta": self.sta_delta, "rea_delta": self.rea_delta,
            "doom_delta": self.doom_delta, "exp_delta": self.exp_delta,
            "funds_delta": self.funds_delta,
            "item_take": list(self.item_take), "item_grant": list(self.item_grant),
            "tags_remove": list(self.tags_remove),
            "exorcism": self.exorcism,
            "turns_detail": [dict(x) for x in self.turns_detail],
            "ledger_events": len(self.events),
            "notes": list(self.notes),
        }


class Fight:
    """一场战斗的**结算器**（工作副本 + 账本；**不改权威状态的资源字段**）。

    为什么用类而不是一串函数：结算过程需要跨步保持状态
    （敌人 HP、玩家 STA/REA/EXP/FUNDS、背包、本回合的 Dodge/Brace 标志、
    `Prepare Your Attack` 的延续、驱魔提交的累计），而每一步都要读它们。
    这些值都放在工作副本里 ⇒ 权威状态在整场战斗期间**逐字节不变**，
    调用方拿到的只有 `deltas()`。
    """

    def __init__(self, state: dict, rng, enemy: dict, cfg: dict, *,
                 is_boss: bool = False, extra_dmg: int = 0,
                 table: dict | None = None, items: dict | None = None):
        if not isinstance(enemy, dict) or not (enemy.get("id") or enemy.get("enemy")):
            raise CombatError("敌人记录缺 id，无法结算")
        if "difficulty" not in state:
            raise CombatError("state 缺 difficulty，无法取难度修正")
        self.state = state
        self.rng = rng
        self.cfg = cfg
        self.table = table
        self.enemy = dict(enemy)
        self.enemy_id = str(enemy.get("id") or enemy.get("enemy"))
        self.enemy_name = str(enemy.get("name") or self.enemy_id)
        self.is_boss = bool(is_boss or enemy.get("boss"))
        self.extra_dmg = _as_int(extra_dmg)
        diff_all = t(cfg, "difficulty")
        self.diff = diff_all.get(state["difficulty"], {}) if isinstance(diff_all, dict) else {}
        self.time_budget = budget_of(cfg)
        self.max_actions = max_actions_of(cfg)
        self.cap = int(t(cfg, "combat.round_cap"))
        # ---- 工作副本 ----
        self.hp = _as_int(enemy.get("hp"), 1)
        self.hp_max = self.hp
        self.sta = _as_int(state.get("sta"), 0)
        self.rea = _as_int(state.get("rea"), 0)
        self.exp = _as_int(state.get("exp"), 0)
        self.funds = _as_int(state.get("funds"), 0)
        inv = state.get("inventory") if isinstance(state.get("inventory"), dict) else {}
        self.backpack: list[str] = [str(x) for x in (inv.get("backpack") or [])]
        # ---- 只读派生 ----
        self.incorporeal = TRAIT_INCORPOREAL in (self.enemy.get("traits") or [])
        self.crucifix = has_item(state, CRUCIFIX_ID)
        self.weapon = weapon_profile(state, table=table, items=items)
        # ---- 跨回合标志 ----
        self.autohit_pending = False
        self.hit_bonus_pending = 0
        self.used_once: set[str] = set()
        self.item_take: list[str] = []
        self.item_grant: list[str] = []
        self.tags_remove: list[str] = []
        self.doom_delta = 0
        self.exp_delta = 0
        self.funds_delta = 0
        self.notes: list[str] = []
        self.events: list[dict] = []
        self.turns_detail: list[dict] = []
        self.total_damage = 0
        self.total_taken = 0
        self.total_time = 0
        self.turns = 0
        self.fled = False
        self.exorcised = False
        self.hit_cap = False

    # ------------------------------------------------------------ 账本

    def log(self, kind: str, data: dict) -> dict:
        ev = {"kind": kind, **data}
        self.events.append(ev)
        return ev

    # ------------------------------------------------------------ 上下文 / 增量

    def ctx(self) -> dict:
        return queue_context(self.state, self.enemy, is_boss=self.is_boss,
                             used_once=self.used_once, weapon=self.weapon, table=self.table)

    def deltas(self) -> dict:
        """本场战斗对权威状态的**全部**影响（交给 `loop.Run.apply` 落账）。

        `exp` 用 `exp_delta` 而不是工作副本之差：`exp` 的变化既有消耗（Combat Skill −5）
        又有获得（驱魔成功 +15），工作副本只反映"最终净值"，而账本要的是净值本身 ——
        两者在数值上一致，但 `exp_delta` 是**有意累加**的，便于将来只走一边。
        """
        st = self.state
        return {
            "sta": self.sta - _as_int(st.get("sta"), 0),
            "rea": self.rea - _as_int(st.get("rea"), 0),
            "doom": self.doom_delta,
            "exp": self.exp_delta,
            "funds": self.funds_delta,
            "item_take": list(self.item_take),
            "item_grant": list(self.item_grant),
            "tags_remove": list(self.tags_remove),
        }

    # ------------------------------------------------------------ 敌人反击

    def _enemy_counter(self, flags: dict) -> dict:
        """敌人反击（沿用既有 `loop.py:370` 的伤害公式，未改动任何一项）。

        `MECHANICS.md` §5.1 的流程是"玩家动作 → 系统结算 → 敌人对玩家造成伤害"，
        本模式取 **per_action**（每个玩家动作之后反击一次，见
        `combat_actions.json.enemy_counter.mode._source` 的三条理由）。
        Dodge/Brace 都按 §5.2 的"本回合"语义作用在**余下**的反击上（不叠加）。
        """
        base = max(0, _as_int(self.enemy.get("damage"), 1)
                   + _as_int(self.diff.get("enemy_dmg"), 0) + self.extra_dmg)
        braced = bool(flags.get("brace"))
        amount = base // 2 if braced else base
        miss_pct = _as_int(flags.get("dodge_miss_pct"), 0)
        roll, missed = None, False
        if miss_pct > 0:
            roll = int(self.rng.randint(1, 100))
            missed = roll <= miss_pct
        if missed:
            amount = 0
        self.sta -= amount
        self.total_taken += amount
        return {"base": base, "brace_halved": braced, "dodge_miss_pct": miss_pct,
                "roll": roll, "missed": missed, "damage": amount, "sta_after": self.sta}

    # ------------------------------------------------------------ 效果

    def _apply_effect(self, act: dict, turn_flags: dict) -> list[str]:
        """非攻击类动作的效果（逐条对 §5.2 原文）。返回备注列表。"""
        eff = act.get("effects") or {}
        notes: list[str] = []
        if "enemy_miss_pct" in eff:
            if turn_flags.get("dodge_miss_pct"):
                notes.append("本次 Dodge 不叠加（§5.2「多次 Dodge 不叠加」）")
            else:
                turn_flags["dodge_miss_pct"] = _as_int(eff["enemy_miss_pct"], 50)
                notes.append(f"本回合余下的敌人攻击 {turn_flags['dodge_miss_pct']}% miss（§5.2）")
        if "enemy_damage_mult" in eff:
            if turn_flags.get("brace"):
                notes.append("本次 Brace 不叠加（§5.2「多次不叠加」）")
            else:
                turn_flags["brace"] = True
                notes.append("本回合余下的敌人伤害减半（向下取整，§5.2）")
        if eff.get("enemy_power_delta"):
            self.enemy["power_delta_in_turn"] = \
                _as_int(self.enemy.get("power_delta_in_turn"), 0) \
                + _as_int(eff["enemy_power_delta"], 0)
            notes.append(f"敌人 PWR {_as_int(eff['enemy_power_delta'], 0):+d}"
                         f"（本场战斗内持续，§5.2 Camera Flash 未给持续时间）")
        if eff.get("next_attack_autohit"):
            self.autohit_pending = True
            notes.append("下一次攻击必中（§5.2 Prepare Your Attack）")
        if eff.get("next_attack_hit_bonus"):
            add = _as_int(eff["next_attack_hit_bonus"], 0)
            self.hit_bonus_pending += add
            notes.append(f"下一次攻击 +{add}% 命中（可叠加，当前待用 {self.hit_bonus_pending}%；"
                         f"§5.2 Boost Hit）")
        if eff.get("this_action_hit_bonus"):
            turn_flags["this_action_hit_bonus"] = _as_int(eff["this_action_hit_bonus"], 0)
        if "sta" in eff or "rea" in eff:
            self.sta = max(0, self.sta + _as_int(eff.get("sta"), 0))
            self.rea = max(0, self.rea + _as_int(eff.get("rea"), 0))
            notes.append(f"资源转换：STA {_as_int(eff.get('sta'), 0):+d} / "
                         f"REA {_as_int(eff.get('rea'), 0):+d}（§5.2） ⇒ STA={self.sta} REA={self.rea}")
        if eff.get("funds_cost") is not None:
            cost = _as_int(eff["funds_cost"], 0)
            paid = min(self.funds, cost)
            self.funds -= paid
            self.funds_delta -= paid
            notes.append(f"消耗 {paid} FND（§5.2 Leave an Offering 需 1 FND）；"
                         f"剩余 FUNDS={self.funds}")
        if eff.get("doom_cost_key") is not None:
            cost = _as_int(t(self.cfg, str(eff["doom_cost_key"])), 0)
            self.doom_delta += cost
            notes.append(f"逃跑 +{cost}% DOOM（§5.2 / tuning.combat.escape_doom）")
        if eff.get("ends_combat"):
            ok, why = can_flee(self.state, self.enemy, is_boss=self.is_boss)
            if ok:
                self.fled = True
                notes.append("逃跑成功：战斗结束（§5.2）")
            else:
                notes.append(f"逃跑失败：{why}")
        if eff.get("reveals"):
            notes.append("情报已获得：" + "、".join(str(x) for x in eff["reveals"])
                         + "（§5.2 Assess the Situation：情报是有代价的一个动作）")
        if eff.get("grants_random"):
            cand = [str(x) for x in eff["grants_random"]]
            got = str(self.rng.choice(cand))
            self.item_grant.append(got)
            notes.append(f"捡到 {got}（§5.2 Look for an Improvised Weapon；该物品不在本模式 MVP 的 "
                         f"items.json 里，故只记账不叠加数值）")
        if eff.get("success_pct") is not None:
            roll = int(self.rng.randint(1, 100))
            if roll <= _as_int(eff["success_pct"], 0):
                notes.append(f"盟友引开成功（{roll} ≤ {eff['success_pct']}%，§5.2）")
            else:
                notes.append(f"盟友引开失败（{roll} > {eff['success_pct']}%）——§5.2 原文："
                             f"「失败则盟友死亡」（盟友系统的接线不在本任务范围，账本已记下该结果）")
        if eff.get("uses_consumable"):
            cons = [i for i in self.backpack if item_index().get(i, {}).get("kind") == "consumable"]
            if cons:
                used = cons[0]
                self.backpack.remove(used)
                self.item_take.append(used)
                cures = str(item_index().get(used, {}).get("cures") or "")
                notes.append(f"用掉 {used}（cures={cures or '—'}，§2.3）")
                if cures:
                    for x in (self.state.get("tags") or []):
                        if isinstance(x, dict) and x.get("kind") == "injury" \
                                and str(x.get("severity")) == cures:
                            self.tags_remove.append(str(x.get("id")))
                            notes.append(f"治愈 {x.get('id')}（severity={cures}，§2.3）")
                            break
        if eff.get("exp_cost"):
            cost = _as_int(eff["exp_cost"], 0)
            self.exp = max(0, self.exp - cost)
            self.exp_delta -= cost
            notes.append(f"消耗 {cost} EXP（§5.2 Combat Skill）")
        if eff.get("ready_weapon"):
            notes.append("武器就绪（本模式无枪械数据，该效果在当前数据下是空操作；§5.2 只提到 "
                         "Fire Weapon 消耗 1 发子弹，没有独立装填动作）")
        if eff.get("exorcism_token"):
            tokens = list(turn_flags.get("exorcism_tokens") or [])
            tokens.append(str(eff["exorcism_token"]))
            turn_flags["exorcism_tokens"] = tokens
            notes.append(f"驱魔记号：{eff['exorcism_token']}（§5.2 Clap/Bow；§5.4 需 5 步）")
        if eff.get("once_per_combat"):
            self.used_once.add(str(act.get("id")))
        if eff.get("damage_mult"):
            notes.append(f"伤害 ×{eff['damage_mult']}（在其它修正之前计算，§5.2 Desperate Struggle）")
        return notes

    # ------------------------------------------------------------ 单步

    def _resolve_step(self, act: dict, at: int, turn: int, turn_flags: dict,
                      used_time: int) -> dict:
        """结算一个动作 + 紧随其后的敌人反击，返回该步的账本记录。"""
        aid = str(act.get("id"))
        stat_name, stat_val = action_stat(self.state, act, weapon=self.weapon)
        mult, _hits = speed_multiplier(self.state, table=self.table)
        cost = time_cost(act, self.state, cfg=self.cfg, table=self.table,
                         speed=mult, stat_value=stat_val)
        used_time += cost
        self.total_time += cost
        hp_before, sta_before = self.hp, self.sta
        notes = self._apply_effect(act, turn_flags)
        damage, dmg_detail = 0, {"kind": "none", "value": 0, "note": "无伤害动作（纯效果）"}
        attacking = is_attack(act)
        blocked, block_why = is_blocked_by_incorporeal(
            act, self.enemy, has_crucifix=self.crucifix, state=self.state)
        roll = None
        hit: bool | None = None
        hit_detail: dict = {}
        channel = damage_channel(act)
        if blocked:
            notes.append(block_why)
        elif attacking:
            bonus = _as_int(turn_flags.pop("this_action_hit_bonus", 0), 0) + self.hit_bonus_pending
            autohit = self.autohit_pending
            self.hit_bonus_pending = 0
            self.autohit_pending = False
            pct, hit_detail = hit_pct(self.state, act, self.enemy, cfg=self.cfg,
                                      weapon=self.weapon, stat_value=stat_val,
                                      autohit=autohit, hit_bonus=bonus, table=self.table)
            conf = hit_conf(self.cfg, self.table)
            roll = int(self.rng.randint(conf["roll_min"], conf["roll_max"]))
            hit = roll <= pct
            if hit:
                damage, dmg_detail = damage_value(act, self.state, weapon=self.weapon,
                                                 cfg=self.cfg, table=self.table)
                self.hp = max(0, self.hp - damage)
                self.total_damage += damage
            else:
                notes.append(f"未命中（{roll} > {pct}%，§5.3）")
            if autohit:
                notes.append("必中生效（§5.2 Prepare Your Attack）")
            if bonus:
                notes.append(f"蓄力命中 +{bonus}%（§5.2 Boost Hit）")
            if damage == 0:
                notes.append("本次命中但伤害为 0：" + str(dmg_detail.get("note") or dmg_detail))
        counter = skipped_counter(self.sta)
        # ⚠️ **不要**在这里加 `self.sta > 0`：本步的敌人反击必须照打，哪怕它会把玩家打到 0 以下。
        #    旧实现（t48 的 while 循环）的语义是「先结算这一轮伤害，再 `if sta <= 0: break`」——
        #    即"让你掉到 0 的那一下照打"。若在这里预检 sta，玩家一旦到 0 就再也吃不到伤害，
        #    而 `last_stand` 的死亡线是 **-5**（`sanitize.last_stand_floor`），
        #    于是 STA 会**永久卡在 -3**、`END_STA_ZERO` 变成不可达 —— 这正是 A5 抓到的回归。
        if self.hp > 0 and not self.fled:
            counter = self._enemy_counter(turn_flags)
            counter["skipped"] = False
        step = {
            "turn": turn, "step": at, "action": aid, "name": str(act.get("name", aid)),
            "group": str(act.get("group", "")), "channel": channel,
            "time_cost": cost, "time_used": used_time,
            "time_left": self.time_budget - used_time,
            "stat": stat_name, "stat_value": stat_val,
            "hit_pct": hit_detail.get("pct"), "roll": roll, "hit": hit,
            "hit_detail": hit_detail, "damage": damage, "damage_detail": dmg_detail,
            "incorporeal_blocked": bool(blocked), "is_attack": bool(attacking),
            "enemy_hp_before": hp_before, "enemy_hp": self.hp,
            "sta_before": sta_before, "sta": self.sta,
            "enemy_counter": counter, "notes": notes,
        }
        self.log(EVENT_STEP, step)
        if roll is not None:
            conf = hit_conf(self.cfg, self.table)
            self.log(EVENT_CHECK, {
                "phase": "combat", "turn": turn, "step": at, "action": aid,
                "name": str(act.get("name", aid)), "channel": channel,
                "stat": stat_name, "stat_value": stat_val,
                "roll": roll, "roll_min": conf["roll_min"], "roll_max": conf["roll_max"],
                "target": hit_detail.get("pct"), "hit_pct": hit_detail.get("pct"),
                # `success` 与调查检定同名：读 kind="check" 的消费者都能看到"这次成没成"
                "success": bool(hit), "hit": bool(hit),
                "damage": damage, "enemy_hp": self.hp,
                "tsf_shift": _as_int(hit_detail.get("tsf_shift"), 0),
                "tsf_progress": _tsf.progress(self.state),
                "dc": 0, "threat": "COMBAT", "threat_mod": 0, "is_tie": False,
                "reason": (f"{aid}: {stat_name or '—'} {stat_val} → 命中率 "
                           f"{hit_detail.get('pct')}% vs roll {roll} → "
                           f"{'命中' if hit else '未命中'}"),
            })
        return step

    # ------------------------------------------------------------ 一个回合

    def submit(self, sequence: Any, *, turn: int = 1,
               free_actions: Sequence[str] = ()) -> TurnResult:
        """结算**一个回合**（一次提交的队列）。

        `sequence` 是玩家编排的有序动作 id 列表；越界 ⇒ `QueueRejected`（带全部理由）。
        `free_actions`：法术/投掷（不占队列、不耗时间，§5.1）。本模式 MVP 无该类内容，
        但白名单与账本已就位。
        """
        idx = action_index(self.table)
        plan = validate_queue(sequence, cfg=self.cfg, state=self.state, enemy=self.enemy,
                              table=self.table, is_boss=self.is_boss,
                              used_once=self.used_once, weapon=self.weapon)
        free_ids: list[str] = []
        free_reasons: list[str] = []
        for j, fid in enumerate(free_actions or (), start=1):
            act = idx.get(str(fid))
            if act is None:
                free_reasons.append(f"自由动作第{j}个 {fid!r} 不在动作表里")
            elif not act.get("free"):
                free_reasons.append(f"自由动作第{j}个 {fid!r} 不是自由动作（`free != true`）；"
                                    f"它必须走主队列并消耗时间（§5.1）")
            else:
                free_ids.append(str(fid))
        if free_reasons:
            raise QueueRejected(free_reasons)
        self.turns = turn
        turn_flags: dict = {}
        steps: list[dict] = []
        sta0, hp0, rea0 = self.sta, self.hp, self.rea
        used_time = 0
        for fid in free_ids:
            act = idx[fid]
            notes = self._apply_effect(act, turn_flags)
            notes.append("自由动作：不占队列、不耗时间（§5.1）；代价走 REA/DOOM（§5.6 设计建议）")
            self.log(EVENT_STEP, {
                "turn": turn, "step": 0, "action": fid,
                "name": str(act.get("name", fid)), "group": str(act.get("group", "")),
                "channel": damage_channel(act), "time_cost": 0, "time_used": 0,
                "time_left": self.time_budget, "stat": None, "stat_value": 0,
                "hit_pct": None, "roll": None, "hit": None, "hit_detail": {},
                "damage": 0, "damage_detail": {"kind": "free"}, "is_attack": False,
                "incorporeal_blocked": False,
                "enemy_hp_before": self.hp, "enemy_hp": self.hp,
                "sta_before": self.sta, "sta": self.sta,
                "enemy_counter": skipped_counter(self.sta),
                "free": True, "notes": notes,
            })
        for i, aid in enumerate(sequence, start=1):
            # ⚠️ 这里**只**看"敌人倒了 / 逃了"，**不预检 STA**：
            #    STA ≤ 0 的玩家仍要**打出这一步并因此挨这一下反击**，
            #    否则 `last_stand` 的 -5 死亡线永远到不了（A5 的回归根因）。
            #    "这一步之后没有行动能力了"用**步末**的 break 表达（等价于旧实现
            #    `while` 循环里的 `if sta <= 0: break`）。
            if self.hp <= 0 or self.fled:
                break
            step = self._resolve_step(idx[str(aid)], i, turn, turn_flags, used_time)
            used_time = step["time_used"]
            steps.append(step)
            if self.sta <= 0:
                break
        ended = None
        if self.exorcised:
            ended = "exorcised"
        elif self.hp <= 0:
            ended = "enemy_down"
        elif self.fled:
            ended = "fled"
        elif self.sta <= 0:
            ended = "player_down"
        # ---- 驱魔判分（§5.4：每次提交 5 步，返回「有几步正确」）----
        exorcism_note = None
        tokens = [str(x) for x in (turn_flags.get("exorcism_tokens") or [])]
        if self.incorporeal and tokens:
            conf = exorcism_conf(self.table)
            size = conf["steps"]
            target = exorcism_target(self.state, self.rng, self.table)
            groups = [tokens[i:i + size] for i in range(0, len(tokens), size)]
            scores = [score_exorcism(g, target) for g in groups if len(g) == size]
            partial = len(groups[-1]) if groups and len(groups[-1]) != size else 0
            info = note_exorcism_try(self.state, scores, steps=size)
            exorcism_note = {**info,
                             "submitted": tokens,
                             "partial_tail": partial,
                             "note": ("提交不足 5 步，本回合不判分（§5.4「每次提交 5 步」）"
                                      if (partial and not scores) else
                                      f"有 {info['score']}/{info['steps']} 步正确（§5.4）")}
            if info["solved"]:
                self.exorcised = True
                self.exp_delta += conf["exp"]
                ended = "exorcised"
                exorcism_note["note"] = (f"驱魔成功：5/5 正确，额外 +{conf['exp']} EXP（§5.4）")
            self.log("combat_exorcism", {"turn": turn,
                                        **{k: v for k, v in exorcism_note.items()}})
        elif tokens:
            self.log("combat_exorcism", {"turn": turn, "submitted": tokens,
                                        "note": "Clap/Bow 只对幽灵有意义（§5.4 幽灵名单）；"
                                                "当前敌人不是幽灵，不判分"})
        turn_res = TurnResult(
            turn=turn, plan=plan.to_json(), steps=steps,
            enemy_hp_before=hp0, enemy_hp_after=self.hp,
            damage_dealt=max(0, hp0 - self.hp), damage_taken=max(0, sta0 - self.sta),
            sta_delta=self.sta - sta0, rea_delta=self.rea - rea0,
            time_spent=used_time, ended=ended, notes=[],
        )
        self.log(EVENT_TURN, {"turn": turn, "plan": plan.to_json(),
                              "damage_dealt": turn_res.damage_dealt,
                              "damage_taken": turn_res.damage_taken,
                              "sta": self.sta, "rea": self.rea, "enemy_hp": self.hp,
                              "time_spent": used_time, "ended": ended,
                              "exorcism": exorcism_note})
        self.turns_detail.append(turn_res.to_json())
        return turn_res

    # ------------------------------------------------------------ 整场

    def outcome(self, *, submitted: Sequence[str], free_actions: Sequence[str] = ()) -> Outcome:
        """把当前工作副本整理成 `Outcome`（含完整账本）。"""
        won = self.hp <= 0 or self.exorcised
        return Outcome(
            enemy=self.enemy_id, enemy_name=self.enemy_name, is_boss=self.is_boss,
            won=bool(won), fled=self.fled, exorcised=self.exorcised, hit_cap=self.hit_cap,
            incorporeal=self.incorporeal, turns=self.turns, time_spent=self.total_time,
            time_budget=self.time_budget, player_damage=self.total_damage,
            damage_taken=self.total_taken, sta_delta=self.deltas()["sta"],
            rea_delta=self.deltas()["rea"], doom_delta=self.doom_delta,
            exp_delta=self.exp_delta, funds_delta=self.funds_delta,
            item_take=list(self.item_take), item_grant=list(self.item_grant),
            tags_remove=list(self.tags_remove), sequence=list(submitted),
            free_actions=[str(x) for x in (free_actions or ())],
            exorcism=exorcism_public(self.state) if self.incorporeal else None,
            turns_detail=list(self.turns_detail), notes=list(self.notes),
            events=list(self.events),
        )

    def resolve(self, sequence: Any = None, *, free_actions: Sequence[str] = (),
                max_turns: int | None = None,
                repeat_sequence: bool = False) -> Outcome:
        """打到一方倒下 / 逃跑 / 驱魔成功 / 回合上限。

        ## `sequence` 给定时：**默认只打这一个回合**（`repeat_sequence=False`）

        第一版在"玩家给了队列、但一回合没打完"时会**自动改用 `default_sequence` 打下去**，
        于是一局 `resolve(sequence=["Kick","Kick"])` 会在第 2 回合自己换成 `LeaveOffering`
        并造成 5 点伤害 —— 玩家编排的队列被**静默接管**，而结果看起来像是"这次编排生效了"。
        这对"编排策略"这个核心乐趣是致命的（§5.6：「编排顺序正是 WoH 战斗区别于普通回合制的唯一理由」），
        而且它让"排 Kick 打幽灵 = 0 伤害"这种**本该一眼看出的结论**被自动接管掩盖。

        ⇒ 现在：给了 `sequence` 就只结算那一回合；回合结束后若未分胜负，
        在 `notes` 里如实写明"需要下一回合的新编排"，并由调用方决定下一步。
        玩家交互路径请直接用 `Fight.submit()` / `combat.submit()`（一次一回合）。
        `sequence=None`（自动路径，`loop.resolve_combat` 的既有调用）仍逐回合用
        `default_sequence` 打到底 —— 这条路径没有任何玩家编排可被接管。
        需要"同一队列重复打到底"的探针场景显式传 `repeat_sequence=True`。
        """
        cap = self.cap if max_turns is None else max(1, _as_int(max_turns, self.cap))
        player_sequence = list(sequence) if sequence else None
        first: list[str] | None = player_sequence
        if first is None:
            first, why = default_sequence(self.state, self.enemy, cfg=self.cfg,
                                         is_boss=self.is_boss, table=self.table)
            if why:
                self.notes.append(f"自动队列：{why}")
        submitted = list(first)
        turn = 0
        while turn < cap:
            # ⚠️ 入口**不预检 `self.sta <= 0`**，只在**本场结束后**用步末的检查收尾。
            #    理由见 `submit` 里步循环的注释：这是 A5 回归的根因 ——
            #    预检会让"已经掉到 0 以下但还没到 -5 死亡线"的玩家**从此不再挨任何伤害**，
            #    于是 `END_STA_ZERO` 不可达。
            if self.hp <= 0 or self.fled or self.exorcised:
                break
            turn += 1
            if turn == 1:
                seq = submitted
            elif repeat_sequence:
                seq = submitted
            elif player_sequence is not None:
                self.notes.append(
                    f"第 1 回合结束后战斗未结束（敌人 HP {self.hp}、玩家 STA {self.sta}）："
                    f"提交的队列只作用于一个回合（§5.1「每回合 200 时间单位」），"
                    f"需要**下一回合的新编排** —— 这里**不会**用自动队列接管玩家的编排")
                break
            else:
                seq = None
            if seq is None:
                seq, why = default_sequence(self.state, self.enemy, cfg=self.cfg,
                                            is_boss=self.is_boss, table=self.table)
                if not seq:
                    self.notes.append(f"第 {turn} 回合没有可用动作：{why}")
                    break
            try:
                self.submit(seq, turn=turn, free_actions=free_actions if turn == 1 else ())
            except QueueRejected as exc:
                # 自动路径不该撞上校验（default_sequence 已做前置检查）；
                # 玩家路径撞上就如实记账并停下，而不是"跳过继续打"。
                self.notes.append(f"第 {turn} 回合队列被拒：{'；'.join(exc.reasons)}")
                break
            if self.sta <= 0:
                # 等价于旧实现 `while` 循环里的 `if sta <= 0: break`：
                # **这一场的伤害已经结算完了**，本场到此为止；但**下一次**遭遇仍会照打
                # （由 `resolve_combat` 再调一次），所以 STA 会继续往 -5 的死亡线掉。
                self.notes.append(
                    f"本场在玩家 STA={self.sta}（≤0）时结束：已无行动能力"
                    f"（LAST STAND 缓冲带的死亡线由 tuning.sanitize.last_stand_floor 与"
                    f"`decide_endings` 判定，战斗本身不判死）")
                break
        if not (self.hp <= 0 or self.exorcised) and not self.fled and turn >= cap:
            self.hit_cap = True
            self.notes.append(f"到达回合上限 {cap}（tuning.combat.round_cap）仍未分出胜负"
                              f"—— 防死循环保护（COC-DESIGN §2.6）")
        if self.incorporeal:
            self.notes.append("对手是 incorporeal 幽灵：普通进攻动作完全无效（§5.4）；"
                              "本场的可行手段见账本里 combat_step.incorporeal_blocked")
        outcome = self.outcome(submitted=submitted, free_actions=free_actions)
        self.log(EVENT_COMBAT, {k: v for k, v in outcome.to_json().items()
                                if k != "turns_detail"})
        outcome.events = list(self.events)
        return outcome


# ---------------------------------------------------------------- 模块级入口

def resolve(state: dict, rng, enemy: dict, cfg: dict, *, sequence: Any = None,
            is_boss: bool = False, extra_dmg: int = 0,
            free_actions: Sequence[str] = (), max_turns: int | None = None,
            repeat_sequence: bool = False,
            table: dict | None = None, items: dict | None = None) -> Outcome:
    """一站式的整场战斗结算（`Fight` 的便捷入口；**不改** `state` 的资源字段）。

    `sequence` 给了就**只打一个回合**（见 `Fight.resolve` 的说明）。
    """
    f = Fight(state, rng, enemy, cfg, is_boss=is_boss, extra_dmg=extra_dmg,
              table=table, items=items)
    return f.resolve(sequence, free_actions=free_actions, max_turns=max_turns,
                     repeat_sequence=repeat_sequence)


def submit(state: dict, rng, enemy: dict, cfg: dict, sequence: Any, *,
           turn: int = 1, is_boss: bool = False, extra_dmg: int = 0,
           free_actions: Sequence[str] = (), table: dict | None = None,
           items: dict | None = None) -> tuple[TurnResult, dict, dict]:
    """**玩家交互路径**：只结算一个回合。

    返回 `(turn_result, deltas, public)`：
      - `turn_result`：这一回合每一步的账本记录（可直接显示）；
      - `deltas`：交给 `loop.Run.apply` 的增量（本回合的 STA/REA/… 变化）；
      - `public`：可安全展示的摘要（**不含驱魔答案**）。

    越界 ⇒ `QueueRejected`（理由在 `.reasons`）。
    """
    f = Fight(state, rng, enemy, cfg, is_boss=is_boss, extra_dmg=extra_dmg,
              table=table, items=items)
    tr = f.submit(sequence, turn=turn, free_actions=free_actions)
    public = {
        "enemy": f.enemy_id, "enemy_hp": f.hp, "enemy_hp_max": f.hp_max,
        "sta": f.sta, "rea": f.rea, "time_budget": f.time_budget,
        "time_left": f.time_budget - tr.time_spent, "turns": f.turns,
        "fled": f.fled, "exorcised": f.exorcised, "won": f.hp <= 0 or f.exorcised,
        "incorporeal": f.incorporeal,
        "exorcism": exorcism_public(state) if f.incorporeal else None,
        "ledger_events": len(f.events),
    }
    return tr, f.deltas(), public


# ---------------------------------------------------------------- 视图

def action_menu(state: dict, enemy: dict | None = None, *, cfg: dict,
                table: dict | None = None, items: dict | None = None) -> dict:
    """给前端/LLM 的**可选动作菜单**（§10.1 #3 的等价物：直接印出耗时/命中，不需要 hover）。

    每项：`{id, name, group, mvp, time_cost, base_time, stat, damage, free,
            requirements, effects, available, reject_reasons, _source}`。
    `available=False` 的动作**不隐藏**，而是带着 `reject_reasons` 一起给出来 ——
    这样"为什么这个动作现在不能排"本身就是可读信息（与 §5.2 的"情报有代价"一致）。
    """
    idx = action_index(table)
    ctx = queue_context(state, enemy or {}, table=table, items=items)
    mult, speed_hits = speed_multiplier(state, table=table)
    rows: list[dict] = []
    for aid, act in idx.items():
        stat_name, stat_val = action_stat(state, act, weapon=ctx["weapon"])
        cost = time_cost(act, state, cfg=cfg, table=table, speed=mult, stat_value=stat_val)
        dmg, dmg_detail = damage_value(act, state, weapon=ctx["weapon"], cfg=cfg, table=table)
        reasons = prereq_reasons(act, ctx, 0)
        rows.append({
            "id": aid, "name": str(act.get("name", aid)), "group": str(act.get("group", "")),
            "mvp": bool(act.get("mvp")), "base_time": _as_int(act.get("base_time"), 0),
            "time_cost": cost, "stat": stat_name, "stat_value": stat_val,
            "scale_coef": _as_int(act.get("scale_coef"), 0),
            "damage": dmg, "damage_detail": dmg_detail, "free": bool(act.get("free")),
            "is_attack": is_attack(act), "channel": damage_channel(act),
            "requirements": dict(act.get("requires") or {}),
            "effects": dict(act.get("effects") or {}),
            "available": not reasons, "reject_reasons": reasons,
            "_source": act.get("_source", ""),
        })
    return {"actions": rows, "speed_multiplier": mult, "speed_mods_applied": speed_hits,
            "time_budget": budget_of(cfg), "max_actions": max_actions_of(cfg),
            "weapon": ctx["weapon"], "incorporeal": ctx["facing_incorporeal"]}


# ---------------------------------------------------------------- 旧档兼容

#: 本模块引入的字段及默认值（读档补齐用；与 `tsf.DEFAULT_FIELDS` 同形）
DEFAULT_FIELDS: dict[str, Any] = {
    EXORCISM_KEY: {"target": [], "tries": 0, "solved": False, "last_score": None, "steps": 0},
}


def ensure_fields(state: dict) -> list[str]:
    """旧档兼容：缺 `exorcism` 就补一个**空**结构（不抽答案 —— 答案在首次遇到幽灵时才抽）。

    与 `tsf.ensure_fields` 同一手法（**不得抛异常**）。
    """
    filled: list[str] = []
    if not isinstance(state, dict):
        return filled
    node = state.get(EXORCISM_KEY)
    if not isinstance(node, dict) or not isinstance(node.get("target"), list):
        state[EXORCISM_KEY] = {"target": [], "tries": 0, "solved": False,
                               "last_score": None, "steps": 0}
        filled.append(EXORCISM_KEY)
    return filled

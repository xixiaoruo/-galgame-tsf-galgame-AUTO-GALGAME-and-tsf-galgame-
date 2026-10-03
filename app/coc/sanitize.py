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
import math
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

#: **引擎信任层**允许的键全集（= LLM 白名单 ∪ 确定性层独占键 ∪ 状态上限字段）。
#: 只有 `apply_delta(trust="engine")` / `apply_delta_engine()` 能用后者。
#: 注意 `sta_max` / `rea_max` 也在其中：`DETERMINISTIC_ONLY_KEYS` 已声明它们属
#: 确定性层（伤势/诅咒会改上限），引擎应能写；先前漏掉它们会被信任层误拒
#: —— 这是本测试第一次跑就抓到的（"未知键在两种信任层下都被拒"有一条红）。
ENGINE_ALLOWED_KEYS: tuple[str, ...] = tuple(sorted(
    set(LLM_DELTA_WHITELIST) | set(DETERMINISTIC_ONLY_KEYS)
    | {"stat", "sta_max", "rea_max"}
))


class SchemaViolation(RuntimeError):
    """白名单常量与 tuning.json 不一致（说明有人改了其中一处）。"""


class TrustViolation(ValueError):
    """**未被授权**的 delta 键试图进入状态。

    tsf-security 的独立复核发现（第二轮）：`clamp_turn_delta` 会把"非数值键"
    **原样透传**，而 `apply_delta` 自己不做白名单 —— 于是
    `apply_delta(st, {"ending": "通关"})` **真的能把结局写进去**。
    也就是说：当时的防线**完全依赖解析层**（`sanitize_llm_delta`），
    将来任何一条新路径把模型原值直接交给 `apply()` 就会**静默绕过**。

    ⇒ 现在把它变成**结构性的**：`apply_delta` 默认按 **LLM 信任层**校验键，
    含 `ending`/`tags_*`/`flag_set`/`doom`/`sta`/`rea` 一律**抛 TrustViolation**；
    引擎自己的结算必须显式走 `apply_delta_engine()`（或 `trust="engine"`）。
    """


def _assert_keys_allowed(delta: Any, trust: str, who: str = "") -> None:
    """按信任层校验 delta 的键；不合法的**抛错**而不是静默丢弃。

    为什么抛错而不是丢弃：丢弃会让"调用方走错了信任层"这件事被掩盖成"这次没生效"；
    而抛错会让它在**第一次被调用时就炸**，正是我们想要的。
    """
    if trust not in ("llm", "engine"):
        raise ValueError(f"未知信任层 {trust!r}（只允许 'llm' / 'engine'）")
    if not isinstance(delta, dict):
        return
    allowed = set(LLM_DELTA_WHITELIST) if trust == "llm" else set(ENGINE_ALLOWED_KEYS)
    illegal = sorted(str(k) for k in delta if k not in allowed)
    if not illegal:
        return
    hint = ""
    if trust == "llm":
        hint = ("；这些键由确定性层独占（COC-DESIGN §3.2 E2）。"
                "引擎结算请显式调用 apply_delta_engine()")
    raise TrustViolation(
        f"{who or 'apply_delta'}（trust={trust!r}）收到未授权键 {illegal}{hint}"
    )


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
    """把值转成 int；**任何非有限/非可转形态一律返回 None（调用方丢弃）**。

    ⚠️ 这里踩过一次真实缺陷（tsf-security 独立复核发现 D1）：
    第一版只捕获 `(TypeError, ValueError)`，而 **`float("inf")` 会在 `int()` 上抛
    `OverflowError`**（`int(round(float("inf")))` → OverflowError）——
    于是一个 `{"exp": Infinity}` 就能让**整回合异常**。

    **可达性是实测过的**：`json.loads('{"exp": Infinity}')` 是**成功**的，
    而 LLM 回包正是 `json.loads` 出来的 ⇒ 模型只要吐 `Infinity`，回合就炸。
    （`NaN` 反而安全：`int(round(nan))` 走 `ValueError` 被丢弃。**只有 ±Inf 崩。**）

    修法不是"补上 OverflowError 就完"——那只是补了一个洞。这里改成**两道**：
      ① 先 `math.isfinite()` 把非有限值直接判死（覆盖 inf/-inf/nan 及其各种来源）；
      ② 异常**不再枚举**：`except Exception` —— 本函数的契约是"转不成 int 就返回 None
         （调用方丢弃）"，所以**任何**转失败都该走这条路，而不是"我列全了已知异常"。

    为什么②要从枚举改成 `Exception`（t53 接手时实测的第二个洞）：枚举法永远只覆盖
    *已知* 的异常类型。只要有一个对象在 `__float__` / `__index__` 里抛**别的**
    异常（实测一个 `__float__` 抛 `RuntimeError` 的哨兵对象），`_as_int` 就会
    把它原样抛给上层 —— 与 D1 的 OverflowError 是**同一类**缺陷，只是换了个异常名。
    注意**不吞 `BaseException`**：KeyboardInterrupt / SystemExit 必须照常向上传播。

    配套完备性测试：`app/coc/tests/test_numeric_edge.py`（24 形态 × 3 入口 + 嵌套形态）。
    """
    if isinstance(v, bool):
        return None              # bool 是 int 的子类，但语义上不该出现在这里
    try:
        f = float(v)
    except Exception:
        return None
    if not math.isfinite(f):
        return None              # inf / -inf / nan 一律丢弃（**不抛异常**）
    try:
        return int(round(f))
    except Exception:
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
                clamp: bool = True, trust: str = "llm") -> dict:
    """**唯一写权威状态数值的函数**。返回 `{applied, dropped, notes}`。

    ## 信任层（第二轮复核后新增，这是结构性防线）

    | trust | 允许的键 | 谁用 |
    |---|---|---|
    | `"llm"`（**默认**） | `LLM_DELTA_WHITELIST`：`exp`/`funds`/`item_grant`/`item_take`/`stat` | 任何可能拿到**模型原值**的路径 |
    | `"engine"` | 上表 ∪ `doom`/`sta`/`rea`/`tags_grant`/`tags_remove`/`flag_set`/`ending` | 引擎自己的结算（经 `apply_delta_engine`） |

    **默认取"最不信任"的那一层**：调用方必须**显式**索取引擎权限，才可能写 `ending` / `doom`。
    未授权的键**抛 `TrustViolation`**（不是静默丢弃）——因为静默丢弃会把
    "调用方走错信任层"掩盖成"这次没生效"，而抛错会在第一次调用时就炸。

    > 为什么加这个：`clamp_turn_delta` 会把非数值键**原样透传**，所以在此之前的防线
    > **完全依赖解析层**（`sanitize_llm_delta` 先丢干净）。复核实测
    > `apply_delta(st, {"ending": "通关"})` 真能写进去 ⇒ 将来任何新路径把模型原值
    > 直接交给 `apply()` 就会静默绕过。现在这条路**必然失败**。
    """
    if not isinstance(delta, dict):
        return {"applied": {}, "dropped": ["<非 dict>"], "notes": []}

    _assert_keys_allowed(delta, trust, who="apply_delta")

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
        elif key in ("sta_max", "rea_max"):
            # 体力/理智**上限**（伤势·诅咒会削减，见 MECHANICS.md §2.3）。
            # 加这个分支是被测试逼出来的：先前 `sta_max` 在 DETERMINISTIC_ONLY_KEYS 里
            # 声明为"确定性层专属"，却既没进引擎白名单、也没有落账分支 ——
            # 于是它会静默掉进 `else: dropped`（"声明了但没人实现"）。
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


def apply_delta_engine(state: dict, delta: Any, cfg: dict, *,
                       clamp: bool = True) -> dict:
    """**引擎信任层**的写入入口：允许 `doom`/`sta`/`rea`/标签/flag/`ending`。

    命名是刻意的：它必须在代码里**显式出现**，这样"某处绕过了信任层"就变成
    一个能被 `grep` 与审查看见的事实，而不是"所有写入点长得一样"。

    引擎自身的结算（抽卡结果、章完成、战斗伤害、survey 刻度）都走这里；
    可能携带**模型原值**的路径必须走 `apply_delta`（默认 `trust="llm"`）。
    """
    return apply_delta(state, delta, cfg, clamp=clamp, trust="engine")

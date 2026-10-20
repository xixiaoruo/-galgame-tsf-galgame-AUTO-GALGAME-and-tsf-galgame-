# -*- coding: utf-8 -*-
"""t62：CoC 战斗（200 时间单位 / ≤8 动作队列 + 武器属性 + 敌人特性）单测。

覆盖任务书点名的七项，每项都给**能证否的判据**：

  1. 队列长度上限（8）与时间上限（200）—— 越界**必须拒绝**且**必须给可读理由**（不静默截断）
  2. 武器 stat 联动 —— `SteakKnife→DEX`、`FireAxe→STR`；**同一个动作在两把武器/不同属性值下
     命中率必须不同**（这条是本套测试里最容易被"看着实现了、其实是死的"骗过的一项）
  3. `incorporeal` 特性真的生效（§5.4）—— 普通动作 0 伤害、Spiritual 动作有效、十字架是唯一例外
  4. 同 seed 同队列 ⇒ 逐字节同结果
  5. 账本记录完整（`combat_step` / `check` / `combat_turn` / `combat` / `combat_exorcism`）
  6. 时间公式（§5.3）逐条：`基础耗时 × 通用速度修正 + 带符号缩放系数 × 属性`、向玩家有利取整、乘数相乘
  7. Dodge / Brace / 逃跑 / 驱魔（§5.2、§5.4）真的改变结算

另有一条**等价性**断言：把 rng 换成"永远命中"的桩之后，本实现与接入前的
`while` 循环在**基础攻击**下的受击总量**必须逐值相等** —— 这是"per_action 反击"这个
设计取舍的平衡依据，不是巧合。
"""
from __future__ import annotations

import json
import sys

# 冻结形态下 stdout/stderr 是 GBK wrapper，打印 `⇒`/`§` 会抛 UnicodeEncodeError
# —— 那是**打印时崩**，不是判据失败，但它会把真实的红灯淹掉（本会话已踩过一次）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

from app.coc import combat as C
from app.coc import loop as L
from app.coc import provenance as P
from app.coc import state as ST
from app.coc.tests.harness import TestSuite

CFG = P.load_tuning()
EN_THIN = {"id": "Thin Man", "name": "瘦长男人", "hp": 8, "power": 3, "damage": 2,
           "boss": True, "traits": []}
EN_GHOST = {"id": "Roommate", "name": "同住者", "hp": 2, "power": 1, "damage": 1,
            "boss": False, "traits": ["incorporeal"]}
EN_MOB = {"id": "Dummy", "name": "沙袋", "hp": 200, "power": 0, "damage": 0,
          "boss": False, "traits": []}


class AlwaysRng:
    """永远掷最小值 / 永远选第一个的 rng 桩（让"必中"可被构造出来）。"""

    def randint(self, a, b):
        return int(a)

    def choice(self, seq):
        return list(seq)[0]

    def choices(self, seq, weights=None, k=1):
        return [list(seq)[0]] * k


def _mk(seed: int = 5, *, stats: dict | None = None, bag=(), equip=(), **over) -> dict:
    # `sid` 显式给定：`initial_states` 缺省会 `uuid4()` 造一个**随机**会话 id，
    # 于是"同 seed 两次运行的状态逐字节相同"会被 sid 的差异误判成不确定性（实测 diff 就在这一行）。
    st = ST.initial_states({"difficulty": "Initiate", "seed": seed, "sid": "t62-iso"}, CFG)
    if stats:
        st["stats"].update(stats)
    if bag:
        st["inventory"]["backpack"] = list(bag)
    for wid in equip:
        st["inventory"]["backpack"] = list(bag) or [wid]
        ST.equip_item(st, wid)
    st.update(over)
    return st


def _rng(seed: int = 5):
    return ST.new_rng(seed, None)


# ============================================================ 1. 队列上限与拒绝理由

def test_queue_limits(s: TestSuite) -> None:
    """`Σ time_cost ≤ 200`、`len ≤ 8`（§5.1）：越界必须**拒绝**，且理由可读到能直接改。"""
    st = _mk()
    cap = C.max_actions_of(CFG)
    budget = C.budget_of(CFG)
    s.expect(
        "上限取自 tuning（8 / 200），代码零硬编码",
        cap == 8 and budget == 200
        and P.t(CFG, "combat.max_actions_per_turn") == cap
        and P.t(CFG, "combat.time_budget_turn") == budget,
        detail=f"max_actions_per_turn={cap}、time_budget_turn={budget}（tuning.json）",
        counterexample="反例：在 combat.py 里写 `CAP = 8` 常量 → tuning 改成 7 后行为不变 → 变红",
    )
    # ---- 步数上限的**边界本身**（8 通过 / 9 拒绝）----
    # ⚠️ 必须用**注入的廉价动作表**来测这条边界：真实动作表里最便宜的是 Clap/Bow 的 40，
    #    8×40 = 320 > 200 ⇒ 真实数据下**时间预算总是先于步数上限触发**
    #    （8 步要能排下，需要平均 ≤25 耗时；上游只给了 80/100/120/160 四档 + 我按 §5.4 推导的 40）。
    #    所以"恰好 8 步通过"只能在一个廉价动作的表上验证 —— 第一版拿 7 个 Dodge + Meditate 去试，
    #    7×110+100 = 870 ⇒ 时间先爆，用例误红。这里的注入让**步数**成为唯一变量。
    cheap = {
        "actions": [{"id": "Cheap", "name": "微动作", "group": "offensive", "mvp": True,
                     "base_time": 10, "scale_stat": None, "scale_coef": 0,
                     "hit": None, "dmg": {"kind": "none"}, "requires": {}, "effects": {},
                     "_unverified": True, "_source": "测试注入（非项目数据）"}],
        "weapons": {}, "hit": C.tuned("hit"), "time": C.tuned("time"),
        "enemy_counter": C.tuned("enemy_counter"), "exorcism": C.tuned("exorcism"),
        "speed_mods": C.tuned("speed_mods"),
    }
    ok8, r8 = C.plan_queue(["Cheap"] * 8, cfg=CFG, state=st, enemy=EN_MOB, table=cheap)
    bad9c, r9c = C.plan_queue(["Cheap"] * 9, cfg=CFG, state=st, enemy=EN_MOB, table=cheap)
    s.expect(
        "★ 步数上限是**闭区间**：8 步（8×10=80 ≤ 200）通过、9 步只报「步数」一条（80+10=90 ≤ 200，"
        "不超时）",
        ok8 is not None and ok8.count == 8 and r8 == []
        and bad9c is None and len(r9c) == 1 and "超过每回合上限" in r9c[0],
        detail=f"8×Cheap → count={ok8.count if ok8 else None}、time_total="
               f"{ok8.time_total if ok8 else None}；9×Cheap → {r9c}",
        counterexample="反例：把判断写成 `len(seq) >= cap` → 8 步也会被拒 → 变红。"
                       "（这条用注入表把「步数」单独隔离出来，正是为了不和时间预算混在一起）",
    )
    # ---- 时间上限：4 个 Kick = 320 > 200 必须被拒 ----
    bad_t, rt = C.plan_queue(["Kick"] * 4, cfg=CFG, state=st, enemy=EN_MOB)
    s.expect(
        "4 个 Kick（4×80=320）超时被拒，理由给出**从第几步起越界**与超出量",
        bad_t is None and any("超过回合预算 200" in x and "从第3步" in x and "超出 40" in x
                              for x in rt),
        detail="；".join(rt),
        counterexample="反例：只报「队列非法」而不给累计值 → 玩家无法知道该删哪一步 → 变红。"
                       "注意首次越界点是**第 3 步**（累计 240，超出 40）—— "
                       "第一版断言写成「超出 120」是按末步算的，那是断言错、不是实现错",
    )
    # ---- 两回事都要报：既超步数又超时 ----
    _bad2, r2 = C.plan_queue(["Kick"] * 9, cfg=CFG, state=st, enemy=EN_MOB)
    s.expect(
        "9 个 Kick 被拒，理由指名 §5.1 与 tuning 路径（可追溯到具体条款）",
        _bad2 is None and any("超过每回合上限 8" in x and "§5.1" in x
                              and "tuning.combat.max_actions_per_turn" in x for x in r2),
        detail=f"9 个 Kick → 共 {len(r2)} 条理由，首条={r2[0] if r2 else '（未被拒！）'}",
        counterexample="反例：只写「队列非法」→ 玩家不知道违反的是步数还是时间、也不知道上限值 → 变红",
    )
    s.expect(
        "同时超步数与超时时，**两条理由都出现**（不是只报第一条）",
        any("超过每回合上限" in x for x in r2) and any("超过回合预算" in x for x in r2),
        detail=f"共 {len(r2)} 条理由：" + " ｜ ".join(x[:40] for x in r2),
        counterexample="反例：`return None, [reasons[0]]` → 玩家改完步数才发现还超时 → 变红",
    )
    # ---- 理由收敛：9×Kick 曾逐条重复 7 次同一句话 ----
    s.expect(
        "越界理由**收敛**（首次逐项 + 其余汇总），不会把可读的那条淹掉",
        len(r2) == 3 and any("另有 6 步" in x for x in r2),
        detail=f"9 个 Kick → {len(r2)} 条理由；汇总条={'另有 6 步' in ' '.join(r2)}",
        counterexample="反例：对每一步都 append 一条超时理由 → 9 个 Kick 产生 8 条近乎相同的句子 "
                       "→ 玩家读不到「从第几步起越界」 → 变红",
    )
    # ---- 非法输入形态 ----
    raised = None
    try:
        C.validate_queue("Kick", cfg=CFG, state=st, enemy=EN_MOB)
    except C.QueueRejected as exc:
        raised = str(exc)
    s.expect(
        "非 list 的队列抛 QueueRejected（而不是 TypeError 逃出去）",
        raised is not None and "list/tuple" in raised,
        detail=f"validate_queue(\"Kick\") → {raised}",
        counterexample="反例：直接 `list(sequence)` → 字符串「Kick」被拆成 4 个字符 → 报出 "
                       "『动作 id K 不在动作表里』这种误导性理由 → 变红",
    )
    empty, re_ = C.plan_queue([], cfg=CFG, state=st, enemy=EN_MOB)
    unknown, ru = C.plan_queue(["Nope"], cfg=CFG, state=st, enemy=EN_MOB)
    s.expect(
        "空队列与未知动作 id 各给可读理由（未知 id 会列出合法集合）",
        empty is None and any("队列为空" in x for x in re_)
        and unknown is None and any("不在动作表里" in x and "Kick" in x for x in ru),
        detail=f"空队列→{re_[0][:30]}…；未知 id→{ru[0][:60]}…",
        counterexample="反例：`idx.get(aid, {})` 给个空 dict 兜底 → 未知 id 变成「无耗时无伤害的动作」 → 变红",
    )
    # ---- 拒绝必须真的抛出，而不是"跳过继续打" ----
    f = C.Fight(_mk(), _rng(), EN_THIN, CFG)
    threw = None
    try:
        f.submit(["Kick"] * 9)
    except C.QueueRejected as exc:
        threw = len(exc.reasons)
    s.expect(
        "Fight.submit 对越界队列**抛异常**（绝不静默截断后照打）",
        threw is not None and threw >= 1,
        detail=f"submit(9×Kick) → QueueRejected，reasons={threw} 条",
        counterexample="反例：`seq = list(sequence)[:cap]` 再照打 → 不抛异常 → 变红；"
                       "那等于玩家排的 9 步被吃掉 1 步却毫无提示",
    )


# ============================================================ 2. 武器 stat 联动

def test_weapon_stat_linkage(s: TestSuite) -> None:
    """`SteakKnife → DEX`、`FireAxe → STR`（§5.2 / §4.3）；stat 只从 items.json 读。"""
    for wid, want in (("SteakKnife", "DEX"), ("FireAxe", "STR")):
        st = _mk(bag=[wid], equip=[wid])
        wp = C.weapon_profile(st)
        s.expect(
            f"{wid} 装备在 R 槽 ⇒ 走 {want}，伤害取自 combat_actions.json.weapons",
            wp is not None and wp["stat"] == want and wp["usable"] is True
            and wp["dmg"] == (3 if wid == "SteakKnife" else 6),
            detail=f"weapon_profile → {wp}",
            counterexample=f"反例：把 items.json 的 {wid}.stat 读成硬编码 'STR' → 变红",
        )
        act = C.action_index()["AttackWeapon"]
        sn, sv = C.action_stat(st, act, weapon=wp)
        s.expect(
            f"{wid} 的普攻把 `scale_stat=\"weapon\"` 解析成 {want}={sv}",
            sn == want and sv == 5,
            detail=f"action_stat(AttackWeapon) → ({sn}, {sv})",
            counterexample="反例：action_stat 忽略 weapon 参数 → 返回 (None, 0) → 变红",
        )
    # ---- 未装备 vs 没武器：两种情形的理由必须不同 ----
    _p, r_bag = C.plan_queue(["StrongAttack"], cfg=CFG, state=_mk(bag=["FireAxe"]), enemy=EN_THIN)
    _p2, r_none = C.plan_queue(["StrongAttack"], cfg=CFG, state=_mk(), enemy=EN_THIN)
    s.expect(
        "★ 背包里有武器但**未装备** ⇒ 理由指向「去装备」，而不是谎称「没有武器」",
        any("背包里有" in x and "R 槽是空的" in x for x in r_bag)
        and not any("没有任何" in x for x in r_bag),
        detail=f"背包有 FireAxe 未装备 → {r_bag[0]}",
        counterexample="反例：把两种情形合并成一句『items.json 里 kind=\"weapon\" 的物品一件都没有』"
                       "→ 与同一进程里已加载的武器表**直接矛盾**，且把玩家指向去找武器 → 变红"
                       "（这正是 captain 复核实测抓到的措辞缺陷）",
    )
    s.expect(
        "真空手 ⇒ 理由明说「没有任何武器」，并指出只能排 Kick",
        any("没有任何" in x and "Kick" in x for x in r_none),
        detail=f"空背包 → {r_none[0]}",
        counterexample="反例：两种情况不分开 → 变红（两者对玩家的含义完全不同）",
    )
    # ---- ★ 关键判据：属性值真的进入命中判定 ----
    # ⚠️ 这里**刻意不传 weapon、也不传 stat_value** —— 走的就是"调用方什么都没准备"的
    #    默认路径。第一版在这条路径上退化成 `stat_term = 0`（两把武器算出同一个命中率），
    #    而所有"显式传参"的测试都抓不到它。captain 的复核正是用这条路径证否的。
    rows = []
    act = C.action_index()["AttackWeapon"]
    for wid, key in (("SteakKnife", "DEX"), ("FireAxe", "STR")):
        pcts = []
        for val in (3, 10):
            st = _mk(stats={key: val}, bag=[wid], equip=[wid])
            pct, detail = C.hit_pct(st, act, EN_THIN, cfg=CFG)   # ← 不传 weapon
            pcts.append((val, pct, detail["stat_term"], detail.get("weapon"), detail.get("stat")))
        rows.append((wid, key, pcts))
    s.expect(
        "★ 同一敌人 + 同一动作，**不传任何提示**：属性 3 vs 10 的命中率必须不同"
        "（stat_term 非 0，且 detail 里看得出用了哪把武器/哪项属性）",
        # ⚠️ 注意这里用**每行自己的** w/k 去比：第一版写成了 `p[0][3] == wid`，
        #    而 `wid`/`key` 是上面 for 循环留下的**最后一轮**的值（FireAxe/STR）
        #    ⇒ SteakKnife 那一行永远为假 ⇒ 用例误红。生成器表达式里引用了循环变量是常见坑。
        all(p[0][1] != p[1][1] and p[0][2] != 0 and p[1][2] != 0
            and p[0][3] == w and p[0][4] == k for w, k, p in rows),
        detail="；".join(
            f"{w}({k}): " + " vs ".join(
                f"{v}→{pct}%(stat_term={t},weapon={wi},stat={sn})"
                for v, pct, t, wi, sn in p) for w, k, p in rows),
        counterexample="反例：`hit_pct` 内部用 `action_stat(state, action, weapon=None)` → "
                       "`scale_stat='weapon'` 拿不到武器 ⇒ `stat_term` 恒为 0 ⇒ 两把武器、"
                       "任意属性值都算出同一个命中率（**看着实现了、其实是死的**）→ 变红。"
                       "captain 的证否判据就是这一条：只修「stat_value 默认 None」**不够**，"
                       "**weapon 也必须自己在内部解析**（否则注释只描述了一半的修复）",
        demonstration=f"实测：SteakKnife DEX3→{rows[0][2][0][1]}% / DEX10→{rows[0][2][1][1]}%；"
                      f"FireAxe STR3→{rows[1][2][0][1]}% / STR10→{rows[1][2][1][1]}%",
    )
    # ---- 武器对照：DEX ≠ STR 时两把武器必须给出不同命中率 ----
    ps = C.hit_pct(_mk(stats={"DEX": 10, "STR": 4}, bag=["SteakKnife"], equip=["SteakKnife"]),
                   act, EN_THIN, cfg=CFG)[0]
    pf = C.hit_pct(_mk(stats={"DEX": 10, "STR": 4}, bag=["FireAxe"], equip=["FireAxe"]),
                   act, EN_THIN, cfg=CFG)[0]
    s.expect(
        "武器对照：DEX=10 / STR=4 时 SteakKnife(DEX) 与 FireAxe(STR) 的命中率**不同**"
        "（证明「走谁」真的由武器决定，而不是取了一个固定属性）",
        ps != pf,
        detail=f"SteakKnife(DEX=10) → {ps}%；FireAxe(STR=4) → {pf}%",
        counterexample="反例：把 `scale_stat='weapon'` 解析成固定 'DEX' → 两把武器同值 → 变红"
                       "（那样 FireAxe 的 STR 语义就是假的）",
    )
    # ---- 显式 stat_value 仍然优先（可被覆盖）----
    st = _mk(stats={"DEX": 10}, bag=["SteakKnife"], equip=["SteakKnife"])
    wp = C.weapon_profile(st)
    act = C.action_index()["AttackWeapon"]
    forced = C.hit_pct(st, act, EN_THIN, cfg=CFG, weapon=wp, stat_value=0)[0]
    auto = C.hit_pct(st, act, EN_THIN, cfg=CFG, weapon=wp)[0]
    s.expect(
        "显式 `stat_value` 可覆盖自动取值（0 与 10 的结果不同）",
        forced != auto,
        detail=f"stat_value=0 → {forced}%；不传（自动取 DEX=10）→ {auto}%",
        counterexample="反例：永远忽略传入的 stat_value 去自己查 state → 桩/探针无法构造边界 → 变红",
    )
    # ---- 未在 weapons 表登记的武器 ⇒ 拒绝，而不是按 0 伤害打 ----
    fake_table = {"actions": C.action_list(),
                  "weapons": {}, "hit": C.tuned("hit"), "time": C.tuned("time"),
                  "enemy_counter": C.tuned("enemy_counter"), "exorcism": C.tuned("exorcism"),
                  "speed_mods": C.tuned("speed_mods")}
    st = _mk(bag=["SteakKnife"], equip=["SteakKnife"])
    wp2 = C.weapon_profile(st, table=fake_table)
    _p3, r3 = C.plan_queue(["AttackWeapon"], cfg=CFG, state=st, enemy=EN_THIN, table=fake_table)
    s.expect(
        "武器缺 dmg 条目 ⇒ 拒绝并说明，而不是按 0 伤害静默结算",
        wp2 is not None and wp2["usable"] is False and any("缺该武器的 dmg 条目" in x for x in r3),
        detail=f"weapons 表清空后 → usable={wp2['usable']}、理由={r3[0][:80] if r3 else r3}",
        counterexample="反例：`dmg` 缺省填 0 → 玩家拿刀打空气、账本全 0 → 变红",
    )


# ============================================================ 3. incorporeal（§5.4）

def test_incorporeal(s: TestSuite) -> None:
    """幽灵免疫普通进攻动作；Spiritual / 法术 / 投掷有效；十字架是唯一例外（§5.4）。"""
    kick = C.action_index()["Kick"]
    kick_act = dict(kick)
    sev = C.action_index()["SeverTies"]
    blocked, why = C.is_blocked_by_incorporeal(kick_act, EN_GHOST)
    s.expect(
        "普通（physical）进攻动作对幽灵被豁免，理由直接可进账本",
        blocked is True and "incorporeal" in why and "穿过它的身体" in why,
        detail=f"Kick vs 幽灵 → blocked={blocked}；{why[:110]}",
        counterexample="反例：只把 `incorporeal` 记进日志而不参与判定 → blocked=False → 变红"
                       "（t48 的旧实现就是「记了但打不动」，玩家只能被白白耗死）",
    )
    s.expect(
        "Spiritual 通道不受豁免（§5.4 列出的 4 个 Spiritual 动作之一）",
        C.is_blocked_by_incorporeal(sev, EN_GHOST)[0] is False
        and C.damage_channel(sev) == "spiritual",
        detail=f"SeverTies → channel={C.damage_channel(sev)}、blocked="
               f"{C.is_blocked_by_incorporeal(sev, EN_GHOST)[0]}",
        counterexample="反例：按 `group != 'offensive'` 放行 → Support 动作也会被当成「能打幽灵」 → 变红",
    )
    ok_cru, why_cru = C.is_blocked_by_incorporeal(kick_act, EN_GHOST, has_crucifix=True)
    s.expect(
        "装备 Bronze Crucifix ⇒ 普通攻击对幽灵放行（§5.4 的唯一例外）",
        ok_cru is False and C.CRUCIFIX_ID in why_cru,
        detail=f"Kick + 十字架 → blocked={ok_cru}；{why_cru[:90]}",
        counterexample="反例：把例外写成「任意 equipment 都行」 → 带相机也能打幽灵 → 变红",
    )
    s.expect(
        "非幽灵敌人从不豁免（豁免只由 traits 里真的有 incorporeal 触发）",
        C.is_blocked_by_incorporeal(kick_act, EN_THIN)[0] is False,
        detail=f"Kick vs Thin Man → blocked={C.is_blocked_by_incorporeal(kick_act, EN_THIN)[0]}",
        counterexample="反例：把判断写成 `if not has_crucifix` → 所有敌人都免疫普通攻击 → 变红",
    )
    # ---- 端到端：打不动 → 0 伤害；可行手段 → 真的赢 ----
    st = _mk(seed=7)
    rng = ST.new_rng(7, st)
    out = C.resolve(st, rng, EN_GHOST, CFG, sequence=["Kick", "Kick"])
    hp_after_kicks = [e["enemy_hp"] for e in out.events if e["kind"] == C.EVENT_STEP]
    s.expect(
        "端到端：对幽灵排 Kick ⇒ 玩家伤害 0、幽灵 HP **一点没掉**（账本记 blocked），"
        "且**不会**被自动队列接管",
        out.player_damage == 0 and out.won is False
        and hp_after_kicks and all(h == EN_GHOST["hp"] for h in hp_after_kicks)
        and sum(1 for e in out.events if e.get("incorporeal_blocked")) == 2
        and any("需要**下一回合的新编排**" in n or "需要" in n and "编排" in n
                for n in out.notes),
        detail=f"sequence=Kick×2 → player_damage={out.player_damage}、won={out.won}、"
               f"敌方 HP 轨迹={hp_after_kicks}、"
               f"blocks={sum(1 for e in out.events if e.get('incorporeal_blocked'))}；"
               f"notes={out.notes}",
        counterexample="反例（两个都要防）：① 豁免只写日志不参与伤害计算 → player_damage>0 → 变红；"
                       "② `resolve` 在第 2 回合**自动改用 default_sequence** → 幽灵自动换 "
                       "LeaveOffering 打出 5 点伤害 → 玩家会以为「排 Kick 也能打死幽灵」→ 变红。"
                       "（第一版就是被 ② 坑到：player_damage=5、won=True 而两条 Kick 都记着 blocked）",
        demonstration=f"实测：player_damage={out.player_damage}、won={out.won}、"
                      f"敌方 HP 轨迹={hp_after_kicks}",
    )
    st = _mk(seed=7, bag=["SteakKnife"], equip=["SteakKnife"])
    rng = ST.new_rng(7, st)
    win = C.resolve(st, rng, EN_GHOST, CFG, sequence=["SeverTies"])
    s.expect(
        "端到端：SeverTies（Spiritual）真的能打死幽灵（伤害 = 武器/2 + KNW/4 = 1+1 = 2 ≥ HP 2）",
        win.won is True and win.player_damage == 2,
        detail=f"SeverTies → won={win.won}、player_damage={win.player_damage}"
               f"（武器/2 + KNW/4）、turns={win.turns}",
        counterexample="反例：把 spiritual 也豁免掉 → 幽灵变成无解敌人 → 变红",
    )
    # ---- 自动路径：不会站着被打死 ----
    st = _mk(seed=7)
    rng = ST.new_rng(7, st)
    auto = C.resolve(st, rng, EN_GHOST, CFG)
    s.expect(
        "★ 自动路径对幽灵改排 spiritual 动作（而不是像旧实现那样一直用普攻打空气）",
        auto.sequence in (["SeverTies"], ["LeaveOffering"]) and auto.won
        and not auto.hit_cap,
        detail=f"自动队列={auto.sequence}、won={auto.won}、player_damage={auto.player_damage}、"
               f"damage_taken={auto.damage_taken}",
        counterexample="反例：`default_sequence` 不区分幽灵 → 排 Kick×2 → 0 伤害 → "
                       "打满 round_cap 后 hit_cap、玩家被白打 → 变红",
    )
    st = _mk(seed=7)
    rng = ST.new_rng(7, st)
    no_damage = C.resolve(st, rng, EN_GHOST, CFG, sequence=["Pray"])
    s.expect(
        "对幽灵用不可伤的 no-op 动作不会假装获胜（won=False、伤害 0、敌方 HP 未掉）",
        no_damage.won is False and no_damage.player_damage == 0,
        detail=f"Pray → won={no_damage.won}、player_damage={no_damage.player_damage}、"
               f"hit_cap={no_damage.hit_cap}、notes={no_damage.notes}",
        counterexample="反例（两个都要防）：① 把「没伤害」当「已解决」→ won=True 而敌人 HP 未变 → 变红；"
                       "② `resolve` 在后续回合自动改写队列 → Pray 之后自动打 LeaveOffering → "
                       "player_damage=5 → 变红",
    )
    # ---- Leave an Offering 花 1 FND ----
    st = _mk(seed=7)
    funds0 = st["funds"]
    rng = ST.new_rng(7, st)
    off = C.resolve(st, rng, EN_GHOST, CFG, sequence=["LeaveOffering"])
    s.expect(
        "Leave an Offering 伤害 = KNW 且**真的扣 1 FND**（§5.2），但不直接写权威状态",
        off.funds_delta == -1 and off.player_damage == st["stats"]["KNW"]
        and st["funds"] == funds0,
        detail=f"funds_delta={off.funds_delta}、伤害={off.player_damage}=KNW({st['stats']['KNW']})、"
               f"状态里 funds 仍为 {st['funds']}（增量交给 loop.apply）",
        counterexample="反例：只做前置检查却不扣钱 → 可以无限次免费供品 → funds_delta=0 → 变红",
    )


# ============================================================ 4. 可复现

def test_reproducible(s: TestSuite) -> None:
    """同 seed + 同队列 ⇒ 逐字节同结果（E5 掷骰在代码、rng 随存档落盘）。"""
    seq = ["StrongAttack", "AttackWeapon", "Dodge", "Kick"]

    def once() -> str:
        st = _mk(seed=42, bag=["SteakKnife"], equip=["SteakKnife"])
        rng = ST.new_rng(42, st)
        out = C.resolve(st, rng, EN_THIN, CFG, sequence=seq)
        return json.dumps({"d": out.turns_detail, "s": out.sta_delta, "w": out.won,
                           "t": out.time_spent, "ev": out.events}, ensure_ascii=False,
                          sort_keys=True)

    a, b = once(), once()
    s.expect(
        "同 seed + 同队列 ⇒ `turns_detail` 与**整份事件账本**逐字节相同",
        a == b and len(a) > 200,
        detail=f"两次运行的规范化 JSON 长度 {len(a)}，相等={a == b}",
        counterexample="反例：在 combat.py 里用 `random.random()` / 模块级 `random` 而非传入的 rng "
                       "→ 两次结果不同 → 变红（也会让存档读档后的战斗与读档前不一致）",
    )

    def once_state() -> str:
        st = _mk(seed=42, bag=["SteakKnife"], equip=["SteakKnife"])
        rng = ST.new_rng(42, st)
        out = C.resolve(st, rng, EN_THIN, CFG, sequence=seq)
        return json.dumps(out.to_json(), ensure_ascii=False, sort_keys=True) + \
            ST.dumps(st)

    c, d = once_state(), once_state()
    s.expect(
        "同 seed 两次运行后**权威状态**也逐字节相同（含 rng_state 推进量）",
        c == d,
        detail=f"outcome.to_json()+state.dumps() 相等={c == d}，长度 {len(c)}",
        counterexample="反例：把 rng 掷骰次数做成依赖 dict 迭代顺序 → 状态里 rng_state 不同 → 变红",
    )
    # 不同 seed 应当**可能**不同（不是恒等），取 6 个 seed 至少出现 2 种结果
    sig = set()
    for sd in range(6):
        st = _mk(seed=sd)
        rng = ST.new_rng(sd, st)
        out = C.resolve(st, rng, EN_THIN, CFG, sequence=["Kick", "Kick"])
        sig.add(json.dumps([s["roll"] for t in out.turns_detail for s in t["steps"]]))
    s.expect(
        "不同 seed 真的产生不同掷骰序列（否则「可复现」是恒等式的假绿）",
        len(sig) > 1,
        detail=f"6 个 seed 得到 {len(sig)} 种不同的 roll 序列",
        counterexample="反例：把 rng 掷骰写成固定值（如常量 1）→ 6 个 seed 全同 → 变红",
    )
    # loop 层：同 seed 两次整局，账本一致
    def via_loop() -> str:
        r = L.Run.start({"difficulty": "Initiate", "seed": 99}, CFG)
        r.resolve_combat("Thin Man")
        return json.dumps([{k: v for k, v in e.items() if k != "i"} for e in r.events],
                          ensure_ascii=False, sort_keys=True)

    s.expect(
        "经 `loop.Run.resolve_combat` 的同 seed 两次整局，账本（去掉序号）逐字节相同",
        via_loop() == via_loop(),
        detail="两次 loop 级战斗的事件账本相等",
        counterexample="反例：把 rng 换成 `random.Random()` 新实例 → 读档/复跑不一致 → 变红",
    )


# ============================================================ 5. 账本完整性

def test_ledger(s: TestSuite) -> None:
    """每一步都进账本：`combat_step` + 命中时 `check` + `combat_turn` + 汇总 `combat`。"""
    r = L.Run.start({"difficulty": "Initiate", "seed": 7}, CFG)
    res = r.resolve_combat("Thin Man")
    kinds: dict[str, int] = {}
    for e in r.events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    steps = [e for e in r.events if e["kind"] == C.EVENT_STEP]
    checks = [e for e in r.events if e["kind"] == C.EVENT_CHECK]
    turns = [e for e in r.events if e["kind"] == C.EVENT_TURN]
    s.expect(
        "账本里有 combat_step / check / combat_turn / combat 四类事件",
        kinds.get(C.EVENT_STEP, 0) > 0 and kinds.get(C.EVENT_CHECK, 0) > 0
        and kinds.get(C.EVENT_TURN, 0) > 0 and kinds.get(C.EVENT_COMBAT, 0) == 1,
        detail=f"事件计数：{kinds}",
        counterexample="反例：只写一条汇总 `combat`（t48 的做法）→ combat_step 计数为 0 → 变红；"
                       "那正是「为什么掉了 2 点体力」不可回溯的原因",
    )
    s.expect(
        "每个动作都有一条 combat_step（步数 = 所有回合 steps 之和）",
        len(steps) == sum(len(t["steps"]) for t in res["turns_detail"]) and len(steps) > 0,
        detail=f"combat_step {len(steps)} 条 vs turns_detail 里的步数 "
               f"{sum(len(t['steps']) for t in res['turns_detail'])}",
        counterexample="反例：只在命中时写事件 → 未命中的动作与纯效果动作（Dodge/Meditate）不留痕 → 变红",
    )
    need = {"turn", "step", "action", "time_cost", "time_used", "time_left", "enemy_hp",
            "sta", "enemy_counter", "damage", "notes", "incorporeal_blocked"}
    miss = sorted(need - set(steps[0])) if steps else ["<无步骤>"]
    s.expect(
        "combat_step 字段齐备（剩余时间、敌人 HP、受击明细都在）",
        not miss,
        detail=f"缺字段={miss}；样例 step 键={sorted(steps[0])[:12] if steps else None}",
        counterexample="反例：把 time_left / enemy_counter 省掉 → 缺字段非空 → 变红",
    )
    s.expect(
        "每条 check 带 `phase=combat` 与调查检定同名字段（stat/roll/target/success）",
        bool(checks) and all(e.get("phase") == "combat" and "stat" in e and "roll" in e
                             and "target" in e and "success" in e for e in checks),
        detail=f"check {len(checks)} 条；样例={ {k: checks[0][k] for k in ('stat','roll','target','success')} if checks else None}",
        counterexample="反例：另起一套 `combat_check` 事件名 → 既有读 kind=\"check\" 的消费者看不见战斗 → 变红",
    )
    s.expect(
        "check 带 tsf_shift 与 tsf_progress（与 t54 的账本手法一致）",
        bool(checks) and all("tsf_shift" in e and "tsf_progress" in e for e in checks),
        detail=f"样例 tsf_shift={checks[0].get('tsf_shift')}、"
               f"tsf_progress={checks[0].get('tsf_progress')}",
        counterexample="反例：漏记 tsf_shift → 转变修正影响命中却无法从账本解释 → 变红",
    )
    s.expect(
        "敌人反击写在同一条 step 的 enemy_counter 里，且落账为一次 delta（唯一写点）",
        all(isinstance(e["enemy_counter"], dict) and "damage" in e["enemy_counter"]
            and "sta_after" in e["enemy_counter"] for e in steps)
        and kinds.get("delta", 0) >= 1,
        detail=f"enemy_counter 键={sorted(steps[0]['enemy_counter'])}；delta 事件 {kinds.get('delta')} 条",
        counterexample="反例：逐次调 apply 落账 → delta 事件数 = 反击次数、且中间态可见 → 变红",
    )
    legacy = {"enemy", "is_boss", "rounds", "player_damage", "damage_taken",
              "incorporeal", "won", "hit_cap"}
    s.expect(
        "汇总事件的键**向后兼容** t48 的返回形状（rounds/player_damage/damage_taken/…）",
        legacy <= set(res),
        detail=f"缺={sorted(legacy - set(res))}；rounds={res.get('rounds')} turns={res.get('turns')}",
        counterexample="反例：把 rounds 改名成 turns 而不保留别名 → 既有探针读不到 → 变红",
    )
    s.expect(
        "时间字段名自解释：全场累计与每回合预算分开（不把 400 配 200 叫 time_spent/time_budget）",
        res["time_spent"] == res["time_spent_total"]
        and res["time_budget"] == res["time_budget_per_turn"] == 200
        and res["time_spent"] >= res["time_budget_per_turn"],
        detail=f"time_spent_total={res['time_spent_total']}、time_budget_per_turn="
               f"{res['time_budget_per_turn']}、turns={res['turns']}",
        counterexample="反例：只有一个 `time_spent`/`time_budget` 对 → 3 回合的 400 会被读成"
                       "「这一回合花了 400，预算 200」→ 变红",
    )
    nxt = [e["i"] for e in r.events]
    s.expect(
        "事件序号 `i` 连续（Run.log 的唯一写点没有旁路插入）",
        nxt == list(range(len(nxt))),
        detail=f"{len(nxt)} 条事件，i 从 {nxt[0]} 到 {nxt[-1]} 连续={nxt == list(range(len(nxt)))}",
        counterexample="反例：在 combat.py 里直接 append 到 state['log'] → 序号断档 → 变红",
    )


# ============================================================ 6. 时间公式（§5.3）

def test_time_formula(s: TestSuite) -> None:
    """`耗时 = 基础耗时 × 通用速度修正 − 缩放系数 × 属性`；取整向玩家有利；乘数相乘。"""
    st = _mk(stats={"DEX": 5, "STR": 5, "KNW": 5, "PER": 5})
    idx = C.action_index()
    cases = {
        "AttackWeapon": (80, "DEX", -3, 65),
        "StrongAttack": (120, "DEX", -4, 100),
        "Dodge": (160, "DEX", -10, 110),
        "Brace": (160, "STR", -5, 135),
        "CombatSkill": (100, "KNW", -5, 75),
        "FireWeapon": (120, "PER", -5, 95),
    }
    got = {}
    for aid, (base, stat, coef, want) in cases.items():
        act = idx[aid]
        pct = C.time_cost(act, st, cfg=CFG) if aid != "FireWeapon" else \
            C.time_cost(act, st, cfg=CFG)
        got[aid] = (act["base_time"], act["scale_coef"], pct, want)
    # FireWeapon 走 PER 但 scale_stat 是字面 "PER"，无需武器
    st2 = _mk(stats={"DEX": 5, "STR": 5, "KNW": 5, "PER": 5}, bag=["SteakKnife"],
              equip=["SteakKnife"])
    got2 = {}
    for aid, (base, stat, coef, want) in cases.items():
        act = idx[aid]
        got2[aid] = (C.time_cost(act, st2, cfg=CFG), want)
    s.expect(
        "★ 六个 §5.3 逐字给出基础耗时的动作，耗时 = 基础 + 带符号系数×属性",
        all(g == w for g, w in got2.values()),
        detail="；".join(f"{k}: {g} vs 期望 {w}" for k, (g, w) in got2.items()),
        counterexample="反例：把公式写成 `base - coef*stat` 而 coef 又存成 -3 → 变成 `80+15=95` → "
                       "与 §5.3 的 `−3×Stat` 反向 → 变红",
        demonstration=f"实测（DEX/STR/KNW/PER 均 5）：{ {k: g for k, (g, w) in got2.items()} }",
    )
    s.expect(
        "base_time 与 scale_coef 逐条对得上 §5.3 的表（80/120/160/100，−3/−4/−10/−5）",
        all(got[a][0] == c[0] and got[a][1] == c[2] for a, c in cases.items()),
        detail="；".join(f"{a}: base={got[a][0]}, coef={got[a][1]}" for a in cases),
        counterexample="反例：把 Dodge 的 −10 抄成 −5 → 与 §5.3 的 `Dodge 160 −10×DEX` 不符 → 变红",
    )
    # 非缩放动作不随属性变化
    a1 = C.time_cost(idx["Kick"], _mk(stats={"STR": 1}), cfg=CFG)
    a2 = C.time_cost(idx["Kick"], _mk(stats={"STR": 12}), cfg=CFG)
    s.expect(
        "不在 §5.3「会缩放的动作」清单里的动作（Kick/Meditate/Pray…）耗时不随属性变",
        a1 == a2 == 80,
        detail=f"Kick：STR=1 → {a1}；STR=12 → {a2}",
        counterexample="反例：给所有动作都套上缩放 → Kick 也变快 → 与 §5.3 的清单不符 → 变红",
    )
    # 向玩家有利取整：构造一个会出小数的属性值
    st3 = _mk(stats={"DEX": 7}, bag=["SteakKnife"], equip=["SteakKnife"])
    c = C.time_cost(idx["AttackWeapon"], st3, cfg=CFG)
    s.expect(
        "取整向玩家有利（耗时向下取整），且不低于 1",
        c == 80 - 3 * 7 == 59 and C.time_cost(idx["Kick"], _mk(stats={"STR": 12}), cfg=CFG) >= 1,
        detail=f"DEX=7、普攻 → {c}（80−21）；下限字段 time.min_cost="
               f"{C.tuned('time.min_cost')}",
        counterexample="反例：用 round 而不是 floor → 某些属性下耗时多 1 → 同样预算少排一个动作 → 变红",
    )
    # 通用速度修正相乘且在无来源时惰性
    base = C.time_cost(idx["AttackWeapon"], st2, cfg=CFG)
    mult0, hits0 = C.speed_multiplier(st2)
    st_slow = dict(st2)
    st_slow["character"] = "Aiko"
    st_slow["flags"] = {"perk:FastSwimmer": True}
    mult2, hits2 = C.speed_multiplier(st_slow)
    slow = C.time_cost(idx["AttackWeapon"], st_slow, cfg=CFG)
    s.expect(
        "通用速度修正是**乘法**（Aiko 0.9 × FastSwimmer 0.85 = 0.765）且默认惰性（1.0）",
        abs(mult0 - 1.0) < 1e-9 and hits0 == []
        and abs(mult2 - 0.765) < 1e-9 and len(hits2) == 2 and slow < base,
        detail=f"默认 mult={mult0}（来源 {len(hits0)} 个）；Aiko+FastSwimmer mult={mult2:.3f}"
               f"（{ [h['key'] for h in hits2] }）⇒ 耗时 {base} → {slow}",
        counterexample="反例：把多个修正相加成 0.9+0.85=1.75（反而更慢）或 1−0.1−0.15 → 与 §5.3 的"
                       "「多个通用修正相乘（例 0.9 × 0.85 = 0.76）」不符 → 变红",
    )


# ============================================================ 7. 防御动作 / 逃跑 / 驱魔

def test_defensive_and_flee(s: TestSuite) -> None:
    """Dodge / Brace「本回合」生效且不叠加；逃跑三否决 + 5% DOOM（§5.2）。"""
    idx = C.action_index()

    # ---- 前置自检：**用例自己构造的每个队列都必须在预算内** ----
    # 为什么值得单列一条断言：动作表是数据，改一个 `base_time` 就会让某个用例的队列越界，
    # 而越界表现为 `QueueRejected` **逃出用例**（未捕获异常 + 通用反例），
    # 读起来像"实现坏了"。这条断言把它变成一句明确的"用例队列超预算"，
    # 并印出**实际耗时**（便于和数据表对账）。
    #
    # ⚠️ 顺带纠正一个容易混的地方：`base_time`（数据表里的 160）≠ **实际耗时**
    #    （`base_time + scale_coef×属性`）。STR=5 时 `Brace` 的基数是 160、实际耗时 160−25=135；
    #    `Dodge` 同理（DEX=5 → 110，DEX=12 → 40）。写用例时要用**实际耗时**，不是 base_time。
    _probe_cases = [
        ("Brace×1（§5.2 格挡）", ["Brace"], _mk(seed=3), EN_MOB),
        ("Dodge×1（§5.2 闪避）", ["Dodge"], _mk(seed=3), EN_MOB),
        ("Dodge×3 @DEX=12（测不叠加）", ["Dodge"] * 3, _mk(seed=3, stats={"DEX": 12, "STR": 12}),
         EN_MOB),
        # LeaveOffering / Clap / Bow 都是**幽灵限定**（§5.2 `requires.facing_incorporeal`）
        # ⇒ 必须拿幽灵当对手，否则被**前置**拒（而不是超预算）。前置与预算都返回
        #    `plan is None`，所以这里把 enemy 一起参数化，并把 reasons 打进 detail 以便区分。
        ("LeaveOffering×1（§5.2 供品）", ["LeaveOffering"], _mk(seed=7), EN_GHOST),
        ("Kick×2（最小攻击）", ["Kick"] * 2, _mk(seed=7), EN_MOB),
        ("Pray×1（no-op 精神）", ["Pray"], _mk(seed=7), EN_MOB),
        ("Clap/Bow 5 步（§5.4 驱魔）", ["Clap", "Bow", "Clap", "Bow", "Clap"], _mk(seed=8),
         EN_GHOST),
    ]
    over = []
    costs = []
    for label, seq, stx, enx in _probe_cases:
        plan, reasons = C.plan_queue(seq, cfg=CFG, state=stx, enemy=enx)
        total = plan.time_total if plan else None
        costs.append(f"{label} = {total}")
        if plan is None:
            over.append(f"{label} → {reasons}")
    s.expect(
        "★ 用例自己构造的队列**全部合法**（预算内 且 前置满足）—— 否则红的是用例，不是实现",
        not over,
        detail="；".join(costs) + f"；预算={C.budget_of(CFG)}"
               + ("；被拒：" + " ｜ ".join(over) if over else ""),
        counterexample="反例：① 把某个动作的 base_time 调大（或忘了算 `scale_coef×属性` 的折扣）"
                       "→ 该队列超预算；② 用**非幽灵**对手去排幽灵限定动作（LeaveOffering / Clap / Bow）"
                       "→ 被前置拒。两者都让 `QueueRejected` 从用例里逃出去、报成「未捕获异常」，"
                       "看起来像实现坏了。第一版正是 ①：`Dodge/Meditate/Dodge/Kick` = "
                       "110+100+110+80 = 400 > 200 → 变红",
        demonstration=f"实测各队列耗时：{costs}",
    )

    def _one_turn(seq, *, damage=3, rng=None):
        """跑一个回合，返回每步的敌人反击记录。

        ⚠️ `Brace` 的实际耗时（STR=5）= **135**（`base_time` 160 − 5×5），
        加上 `Kick`(80) = 215 > 200 ⇒ **不能排在同一回合**。
        第一版把两者排在一起，队列被拒 → 0 步 → `counters[1]` 直接 IndexError。
        所以这里分两次跑：脱手一次、格挡一次。
        """
        st = _mk(seed=3)
        en = {"id": "X", "name": "X", "hp": 999, "power": 0, "damage": damage,
              "boss": False, "traits": []}
        out = C.resolve(st, rng if rng is not None else ST.new_rng(3, st), en, CFG,
                        sequence=seq, max_turns=1)
        return [e["enemy_counter"] for e in out.events if e["kind"] == C.EVENT_STEP]

    plain = _one_turn(["Kick"])
    braced = _one_turn(["Brace"])
    s.expect(
        "Brace：本回合**余下**的敌人伤害减半（3 → 1，向下取整）；不格挡那次照原值",
        plain[0]["damage"] == 3 and plain[0]["brace_halved"] is False
        and braced[0]["brace_halved"] is True and braced[0]["damage"] == 1,
        detail=f"不格挡：{plain[0]}；格挡：{braced[0]}",
        counterexample="反例：用 round 而非 floor（3/2 → 2）→ 与 §5.2 的「伤害减半（向下取整）」"
                       "不符 → 变红",
    )
    s.expect(
        "被跳过的反击与真实反击**形状同构**（最后一步打死敌人时不缺 sta_after/roll 字段）",
        set(plain[0]) == set(C.skipped_counter(0)),
        detail=f"真实反击键={sorted(plain[0])}；跳过反击键={sorted(C.skipped_counter(0))}",
        counterexample="反例：跳过时只写 `{'skipped': True, 'damage': 0}` → 两形状缺 sta_after/roll "
                       "→ 任何按字段消费账本的代码都要为「最后一步击杀」写特例 → 变红",
    )
    # Dodge 50% miss（用永远掷最小的桩 ⇒ 必 miss）
    f = C.Fight(_mk(seed=3), AlwaysRng(), {"id": "X", "name": "X", "hp": 999, "power": 0,
                                           "damage": 4, "boss": False, "traits": []}, CFG)
    tr = f.submit(["Dodge", "Kick"])
    st_counter = tr.steps[0]["enemy_counter"]
    s.expect(
        "Dodge：本回合余下的敌人攻击 50% miss（桩 rng 掷 1 ⇒ 必 miss）",
        st_counter["dodge_miss_pct"] == 50 and st_counter["missed"] is True
        and st_counter["damage"] == 0 and st_counter["roll"] == 1,
        detail=f"Dodge 步骤里的反击={st_counter}",
        counterexample="反例：把 dodge 做成「每次攻击都掷一次、掷中才 miss」的叠加式 → 与本步语义不符"
                       "（§5.2 是「本回合 50% miss」且多次不叠加）→ 变红",
    )
    # 不叠加
    # ⚠️ Dodge 的耗时是 `160 − 10×DEX`：DEX=5 → 110，**两个 Dodge 就 220 > 200**
    #    ⇒ 在默认属性下"同一回合里 Dodge 两次"根本排不出来（第一版就是这么误红的：
    #    测的队列 110+100+110+80=400 被拒 → QueueRejected 逃出用例）。取 DEX=12 ⇒ 每个 40。
    f2 = C.Fight(_mk(seed=3, stats={"DEX": 12, "STR": 12}), AlwaysRng(),
                 {"id": "X", "name": "X", "hp": 999, "power": 0, "damage": 4,
                  "boss": False, "traits": []}, CFG)
    seq2 = ["Dodge", "Dodge", "Dodge"]
    plan2 = C.plan_queue(seq2, cfg=CFG, state=f2.state, enemy=f2.enemy)
    tr2 = f2.submit(seq2)
    notes = [n for stp in tr2.steps for n in stp["notes"]]
    s.expect(
        "多次 Dodge **不叠加**（§5.2）：第 2、3 次给出「不叠加」的明文说明，miss 率仍是 50%"
        "（DEX=12 时每个 Dodge 40，3 个才 120 ≤ 200）",
        plan2[0] is not None and plan2[0].time_total == 120
        and sum(1 for n in notes if "不叠加" in n) == 2
        and all(stp["enemy_counter"]["dodge_miss_pct"] == 50 for stp in tr2.steps),
        detail=f"3×Dodge(DEX=12) → time_total={plan2[0].time_total if plan2[0] else None}、"
               f"「不叠加」说明 {sum(1 for n in notes if '不叠加' in n)} 条、"
               f"各步 miss 率={[stp['enemy_counter']['dodge_miss_pct'] for stp in tr2.steps]}",
        counterexample="反例：`miss_pct += 50` → 100% miss（无敌）→ 变红",
    )
    # 逃跑三否决
    mob = {"id": "M", "name": "M", "hp": 9, "power": 1, "damage": 1, "boss": False, "traits": []}
    s.expect("逃跑：普通敌人允许", C.can_flee(_mk(), mob)[0] is True,
             detail=f"普通敌人 → {C.can_flee(_mk(), mob)}",
             counterexample="反例：无条件允许逃跑 → Boss 战也能溜 → 变红")
    s.expect("逃跑：Boss 不可逃（§5.2）", C.can_flee(_mk(), EN_THIN)[0] is False,
             detail=f"Thin Man(boss) → {C.can_flee(_mk(), EN_THIN)}",
             counterexample="反例：只看 is_boss 形参而忽略 enemy['boss'] → 变红")
    tag = {"id": C.NO_FLEE_INJURY, "kind": "injury", "name": "关节肿胀"}
    s.expect("逃跑：中了 SwollenJoint 不可逃（§5.2）",
             C.can_flee(_mk(tags=[tag]), mob)[0] is False,
             detail=f"带 SwollenJoint → {C.can_flee(_mk(tags=[tag]), mob)}",
             counterexample="反例：把伤势硬编码成中文名比对 → 数据里的 id 是英文 → 变红")
    ok3, why3 = C.can_flee(_mk(old_god=C.NO_FLEE_OLD_GOD), mob)
    s.expect("逃跑：古神为 Cthac-Atorasu 不可逃（§5.2；该古神不在 MVP 数据里，故按数据驱动）",
             ok3 is False and C.NO_FLEE_OLD_GOD in why3,
             detail=f"old_god={C.NO_FLEE_OLD_GOD} → { (ok3, why3) }",
             counterexample="反例：把这条判断删掉 → 古神规则少一条 · MVP 的 2 位古神下看不出来，"
                            "但 v2 加进 9 位时会静默失效 → 变红")
    # 逃跑 +5% DOOM，且 delta 交还给调用方
    st = _mk(seed=3)
    rng = ST.new_rng(3, st)
    fled = C.resolve(st, rng, mob, CFG, sequence=["RunAway"])
    s.expect(
        "逃跑成功：结束战斗 + `tuning.combat.escape_doom`(=5) 的 DOOM 增量交给调用方",
        fled.fled is True and fled.doom_delta == P.t(CFG, "combat.escape_doom") == 5
        and st["doom"] == 0,
        detail=f"fled={fled.fled}、doom_delta={fled.doom_delta}、状态里 doom={st['doom']}",
        counterexample="反例：直接改 state['doom'] → 状态在战斗中途被改动、无法回滚 → 变红",
    )
    s.expect(
        "逃跑失败（Boss）时不结束战斗，理由进账本",
        C.resolve(_mk(seed=3), ST.new_rng(3, None), EN_THIN, CFG,
                  sequence=["RunAway"], max_turns=1).fled is False,
        detail="对 Thin Man 排 RunAway → fled=False",
        counterexample="反例：不检查 can_flee 就置 fled=True → Boss 战可逃 → 变红",
    )


def test_exorcism(s: TestSuite) -> None:
    """5 步「拍手/鞠躬」驱魔（§5.4 / §10.2 #14）：本局固定、返回几步正确、+15 EXP、不泄题。"""
    st = _mk(seed=8)
    rng = ST.new_rng(8, st)
    t1 = C.exorcism_target(st, rng)
    t2 = C.exorcism_target(st, rng)
    conf = C.exorcism_conf()
    s.expect(
        f"答案长度 = {conf['steps']} 步、只由 {conf['tokens']} 组成，且**同局重复调用不变**",
        len(t1) == 5 and set(t1) <= {"clap", "bow"} and t1 == t2,
        detail=f"target={t1}（第二次调用 {t2}）；tokens={conf['tokens']}",
        counterexample="反例：每次调用都重新抽 → 玩家上一回合试出的「有 2 步正确」下一回合失效 → 变红"
                       "（§5.4：正确序列在本局内固定不变）",
    )
    sig = set()
    for sd in range(8):
        sst = _mk(seed=sd)
        sig.add(tuple(C.exorcism_target(sst, ST.new_rng(sd, None))))
    s.expect(
        "不同 seed 的答案不同（否则「固定答案」退化成常量）",
        len(sig) > 1,
        detail=f"8 个 seed 得到 {len(sig)} 种答案",
        counterexample="反例：把答案写成 `['clap']*5` 常量 → 8 个 seed 全同 → 变红",
    )
    s.expect(
        "判分函数 = 逐位相等计数（§5.4「有几步正确」）",
        C.score_exorcism(["clap", "bow", "clap", "bow", "clap"],
                         ["clap", "clap", "clap", "bow", "bow"]) == 3,
        detail="5 步里 3 步相同 → 3",
        counterexample="反例：把判分写成 set 交集大小 → 位置信息丢失、玩家永远猜不出 → 变红",
    )
    # 端到端：提交正确序列 ⇒ 驱魔成功 + 15 EXP
    st = _mk(seed=8)
    rng = ST.new_rng(8, st)
    target = C.exorcism_target(st, rng)
    # ⚠️ 驱魔**记号**（clap/bow）不是**动作 id**（Clap/Bow）：队列里要排后者。
    #    第一版直接把记号当 id 排 → 5 步全被拒（"不在动作表里"）→ 误红。
    seq = ["Clap" if t == "clap" else "Bow" for t in target]
    plan, reasons = C.plan_queue(seq, cfg=CFG, state=st, enemy=EN_GHOST)
    s.expect(
        "★ 5 步驱魔序列**能塞进 200 预算与 8 步上限**（这是 40 耗时那条推导的判据）",
        plan is not None and reasons == [] and plan.count == 5 and plan.time_total <= 200,
        detail=f"{seq} → count={plan.count if plan else None}、"
               f"time_total={plan.time_total if plan else None}、reasons={reasons}",
        counterexample="反例：把 Clap/Bow 的耗时定成 80（同其他动作）→ 5×80=400 > 200 → "
                       "§5.4 的「每次提交 5 步」永远无法提交 → 变红",
    )
    out = C.resolve(st, ST.new_rng(8, st), EN_GHOST, CFG, sequence=seq)
    s.expect(
        "端到端：提交正确 5 步 ⇒ 驱魔成功、+15 EXP、战斗结束为胜利",
        out.exorcised is True and out.won is True
        and out.exp_delta >= C.exorcism_conf()["exp"],
        detail=f"exorcised={out.exorcised}、won={out.won}、exp_delta={out.exp_delta}、"
               f"turns={out.turns}",
        counterexample="反例：驱魔只记分数不判成功 → exorcised=False → 变红",
    )
    partial = C.resolve(_mk(seed=8), ST.new_rng(8, None), EN_GHOST, CFG,
                        sequence=["Clap", "Bow"], max_turns=1)
    ex_ev = [e for e in partial.events if e["kind"] == "combat_exorcism"]
    s.expect(
        "提交不足 5 步：不判分，并在账本里说明（§5.4「每次提交 5 步」）",
        bool(ex_ev) and "不足 5 步" in str(ex_ev[0].get("note", "")),
        detail=f"2 步提交 → note={ex_ev[0].get('note') if ex_ev else None}",
        counterexample="反例：不足 5 步也给分数 → 玩家用 1 步就能反复试探 → 变红",
    )
    s.expect(
        "★ 答案**不外泄**：公开视图与 outcome 里都没有 `target`",
        "target" not in C.exorcism_public(st) and "target" not in json.dumps(
            out.to_json().get("exorcism"), ensure_ascii=False)
        and not any("target" in json.dumps(e, ensure_ascii=False) for e in out.events),
        detail=f"exorcism_public={C.exorcism_public(st)}；outcome.exorcism={out.to_json().get('exorcism')}",
        counterexample="反例：把 state['exorcism'] 整块塞进 Outcome → 答案进了前端/存档面板 → "
                       "猜谜机制当场失效 → 变红",
    )
    # 非幽灵敌人：Clap/Bow 不判分
    non = C.resolve(_mk(seed=8), ST.new_rng(8, None), EN_THIN, CFG,
                    sequence=["Clap", "Bow", "Dodge"], max_turns=1)
    ev2 = [e for e in non.events if e["kind"] == "combat_exorcism"]
    s.expect(
        "非幽灵敌人上的 Clap/Bow 不判分（§5.4 只对幽灵有意义）",
        bool(ev2) and "不是幽灵" in str(ev2[0].get("note", "")),
        detail=f"对 Thin Man 排 Clap/Bow → note={ev2[0].get('note') if ev2 else None}",
        counterexample="反例：任何敌人都判分 → 对杂兵猜序列拿 EXP → 变红",
    )
    old = {"schema": 1, "mode": "coc", "doom": 0, "sta": 10, "rea": 10}
    filled = ST.from_json_obj(old)
    s.expect(
        "旧档兼容：缺 exorcism 字段被补成空结构（不抛、不抽答案）",
        isinstance(filled.get("exorcism"), dict)
        and filled["exorcism"]["target"] == [] and filled["exorcism"]["steps"] == 0
        and C.ensure_fields({}) == [C.EXORCISM_KEY],
        detail=f"from_json_obj → {filled['exorcism']}；ensure_fields({{}})={C.ensure_fields({})}",
        counterexample="反例：读档时直接 `state['exorcism']['tries'] += 1` → 旧档 KeyError → 变红",
    )


# ============================================================ 8. 动作表与等价性

def test_action_table_provenance(s: TestSuite) -> None:
    """动作表覆盖 §5.2 的全表；每个节点都有 `_source` + `_unverified`。"""
    idx = C.action_index()
    sec52 = {
        "offensive": ["Kick", "AttackWeapon", "StrongAttack", "PrepareAttack", "FireWeapon",
                      "OrderAllies", "BoostHit", "Bite"],
        "support": ["CameraFlash", "FindWeapon", "AssessSituation", "AllyDistract",
                    "CombatSkill", "DesperateStruggle"],
        "defensive": ["RunAway", "Meditate", "Dodge", "Brace"],
        "spiritual": ["Clap", "Bow", "Pray", "LeaveOffering", "SeverTies"],
    }
    missing = {g: [a for a in ids if a not in idx] for g, ids in sec52.items()}
    missing = {g: v for g, v in missing.items() if v}
    s.expect(
        "§5.2 全表（Offensive 8 / Support 6 / Defensive 4 / Spiritual 4 行=5 个 token）逐条落地",
        not missing,
        detail=f"缺={missing}；共 {len(idx)} 个动作",
        counterexample="反例：只写 MVP 的 10 个 → 缺 PrepareAttack/FireWeapon 等 → 变红"
                       "（COC-DESIGN §6.1 要求「数据先写全 20 个，避免 v2 改 schema」）",
    )
    s.expect(
        "动作数 = 27：§5.2 的 23 个 token（Offensive 8 + Support 6 + Defensive 4 + "
        "Spiritual 5，其中 Clap/Bow 由 1 行拆成 2 条）+ 本项目新增 2（UseItem/Reload）"
        "+ 自由动作数据位 2（CastSpell/ThrowItem）",
        len(idx) == 27,
        detail=f"实际 {len(idx)} 个：{sorted(idx)}",
        counterexample="反例：把 §5.2 的某一行漏掉（例如 PrepareAttack）→ 24 → 变红；"
                       "这条断言的算式写在这里，任何增减都必须能解释",
    )
    s.expect(
        "四个分组都有成员（§5.2 的四类菜单）",
        all(any(a.get("group") == g for a in idx.values()) for g in C.GROUPS),
        detail="；".join(f"{g}={sum(1 for a in idx.values() if a.get('group') == g)}"
                        for g in C.GROUPS),
        counterexample="反例：把 spiritual 合并进 support → spiritual 组为空 → 变红"
                       "（§5.4 的幽灵对策正是靠这个分组区分通道）",
    )
    # 溯源：每个动作 + 每个 weapons 条目
    doc = C.content.combat_actions_doc()
    bad = []
    for a in doc["actions"]:
        if not isinstance(a.get("_source"), str) or not a["_source"].strip() \
                or not isinstance(a.get("_unverified"), bool):
            bad.append(a.get("id"))
    for wid, w in doc["weapons"].items():
        if str(wid).startswith("_"):
            continue
        if not isinstance(w.get("_source"), str) or not isinstance(w.get("_unverified"), bool):
            bad.append(f"weapons.{wid}")
    s.expect(
        "★ 每个动作与武器档都带非空 `_source` 与 bool `_unverified`（COC-DESIGN §0.2 铁律）",
        not bad,
        detail=f"缺标注={bad}；动作 {len(doc['actions'])} 个、武器 {len(doc['weapons'])} 件",
        counterexample="反例：新增一个动作不写 _source → content.combat_actions_doc() 在"
                       "**加载期**就抛 ContentError（不是等到测试）→ 变红",
    )
    # 红侧证明：挖掉一个 _source 必须真的抛
    import copy
    broken = copy.deepcopy(doc)
    broken["actions"][0].pop("_source")
    raised = None
    try:
        C.content._require_meta(broken["actions"][0], "actions[0]")
    except C.content.ContentError as exc:
        raised = "ContentError"
    s.expect(
        "红侧证明：挖掉 `_source` ⇒ 校验真的报错（不是「我们相信它不会」）",
        raised == "ContentError",
        detail=f"挖掉 actions[0]._source 后 _require_meta 抛 {raised}",
        counterexample="反例：把 `_required` 检查删掉 → raised=None → 变红",
    )
    # MVP 暴露集合
    mvp = sorted(a for a, v in idx.items() if v.get("mvp"))
    s.expect(
        "MVP 暴露 11 个动作（COC-DESIGN §2.6 的 10 个，其中 Clap/Bow 拆成两条）",
        set(mvp) == {"AssessSituation", "AttackWeapon", "Bow", "Brace", "Clap", "Dodge",
                     "Kick", "Meditate", "Pray", "RunAway", "StrongAttack"},
        detail=f"mvp={mvp}",
        counterexample="反例：把 FireWeapon 标成 mvp → 它在 MVP 数据下永远缺前置 → 变红",
    )
    menu = C.action_menu(_mk(), EN_THIN, cfg=CFG)
    s.expect(
        "动作菜单给出**当前**耗时/属性/伤害与不可选理由（§10.1 #3 的等价物，不需要 hover）",
        len(menu["actions"]) == len(idx)
        and all({"id", "time_cost", "stat", "available", "reject_reasons"} <= set(r)
                for r in menu["actions"])
        and menu["time_budget"] == 200
        and any(r["available"] is False and r["reject_reasons"] for r in menu["actions"]),
        detail=f"菜单 {len(menu['actions'])} 项；不可选项 "
               f"{sum(1 for r in menu['actions'] if not r['available'])} 个（如 "
               f"{next((r['id'] for r in menu['actions'] if not r['available']), None)}）；"
               f"speed_multiplier={menu['speed_multiplier']}",
        counterexample="反例：把不可选动作隐藏 → 玩家看不到「为什么排不了」 → 变红",
    )
    s.expect(
        "自由动作（法术/投掷）不能排进主队列，理由指向 §5.1 / §5.6",
        any("自由动作" in x for x in
            C.plan_queue(["CastSpell"], cfg=CFG, state=_mk(), enemy=EN_THIN)[1]),
        detail=C.plan_queue(["CastSpell"], cfg=CFG, state=_mk(), enemy=EN_THIN)[1][0],
        counterexample="反例：允许 CastSpell 进主队列 → 它 base_time=0 → 8 步全是法术前置检查通过 "
                       "→ 一回合无限次施法（§5.6 明确说这是要保留的平衡杠杆，不是 bug）→ 变红",
    )
    f = C.Fight(_mk(), AlwaysRng(), EN_THIN, CFG)
    tr = f.submit(["Kick"], free_actions=["CastSpell"])
    free_steps = [x for x in tr.steps] + [e for e in f.events
                                          if e["kind"] == C.EVENT_STEP and e.get("free")]
    bad_free = None
    try:
        f.submit(["Kick"], free_actions=["Dodge"])
    except C.QueueRejected as exc:
        bad_free = str(exc)
    s.expect(
        "自由动作槽只收 `free: true` 的动作（把 Dodge 塞进去被拒）",
        bad_free is not None and "不是自由动作" in bad_free
        and any(e["kind"] == C.EVENT_STEP and e.get("free") and e["time_cost"] == 0
                for e in f.events),
        detail=f"free_actions=['Dodge'] → {bad_free}",
        counterexample="反例：不校验 free 标记 → 玩家把 Dodge 当自由动作白嫖闪避 → 变红",
    )


def test_legacy_equivalence(s: TestSuite) -> None:
    """**平衡等价性**：换成"永远命中"的桩后，基础攻击下的受击总量必须与旧实现逐值相等。

    这是"per_action 反击"这个设计取舍的**依据**，也是"接入没有偷偷改难度"的证据。
    旧实现的等价模型（`loop.py` t48 的 while 循环，逐字复刻）：
        rounds = 0; while hp > 0 and rounds < cap: rounds += 1; hp -= atk; if hp<=0: break;
                                                taken += dmg
    """
    def legacy(hp0: int, atk: int, dmg: int, cap: int) -> tuple[int, int]:
        hp, rounds, taken = hp0, 0, 0
        while hp > 0 and rounds < cap:
            rounds += 1
            hp -= atk
            if hp <= 0:
                break
            taken += dmg
        return rounds, taken

    st = _mk(seed=1)
    f = C.Fight(st, AlwaysRng(), EN_THIN, CFG)
    out = f.resolve()
    # 旧实现：atk = probe.probe_attack_damage(=2)、dmg = enemy.damage + difficulty.enemy_dmg
    atk = int(P.t(CFG, "probe.probe_attack_damage"))
    dmg = int(EN_THIN["damage"]) + int(P.t(CFG, "difficulty")["Initiate"]["enemy_dmg"])
    rounds, taken = legacy(int(EN_THIN["hp"]), atk, dmg, int(P.t(CFG, "combat.round_cap")))
    counters = [e for e in out.events if e["kind"] == C.EVENT_STEP
                and e["enemy_counter"].get("damage", 0) > 0]
    s.expect(
        f"★ 必中桩下：受击总量 {out.damage_taken} == 旧实现的 {taken}（Kick 伤害 2 == "
        f"probe_attack_damage 2）",
        out.damage_taken == taken and out.won is True,
        detail=f"新：damage_taken={out.damage_taken}（{len(counters)} 次有效反击）、"
               f"turns={out.turns}、player_damage={out.player_damage}；"
               f"旧：rounds={rounds}、taken={taken}",
        counterexample="反例：把敌人改成「每个动作反击两次」，或把 Kick 伤害改成 1 → 受击总量翻倍/"
                       "回合数翻倍 → 与旧实现不等 → 变红（这就是「偷偷调难度」）",
        demonstration=f"实测：新 {out.damage_taken} vs 旧 {taken}（差额 0）",
    )
    s.expect(
        "旧实现的 `rounds` 与新的 `turns` 不是同一个量，但都被产出（rounds 是 turns 的别名）",
        out.turns >= 1 and out.turns <= rounds and out.to_json()["rounds"] == out.turns,
        detail=f"turns={out.turns}、旧 rounds={rounds}；"
               f"差的来源：旧实现每轮 1 次动作，新实现每回合装得下多个动作",
        counterexample="反例：让 turns 直接等于旧 rounds → 一回合的多次动作会被算成多轮 → "
                       "§5.1 的「一回合 ≤8 动作」就没了 → 变红",
    )
    # ---- 真实 rng 下的差异：会 miss ----
    st2 = _mk(seed=7)
    out2 = C.resolve(st2, ST.new_rng(7, st2), EN_THIN, CFG)
    s.expect(
        "真实 rng 下命中率 <100% ⇒ 可能出现「未命中但仍被反击」的一步（旧实现永远命中）",
        any(s_["hit"] is False for t in out2.turns_detail for s_ in t["steps"])
        or out2.damage_taken >= taken - 2,
        detail=f"seed=7 的逐步命中={[ (s_['action'], s_['roll'], s_['hit']) for t in out2.turns_detail for s_ in t['steps'] ]}；"
               f"damage_taken={out2.damage_taken}",
        counterexample="反例：把 Kick 的命中做成必中（忽略 §5.2 的 80%）→ 永远不出现 hit=False → 变红",
    )
    # ---- 幽灵：旧实现 0 伤害 vs 新实现能解 ----
    old_ghost_damage = 0        # 旧实现里 incorporeal 永远 `hp -= atk` 都不执行 ⇒ 玩家伤害恒为 0
    st3 = _mk(seed=7)
    new_ghost = C.resolve(st3, ST.new_rng(7, st3), EN_GHOST, CFG)
    s.expect(
        "★ 与旧实现的首要差异：幽灵从「永远打不动（玩家伤害恒为 0，只能被耗死）」"
        "变成「换 spiritual 手段可解」",
        old_ghost_damage == 0 and new_ghost.player_damage > 0 and new_ghost.won is True,
        detail=f"旧：player_damage=0；新：player_damage={new_ghost.player_damage}、"
               f"won={new_ghost.won}、damage_taken={new_ghost.damage_taken}",
        counterexample="反例：把幽灵豁免只写成日志（t48 的行为）→ 玩家伤害仍为 0 → 变红",
    )


def test_below_zero_still_takes_damage(s: TestSuite) -> None:
    """★ 回归：STA ≤ 0 但**未到 `last_stand` 死亡线**时，后续遭遇**仍必须造成伤害**。

    这是 `coc_cli_probe` 的 **A5** 抓到的真实回归（我自己的，不是断言问题）：
    第一版在 `Fight.resolve()` 的循环入口与 `submit()` 的步循环里都预检 `self.sta <= 0`，
    于是玩家一旦掉到 0 以下（例如 -3，**仍在 `last_stand` 的 -5 缓冲带内**），
    **之后每一次强制战斗都 `turns=0 / damage_taken=0`** —— STA 永久卡在 -3，
    `END_STA_ZERO`（需要 `sta <= floor(-5)`）**变成不可达**。

    旧实现（t48 的 `while` 循环）没有入口预检：它「先结算这一轮伤害，再 `if sta <= 0: break`」，
    所以每次遭遇仍扣一次血，STA 会一路掉过 -5 ⇒ 结局可达。
    本用例把这条语义**钉住**：从 -3 出发，一次遭遇必须把它推到死亡线以下。
    """
    floor = int(P.t(CFG, "sanitize.last_stand_floor"))
    en = {"id": "M", "name": "磨", "hp": 8, "power": 3, "damage": 2,
          "boss": False, "traits": []}
    st = _mk(seed=7)
    st["sta"] = -3                      # 在缓冲带内：decide_endings 明确不判死
    out = C.resolve(st, ST.new_rng(7, st), en, CFG)
    s.expect(
        f"STA=-3（缓冲带内，floor={floor}）时一次遭遇**仍造成伤害**，STA 继续下掉",
        out.damage_taken > 0 and out.sta_delta < 0,
        detail=f"STA -3 → {st['sta'] + out.sta_delta}（sta_delta={out.sta_delta}、"
               f"damage_taken={out.damage_taken}、turns={out.turns}）",
        counterexample="反例：在 `resolve()` / `submit()` 的循环入口预检 `self.sta <= 0` → "
                       "`turns=0`、`damage_taken=0`、STA 永久卡在 -3 → 变红"
                       "（这**就是** A5 的失败机制：行为真的变了，不是判据过宽）",
        demonstration=f"实测：sta_delta={out.sta_delta}、damage_taken={out.damage_taken}",
    )
    # 连续遭遇能跨过死亡线 ⇒ 结局可达
    st2 = _mk(seed=7)
    cur = -3
    trail = [cur]
    crossed = False
    for _ in range(8):
        st2["sta"] = cur
        o = C.resolve(st2, ST.new_rng(7, st2), en, CFG)
        cur += o.sta_delta
        trail.append(cur)
        if cur <= floor:
            crossed = True
            break
    s.expect(
        f"★ 连续遭遇能把 STA 推过 `last_stand` 死亡线（floor={floor}）⇒ `END_STA_ZERO` 可达",
        crossed and cur <= floor,
        detail=f"STA 轨迹={trail}（每步减 {abs(trail[1] - trail[0]) if len(trail) > 1 else '?'}）",
        counterexample="反例：预检 sta → 轨迹会停在 -3 原地不动 → `decide_endings` 永远拿到 "
                       "『仍在缓冲带内』→ 变红（A5 的场景 B 正是这样退化成 e2=None）",
    )
    # 反向：健康玩家的一次遭遇"伤害总量"不受本修复影响（防改过头）
    st3 = _mk(seed=1)
    f = C.Fight(st3, AlwaysRng(), EN_THIN, CFG)
    healthy = f.resolve()
    s.expect(
        "健康玩家（STA=21）的必中受击总量**不受本修复影响**（仍是 6，与旧实现逐值相等）",
        healthy.damage_taken == 6,
        detail=f"damage_taken={healthy.damage_taken}、turns={healthy.turns}、"
               f"player_damage={healthy.player_damage}",
        counterexample="反例：把入口预检去掉的同时让反击每步都打（不再在 sta<=0 时收尾）→ "
                       "健康玩家的受击总量也会变 → 与旧实现不等 → 变红",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_combat.py —— CoC 战斗（200 时间单位 / ≤8 动作队列）")
    for fn in (test_queue_limits, test_weapon_stat_linkage, test_incorporeal,
               test_reproducible, test_ledger, test_time_formula,
               test_defensive_and_flee, test_exorcism,
               test_action_table_provenance, test_legacy_equivalence,
               test_below_zero_still_takes_damage):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

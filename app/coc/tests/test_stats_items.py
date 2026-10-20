# -*- coding: utf-8 -*-
"""t63：dc 梯度 / 属性门槛 / 物品栏 / 武器判定 的单测。

覆盖任务书点名项：
  1. **dc 数据梯度**（分布前后对照 + 逐卡 dc 合法性 + 三档齐备）；
  2. **dc 矩阵实测**：3 属性 × 3 档 dc × 高/低属性值的成功率（穷举理论值）；
  3. **门槛实测**：同一 seed 下属性够/不够 ⇒ 可选集不同（逐卡对比）；
  4. **fail-closed**：未知门槛字段必须**不满足**（不能静默放行 —— 那正是任务书禁止的）；
  5. **物品栏**：grant → 视图 → 存档往返仍在；equip/unequip 走唯一写点；
  6. **武器判定**：SteakKnife→DEX、FireAxe→STR 真实用于检定（含账本证据）。
"""
from __future__ import annotations

import io
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.coc import content
from app.coc import dice
from app.coc import loop as L
from app.coc import provenance as P
from app.coc import sanitize as S
from app.coc import session as SESS
from app.coc import state as ST
from app.coc.tests.harness import TestSuite

CFG = P.load_tuning()
LO = int(P.t(CFG, "checks.roll_min"))
HI = int(P.t(CFG, "checks.roll_max"))


def _mk(seed: int = 4242, **stats) -> L.Run:
    run = L.Run.start({"difficulty": "Initiate", "seed": seed}, CFG)
    if stats:
        run.state["stats"].update(stats)
    return run


def _card(cid: str) -> dict:
    for c in content.cards():
        if c["id"] == cid:
            return c
    raise KeyError(cid)


def _rate(stat_value: int, dc: int, threat: str = "SAFE") -> float:
    """穷举全部掷骰结果的**精确**成功率（理论值，不用统计）。"""
    win = 0
    for roll in range(LO, HI + 1):
        if dice.resolve_deterministic("X", stat_value, threat, roll, CFG,
                                      dc=dc).success:
            win += 1
    return win / (HI - LO + 1)


def _all_checks() -> list[tuple[str, int, dict]]:
    out = []
    for c in content.cards():
        for i, ch in enumerate(c.get("choices") or []):
            spec = ch.get("check")
            if isinstance(spec, dict):
                out.append((c["id"], i, spec))
    return out


# ---------------------------------------------------------------- 1. dc 数据

def test_dc_gradient_data(s: TestSuite) -> None:
    """dc 从全 0 变成三档梯度，且每个 dc 都在合法域内。"""
    checks = _all_checks()
    dist: dict[int, int] = {}
    for _cid, _i, spec in checks:
        d = int(spec.get("dc", 0))
        dist[d] = dist.get(d, 0) + 1
    s.expect(
        f"★ dc 不再是单值：分布 = {dict(sorted(dist.items()))}（原为 {{0: 12}}）",
        len(dist) >= 3 and 0 in dist and sum(1 for d in dist if d < 0) >= 2,
        detail=f"共 {len(checks)} 个 check；dc 分布={dict(sorted(dist.items()))}"
               f"（三档 = 0 / −1 / −2；原为全 0）",
        counterexample="反例：把 dc 全部写回 0 → 只有 1 个键 → 变红。"
                       "这正是本任务要修的现状（属性值几乎不影响成败）。"
                       "注意 dc 必须是**非正**的：正 dc 会让检定变**容易**（实测 33.3%→50%），"
                       "所以断言里要求至少 2 档是负数",
    )
    lo, hi = int(P.t(CFG, "checks.dc_min")), int(P.t(CFG, "checks.dc_max"))
    bad = [(c, i, d) for c, i, sp in checks
           for d in [int(sp.get("dc", 0))] if not (lo <= d <= hi)]
    s.expect(
        f"每个 dc 都落在 tuning 的 dc 域 [{lo},{hi}] 内",
        not bad,
        detail=f"越界 {bad or '无'}",
        counterexample="反例：写 dc=9 → 超出 dc_max=6 → 会被 clamp_dc 静默夹取，"
                       "作者以为自己设了 9 → 变红",
    )
    s.expect(
        "12 个既有 check 全部仍在（没有因为加门槛/加选项丢检查）",
        len(checks) >= 12,
        detail=f"check 总数 = {len(checks)}",
        counterexample="反例：改写卡片时把 check 字段删掉 → 总数下降 → 变红",
    )
    dice_expect = [(c, i) for c, i, sp in checks if "check" in sp]
    s.expect(
        "「只填 dc」没有引入新字段到 check 里（公式输入面未变）",
        all(set(sp) <= {"stat", "threat", "dc", "weapon_stat"} for _c, _i, sp in checks),
        detail=f"出现过的 check 键 = "
               f"{sorted({k for _c, _i, sp in checks for k in sp})}",
        counterexample="反例：往 check 里塞 `bonus`/`auto_success` 之类字段 → "
                       "公式不认它，等于静默无效 → 变红",
    )


# ---------------------------------------------------------------- 2. dc 矩阵

def test_dc_matrix(s: TestSuite) -> None:
    """★ 3 属性 × 3 档 dc × 高/低属性值的成功率矩阵（穷举理论值）。

    ⚠️ **符号约定（本任务实测确认）**：公式是 `stat + threat_mod + dc >= roll + 1`，
    `dc` 加在**玩家侧**，与 `threat_mod`（SAFE 0 / LOW −1 / DOOMED −2）**同向**
    ⇒ **负 dc = 更难**。第一版按契约字面填了正的 0/1/2，实测发现成功率从
    33.3% **升到** 50.0%（越"难"越容易，语义反了），已翻成 0/−1/−2。
    """
    stats_probe = ("STR", "PER", "CHA")
    dcs = (0, -1, -2)          # 0 = 常规；−1 = 需技能；−2 = 吃力
    low, high = 5, 9          # 低=默认开局值 5；高=投入后的 9
    lines = []
    rows: dict[str, dict] = {}
    for st_name in stats_probe:
        rows[st_name] = {}
        for dc in dcs:
            rl = _rate(low, dc)
            rh = _rate(high, dc)
            rows[st_name][dc] = (rl, rh)
            lines.append(f"  {st_name} dc={dc}: stat={low}→{rl:.1%}  "
                         f"stat={high}→{rh:.1%}  （差 {rh - rl:+.1%}）")
    s.expect(
        "★ 矩阵：每档 dc 下，高属性值的成功率**严格高于**低属性值",
        all(rows[a][d][1] > rows[a][d][0] for a in stats_probe for d in dcs),
        detail="\n".join(lines),
        counterexample="反例：把 dc 接到**掷骰侧**（`roll + dc`）→ 高属性反而更容易失败 → 变红",
    )
    s.expect(
        "★ 矩阵：同一属性下，dc 越负成功率**越低**（三档单调递减）",
        all(rows[a][0][0] > rows[a][-1][0] > rows[a][-2][0] for a in stats_probe),
        detail="；".join(f"{a}: dc0={rows[a][0][0]:.1%} > dc-1={rows[a][-1][0]:.1%} "
                         f"> dc-2={rows[a][-2][0]:.1%}" for a in stats_probe),
        counterexample="反例：按契约字面填**正** 0/1/2 → 成功率变成 33.3% < 41.7% < 50.0%"
                       "（越说难越容易）→ 单调方向反过来 → 变红。**这正是第一版踩到的坑**",
    )
    step = rows["STR"][0][0] - rows["STR"][-1][0]
    s.expect(
        "每 −1 dc ≈ 8.3pp（= 12 面骰域里的一格），量纲与 dc 域 ±6 匹配",
        abs(step - 1 / 12) < 1e-9,
        detail=f"dc 0→−1 的成功率落差 = {step:.4f}（1/12 = {1/12:.4f}）",
        counterexample="反例：把 dc 接到 100 面骰上 → 落差变成 1% → 与实际骰域不符 → 变红",
    )
    s.expect(
        "★ 梯度真的改变机械强度：stat=5 时三档落在 33.3% / 25.0% / 16.7%",
        (round(rows["STR"][0][0], 4), round(rows["STR"][-1][0], 4),
         round(rows["STR"][-2][0], 4)) == (round(1 / 3, 4), 0.25, round(1 / 6, 4)),
        detail=f"stat=5：dc0={rows['STR'][0][0]:.4f}、dc-1={rows['STR'][-1][0]:.4f}、"
               f"dc-2={rows['STR'][-2][0]:.4f}（期望 0.3333 / 0.2500 / 0.1667）",
        counterexample="反例：三档写成 0/−1/−3 → 最后一档变成 8.3% → 与期望不符 → 变红",
    )


# ---------------------------------------------------------------- 3. 门槛实测

def test_stat_gate(s: TestSuite) -> None:
    """★ 同一 seed 下属性够/不够 ⇒ 可选集不同，且**后端强制**（不只前端灰掉）。"""
    gated = []
    for c in content.cards():
        for i, ch in enumerate(c.get("choices") or []):
            req = ch.get("requirements") or {}
            if req.get("stat_min"):
                gated.append((c, i, req["stat_min"]))
    s.expect(
        f"★ 带 `stat_min` 门槛的卡 ≥6 张（实际 {len({c['id'] for c, _i, _r in gated})} 张，"
        f"共 {len(gated)} 个选项）",
        len({c["id"] for c, _i, _r in gated}) >= 6,
        detail=f"门槛选项 = {[(c['id'], i, r) for c, i, r in gated]}",
        counterexample="反例：只给 3 张卡加门槛 → 不满足任务书的「至少 6 张」 → 变红,"
    )

    # 逐条：低属性 ⇒ 不可选；高属性 ⇒ 可选。
    # ⚠️ 其中两个选项**同时**要求"装备武器"（C-406#0 / C-304#2）——
    # 那种情况下即使属性拉满也必须**先装备**，否则仍然不可选。第一版漏了这点，
    # 把它们误判成失败。这里按"是否还有 weapon 门槛"分流，两种情形都断言。
    lines = []
    fails = []
    weapon_gated = []
    for c, i, req in gated:
        key, need = next(iter(req.items()))
        whole_req = (c["choices"][i].get("requirements") or {})
        needs_weapon = bool(whole_req.get("weapon"))
        if needs_weapon:
            weapon_gated.append((c["id"], i, whole_req.get("weapon_any")))

        def _run(want_stat: int) -> L.Run:
            r = _mk(seed=88, **{key: want_stat})
            if needs_weapon:
                # 装上这个选项要求的武器（没有 weapon_any 就给任意一把能用的）
                allowed = whole_req.get("weapon_any") or ["SteakKnife", "FireAxe"]
                wid = allowed[0]
                r.apply({"item_grant": [wid]}, reason="t63")
                r.apply({"equip": wid}, reason="t63")
            return r

        lo_view = _run(max(1, int(need) - 2)).choices_view(c)[i]
        hi_view = _run(min(12, int(need) + 2)).choices_view(c)[i]
        lines.append(f"  {c['id']}#{i} {key}>={need}"
                     f"{'（另需武器）' if needs_weapon else ''}: "
                     f"低属性 available={lo_view['available']}"
                     f"({lo_view['blocked_reason']})  "
                     f"高属性 available={hi_view['available']}")
        if lo_view["available"] or not hi_view["available"]:
            fails.append((c["id"], i, key, need, needs_weapon))
    s.expect(
        "★ 每个 stat_min 门槛：属性不足 ⇒ available=False（带原因）；属性足够 ⇒ True"
        "（装备类选项在补齐武器后同样成立）",
        not fails,
        detail="\n".join(lines) + (f"\n  不符={fails}" if fails else "")
               + f"\n  其中另需武器的选项：{weapon_gated}",
        counterexample="反例：门槛只写进数据但 `choices_view` 不读它 → 两种情况都 True → 变红",
    )
    # 单独钉住"武器门槛独立生效"：属性拉满但没武器 ⇒ 仍不可选
    wfails = []
    for cid, i, want in weapon_gated:
        c = _card(cid)
        key, need = next(iter((c["choices"][i].get("requirements") or {})
                              ["stat_min"].items()))
        r = _mk(seed=88, **{key: min(12, int(need) + 2)})   # 属性拉满，不装备
        v = r.choices_view(c)[i]
        if v["available"] or v["blocked_reason"] != "weapon_required":
            wfails.append((cid, i, v))
    s.expect(
        "★ 武器门槛独立生效：属性拉满但**没装备武器** ⇒ 该选项仍不可选（reason=weapon_required）",
        not wfails and bool(weapon_gated),
        detail=f"另需武器的选项 {weapon_gated}；未装备时的视图不符={wfails or '无'}",
        counterexample="反例：把 weapon 门槛写成「有武器也行、没武器也行」→ 拉满属性即可选 → "
                       "变红（武器判定就成了摆设）",
    )

    # ★ 回归：属性低时**不能卡死**（t63 引入门槛后实测踩到的真缺陷）
    #   症状：`_resolve_once`（探针/无 LLM 环境）按固定下标选到被锁选项 ⇒
    #   `resolve_card` 抛 LoopError ⇒ 探索推进整条挂掉（C-201#0 PER 5<6）。
    #   修法：请求下标不可用时退到 `first_playable_choice`。
    from app.coc import session as _S
    pub = _S.start({"difficulty": "Initiate", "seed": 4242}, CFG)
    sid = pub["sid"]
    run_low = _S.get(sid)["run"]
    for _k in run_low.state["stats"]:
        run_low.state["stats"][_k] = 1    # 全员属性最低 ⇒ 大部分选项被锁
    raised_low = None
    steps = 0
    try:
        for j in range(12):
            _S.advance_without_llm(_S.get(sid), j % 4)
            steps += 1
    except BaseException as e:                            # noqa: BLE001
        raised_low = f"{type(e).__name__}: {e}"
    s.expect(
        "★ 属性全为 1（大量选项被锁）时，`advance_without_llm` 仍能连续推进 12 步不卡死",
        raised_low is None and steps == 12,
        detail=f"完成步数={steps}；raised={raised_low!r}；"
               f"investigations_total={run_low.state.get('investigations_total')}",
        counterexample="反例：把 `_resolve_once` 的兜底删掉（直接 `resolve_card(card, want)`）→ "
                       "撞上被锁选项抛 LoopError → 第 1 步就退出 → 变红。"
                       "**门槛不该让游戏走不下去**",
    )
    s.expect(
        "★ 兜底不放松门槛：被锁的选项**仍然**不可选（`choices_view` 依旧 False）",
        _mk(seed=1, STR=1).choices_view(_card("C-204"))[0]["available"] is False,
        detail="C-204#0（STR>=7）在 STR=1 时 available=False",
        counterexample="反例：为了不卡死而把门槛判定改成「总是可选」→ 变红",
    )

    # 后端强制：属性不足时 resolve_card 必须拒绝（不能只靠前端灰掉）
    sample = gated[0]
    c, i, req = sample
    key, need = next(iter(req.items()))
    run = _mk(seed=99, **{key: max(1, int(need) - 2)})
    raised = None
    try:
        run.resolve_card(c, i)
    except L.LoopError as e:
        raised = str(e)
    except BaseException as e:                                # noqa: BLE001
        raised = f"{type(e).__name__}: {e}"
    s.expect(
        f"★ 后端强制：属性不足时 `resolve_card` 抛 LoopError（不是静默按普通检定处理）",
        raised is not None and "门槛未满足" in str(raised),
        detail=f"卡 {c['id']}#{i}（{key}>={need}，实际 {max(1, int(need) - 2)}）"
               f"→ raised={raised!r}",
        counterexample="反例：只在 choices_view 里标 available、resolve_card 不加校验 → "
                       "前端被绕过（或旧客户端）就能强行选 → 变红。"
                       "**服务端守门**是任务书的要求",
    )

    # 同一 seed、不同属性 → 整张卡的可选集不同（任务书点名的"真的遇到"）
    demo = []
    for c in content.cards():
        reqs = [(i, ch.get("requirements") or {}) for i, ch in
                enumerate(c.get("choices") or [])]
        smin = [(i, r["stat_min"]) for i, r in reqs if r.get("stat_min")]
        if not smin:
            continue
        i0, m = smin[0]
        key, need = next(iter(m.items()))
        lo = _mk(seed=1234, **{key: max(1, int(need) - 2)})
        hi = _mk(seed=1234, **{key: min(12, int(need) + 2)})
        lo_set = [v["available"] for v in lo.choices_view(c)]
        hi_set = [v["available"] for v in hi.choices_view(c)]
        demo.append((c["id"], key, lo_set, hi_set))
    differ = [d for d in demo if d[2] != d[3]]
    s.expect(
        f"★ 同一 seed、只有属性不同 ⇒ **至少 2 张卡**的整卡可选集不同（实际 {len(differ)} 张）",
        len(differ) >= 2,
        detail="\n".join(f"  {cid} {k}: 低={lo} 高={hi}" for cid, k, lo, hi in differ),
        counterexample="反例：门槛写成「永远满足」（如 `>= 1`）→ 两种属性下可选集相同 → 变红,"
    )


# ---------------------------------------------------------------- 4. fail-closed

def test_fail_closed(s: TestSuite) -> None:
    """未知门槛字段必须**不满足**，不能静默放行。"""
    run = _mk()
    cases = [
        ({"stat_mim": {"STR": 1}}, "写错字段名（stat_mim）"),
        ({"stat_min": {"MAG": 1}}, "未知属性名（MAG）"),
        ({"wat": True}, "完全不认识的键"),
        ({"weapon": True}, "未装备武器时要求武器"),
    ]
    bad = []
    for req, why in cases:
        ok, reason = run.check_requirements(req)
        if ok:
            bad.append((req, why))
    s.expect(
        "★ fail-closed：4 类非法/未满足门槛全部判定为**不满足**（并给出原因）",
        not bad,
        detail="；".join(f"{json.dumps(r, ensure_ascii=False)}→"
                        f"{run.check_requirements(r)}" for r, _ in cases),
        counterexample="反例：改成 fail-open（未知字段忽略）→ `stat_mim` 会静默通过 → "
                       "**玩家永远不知道自己写错了卡片**，且门槛形同不存在 → 变红",
    )
    ok, reason = run.check_requirements({"stat_mim": {"STR": 1}})
    s.expect(
        "原因里点名了写错的字段（便于发现拼写错误，而不是只说「不满足」）",
        "stat_mim" in reason,
        detail=f"reason={reason!r}",
        counterexample="反例：只回 'not_met' → 卡片作者无法定位是哪个字段写错 → 变红",
    )
    s.expect(
        "合法但满足的门槛仍返回 True（fail-closed 不等于一律拒绝）",
        run.check_requirements({"stat_min": {"STR": 1}})[0] is True
        and run.check_requirements({})[0] is True,
        detail=f"stat_min STR>=1 → {run.check_requirements({'stat_min': {'STR': 1}})}；"
               f"空门槛 → {run.check_requirements({})}",
        counterexample="反例：把判定写成「任何门槛都拒绝」 → 开局所有卡都抽不到 → 变红,"
    )


# ---------------------------------------------------------------- 5. 物品栏

def test_inventory(s: TestSuite) -> None:
    """grant → 视图 → 存档往返仍在；equip/unequip 走唯一写点。"""
    pub = SESS.start({"difficulty": "Initiate", "seed": 3131}, CFG)
    sid = pub["sid"]
    run = SESS.get(sid)["run"]

    inv0 = pub["coc"]["inventory"]
    s.expect(
        "开局即有完整物品栏视图（backpack/storage/equipped/slots/weapon/counts）",
        {"backpack", "storage", "equipped", "slots", "weapon", "counts"} <= set(inv0),
        detail=f"视图键 = {sorted(inv0)}；counts={inv0['counts']}",
        counterexample="反例：panel 只挂 `inventory` 的 backpack 列表 → 缺 slots/weapon → 变红",
    )

    # grant 一件 → 出现在视图
    run.apply({"item_grant": ["SteakKnife"]}, reason="t63_test")
    view = ST.inventory_view(run.state)
    knife = [x for x in view["backpack"] if x["id"] == "SteakKnife"]
    s.expect(
        "★ grant 一件武器后，它出现在物品栏视图里并带完整元数据",
        len(knife) == 1 and knife[0].get("kind") == "weapon"
        and knife[0].get("stat") == "DEX" and knife[0].get("equip_slot") == "R",
        detail=f"背包装备项 = {json.dumps(knife, ensure_ascii=False)}",
        counterexample="反例：视图只回 id 列表（不带 kind/stat/equip_slot）→ "
                       "前端无法显示「走 DEX」、也无法知道能装备 → 变红",
    )

    # 装备 → weapon 字段出现
    res = SESS.equip_action(sid, "equip", item="SteakKnife")
    s.expect(
        "★ 装备后 `inventory.weapon` = {id,name,stat}（武器判定的数据源）",
        res["ok"] and res["inventory"]["weapon"] == {
            "id": "SteakKnife", "name": "牛排刀", "stat": "DEX"},
        detail=f"equip 结果 ok={res['ok']} slot={res['slot']}；"
               f"weapon={json.dumps(res['inventory']['weapon'], ensure_ascii=False)}",
        counterexample="反例：equip 只改 equipped 不刷新 weapon → 前端显示已装备但"
                       "判定仍走基础属性 → 变红",
    )
    s.expect(
        "装错东西会被拒（不可装备的物品 / 不在背包的物品）",
        SESS.equip_action(sid, "equip", item="OccultDiary")["ok"] is False
        and SESS.equip_action(sid, "equip", item="NopeItem")["ok"] is False,
        detail=f"OccultDiary→{SESS.equip_action(sid, 'equip', item='OccultDiary')['reason']}；"
               f"NopeItem→{SESS.equip_action(sid, 'equip', item='NopeItem')['reason']}",
        counterexample="反例：equip 不校验 equip_slot → 会把日记塞进 R 槽并当成武器 → 变红",
    )

    # 存档往返
    slot = SESS.save_slot(sid, "t63-inv")
    loaded = SESS.load_slot(slot["slot_id"])
    run2 = SESS.get(loaded["sid"])["run"]
    view2 = ST.inventory_view(run2.state)
    s.expect(
        "★ 存档往返后：背包物品与装备槽位**都还在**（JSON 片段见报告）",
        [x["id"] for x in view2["backpack"]] == ["SteakKnife"]
        and view2["equipped"]["R"] is not None
        and view2["equipped"]["R"]["id"] == "SteakKnife",
        detail=f"读档后 backpack={[x['id'] for x in view2['backpack']]} "
               f"equipped.R={(view2['equipped']['R'] or {}).get('id')}",
        counterexample="反例：把 equipped 存在会话 dict 而不在 run.state → 读档后清空 → 变红",
    )

    # 卸下
    r3 = SESS.equip_action(sid, "unequip", slot="R")
    s.expect(
        "卸下后槽位清空、物品**仍在背包**（不会消失）",
        r3["ok"] and r3["inventory"]["weapon"] is None
        and any(x["id"] == "SteakKnife" for x in r3["inventory"]["backpack"]),
        detail=f"unequip ok={r3['ok']}；weapon={r3['inventory']['weapon']}；"
               f"背包={[x['id'] for x in r3['inventory']['backpack']]}",
        counterexample="反例：unequip 把物品从背包一起删掉 → 玩家丢装备 → 变红",
    )

    # 越权：装备不是模型能提议的
    s.expect(
        "★ `equip`/`unequip` **不在 LLM 白名单**里（模型无权替玩家装备）",
        "equip" not in S.LLM_DELTA_WHITELIST
        and "unequip" not in S.LLM_DELTA_WHITELIST
        and "equip" in S.ENGINE_ALLOWED_KEYS,
        detail=f"LLM 白名单={list(S.LLM_DELTA_WHITELIST)}；"
               f"engine 允许 equip={'equip' in S.ENGINE_ALLOWED_KEYS}",
        counterexample="反例：把 equip 加进 LLM 白名单 → 模型可以替玩家换武器 → 变红。"
                       "「模型能碰什么」与「玩家能碰什么」必须是两份清单",
    )
    evil, dropped = S.sanitize_llm_delta({"equip": "FireAxe", "doom": -5}, CFG)
    s.expect(
        "模型提议 `equip` 会被白名单丢弃（越权防护未放松）",
        "equip" not in evil and "doom" not in evil,
        detail=f"保留={sorted(evil)}；丢弃={sorted(map(str, dropped))}",
        counterexample="反例：把 equip 放进白名单 → 出现在保留里 → 变红",
    )


# ---------------------------------------------------------------- 6. 武器判定

def test_weapon_routing(s: TestSuite) -> None:
    """★ 武器 stat 真的被用于判定：SteakKnife→DEX、FireAxe→STR。"""
    wp = {m["id"]: m for m in [
        {"id": "SteakKnife", "stat": "DEX"}, {"id": "FireAxe", "stat": "STR"}]}
    s.expect(
        "items.json 里两把武器都登记了 stat（DEX/STR）与 equip_slot=R",
        ST.item_table()["SteakKnife"].get("stat") == "DEX"
        and ST.item_table()["FireAxe"].get("stat") == "STR"
        and ST.item_table()["SteakKnife"].get("equip_slot") == "R"
        and ST.item_table()["FireAxe"].get("equip_slot") == "R",
        detail=f"SteakKnife={json.dumps(ST.item_table()['SteakKnife'], ensure_ascii=False)[:100]}",
        counterexample="反例：把 stat 字段删掉 → 路由退回基础属性 → 变红",
    )

    card = _card("C-406")
    spec = card["choices"][2]["check"]
    s.expect(
        "C-406 的新选项声明了 `weapon_stat`（走武器属性）+ `requirements.weapon`",
        spec.get("weapon_stat") is True
        and (card["choices"][2].get("requirements") or {}).get("weapon") is True,
        detail=f"C-406#2 = {json.dumps(card['choices'][2], ensure_ascii=False)[:170]}",
        counterexample="反例：只声明 requirements 不声明 weapon_stat → "
                       "装备了却仍走原属性 → 武器只是个门槛、不是判定依据 → 变红",
    )

    # 逐把武器：账本里的 stat 必须跟着武器走
    got = {}
    for wid, want in (("SteakKnife", "DEX"), ("FireAxe", "STR")):
        run = _mk(seed=909, STR=9, DEX=9)
        run.apply({"item_grant": [wid]}, reason="t63")
        run.apply({"equip": wid}, reason="t63")
        run.resolve_card(card, 2)
        chk = [e for e in run.state["log"] if e.get("kind") == "check"][-1]
        got[wid] = (chk["stat"], chk.get("weapon"), chk.get("stat_source"))
    s.expect(
        "★★ 装备 SteakKnife ⇒ 检定走 DEX；装备 FireAxe ⇒ 走 STR（账本留证）",
        got["SteakKnife"][0] == "DEX" and got["FireAxe"][0] == "STR"
        and got["SteakKnife"][1] == "SteakKnife" and got["FireAxe"][1] == "FireAxe",
        detail="；".join(f"{k}: stat={v[0]} weapon={v[1]} source={v[2]!r}"
                        for k, v in got.items()),
        counterexample="反例：`resolve_weapon_stat` 忽略武器、永远返回 spec.stat → "
                       "两把武器都走 STR → FireAxe 那格碰巧对、SteakKnife 那格红 → 变红",
    )
    s.expect(
        "账本里记下了 `stat_source`（可解释「为什么这次走 DEX」）",
        all("（" in str(v[2]) for v in got.values()),
        detail=f"stat_source = {[v[2] for v in got.values()]}",
        counterexample="反例：只记 stat 不记来源 → 玩家看到「CHA 检定」却不知道自己"
                       "拿的是斧头 → 无法排查 → 变红",
    )

    # 未装备时退回原属性（路由不是隐性门槛）
    run0 = _mk(seed=909, STR=9)
    stat0, src0 = run0.resolve_weapon_stat({"stat": "STR", "weapon_stat": True})
    s.expect(
        "未装备武器时**退回 `spec.stat`**（路由本身不做门槛，门槛由 requirements 显式表达）",
        stat0 == "STR" and src0 == "STR",
        detail=f"未装备 → ({stat0!r}, {src0!r})",
        counterexample="反例：未装备时返回空属性名 → 检定拿 stat_value=0 → "
                       "玩家会莫名失败且看不出原因 → 变红",
    )

    # 换武器互斥（R 槽唯一）
    run = _mk(seed=1)
    run.apply({"item_grant": ["FireAxe", "SteakKnife"]}, reason="t63")
    run.apply({"equip": "FireAxe"}, reason="t63")
    r = run.apply({"equip": "SteakKnife"}, reason="t63")
    s.expect(
        "R 槽唯一：换武器时旧件被顶掉（`replaced` 记录）且不再生效",
        run.state["equipped"]["R"] == "SteakKnife"
        and r["applied"]["equip"][0]["replaced"] == "FireAxe"
        and (ST.wielded_weapon(run.state) or {}).get("id") == "SteakKnife",
        detail=f"equipped.R={run.state['equipped']['R']}；"
               f"replaced={r['applied']['equip'][0].get('replaced')}；"
               f"weapon={(ST.wielded_weapon(run.state) or {}).get('id')}",
        counterexample="反例：两个槽位都留着武器 → `wielded_weapon` 取到旧的 → 变红",
    )
    s.expect(
        "`wielded_weapon` 只认 `kind=weapon`（把护符塞进 R 槽不算武器）",
        (lambda rr: (rr.state["equipped"].__setitem__("R", "GruesomeTotem"),
                     ST.wielded_weapon(rr.state) is None)[1])(_mk(seed=2)),
        detail="R 槽放 GruesomeTotem（kind=equipment）→ wielded_weapon 返回 None",
        counterexample="反例：不校验 kind → 图腾被当成武器并按其（不存在的）stat 判定 → 变红",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_stats_items.py —— dc 梯度 / 属性门槛 / 物品栏 / 武器判定")
    for fn in (test_dc_gradient_data, test_dc_matrix, test_stat_gate,
               test_fail_closed, test_inventory, test_weapon_routing):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

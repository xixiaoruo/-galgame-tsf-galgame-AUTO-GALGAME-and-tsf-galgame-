"""`loop.py` 单测 —— 章推进 / DOOM 账本 / 结局判定 / 存档往返。

这些是 V-08 的 A1/A2/A6/A7 的**单元化版本**：探针跑的是整局端到端，
这里把每一层单独钉住，出问题能一眼定位是哪一层（而不是只在整局里看到"某处不对"）。
"""
from __future__ import annotations

import sys

from app.coc import loop as L
from app.coc import provenance as P
from app.coc import state as ST
from app.coc.tests.harness import TestSuite

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _run(cfg, **kw):
    payload = {"difficulty": "Initiate", "seed": kw.pop("seed", 42),
               "stats": {"STR": 5, "DEX": 5, "PER": 5, "KNW": 9, "CHA": 5}}
    payload.update(kw)
    return L.Run.start(payload, cfg)


def _drive_to_first_mystery(run, max_turns=40):
    """推进到第一个谜团完成（每轮重取地点，章后会换）。"""
    i = 0
    while i < max_turns and not run.state.get("ending"):
        run.advance_town(run.available_locations()[i % len(run.available_locations())])
        run.settle_ending()
        if any(e.get("kind") == "mystery_complete" for e in run.events):
            break
        i += 1
    return run


def test_doom_ledger(s: TestSuite) -> None:
    """A2 的单元版：DOOM 每次调查 +1；只有章完成会让它下降，幅度等于难度表。"""
    cfg = P.load_tuning()
    run = _drive_to_first_mystery(_run(cfg))
    downs = [e for e in run.events if e.get("kind") == "delta"
             and "doom" in (e.get("changes") or {}) and e["changes"]["doom"] < 0]
    ups = [e for e in run.events if e.get("kind") == "delta"
           and "doom" in (e.get("changes") or {}) and e["changes"]["doom"] > 0]
    base = int(P.t(cfg, "doom_up.investigate"))
    relief = int(P.t(cfg, "difficulty.Initiate.doom_relief_per_mystery"))
    inv_ups = [e for e in ups if e.get("reason") == "investigate"]
    s.expect(
        "每次调查 DOOM 恰好 +1（= tuning.doom_up.investigate）",
        bool(inv_ups) and all(e["changes"]["doom"] == base for e in inv_ups),
        detail=f"investigate 上升 {len(inv_ups)} 笔，幅度 "
               f"{sorted({e['changes']['doom'] for e in inv_ups})}（tuning={base}）",
        counterexample=f"反例：把 doom_up.investigate 改成 0 或把调查的 apply 注掉 → "
                       f"inv_ups 为空 → 变红",
    )
    s.expect(
        f"DOOM 下降只发生在章完成，且幅度恰好 -{relief}（Initiate 难度表）",
        len(downs) == 1 and downs[0]["reason"] == "mystery_complete"
        and downs[0]["changes"]["doom"] == -relief,
        detail=f"下降 {len(downs)} 笔："
               f"{[(e['reason'], e['changes']['doom']) for e in downs]}（难度表 -{relief}）",
        counterexample="反例：让 complete_mystery 走 clamp（±3 预算）→ 会把 -4 压成 -3 "
                       "→ 幅度不符 → 变红。**这个 bug 真的发生过**，就是被这条抓到的",
        demonstration=f"实测：{[(e['reason'], e['changes']['doom']) for e in downs]}",
    )
    s.expect(
        "章的 key 计数 +1，且 investigations_done 归零",
        int(run.state["keys"]) == 1 and int(run.state["investigations_done"]) == 0,
        detail=f"keys={run.state['keys']}，investigations_done={run.state['investigations_done']}",
        counterexample="反例：complete_mystery 里忘了重置 investigations_done → 下一轮"
                       "立刻又触发章完成 → 变红",
    )


def test_mystery_completion_town_tag(s: TestSuite) -> None:
    """A6 的单元版：章完成追加**恰好 1 个** duration=run 的 town 标签。"""
    cfg = P.load_tuning()
    run = _drive_to_first_mystery(_run(cfg))
    town = [t for t in run.state["tags"] if t.get("kind") == "town"]
    s.expect(
        "章完成追加恰好 1 个 kind=town / duration=run 的标签",
        len(town) == 1 and town[0].get("duration") == "run",
        detail=f"town 标签 = {[(t['id'], t.get('duration')) for t in town]}",
        counterexample="反例：把 _town_tag_delta 改成返回两条，或把 duration 写成 'mystery' "
                       "→ 数量/时长不符 → 变红",
    )
    s.expect(
        "章完成追加的城镇状态在 Run 上也无法被移除（end-to-end，不只在 sanitize 层）",
        (lambda r: (r.apply({"tags_remove": [town[0]["id"]]}, reason="test_remove"),
                    len([t for t in r.state["tags"] if t.get("kind") == "town"]) == 1)[1])(run),
        detail=f"尝试移除 {town[0]['id']} 后仍剩 "
               f"{len([t for t in run.state['tags'] if t.get('kind') == 'town'])} 个 town 标签",
        counterexample="反例：让 loop.apply 走一条绕过 tags_remove 守卫的路径 → 城镇状态被删掉 "
                       "→ 变红。**这条的价值是：证明守卫在整局里也生效，"
                       "而不只在 sanitize 的单元测试里生效**",
    )


def test_ending_decision_function(s: TestSuite) -> None:
    """A5 的单元版：结局是**函数**（读状态+flag），不是分支树。"""
    cfg = P.load_tuning()

    # DOOM 100 → 该古神的专属末日结局
    st = ST.initial_states({"difficulty": "Initiate", "old_god": "Goizo", "seed": 1}, cfg)
    st["doom"] = 100
    eid, notes = L.decide_endings(st, cfg)
    s.expect(
        "doom=100 → 取**该古神**的专属末日结局（Goizo → END_DOOM_GOIZO）",
        eid == "END_DOOM_GOIZO" and any("doom>=100" in n for n in notes),
        detail=f"eid={eid}；notes={notes}",
        counterexample="反例：把古神绑定去掉、一律返回第一个 doom 结局 → 换成 "
                       "Ath-Yolazsth 时会拿到 Goizo 的结局 → 变红（下一条就是这个对照）",
    )
    st2 = ST.initial_states({"difficulty": "Initiate", "old_god": "Ath-Yolazsth", "seed": 1}, cfg)
    st2["doom"] = 100
    eid2, _ = L.decide_endings(st2, cfg)
    s.expect(
        "换个古神 → 不同的末日结局（证明结局是读状态的函数，不是写死的常量）",
        eid2 == "END_DOOM_ATH_YOLAZSTH" and eid2 != eid,
        detail=f"Goizo → {eid}；Ath-Yolazsth → {eid2}",
        counterexample="反例：decide_endings 里 `return table[0]['id']` → 两个古神给出同一个 "
                       "结局 → 变红",
    )

    # STA 地板：last_stand 有 -5 缓冲，未到地板不判死
    st3 = ST.initial_states({"difficulty": "Initiate", "death_mode": "last_stand", "seed": 2}, cfg)
    st3["sta"] = -2
    eid3, notes3 = L.decide_endings(st3, cfg)
    s.expect(
        "last_stand：sta=-2 仍在 -5 缓冲带内 → 不判死",
        eid3 is None and any("缓冲带" in n for n in notes3),
        detail=f"eid={eid3}；notes={notes3}",
        counterexample="反例：把地板判断写成 `sta <= 0`（忽略 death_mode）→ sta=-2 会直接判 "
                       "END_STA_ZERO → 变红。MECHANICS.md §1.3 的 LAST STAND 就是 -5 缓冲带",
    )
    st3["sta"] = -5
    eid4, notes4 = L.decide_endings(st3, cfg)
    s.expect(
        "last_stand：sta=-5 缓冲带用尽 → 判 END_STA_ZERO",
        eid4 == "END_STA_ZERO",
        detail=f"eid={eid4}；notes={notes4}",
        counterexample="反例：把地板写成 `< -5`（差一寸）→ sta=-5 不判死 → 变红",
    )

    # REA 归零 → 精神病院
    st5 = ST.initial_states({"difficulty": "Initiate", "death_mode": "sudden_death", "seed": 3}, cfg)
    st5["rea"] = 0
    eid5, _ = L.decide_endings(st5, cfg)
    s.expect(
        "REA 归零 → END_REA_ZERO（精神崩溃进精神病院，与 STA 归零是不同结局）",
        eid5 == "END_REA_ZERO",
        detail=f"eid={eid5}",
        counterexample="反例：把 REA 与 STA 合用一个结局 id → 两个失败模式不再有不同收束文本 "
                       "→ 变红（§2.5 设计建议明确「双血条优于单一 SAN…文学收益翻倍」）",
    )

    # 幂等
    st6 = ST.initial_states({"difficulty": "Initiate", "seed": 4}, cfg)
    st6["doom"] = 100
    a, _ = L.decide_endings(st6, cfg)
    b, _ = L.decide_endings(st6, cfg)
    s.expect(
        "结局判定幂等（重复调用同值，说明是纯函数而非消耗状态的副作用）",
        a == b == "END_DOOM_GOIZO",
        detail=f"两次判定：{a} / {b}",
        counterexample="反例：把 decide_endings 写成 pop() 掉规则表（有副作用）→ 第二次返回 "
                       "不同值 → 变红",
    )


def test_card_draw_avoids_repeats(s: TestSuite) -> None:
    """已见卡不重复抽（§9.4 坑 1「重复劳作 / 背板」的规避）。"""
    cfg = P.load_tuning()
    run = _run(cfg, seed=777)
    loc = run.available_locations()[0]
    seen = []
    for _ in range(6):
        c = run.draw_card(loc)
        if c is None:
            break
        seen.append(c["id"])
        run.state.setdefault("seen_cards", []).append(c["id"])
    s.expect(
        "连续抽 6 次（每次记入已见）→ 抽到的 id 不重复",
        len(seen) == len(set(seen)) and len(seen) > 0,
        detail=f"抽到的卡：{seen}",
        counterexample="反例：去掉 `fresh = [c for c in pool if c['id'] not in seen]` 过滤 → "
                       "会出现重复 id → 变红。**只测'抽得到卡'是抓不到重复的**，"
                       "所以这里断言的是集合大小而不是非空",
    )
    s.expect(
        "某地点 6 张卡全部抽完后 → 返回 None（而不是重抽一张已见的）",
        run.draw_card(loc) is None,
        detail=f"该地点共 {len([c for c in run.card_pool if loc in (c.get('locations') or [])])} 张，"
               f"已抽 {len(seen)} 张；allow_seen 默认 False → {run.draw_card(loc)}",
        counterexample="反例：把 draw_card 的兜底改成 `use = pool`（无 fresh 时也抽）→ "
                       "会返回一张已见卡 → 变红",
    )


def test_save_restore_consistency(s: TestSuite) -> None:
    """A7 的单元版：存 → 读 → 再存，状态与 rng 都一致，且**事件账本也在**。"""
    cfg = P.load_tuning()
    run = _drive_to_first_mystery(_run(cfg, seed=31337))
    snap1 = run.snapshot()
    path = run.save()
    restored = ST.load(run.state["sid"])
    run2 = L.Run.resume(restored, cfg)
    snap2 = run2.snapshot()
    eq, diff = ST.state_equal(snap1, snap2)
    s.expect(
        "存→读 后状态逐字节一致（含 log / tags / flags / rng_state）",
        eq,
        detail=f"一致={eq}；差异={diff or '无'}；存档={path.split(chr(92))[-1]}",
        counterexample="反例：Run.resume 里不还原 self.events（本任务**真的**犯过这个错）→ "
                       "snap2 的 log 会是空数组 → 差异显示在 log 行 → 变红",
        demonstration="实测：第一版 resume 没还原 events，这条红在 log 的 37 行差异上",
    )
    s.expect(
        "读档后事件账本非空且条数一致（A2 依赖它，所以它必须一起持久化）",
        len(run2.events) == len(run.events) and len(run.events) > 0,
        detail=f"原 {len(run.events)} 条 / 读档后 {len(run2.events)} 条",
        counterexample="反例：把 state['log'] 从 snapshot 里去掉 → 读档后 events 为空 → "
                       "A2 的'DOOM 为何变化'就无从判定 → 变红",
    )
    run2.save()
    rerestored = ST.load(run.state["sid"])
    eq2, diff2 = ST.state_equal(snap2, ST.to_json_obj(rerestored))
    s.expect(
        "再存→再读 仍然一致（幂等，不是'第一次碰巧对'）",
        eq2,
        detail=f"二次一致={eq2}；差异={diff2 or '无'}",
        counterexample="反例：save 里对事件做累加（append 而非覆盖）→ 二次存储会多出条目 → 变红",
    )
    r1 = ST.restore_rng(snap1["rng_state"])
    r2 = ST.restore_rng(snap2["rng_state"])
    seq1 = [r1.randint(1, 9999) for _ in range(8)]
    seq2 = [r2.randint(1, 9999) for _ in range(8)]
    s.expect(
        "读档后 rng 继续给出**完全相同**的随机序列（含后续检定）",
        seq1 == seq2,
        detail=f"两次 8 连抽：{seq1}",
        counterexample="反例：只存 random() 的种子而不存 getstate → 若局中已消耗过随机数，"
                       "读档后会从头开始 → 序列不同 → 变红",
    )


def test_phase_guards(s: TestSuite) -> None:
    """阶段守卫：结局后不能再推进；地点必须在可选范围内。"""
    cfg = P.load_tuning()
    run = _run(cfg, seed=5)
    run.state["ending"] = "END_STA_ZERO"
    run.state["phase"] = "ending"
    raised = None
    try:
        run.advance_town(run.available_locations()[0])
    except L.LoopError:
        raised = "LoopError"
    s.expect(
        "已判定结局后推进 → LoopError（不会悄悄继续跑）",
        raised == "LoopError",
        detail=f"advance_town 在 phase=ending 时 → {raised}",
        counterexample="反例：去掉 phase 守卫 → 结局后还能继续调查、DOOM 还会涨 → 变红",
    )
    run2 = _run(cfg, seed=6)
    raised2 = None
    try:
        run2.advance_town("NoSuchLocation")
    except L.LoopError:
        raised2 = "LoopError"
    s.expect(
        "地点不在当前可选范围 → LoopError",
        raised2 == "LoopError",
        detail=f"advance_town('NoSuchLocation') → {raised2}；可选={run2.available_locations()}",
        counterexample="反例：去掉地点校验 → 会用不存在的威胁等级去掷骰，静默取默认 → 变红",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_loop.py —— DOOM 账本 / 章推进 / 结局判定 / 存档往返")
    for fn in (test_doom_ledger, test_mystery_completion_town_tag,
               test_ending_decision_function, test_card_draw_avoids_repeats,
               test_save_restore_consistency, test_phase_guards):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

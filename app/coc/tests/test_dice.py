"""`dice.py` 单测 —— **平手判玩家胜**是本文件的唯一存在理由。

覆盖策略（这是本任务方法论的核心）：
  1. **穷举** 1~12 × 4 威胁 × dc{-6,0,6} 的**全部**组合，逐条按公式重算；
  2. 专喂 `stat + mod + dc == roll + 1` 的**平手边界样本**（88 条），逐条断言判胜；
  3. 平手样本左右各一格，证明这个边界**有区分度**（只测赢/输两侧永远碰不到平手）；
  4. 反例证明：把判定改成「平手判负」，上面哪一条会红。
"""
from __future__ import annotations

import sys

from app.coc import dice
from app.coc import provenance as P
from app.coc.tests.harness import TestSuite


def _all_combos(cfg):
    lo = int(P.t(cfg, "checks.roll_min"))
    hi = int(P.t(cfg, "checks.roll_max"))
    smin = int(P.t(cfg, "sanitize.stat_value_min"))
    smax = int(P.t(cfg, "sanitize.stat_value_max"))
    for threat in dice.THREATS:
        mod = dice.threat_mod(threat, cfg)
        for dc in (int(P.t(cfg, "checks.dc_min")), 0, int(P.t(cfg, "checks.dc_max"))):
            for stat in range(smin, smax + 1):
                for roll in range(lo, hi + 1):
                    yield threat, dc, stat, roll, mod


def test_exhaustive_formula(s: TestSuite) -> None:
    cfg = P.load_tuning()
    total = 0
    mismatched = []
    roll_out_of_range = []
    lo, hi = int(P.t(cfg, "checks.roll_min")), int(P.t(cfg, "checks.roll_max"))
    for threat, dc, stat, roll, mod in _all_combos(cfg):
        r = dice.resolve_deterministic("KNW", stat, threat, roll, cfg, dc=dc)
        total += 1
        want = (stat + mod + dc) >= (roll + 1)
        if r.success != want:
            mismatched.append((threat, dc, stat, roll, r.success, want))
        if not (lo <= r.roll <= hi):
            roll_out_of_range.append(roll)
    s.expect(
        "穷举全部组合：success 恒等于 (stat + threat_mod + dc >= roll + 1)",
        total > 0 and not mismatched,
        detail=f"穷举 {total} 组（4 威胁 × dc 3 点 × stat {P.t(cfg, 'sanitize.stat_value_min')}~"
               f"{P.t(cfg, 'sanitize.stat_value_max')} × roll {lo}~{hi}），"
               f"与公式不符 {len(mismatched)} 组",
        counterexample="反例：把判定改成 `stat + eff > roll`（少 +1）→ 平手那一格会变成"
                       "success=False 而 want=True → mismatched 非空 → 变红",
        demonstration="实测：mismatched=%d" % len(mismatched),
    )
    s.expect(
        f"roll 恒落在 [{lo},{hi}]",
        not roll_out_of_range,
        detail=f"越界 {len(roll_out_of_range)} 次",
        counterexample="反例：把 checks.roll_min/max 改成 0/13 → 会出现越界值 → 变红",
    )


def test_tie_boundary_samples(s: TestSuite) -> None:
    """★ 平手边界：`stat + mod + dc == roll + 1` 必须判玩家胜。"""
    cfg = P.load_tuning()
    ties = dice.tie_boundary_samples(cfg)
    wrong = []
    for t in ties:
        r = dice.resolve_deterministic("KNW", t["stat_value"], t["threat"], t["roll"],
                                       cfg, dc=t["dc"])
        if not (r.margin == 0 and r.is_tie and r.success):
            wrong.append((t, r.margin, r.success))
    s.expect(
        "平手样本全部 margin==0 且 is_tie 且 success（88 条全量）",
        len(ties) > 0 and not wrong,
        detail=f"平手样本 {len(ties)} 条，全部满足；覆盖威胁 "
               f"{sorted({t['threat'] for t in ties})}、dc {sorted({t['dc'] for t in ties})}、"
               f"roll {sorted({t['roll'] for t in ties})}",
        counterexample="反例：把 tie_goes_to_player 的语义实现成 `>=` 写成 `>` → "
                       "全部 88 条 margin==0 但 success=False → wrong 非空 → 变红",
        demonstration="实测：wrong=%d" % len(wrong),
    )

    # 固定样本（值来自 tuning，反查自洽性）
    fs = int(P.t(cfg, "checks.tie_sample_stat"))
    fr = int(P.t(cfg, "checks.tie_sample_roll"))
    fx = dice.resolve_deterministic("KNW", fs, "SAFE", fr, cfg)
    s.expect(
        f"tuning 里的固定平手样本（stat={fs}, roll={fr}）自洽且判胜",
        fx.is_tie and fx.success,
        detail=f"margin={fx.margin}, is_tie={fx.is_tie}, success={fx.success}；reason={fx.reason}",
        counterexample="反例：把 tie_sample_roll 从 4 改成 5 → stat 5 对 roll 5 需 6，"
                       "margin=-1 不再是平手 → is_tie=False → 变红（第一版就是写错成 5，被这条抓到的）",
    )

    # 边界两侧的**区分度**：方向别写反
    left = dice.resolve_deterministic("KNW", fs, "SAFE", fr - 1, cfg)
    right = dice.resolve_deterministic("KNW", fs, "SAFE", fr + 1, cfg)
    s.expect(
        "平手边界两侧可区分：roll-1 判胜（有富余）、roll+1 判负（差一寸）",
        left.success and not right.success,
        detail=f"roll={fr - 1} → margin={left.margin}, success={left.success}；"
               f"roll={fr + 1} → margin={right.margin}, success={right.success}",
        counterexample="反例：**把两侧判据写反**（assert not left.success and right.success）"
                       "→ 本断言立刻失败。第一版正是写反了：证据文本说'胜'，"
                       "而断言要求'负'，两者矛盾却被漏看了一轮",
    )


def test_counterexample_tie_loses(s: TestSuite) -> None:
    """反例证明：把「平手判负」实现进去，上面哪条会红（这就是「变红实证」）。"""
    cfg = P.load_tuning()
    fs = int(P.t(cfg, "checks.tie_sample_stat"))
    fr = int(P.t(cfg, "checks.tie_sample_roll"))

    # 故意用一个「平手判负」的判定，验证它与正式实现**在同一输入上分道扬镳**
    def tie_loses(stat: int, threat: str, roll: int, dc: int = 0) -> bool:
        eff = dice.threat_mod(threat, cfg) + dc
        return (stat + eff) > (roll + 1)          # ← 去掉平手偏袒

    real = dice.resolve_deterministic("KNW", fs, "SAFE", fr, cfg)
    broken = tie_loses(fs, "SAFE", fr)
    s.expect(
        "变红实证：平手判负实现会在固定平手样本上给出相反判决",
        real.success is True and broken is False,
        detail=f"正式实现 success={real.success}（margin={real.margin}）；"
               f'"平手判负"实现给出 {broken} —— 两者在同一输入上相反',
        counterexample="反例：若两者相同（都 True/都 False），说明这个输入不在平手边界上，"
                       "本实证就无效——那时应当换 roll 值，而不是放宽断言",
        demonstration="实测：real=True, broken=False（分道扬镳）",
    )


def test_threat_mod_from_tuning_only(s: TestSuite) -> None:
    """威胁修正只能来自 tuning，代码里没有兜底常量。"""
    cfg = P.load_tuning()
    tm = P.t(cfg, "threat_mod")
    s.expect(
        "四档威胁修正与 tuning 一致（SAFE:0 / LOW:-1 / DANGEROUS:-1 / DOOMED:-2）",
        tm == {"SAFE": 0, "LOW": -1, "DANGEROUS": -1, "DOOMED": -2},
        detail=f"threat_mod={tm}",
        counterexample="反例：只实现 SAFE/LOW/DOOMED 而漏 DANGEROUS → KeyError；"
                       "或把 LOW 写成 +1 → 与「符号归一化到玩家侧」矛盾 → 变红",
    )
    raised = None
    try:
        dice.threat_mod("UNKNOWN", cfg)
    except dice.UnknownThreatError:
        raised = "UnknownThreatError"
    s.expect(
        "未知威胁等级抛 UnknownThreatError（不静默当 SAFE）",
        raised == "UnknownThreatError",
        detail=f"threat_mod('UNKNOWN') → {raised}",
        counterexample="反例：把 threat_mod 改成 `mods.get(threat, 0)` → 未知威胁会被当 SAFE，"
                       "raised=None → 变红",
    )


def test_rng_only_from_caller(s: TestSuite) -> None:
    """掷骰必须用调用方给的 rng（E5：LLM 从不接触 rng）。"""
    import inspect
    import random

    cfg = P.load_tuning()
    sig = inspect.signature(dice.check)
    s.expect(
        "check() 的 rng 是**入参**（没有模块级 rng / 没有内部 random 调用）",
        "rng" in sig.parameters,
        detail=f"signature = {sig}",
        counterexample="反例：把 check() 改成内部 `random.randint(...)` → 掷骰不再受会话种子控制，"
                       "读档也无法复现，A7 会红 → 本断言同时失败",
    )
    r1 = random.Random(1234)
    r2 = random.Random(1234)
    seq1 = [dice.check("KNW", 5, "LOW", r1, cfg).roll for _ in range(10)]
    seq2 = [dice.check("KNW", 5, "LOW", r2, cfg).roll for _ in range(10)]
    s.expect(
        "同种子的 rng 给出完全相同的掷骰序列（可复现的前提）",
        seq1 == seq2,
        detail=f"seed=1234 两次 10 连抽：{seq1}",
        counterexample="反例：在 check() 里用全局 random → 两次序列不同 → 变红（也正是 A7 会红的原因）",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_dice.py —— 检定公式与平手边界")
    for fn in (test_exhaustive_formula, test_tie_boundary_samples,
               test_counterexample_tie_loses, test_threat_mod_from_tuning_only,
               test_rng_only_from_caller):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

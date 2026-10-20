"""`tuning.json` 的**溯源机制**单测 —— 本任务的第一条，也是「先红后绿」的载体。

被测语义（`COC-DESIGN.md` §0.2 实现铁律）：
> `tuning.json` 中所有来自 §12 不确定项的数值，必须带 `"_unverified": true` 与 `"_source"`。

**这条要求本身是容易写的废话**（「我们会加注释」）。所以本测试的关键不是
"真文件有来源"（那只是绿的一侧），而是**先证明它会对缺来源的输入变红**：
`test_red_first_proof()` 在内存副本里挖掉一个 `_source`，断言 `ProvenanceError` 被抛出。
如果这一步不红，说明机制根本没生效 —— 那才是真正的失败。
"""
from __future__ import annotations

import sys

from app.coc import provenance as P
from app.coc.tests.harness import TestSuite

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def test_red_first_proof(s: TestSuite) -> None:
    """★ 先红后绿：缺来源必须真的抛错（而不是「我们相信它不会」)。"""
    real = P.load_tuning()

    # ---- 红①：挖掉 _source ----
    broken = P.deep_copy(real)
    victim = broken["combat"]["time_budget_turn"]
    removed = victim.pop("_source")
    raised, msg = None, ""
    try:
        P.validate_provenance(broken)
    except P.ProvenanceError as exc:
        raised, msg = "ProvenanceError", str(exc)
    s.expect(
        "红①·缺 _source 必须抛 ProvenanceError",
        raised == "ProvenanceError",
        detail=f"挖掉 combat.time_budget_turn._source（原值 {removed[:40]}…）后 "
               f"validate_provenance 抛出 {raised}：{msg[:120]}",
        counterexample="反例：把 validate_provenance 里的 _REQUIRED_LEAF_KEYS 检查删掉 → "
                       "这里 raised 会变成 None → 本用例失败（这正是它要证明的）",
        demonstration=f"实测：raised={raised}",
    )

    # ---- 红②：_unverified 写成非 bool（最阴的一种「看起来标了」） ----
    broken2 = P.deep_copy(real)
    broken2["combat"]["max_actions_per_turn"]["_unverified"] = "true"
    raised2, msg2 = None, ""
    try:
        P.validate_provenance(broken2)
    except P.ProvenanceError as exc:
        raised2, msg2 = "ProvenanceError", str(exc)
    s.expect(
        "红②·_unverified 写成字符串 'true' 必须抛错（不能把'看起来标了'当标了）",
        raised2 == "ProvenanceError",
        detail=f"_unverified='true'（字符串）→ {raised2}：{msg2[:120]}",
        counterexample="反例：把校验放宽成 `if not node['_unverified']` → 字符串 'true' 是 truthy，"
                       "raised 会变成 None → 本用例失败",
        demonstration=f"实测：raised={raised2}",
    )

    # ---- 红③：_source 写成空串 ----
    broken3 = P.deep_copy(real)
    broken3["checks"]["tie_sample_roll"]["_source"] = "   "
    raised3 = None
    try:
        P.validate_provenance(broken3)
    except P.ProvenanceError:
        raised3 = "ProvenanceError"
    s.expect(
        "红③·_source 是空白串必须抛错",
        raised3 == "ProvenanceError",
        detail=f"_source='   ' → {raised3}",
        counterexample="反例：把校验改成 `if '_source' not in node` → 空串会被放过 → raised=None → 失败",
        demonstration=f"实测：raised={raised3}",
    )

    # ---- 绿：真实文件必须通过，且**确实遍历到了东西** ----
    n = P.validate_provenance(real)
    s.expect(
        "绿·真实 tuning.json 通过校验且值节点数 > 0",
        n > 0,
        detail=f"validate_provenance 返回已校验值节点数 = {n}",
        counterexample="反例：把 tuning.json 清空成 {} → n=0 → 本断言失败。"
                       "（**这条防的是「空文件也能通过」这种假绿**：只断言"
                       "「不抛异常」的话，{} 也会通过。）",
        demonstration=f"实测：n={n}",
    )


def test_real_data_has_sources(s: TestSuite) -> None:
    """真文件里每个值节点都带来源，并给出统计。"""
    real = P.load_tuning()
    nodes = P.walk_value_nodes(real)
    missing = [(at, node) for at, node in nodes if not node.get("_source")]
    s.expect(
        "每个值节点都有非空 _source",
        not missing and len(nodes) > 0,
        detail=f"值节点 {len(nodes)} 个，缺 _source 的 {len(missing)} 个",
        counterexample="反例：在 tuning.json 里加一个 {\"value\": 5} 的小节（不带 _source）→ "
                       "load_tuning() 会直接抛 ProvenanceError（加载期就挡住，不是等到测试）",
    )
    unverified = P.unverified_leaves(real)
    s.expect(
        "L2（单一来源待验证）数值都被标为 _unverified",
        len(unverified) > 0,
        detail=f"_unverified=true 的数值路径 {len(unverified)} 条，例："
               + "、".join(unverified[:4]),
        counterexample="反例：把 threat_mod._unverified 改成 false → 该条会从这份清单里消失，"
                       "而它恰恰来自 MECHANICS.md §12 #8 的不确定项 —— 这就是'把单一来源当已确证'",
    )


def test_three_conflicts_are_recorded(s: TestSuite) -> None:
    """三处冲突点的处理必须留在数据里可查（不是只写在设计文档里）。"""
    real = P.load_tuning()

    tb = P.meta(real, "combat.time_budget_turn")
    ma = P.meta(real, "combat.max_actions_per_turn")
    s.expect(
        "200 时间单位 / 8 动作：标为未二次确认且带来源",
        tb.get("_unverified") is True and ma.get("_unverified") is True
        and "§12 #6" in str(tb.get("_source", "")),
        detail=f"time_budget_turn._source 含 §12 #6：{'§12 #6' in str(tb.get('_source'))}；"
               f"两者 _unverified 均为 True",
        counterexample="反例：把它们的 _unverified 改成 false（当成已确证事实）→ 本断言失败；"
                       "这正是任务书点名「不得把单一来源当已确证」的第一点",
    )

    hs = P.meta(real, "combat.hit_kick_fixed")
    hfm = P.meta(real, "combat._hit_formula")
    hf = P.t(real, "combat._hit_formula")
    s.expect(
        "命中率 vs Kick 80%：用统一公式调和（两说并存），而非二选一",
        hs.get("_unverified") is True
        and hfm.get("_unverified") is True
        and "两说并存" in str(hfm.get("_source", ""))
        and "base_hit" in str(hf) and "level_bonus" in str(hf),
        detail=f"_hit_formula = {str(hf)[:80]}…；「两说并存」记在 _hit_formula._source "
               f"（{'两说并存' in str(hfm.get('_source'))}），"
               f"hit_kick_fixed._source 只记 Combat 页的原始说法",
        counterexample="反例：把 _hit_formula 删掉、只留 hit_kick_fixed=80 → "
                       "Math_Guide 的公式就没有落点了，本断言失败",
    )

    tm = P.meta(real, "threat_mod")
    s.expect(
        "威胁修正符号：只实现方向 + 记录两页符号相反",
        "符号相反" in str(tm.get("_source", "")) and P.t(real, "threat_mod")["LOW"] == -1,
        detail=f"threat_mod={P.t(real, 'threat_mod')}；来源注明「符号相反方向一致」"
               f"={'符号相反' in str(tm.get('_source'))}",
        counterexample="反例：把 LOW 从 -1 改成 +1（照抄 Math_Guide 给掷骰方）→ "
                       "与归一到玩家侧的约定矛盾，且数值不再等价 → 本断言失败",
    )


def test_paths_and_missing_file(s: TestSuite) -> None:
    """tuning.json 缺失时必须是**明确的加载失败**，而不是退回代码内常量。"""
    import os
    real_path = P.paths.TUNING_PATH
    s.expect(
        "tuning.json 路径在项目 data/coc 下",
        os.path.basename(real_path) == "tuning.json" and "coc" in real_path,
        detail=real_path,
        counterexample="反例：把路径指向 D:/TSF_Galgame_Data（本任务 outOfScope）→ 本断言失败；"
                       "那条路径属于接线任务",
    )
    # 用临时改名验证「缺文件 → TuningMissingError」（改回原状）
    tmp = real_path + ".bak-test"
    moved = False
    try:
        os.replace(real_path, tmp)
        moved = True
        raised = None
        try:
            P.load_tuning()
        except P.TuningMissingError:
            raised = "TuningMissingError"
        except P.ProvenanceError:
            raised = "ProvenanceError(错) "
        s.expect(
            "tuning.json 不存在时抛 TuningMissingError（而非静默兜底）",
            raised == "TuningMissingError",
            detail=f"临时移走 tuning.json → load_tuning() 抛 {raised}",
            counterexample="反例：在 load_tuning 里加 `except: return {}` 兜底 → raised 变成 None，"
                           "且调用方会在 KeyError 里迷路 → 本断言失败",
            demonstration="实测：把文件临时改名为 *.bak-test 再调 load_tuning()",
        )
    finally:
        if moved:
            os.replace(tmp, real_path)


def main() -> int:
    s = TestSuite("app/coc/tests/test_tuning_provenance.py —— 数值溯源机制（先红后绿）")
    for fn in (test_red_first_proof, test_real_data_has_sources,
               test_three_conflicts_are_recorded, test_paths_and_missing_file):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

"""三硬闸门单测 —— **空串 + 最终 prompt 逐字节相等**。

这是用户成人向内容要求的安全边界，也是"分支存在 ≠ 走了那条分支"的正面解法：
不用断言"有条件分支"，而是断言**最终产物的字节等于没有该层时的产物**。

两条独立断言（缺一不可）：
  ① **单元级**：`horror_erotic_block(...) == ""`，且**连段落标题都不写**
     （留空标题会让模型看到「【恐怖×情色写作技法】」却没有内容 → 自行脑补 → 等于闸门没生效）；
  ② **集成级**：拼装完的**最终 system prompt 字符串**与「不注入该层」的基线
     **逐字节相等**（长度与内容都比）。

另外两条是**防恒真**的对照（没有它们，①② 可能被一个"从不注入"的实现骗过）：
  ③ 门通过时 block 非空且含标题；
  ④ 门通过时最终 prompt **必须**比基线长。
"""
from __future__ import annotations

import sys

from app.coc import loop as L
from app.coc import prompt as P
from app.coc import provenance as PR
from app.coc import rules_horror_erotic as R
from app.coc.tests.harness import TestSuite

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

#: 空串场景（G2 未过）：任务书点名的三组里，all / 16 属空串；温和档属"降档"
EMPTY_CASES = (
    ("rating=all", dict(content_rating="all", r18_enabled=False,
                        adult_intensity="浓烈", participants=["Kirie"])),
    ("rating=16", dict(content_rating="16", r18_enabled=False,
                       adult_intensity="浓烈", participants=["Kirie"])),
    ("r18_enabled=False", dict(content_rating="18", r18_enabled=False,
                               adult_intensity="浓烈", participants=["Kirie"])),
    ("participants 为空（未声明）", dict(content_rating="18", r18_enabled=True,
                                    adult_intensity="浓烈", participants=[])),
    ("参与者不在成年白名单", dict(content_rating="18", r18_enabled=True,
                            adult_intensity="浓烈", participants=["Nobody"])),
)


def test_g1_g2_empty(s: TestSuite) -> None:
    cfg = PR.load_tuning()
    for label, kw in EMPTY_CASES:
        gate = R.resolve_gate(kw["content_rating"], kw["r18_enabled"],
                              kw["adult_intensity"], kw["participants"], cfg)
        block = R.horror_erotic_block(gate.erotic_tier, gate, {}, cfg)
        s.expect(
            f"单元级：{label} → block 为空串且不含「恐怖」「情色」字样",
            gate.passed is False and block == ""
            and "恐怖" not in block and "情色" not in block,
            detail=f"passed={gate.passed} block={block!r} reasons={gate.reasons}",
            counterexample="反例：把 horror_erotic_block 的闸门判断删掉（只按 tier 渲染）→ "
                           "这一组会返回带标题的整段 → 变红。"
                           "**关键是 block=='' 而不是'有条件分支'**：留空标题也算失败，"
                           "所以这里额外断言不含「恐怖」「情色」字样",
        )


def test_g3_downgrade_only(s: TestSuite) -> None:
    """G3：保守档只降不升（温和 → subtle），且不是空串。"""
    cfg = PR.load_tuning()
    gate = R.resolve_gate("18", True, "温和", ["Kirie"], cfg)
    block = R.horror_erotic_block(gate.erotic_tier, gate, {}, cfg)
    s.expect(
        "G3：温和档 → 闸门通过但情色档被钳到 subtle（只降不升，且确实注入了内容）",
        gate.passed and gate.erotic_tier == "subtle" and block != ""
        and any("G3" in r for r in gate.reasons),
        detail=f"passed={gate.passed} tier={gate.erotic_tier} len(block)={len(block)} "
               f"reasons={gate.reasons}",
        counterexample="反例：去掉 G3 钳制 → tier 会保持 浓烈→explicit（升档）→ 变红。"
                       "G3 的语义是**只能降不能升**（COC-DESIGN §5.3）",
    )
    # 升档必须由显式参数决定，不能由状态推断
    gate2 = R.resolve_gate("18", True, "浓烈", ["Kirie"], cfg)
    s.expect(
        "浓烈档 → explicit（与温和档形成对照，证明钳制真的按档位走）",
        gate2.erotic_tier == "explicit",
        detail=f"浓烈 → {gate2.erotic_tier}；温和 → {gate.erotic_tier}",
        counterexample="反例：把 INTENSITY_TO_TIER 全部映射成 subtle → 浓烈档也变 subtle → 红",
    )


def test_title_written_when_open(s: TestSuite) -> None:
    """防恒真 ③：门通过时**必须**写标题（否则上一条的空串可能只是"永远不写标题"）。"""
    cfg = PR.load_tuning()
    gate = R.resolve_gate("18", True, "浓烈", ["Kirie"], cfg)
    block = R.horror_erotic_block(gate.erotic_tier, gate, {}, cfg)
    s.expect(
        "门通过时 block 非空且含 BLOCK_TITLE",
        R.BLOCK_TITLE in block and len(block) > 100,
        detail=f"len={len(block)}；含标题={R.BLOCK_TITLE in block}",
        counterexample="反例：把 BLOCK_TITLE 从渲染里去掉 → 本断言红，且"
                       "「空串」断言也随之失去意义（它可能只是永远不写标题）",
    )


def test_final_prompt_byte_equality(s: TestSuite) -> None:
    """★ 集成级：最终 system prompt 与「不注入该层」的基线**逐字节相等**。"""
    cfg = PR.load_tuning()
    run = L.Run.start({"difficulty": "Initiate", "seed": 42}, cfg)

    # 空串场景：显式传入一个"未通过"的 gate（模拟调用方没先判就直接调）
    dead = R.resolve_gate("all", False, "浓烈", ["Kirie"], cfg)
    baseline = P.coc_system(run, cfg=cfg, content_rating="all", r18_enabled=False,
                            adult_intensity="浓烈", participants=["Kirie"], gate=dead)
    # 基线：完全不给该层参数（gate=None 且 participants 为空 → 同样未通过）
    baseline2 = P.coc_system(run, cfg=cfg, content_rating="all", r18_enabled=False,
                             adult_intensity="浓烈", participants=[], gate=None)
    s.expect(
        "空串场景：注入与不注入的**最终 prompt 长度相等且逐字节相等**",
        baseline == baseline2 and len(baseline) == len(baseline2),
        detail=f"len={len(baseline)} vs {len(baseline2)}；equal={baseline == baseline2}；"
               f"两串均不含标题={R.BLOCK_TITLE not in baseline}",
        counterexample="反例：把 coc_system 里改成 `parts.append(block)`（无条件 append）→ "
                       "空串会在列表里留下一个空元素 → join 后多一个 '\\n\\n' → 长度不等 → 变红。"
                       "**半生效（注入了空段但留了空行/标题）就是被这条抓住的**",
    )

    # 防恒真 ④：门通过时最终 prompt 必须更长
    live = P.coc_system(run, cfg=cfg, content_rating="18", r18_enabled=True,
                        adult_intensity="浓烈", participants=["Kirie"], gate=None)
    s.expect(
        "防恒真：门通过时最终 prompt 必须**比基线长**",
        len(live) > len(baseline) and R.BLOCK_TITLE in live,
        detail=f"live len={len(live)} > baseline len={len(baseline)}；"
               f"live 含标题={R.BLOCK_TITLE in live}",
        counterexample="反例：若 live == baseline，说明该层从来不会被注入 → 上一条的'相等'"
                       "任何实现都能满足 → 那条断言就成了废话。这条是它的对照",
    )


def test_block_budget(s: TestSuite) -> None:
    """长度预算：subtle ≤400 字，其余 ≤900（COC-DESIGN §5.5）。"""
    cfg = PR.load_tuning()
    g_sub = R.resolve_gate("18", True, "温和", ["Kirie"], cfg)
    b_sub = R.horror_erotic_block(g_sub.erotic_tier, g_sub, {}, cfg)
    g_exp = R.resolve_gate("18", True, "浓烈", ["Kirie"], cfg)
    b_exp = R.horror_erotic_block(g_exp.erotic_tier, g_exp, {}, cfg)
    s.expect(
        "subtle ≤400 字、explicit ≤900 字，且 subtle 比 explicit 短（确实做了压缩）",
        0 < len(b_sub) <= 400 and 0 < len(b_exp) <= 900 and len(b_sub) < len(b_exp),
        detail=f"subtle={len(b_sub)} explicit={len(b_exp)}",
        counterexample="反例：去掉 subtle 的 H2/H3 压缩 → 两者同长 → 变红。"
                       "上游 §5.5 要求保守档收窄，不是只换一个标签",
    )


def test_block_contains_no_adult_text(s: TestSuite) -> None:
    """本层是**规则**，不是成人向正文：条目必须是祈使式约束，不含露骨描写。"""
    cfg = PR.load_tuning()
    g = R.resolve_gate("18", True, "浓烈", ["Kirie"], cfg)
    block = R.horror_erotic_block(g.erotic_tier, g, {}, cfg)
    banned = ("色情", "肉棒", "阴道", "乳房", "做爱", "射精", "高潮")   # 露骨词
    hits = [w for w in banned if w in block]
    s.expect(
        "该层输出的是**写作规则**而非成人向正文（关键词黑名单扫描）",
        not hits,
        detail=f"扫描 {len(block)} 字，命中 {hits}",
        counterexample="反例：把某条规则改写成一段露骨范例句 → 命中非空 → 变红。"
                       "本任务只设计**结构**、不写文本（用户明确要求）",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_gate_empty.py —— 三硬闸门（空串 + 逐字节相等）")
    for fn in (test_g1_g2_empty, test_g3_downgrade_only, test_title_written_when_open,
               test_final_prompt_byte_equality, test_block_budget,
               test_block_contains_no_adult_text):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

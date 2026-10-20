"""`state.py` 单测 —— 初始值来源、序列化往返、面板/状态块的信息遮蔽。

覆盖 vn.py 范式的复用点（COC-DESIGN §2.1 对应表）：
`initial_states` / `panel_view` / `states_block`，以及与 vn.py 的**故意差异**
（本模式只暴露数值、不暴露标签名 —— 信息遮蔽）。
"""
from __future__ import annotations

import json
import sys

from app.coc import provenance as P
from app.coc import state as ST
from app.coc.tests.harness import TestSuite

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

#: A8 的禁词（结算前不得告诉玩家要考哪个属性）
FORBIDDEN = ("STR", "DEX", "PER", "KNW", "CHA", "检定")


def test_initial_values_from_difficulty(s: TestSuite) -> None:
    cfg = P.load_tuning()
    for tier, want in (("Sceptic", 21), ("Initiate", 21), ("Cultist", 17)):
        st = ST.initial_states({"difficulty": tier, "seed": 1}, cfg)
        d = P.t(cfg, "difficulty")[tier]
        s.expect(
            f"{tier}：初始 sta/rea/funds/mystery_count 全部取自难度表",
            st["sta"] == d["sta"] and st["rea"] == d["rea"]
            and st["funds"] == d["funds"] and st["mystery_count"] == d["mystery_count"],
            detail=f"sta={st['sta']}(表 {d['sta']}) rea={st['rea']}(表 {d['rea']}) "
                   f"funds={st['funds']}(表 {d['funds']}) count={st['mystery_count']}(表 {d['mystery_count']})",
            counterexample="反例：把难度表的 Initiate.sta 改成 99 → 若代码里另外写死了 21，"
                           "本断言会红（说明初始值有两个来源）→ 变红",
        )
    s.expect(
        "Sceptic 的谜团数是 4（与其它档 5 不同，MECHANICS.md §2.4）",
        ST.initial_states({"difficulty": "Sceptic"}, cfg)["mystery_count"] == 4
        and ST.initial_states({"difficulty": "Initiate"}, cfg)["mystery_count"] == 5,
        detail=f"Sceptic={ST.initial_states({'difficulty': 'Sceptic'}, cfg)['mystery_count']}、"
               f"Initiate={ST.initial_states({'difficulty': 'Initiate'}, cfg)['mystery_count']}",
        counterexample="反例：把 mystery_count 对全难度写死成 5 → Sceptic 会失去'4 个谜团'"
                       "这个难度差异 → 变红",
    )


def test_unknown_inputs_rejected(s: TestSuite) -> None:
    cfg = P.load_tuning()
    cases = [({"difficulty": "Nightmare"}, ST.StateError, "未知难度"),
             ({"death_mode": "no_death"}, ST.StateError, "未知死亡模式"),
             ({"stats": {"KNW": 99}}, ST.StateError, "属性越界"),
             ({"stats": {"KNW": 0}}, ST.StateError, "属性下界")]
    for payload, exc_type, label in cases:
        raised = None
        try:
            ST.initial_states(payload, cfg)
        except exc_type:
            raised = exc_type.__name__
        s.expect(
            f"{label} 被拒绝（显式抛错，不静默兜底）",
            raised == exc_type.__name__,
            detail=f"initial_states({payload}) → {raised}",
            counterexample=f"反例：把校验放宽成 `payload.get('difficulty') or 'Initiate'` → "
                           f"未知难度会被静默当成 Initiate，raised=None → 变红",
        )
    s.expect(
        "难度别名 Harbringer → Harbinger（原文拼写与实现拼写并存）",
        ST.initial_states({"difficulty": "Harbringer of Doom"}, cfg)["difficulty"] == "Harbinger",
        detail="Harbringer of Doom → " +
               ST.initial_states({"difficulty": "Harbringer of Doom"}, cfg)["difficulty"],
        counterexample="反例：删掉 difficulty._aliases → 会抛 StateError（未知难度）→ 变红。"
                       "MECHANICS.md §2.4 原文拼写是 Harbringer",
    )


def test_serialization_roundtrip(s: TestSuite) -> None:
    cfg = P.load_tuning()
    st = ST.initial_states({"difficulty": "Cultist", "seed": 99}, cfg)
    text = ST.dumps(st)
    back = ST.loads(text)
    eq, diff = ST.state_equal(st, back)
    s.expect(
        "dumps → loads 状态逐字节一致",
        eq,
        detail=f"一致={eq}；差异={diff or '无'}",
        counterexample="反例：在 to_json_obj 里漏掉 tags（例如 setdefault 覆盖）→ "
                       "tags 会丢 → 差异非空 → 变红",
    )
    s.expect(
        "落盘文本是 UTF-8 且不含 BOM（与项目「显式编码+行尾」约定一致）",
        not text.startswith("\ufeff") and "sid" in text,
        detail=f"首字符 U+{ord(text[0]):04X}；长度 {len(text)}",
        counterexample="反例：用 open(path,'w') 不指定 encoding（Windows 下走 cp936）→ "
                       "中文会写坏，读回时 json 解析失败 → 变红",
    )
    raised = None
    try:
        ST.loads('{"mode": "tsf"}')
    except ST.StateError:
        raised = "StateError"
    s.expect(
        "holds: 非 CoC 存档抛 StateError",
        raised == "StateError",
        detail=f"loads({{'mode':'tsf'}}) → {raised}",
        counterexample="反例：把 mode 校验删掉 → raised=None → 变红",
    )


def test_information_hiding_in_view(s: TestSuite) -> None:
    """**信息遮蔽**：面板与状态块不得泄漏标签名 / 敌人弱点 / 检定属性。"""
    cfg = P.load_tuning()
    st = ST.initial_states({"difficulty": "Initiate", "seed": 3}, cfg)
    st["tags"] = [
        {"id": "TIME_SKIP", "kind": "town", "name": "时间跳跃", "duration": "run",
         "tone": "钟表走得比记性快"},
        {"id": "Fearful", "kind": "curse", "sub": "mental", "name": "惊惧",
         "duration": "run", "flavor": "你的湿衣服贴在皮肤上。"},
    ]
    st["sta"], st["rea"] = 7, 8
    panel = ST.panel_view(st)
    block = ST.states_block(st)
    blob = json.dumps(panel, ensure_ascii=False) + "\n" + block

    leaked_tags = [t for t in ("TIME_SKIP", "Fearful", "时间跳跃", "惊惧") if t in blob]
    s.expect(
        "面板/状态块不出现标签 id 与名字（只给计数）",
        not leaked_tags and panel["tag_counts"] == {"injury": 0, "curse": 1, "town": 1},
        detail=f"泄漏 {leaked_tags}；tag_counts={panel['tag_counts']}",
        counterexample="反例：把 tags 原样放进 panel → 泄漏非空 → 变红。"
                       "上游 §9.4 明确「不给完整状态面板」是恐怖感来源",
        demonstration=f"实测：leaked_tags={leaked_tags}",
    )
    # 作用域要分清（这里踩过两次，写清楚）：
    #   - `panel_view` 是**数值 HUD**：按设计（COC-DESIGN §2.1）它必须给出 DOOM、双血条
    #     与五项属性的当前值。玩家看得见自己的属性，这**不是**泄漏；
    #     泄漏指的是"结算前告诉你这一手要考哪个属性"。
    #   - A8 针对的是**结算前的场景/选项文本**；它的整局验证在
    #     `测试工具/coc_cli_probe.py` 的 A8（扫 6 段卡面）。
    # 所以这里改为断言：面板里不含任何**回合结论**字段（check/roll/success…）。
    leaked_verdict = [k for k in ("check", "roll", "success", "target", "margin",
                                  "threat_mod", "dc", "is_tie") if k in panel]
    s.expect(
        "panel_view 里不含任何回合结论字段（check/roll/success/target/margin…）",
        not leaked_verdict,
        detail=f"面板键 = {sorted(panel)}；泄漏的结论字段 {leaked_verdict}",
        counterexample="反例：在 panel_view 里补一行 `\"this_check\": res.to_json()` → "
                       "玩家在结算前就能看到要考什么、掷了多少 → leaked_verdict 非空 → 变红",
    )
    probe = [tok for tok in FORBIDDEN if tok in "这一步需要 KNW 检定"]
    s.expect(
        "反向自检：禁词扫描对含禁词的输入确实会命中（防止写成恒真）",
        probe == ["KNW", "检定"],
        detail=f"注入串命中 {probe}",
        counterexample="反例：把 FORBIDDEN 写成空元组 → probe 为空 → 变红（那说明扫描永远为真）",
    )
    # 反向自检 2：把结论字段注入面板，确认上面那条真的会红
    dirty = dict(panel)
    dirty["check"] = "KNW 检定：8 vs 7+2 → 失败"
    dirty_hits = [k for k in ("check", "roll", "success") if k in dirty]
    s.expect(
        "变红实证：往面板里注入结论字段会被同一检查命中",
        dirty_hits == ["check"],
        detail=f"注入 check 字段后命中 {dirty_hits}",
        counterexample="反例：若 dirty_hits 为空，说明扫的不是被测对象（视域错了）→ 变红",
    )
    block_has_stats = all(k in block for k in ("STR", "KNW"))
    s.expect(
        "**states_block**（给 LLM）里出现属性名是**设计意图**（§8.2 要求输出属性数值）",
        block_has_stats,
        detail=f"states_block 属性行={block.splitlines()[2] if len(block.splitlines()) > 2 else '?'}",
        counterexample="反例：把 states_block 也做成「不含属性名」 → LLM 不知道角色数值，"
                       "叙述与数值脱节 → 本断言变红（这是**故意**留的反向断言："
                       "防止有人把两条断言的作用域搞混后「顺手」把 LLM 需要的数值也删掉）",
    )


def test_threshold_phrases(s: TestSuite) -> None:
    """阈值 → 自然语言滤镜（§10.2 #12 的转化方案）。"""
    cfg = P.load_tuning()
    cases = [
        ({"sta": 21, "rea": 21}, "normal", "暂时没有明显异样"),
        ({"sta": 7, "rea": 21}, "hurt", "身上有伤"),
        ({"sta": 21, "rea": 7}, "haunted", "神经绷得太紧"),
        ({"sta": 0, "rea": 21}, "collapsed", "撑不住"),
    ]
    for patch, want_sev, want_phrase in cases:
        st = ST.initial_states({"difficulty": "Initiate", "seed": 4}, cfg)
        st.update(patch)
        panel = ST.panel_view(st)
        block = ST.states_block(st)
        s.expect(
            f"sta/rea={patch} → severity={want_sev} 且措辞含「{want_phrase}」",
            panel["severity"] == want_sev and want_phrase in block,
            detail=f"severity={panel['severity']}（期望 {want_sev}）；"
                   f"身体状态行={block.splitlines()[3] if len(block.splitlines()) > 3 else '?'}",
            counterexample="反例：阈值判断用 `<` 而不是 `<=`（写成 sta<9）→ sta=9 时"
                           "severity 会变成 normal → 变红。MECHANICS.md §3.2 是 ≤9",
        )


def test_doom_line_end(s: TestSuite) -> None:
    cfg = P.load_tuning()
    st = ST.initial_states({"difficulty": "Initiate", "seed": 5}, cfg)
    st["doom"] = 47
    line47 = ST.doom_line(st)
    st["doom"] = 100
    line100 = ST.doom_line(st)
    s.expect(
        "[DOOM xx%] 固定行；到 100 显示 END（MECHANICS.md §2.1「数字改为显示 END」）",
        line47 == "[DOOM 47%]" and line100 == "[DOOM END]",
        detail=f"47 → {line47!r}；100 → {line100!r}",
        counterexample="反例：把 line100 也写成 [DOOM 100%] → 变红。上游 §2.1 明确 100 时"
                       "数字改为显示 END，§8.3 设计建议 4 要求每段固定输出这一行",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_state.py —— 初始值 / 序列化 / 信息遮蔽 / 阈值滤镜")
    for fn in (test_initial_values_from_difficulty, test_unknown_inputs_rejected,
               test_serialization_roundtrip, test_information_hiding_in_view,
               test_threshold_phrases, test_doom_line_end):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

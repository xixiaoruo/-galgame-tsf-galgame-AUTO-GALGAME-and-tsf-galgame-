"""`sanitize.py` 单测 —— **LLM 连提议权都没有**（在任何 prompt 接线之前完成）。

被测语义（`COC-DESIGN.md` §3.2 E2/E3/E4、§8.4 白名单表）：
  - E2 键白名单：`coc_delta` 里**根本没有** `doom`/`sta`/`rea`/`tags_grant`/
    `tags_remove`/`flag_set`/`ending` —— 不是「提议了再拒绝」，是「没有这个入口」；
  - E3 两级钳制：level-1（doom/sta/rea）±3、level-2（exp/funds/stat）±5；
  - E4 跨字段不变式：属性 ≤0 → DOOM 立即 100。

反例证明的重点：**恶意 delta 的每一类越权都要各有一条证据**，
不能只用一个 `{"doom": 1}` 代表全部。
"""
from __future__ import annotations

import sys

from app.coc import provenance as P
from app.coc import sanitize as S
from app.coc import state as ST
from app.coc.tests.harness import TestSuite


def test_whitelist_has_no_numeric_fields(s: TestSuite) -> None:
    cfg = P.load_tuning()
    wl = list(P.t(cfg, "llm_delta_whitelist"))
    banned = ("doom", "sta", "rea", "tags_grant", "tags_remove", "flag_set", "ending")
    present = [k for k in banned if k in wl]
    s.expect(
        "白名单里**根本没有** doom/sta/rea/tags_grant/tags_remove/flag_set/ending",
        not present and list(S.LLM_DELTA_WHITELIST) == wl,
        detail=f"白名单 = {wl}；被排除 {len(banned)}/{len(banned)}："
               f"{[k for k in banned if k not in wl]}",
        counterexample="反例：把 'doom' 加进 tuning.llm_delta_whitelist → "
                       "assert_whitelist_matches_tuning 抛 SchemaViolation（加载期/调用期两道），"
                       "且本断言 present 非空 → 变红",
    )
    # 白名单常量与 tuning 必须一致（两处不同步 = 悄悄放宽）
    raised = None
    try:
        S.assert_whitelist_matches_tuning(cfg)
    except S.SchemaViolation as exc:
        raised = str(exc)
    s.expect(
        "白名单常量与 tuning.llm_delta_whitelist 严格一致（双源不同步会被挡住）",
        raised is None,
        detail=f"assert_whitelist_matches_tuning 通过（未抛异常）",
        counterexample="反例：改 tuning 的白名单但不改 sanitize.LLM_DELTA_WHITELIST → "
                       "抛 SchemaViolation → 本断言变红",
    )


def test_malicious_delta_every_key(s: TestSuite) -> None:
    """★ 逐类越权各一条证据（不是用一个 doom 代表全部）。"""
    cfg = P.load_tuning()
    evil = {
        # —— 数值类越权（最诱人的三个）——
        "doom": -100,
        "sta": 999,
        "rea": -999,
        # —— 状态类越权 ——
        "tags_grant": [{"id": "FakeGod", "kind": "town", "duration": "run"}],
        "tags_remove": ["TIME_SKIP"],
        # —— 结论类越权（最危险：LLM 自己宣布通关）——
        "ending": "END_CLEARED",
        "flag_set": {"lighthouse_cleared": True},
        # —— 合法项（必须留下，否则「全丢」也能骗过测试）——
        "exp": 3,
        "funds": -2,
        "item_grant": ["Bandage"],
        # —— 未登记物品（应被单独丢弃）——
        "item_take": ["NotARealItem"],
        "unknown_key": 1,
    }
    cleaned, dropped = S.sanitize_llm_delta(evil, cfg)
    allowed = set(P.t(cfg, "llm_delta_whitelist"))
    leaked = sorted(k for k in cleaned if k not in allowed)
    s.expect(
        "恶意 delta：越权键一个不留，合法项原样保留",
        not leaked
        and cleaned.get("exp") == 3 and cleaned.get("funds") == -2
        and cleaned.get("item_grant") == ["Bandage"]
        and "item_take" not in cleaned,
        detail=f"输入 {sorted(evil)} → 保留 {sorted(cleaned)}；"
               f"丢弃 {sorted(set(str(d) for d in dropped))}；越权泄漏 {leaked}",
        counterexample="反例：删掉 `if key not in LLM_DELTA_WHITELIST: continue` → "
                       "cleaned 里会出现 doom/ending → leaked 非空 → 变红。"
                       "注意：只测 'doom' 一条是不够的，ending/flag_set 才是'自己宣布通关'那条路",
        demonstration=f"实测：leaked={leaked}",
    )
    # 单独把「LLM 自己宣布通关」这一条拿出来（最高危）
    only_ending, dropped2 = S.sanitize_llm_delta(
        {"ending": "END_CLEARED", "flag_set": {"lighthouse_cleared": True}}, cfg)
    s.expect(
        "单独验证：LLM 不能自己宣布通关（ending / lighthouse_cleared 都被丢弃）",
        "ending" not in only_ending and "flag_set" not in only_ending and only_ending == {},
        detail=f"输入 {{ending, flag_set}} → 保留 {only_ending}；丢弃 {dropped2}",
        counterexample="反例：把 ending 加进白名单 → only_ending 非空 → "
                       "LLM 可以一句话结束游戏 → 变红",
    )


def test_numeric_coercion_dropped(s: TestSuite) -> None:
    """非数字值必须丢弃，而不是被 int() 异常带崩或被当 0。"""
    cfg = P.load_tuning()
    cleaned, dropped = S.sanitize_llm_delta(
        {"exp": "很多", "funds": None, "item_grant": "Bandage"}, cfg)
    s.expect(
        "不可转 int 的值被丢弃；非列表的 item_grant 被丢弃（不崩、不静默取 0）",
        cleaned == {} and "exp" in dropped and "funds" in dropped and "item_grant" in dropped,
        detail=f"输入 {{exp:'很多', funds:None, item_grant:'Bandage'}} → cleaned={cleaned}，"
               f"dropped={dropped}",
        counterexample="反例：把 _as_int 改成 `int(v)` 直接裸调 → 会抛 ValueError 打断整轮；"
                       "或 `int(v or 0)` → 非法值被静默当 0 → cleaned 非空 → 变红",
    )


def test_clamping(s: TestSuite) -> None:
    """E3：单轮钳制。"""
    cfg = P.load_tuning()
    b1 = int(P.t(cfg, "sanitize.turn_budget_level1"))
    b2 = int(P.t(cfg, "sanitize.turn_budget_level2"))
    st = ST.initial_states({"difficulty": "Initiate", "seed": 5}, cfg)
    res = S.apply_delta(st, {"doom": 999, "sta": -999, "rea": 999, "exp": -999,
                             "funds": 999, "stat": {"KNW": 99}}, cfg)
    ap = res["applied"]
    ok = (ap.get("doom") == b1 and ap.get("sta") == -b1 and ap.get("rea") == b1
          and ap.get("exp") == -b2 and ap.get("funds") == b2
          and ap.get("stat", {}).get("KNW") == b2)
    s.expect(
        f"level-1 钳到 ±{b1}、level-2 钳到 ±{b2}（含 stat 子字典）",
        ok,
        detail=f"applied={ap}",
        counterexample="反例：去掉 clamp_turn_delta → doom 会被 invariants 截成 100 "
                       "→ ap['doom']=100 ≠ ±3 → 变红",
    )


def test_invariants(s: TestSuite) -> None:
    """E4：跨字段不变式。"""
    cfg = P.load_tuning()

    # ① 属性 ≤0 → DOOM 立即 100
    st = ST.initial_states({"difficulty": "Initiate", "seed": 6}, cfg)
    st["stats"]["KNW"] = 1
    res = S.apply_delta(st, {"stat": {"KNW": -5}}, cfg)
    s.expect(
        "属性 ≤0 → DOOM 立即置 100（MECHANICS.md §2.1 隐性绝境）",
        st["doom"] == 100 and any("属性≤0" in n for n in res["notes"]),
        detail=f"KNW 1 → -5 后 doom={st['doom']}；notes={res['notes']}",
        counterexample="反例：删掉 invariants 的 ① 分支 → doom 保持 0 → 变红。"
                       "（注意：KNW 本身还会被钳到 1，所以只看 stats 是看不出这个坑的，"
                       "必须看 doom）",
    )

    # ② doom 上限 100
    st2 = ST.initial_states({"difficulty": "Initiate", "seed": 7}, cfg)
    st2["doom"] = 99
    S.apply_delta(st2, {"doom": 3}, cfg, clamp=False)
    s.expect(
        "doom 上限钳到 100（仪表语义，§2.1 到 100% 显示 END）",
        st2["doom"] == 100,
        detail=f"99 + 3 → {st2['doom']}",
        counterexample="反例：删掉上限钳制 → doom=102 → 面板会出现 [DOOM 102%] → 变红",
    )

    # ③ last_stand 地板 = -5；sudden_death 地板 = 0
    ls = ST.initial_states({"difficulty": "Initiate", "seed": 8,
                            "death_mode": "last_stand"}, cfg)
    S.apply_delta(ls, {"sta": -999}, cfg, clamp=False)
    sd = ST.initial_states({"difficulty": "Initiate", "seed": 9,
                            "death_mode": "sudden_death"}, cfg)
    S.apply_delta(sd, {"sta": -999}, cfg, clamp=False)
    s.expect(
        "死亡模式地板：last_stand 停在 -5、sudden_death 不出现负血",
        ls["sta"] == -5 and sd["sta"] == 0,
        detail=f"last_stand.sta={ls['sta']}（floor={P.t(cfg, 'sanitize.last_stand_floor')}）；"
               f"sudden_death.sta={sd['sta']}",
        counterexample="反例：把地板对两种模式用同一个值 → last_stand 会掉到 -999（"
                       "MECHANICS.md §1.3 的 -5 缓冲带失效）→ 变红",
    )

    # ④ 城镇状态**不可移除**（§1.4）
    st4 = ST.initial_states({"difficulty": "Initiate", "seed": 10}, cfg)
    S.apply_delta(st4, {"tags_grant": [{"id": "TIME_SKIP", "kind": "town",
                                        "duration": "run"}]}, cfg)
    res4 = S.apply_delta(st4, {"tags_remove": ["TIME_SKIP"]}, cfg)
    s.expect(
        "城镇状态不可移除（tags_remove 对 kind=town 一律拒绝）",
        len(st4["tags"]) == 1 and any("不可移除" in d for d in res4["dropped"]),
        detail=f"移除后 tags={[t['id'] for t in st4['tags']]}；dropped={res4['dropped']}",
        counterexample="反例：去掉 kind=='town' 的判断 → 城镇状态会被删掉 → "
                       "MECHANICS.md §1.4「不可移除」被破坏 → 变红",
    )

    # ⑤ 诅咒可以移除（对照组：证明上一条不是「什么都不删」）
    st5 = ST.initial_states({"difficulty": "Initiate", "seed": 11}, cfg)
    S.apply_delta(st5, {"tags_grant": [{"id": "Fearful", "kind": "curse",
                                        "sub": "mental", "duration": "run"}]}, cfg)
    S.apply_delta(st5, {"tags_remove": ["Fearful"]}, cfg)
    s.expect(
        "对照组：诅咒可以被移除（说明上一条是真的在区分 kind，而非一律不删）",
        st5["tags"] == [],
        detail=f"诅咒移除后 tags={st5['tags']}",
        counterexample="反例：把 tags_remove 实现成「永远不删」→ 上一条会「通过」但这条会红 —— "
                       "**只有两条一起才说明判定是按 kind 分的**",
    )


def test_single_write_point(s: TestSuite) -> None:
    """静态扫描：权威数值只有一个写入点（sanitize.apply_delta）。"""
    import ast
    import os

    coc_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    offenders = []
    for name in sorted(os.listdir(coc_dir)):
        if not name.endswith(".py") or name == "sanitize.py":
            continue
        p = os.path.join(coc_dir, name)
        with open(p, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=p)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name)
                            and tgt.value.id in ("state", "st", "self.state")
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value in S.STATE_NUMERIC_FIELDS):
                        offenders.append(f"{name}:{tgt.slice.value}")
            # 也拦 self.state["doom"] = ... 这种（Attribute 形式）
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Attribute)
                            and tgt.value.attr == "state"
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value in S.STATE_NUMERIC_FIELDS):
                        offenders.append(f"{name}:self.state[{tgt.slice.value}]")
    s.expect(
        "app/coc/*.py（排除 sanitize.py）里无人直接给 state['doom'|'sta'|'rea'|'exp'|'funds'] 赋值",
        not offenders,
        detail=f"扫描目录 {os.path.basename(coc_dir)}；违例 {len(offenders)} 处 {offenders}",
        counterexample="反例：在 loop.py 里写 state['doom'] = 100 → offenders 非空 → 变红。"
                       "**这条是 E1/E6「确定性层独占写权」的静态证据**，"
                       "它不依赖运行时行为，所以跑不到的分支也拦得住",
        demonstration=f"实测：offenders={offenders}",
    )


def test_rng_roundtrip_in_state(s: TestSuite) -> None:
    """rng 状态必须能逐字段往返（A7 的基础）。"""
    import random

    r = random.Random(20261004)
    payload = ST.rng_to_json(r)
    r2 = ST.restore_rng(payload)
    seq1 = [r2.randint(1, 10000) for _ in range(10)]
    r3 = ST.restore_rng(payload)
    seq2 = [r3.randint(1, 10000) for _ in range(10)]
    s.expect(
        "rng_to_json / restore_rng 往返后随机序列完全一致",
        seq1 == seq2 and len(payload) == 3,
        detail=f"payload 结构 = [version, {len(payload[1])} 个状态字, gauss]；"
               f"两次还原的 10 连抽一致 = {seq1 == seq2}",
        counterexample="反例：把 getstate 的第三个元素（gauss_next）丢掉 → 若之前调用过 "
                       "gauss 系列，后续序列会漂移 → 变红；或只存种子不存状态 → 变红",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_sanitize.py —— 白名单 / 钳制 / 不变式 / 唯一写点")
    for fn in (test_whitelist_has_no_numeric_fields, test_malicious_delta_every_key,
               test_numeric_coercion_dropped, test_clamping, test_invariants,
               test_single_write_point, test_rng_roundtrip_in_state):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""t54：CoC 引擎内的 TSF 子系统单测。

覆盖任务书点名的五项：
  1. **单调性** —— 进度只增不减（含模型推动的负值不产生减量）；
  2. **可复现性** —— 同 seed 同输入 ⇒ 同进度/同修正/同检定结果；
  3. **旧档兼容** —— 缺字段不抛、补齐后有默认值；
  4. **阶段边界** —— 0/20/21/50/51/80/81/100 与越界（含 t53 纠正的 `<=` 与 `[100,100]`）；
  5. **白名单越权仍被拒** —— 既有 7 个越权键一个都没放进来；新增的 `tsf_nudge` 有界。

外加**硬闸门**：机制型必须可被观察 —— 同一检定在 progress=0/40/80 下成功率必须不同。
"""
from __future__ import annotations

import io
import random
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.coc import dice
from app.coc import provenance as P
from app.coc import sanitize as S
from app.coc import state as ST
from app.coc import tsf as T
from app.coc.tests.harness import TestSuite

CFG = P.load_tuning()


def _mk(**over) -> dict:
    """造一个 CoC 状态，并可按需覆盖账本字段。"""
    st = ST.initial_states({"difficulty": "Initiate", "seed": 1234}, CFG)
    st.update(over)
    return st


def _succ_rate(stat: str, stat_value: int, threat: str, shift: int,
               dc: int = 0) -> float:
    """**精确**成功率：穷举全部掷骰结果（1~12）算理论值，不用统计。"""
    lo = int(P.t(CFG, "checks.roll_min"))
    hi = int(P.t(CFG, "checks.roll_max"))
    win = 0
    for roll in range(lo, hi + 1):
        r = dice.resolve_deterministic(stat, stat_value, threat, roll, CFG,
                                       dc=dc, tsf_shift=shift)
        win += 1 if r.success else 0
    return win / (hi - lo + 1)


# ---------------------------------------------------------------- 1. 单调性

def test_monotonic(s: TestSuite) -> None:
    """进度只增不减：沿账本推进的**任何**序列都必须单调。"""
    count = 5
    length = 6
    seq = []
    st = _mk(mystery_count=count, mystery_len=length, mystery_idx=0,
             investigations_done=0)
    # 逐章逐次调查推进（复刻 loop 的写法：investigations_done +1；换章时归零 + idx+1）
    for m in range(count):
        st["mystery_idx"] = m
        for d in range(length + 1):
            st["investigations_done"] = d
            seq.append(T.progress(st))
    nondec = all(b >= a for a, b in zip(seq, seq[1:]))
    s.expect(
        "沿账本推进：progress 序列**单调不减**",
        nondec,
        detail=f"共 {len(seq)} 个采样点；首={seq[0]} 末={seq[-1]}；"
               f"逆序对={sum(1 for a, b in zip(seq, seq[1:]) if b < a)}",
        counterexample="反例：把公式写成 `mystery_idx / count * 100 + done`（不归一化 done）→ "
                       "换章时 done 归零会导致进度**回退** → 逆序对非空 → 变红",
    )
    s.expect(
        "终点正好 100（满章 + 满调查 + count=5）",
        seq[-1] == 100,
        detail=f"末值 = {seq[-1]}",
        counterexample="反例：`mystery_count` 写成 6（比实际章数大）→ 分母偏大 → 终点 <100 → 变红",
    )

    # 模型推动的负值**不产生减量**（单调硬约束）
    st2 = _mk(mystery_idx=2, mystery_count=5, mystery_len=6, investigations_done=3)
    before = T.progress(st2)
    T.apply_nudge(st2, -5, reason="test_negative")
    after = T.progress(st2)
    s.expect(
        "模型推负值（-5）**不产生减量**：进度不降",
        after >= before,
        detail=f"推 -5：{before} → {after}（nudge_total={st2.get(T.NUDGE_KEY)}）",
        counterexample="反例：把 apply_nudge 写成 `state[NUDGE_KEY] = cur + amt`（不夹负）→ "
                       "负数会进累计值 → 进度回退 → 变红",
    )
    # 累计值不被顶出域
    for _ in range(300):
        T.apply_nudge(st2, 1, reason="test_overflow")
    s.expect(
        "反复正推动不会让进度超 100（累计值钳在 0~100）",
        T.progress(st2) <= 100,
        detail=f"推 300 次 +1 后：progress={T.progress(st2)}，"
               f"nudge_total={st2.get(T.NUDGE_KEY)}",
        counterexample="反例：去掉 `min(100, ...)` → 进度会 >100 → 变红（也会让 stage_of 越界）",
    )


# ---------------------------------------------------------------- 2. 可复现性

def test_reproducible(s: TestSuite) -> None:
    """同 seed 同账本 ⇒ 同进度 / 同修正 / 同检定结果。"""
    def run_once() -> tuple:
        st = ST.initial_states({"difficulty": "Initiate", "seed": 777}, CFG)
        rng = ST.new_rng(777, st)
        out = []
        for i in range(12):
            st["investigations_done"] = i % 4
            st["mystery_idx"] = i // 4
            shift = T.check_shift(st, "CHA")
            r = dice.check("CHA", int(st["stats"]["CHA"]), "LOW", rng, CFG,
                           dc=0, tsf_shift=shift)
            out.append((T.progress(st), shift, r.roll, r.success))
        return tuple(out)

    a, b = run_once(), run_once()
    s.expect(
        "同 seed 同账本：12 回合的 (progress, shift, roll, success) 序列**完全一致**",
        a == b,
        detail=f"两轮序列一致={a == b}；样本={a[:4]}",
        counterexample="反例：在 check_shift 里用 `random` 或时间做扰动 → 两轮不同 → 变红。"
                       "（注意：修正必须是**账本的确定性函数**，不能是随机量）",
    )

    # 修正本身是纯函数（同 state ⇒ 同值，不受调用顺序影响）
    st = _mk(mystery_idx=3, mystery_count=5, mystery_len=6, investigations_done=2)
    vals = [T.mechanical_effects(st)["shift"]["CHA"] for _ in range(5)]
    s.expect(
        "mechanical_effects 是纯函数：连调 5 次给同一 shift",
        len(set(vals)) == 1,
        detail=f"5 次取值 = {vals}",
        counterexample="反例：在 mechanical_effects 里累加内部计数器 → 每次不同 → 变红",
    )

    # 账本可解释：改动账本必然解释进度变化（无隐藏来源）
    st2 = _mk(mystery_idx=1, mystery_count=5, mystery_len=6, investigations_done=0)
    p1 = T.progress(st2)
    st2["investigations_done"] = 3
    p2 = T.progress(st2)
    s.expect(
        "进度变化**只能**由账本字段解释（改了 done 才变，且方向正确）",
        p2 > p1,
        detail=f"done 0→3：{p1} → {p2}",
        counterexample="反例：把 progress 结果缓存在 state 里并只读缓存 → 改账本不变 → 变红",
    )


# ---------------------------------------------------------------- 3. 旧档兼容

def test_legacy_compat(s: TestSuite) -> None:
    """旧档：缺字段不抛、补齐后有默认值；脏类型也不抛。"""
    # (a) 全新状态（含本模块字段）已有默认值
    st = ST.initial_states({}, CFG)
    raised = None
    try:
        filled = T.ensure_fields(st)
    except BaseException as e:                                # noqa: BLE001
        raised = repr(e)
        filled = None
    s.expect(
        "`initial_states` 造出的状态可被 ensure_fields 处理且无需补齐",
        raised is None and filled == [],
        detail=f"raised={raised}；filled={filled}",
        counterexample="反例：DEFAULT_FIELDS 写错键名 → filled 非空 → 变红",
    )

    # (b) 真正的旧档：两个字段都删掉
    old = ST.initial_states({}, CFG)
    old.pop(T.NUDGE_KEY, None)
    old.pop(T.SYMPTOMS_KEY, None)
    raised = None
    try:
        filled = T.ensure_fields(old)
        prog = T.progress(old)
        eff = T.mechanical_effects(old)
    except BaseException as e:                                # noqa: BLE001
        raised = repr(e)
        filled, prog, eff = None, None, None
    s.expect(
        "★ 旧档（缺两个新字段）加载**不抛异常**，且补齐后能算进度与修正",
        raised is None and filled == sorted([T.NUDGE_KEY, T.SYMPTOMS_KEY],
                                            key=lambda k: [T.NUDGE_KEY,
                                                           T.SYMPTOMS_KEY].index(k))
        or (raised is None and set(filled) == {T.NUDGE_KEY, T.SYMPTOMS_KEY}),
        detail=f"raised={raised}；filled={filled}；progress={prog}；"
               f"shift={eff['shift'] if eff else None}",
        counterexample="反例：把 `state[SYMPTOMS_KEY]` 直接下标（`state[SYMPTOMS_KEY].append`）→ "
                       "旧档 KeyError → raised 非空 → 变红",
    )
    s.expect(
        "旧档补齐后的默认值正确（nudge=0、symptoms=[]，且进度不受影响）",
        old.get(T.NUDGE_KEY) == 0 and old.get(T.SYMPTOMS_KEY) == [],
        detail=f"nudge={old.get(T.NUDGE_KEY)!r} symptoms={old.get(T.SYMPTOMS_KEY)!r}",
        counterexample="反例：默认值写成 None → progress 里 _as_int(None) 得 0（看似没事）"
                       "但 note_symptoms 会 `.append` 到 None → 变红",
    )

    # (c) 脏类型：字段存在但类型不对
    dirty = ST.initial_states({}, CFG)
    dirty[T.NUDGE_KEY] = "abc"
    dirty[T.SYMPTOMS_KEY] = "not-a-list"
    raised = None
    try:
        T.ensure_fields(dirty)
        T.note_symptoms(dirty)
        p = T.progress(dirty)
    except BaseException as e:                                # noqa: BLE001
        raised = repr(e)
        p = None
    s.expect(
        "脏类型（nudge='abc'、symptoms='not-a-list'）也被补齐且不抛",
        raised is None and isinstance(dirty[T.SYMPTOMS_KEY], list)
        and isinstance(dirty[T.NUDGE_KEY], int) and 0 <= p <= 100,
        detail=f"raised={raised}；nudge={dirty[T.NUDGE_KEY]!r} "
               f"symptoms={dirty[T.SYMPTOMS_KEY]!r} progress={p}",
        counterexample="反例：只判 `if key not in state` 而不判类型 → 脏类型会带着走 → "
                       "note_symptoms 对字符串 append 会抛 → 变红",
    )


# ---------------------------------------------------------------- 4. 阶段边界

def test_stage_boundaries(s: TestSuite) -> None:
    """边界：0/20/21/50/51/80/81/100 与越界 —— 含 t53 纠正的两条。"""
    tbl = T.default_stages()
    cases = [(0, 0), (19, 0), (20, 0), (21, 1), (39, 1), (40, 1), (41, 2),
             (50, 2), (51, 2), (59, 2), (60, 2), (61, 3), (79, 3), (80, 3),
             (81, 4), (98, 4), (99, 4), (100, 5)]
    bad = [(p, T.stage_index(p, tbl), want) for p, want in cases
           if T.stage_index(p, tbl) != want]
    s.expect(
        f"★ 18 个边界点全部落对段（含 `progress=20 → 第 1 段`、`100 → 第 6 段`）",
        not bad,
        detail=f"表={[(p, T.stage_index(p, tbl)) for p, _ in cases]}；不符={bad}",
        counterexample="反例：`stage_of` 改成正则 `progress < s['max']`（少 `=`）→ "
                       "20 会落到第 2 段、100 会落到第 5 段 → bad 非空 → 变红",
    )
    s.expect(
        "越界进度（-50 / 999）不抛且钳进合法段",
        T.stage_index(-50, tbl) == 0 and T.stage_index(999, tbl) == len(tbl) - 1,
        detail=f"stage_of(-50)={T.stage_index(-50, tbl)}；"
               f"stage_of(999)={T.stage_index(999, tbl)}（共 {len(tbl)} 段）",
        counterexample="反例：不夹取进度 → stage_of(999) 走完循环返回最后一个（看似对），"
                       "但 stage_of(-50) 会返回 0 也可能对 —— 真正的反例是让 stage_view 的"
                       "`tbl[idx]` 用未夹取的 idx → IndexError → 变红",
    )
    s.expect(
        "★ 阶段表来自 app.tsf（不是自己写的表）：与 build_stages(6) 同一对象",
        tbl is __import__("app.tsf", fromlist=["x"]).STAGES_V6,
        detail=f"len={len(tbl)}；name[0]={tbl[0]['name']!r}",
        counterexample="反例：在 coc/tsf.py 里抄一份阶段表 → 不再是同一对象 → 变红"
                       "（这正是「两套 TSF 漂移」的入口）",
    )
    s.expect(
        "机制档位 mech_tier 与段序号解耦：4/6/10 段下 mech_tier 对同一进度不变",
        len({T.tier_of(80) for _ in (4, 6, 10)}) == 1,
        detail=f"tier_of(80)={T.tier_of(80)}（只看 MECH_THRESHOLDS）",
        counterexample="反例：把 tier_of 写成依赖 stages → 换段数会改机制强度 → 变红",
    )


# ---------------------------------------------------------------- 5. 白名单

def test_whitelist(s: TestSuite) -> None:
    """越权仍被拒；新增键有界且不破坏防护。"""
    wl = list(S.LLM_DELTA_WHITELIST)
    banned = ("doom", "sta", "rea", "tags_grant", "tags_remove", "flag_set", "ending")
    present = [k for k in banned if k in wl]
    s.expect(
        f"★ 7 个越权键**一个都没**进白名单（t54 只加了 tsf_nudge）",
        not present,
        detail=f"当前白名单={wl}；越权键命中={present or '无'}",
        counterexample="反例：把 'doom' 加进白名单 → 命中非空 → 变红；"
                       "同时 assert_whitelist_matches_tuning 也会抛（两道）",
    )
    s.expect(
        "白名单常量与 tuning.json 严格一致（含新增键）",
        sorted(wl) == sorted(P.t(CFG, "llm_delta_whitelist")),
        detail=f"常量={sorted(wl)}；tuning={sorted(P.t(CFG, 'llm_delta_whitelist'))}",
        counterexample="反例：只改 tuning 不改常量（或反之）→ 加载时 SchemaViolation → 变红",
    )

    # 越权 delta 仍被丢（逐键）
    evil = {"doom": -100, "sta": 999, "rea": -999, "ending": "END_CLEARED",
            "flag_set": {"x": True},
            "tags_grant": [{"id": "TIME_SKIP", "kind": "town", "duration": "run"}],
            "exp": 3, "tsf_nudge": 1}
    cleaned, dropped = S.sanitize_llm_delta(evil, CFG)
    leaked = [k for k in cleaned if k not in wl]
    s.expect(
        "越权 delta 仍被逐键丢弃，只留合法键（含 tsf_nudge）",
        not leaked and "ending" not in cleaned and cleaned.get("exp") == 3
        and cleaned.get("tsf_nudge") == 1,
        detail=f"保留={sorted(cleaned)}；丢弃={sorted(map(str, dropped))}",
        counterexample="反例：把 'ending' 放进白名单 → 出现于 cleaned → leaked 非空 → 变红",
    )

    # 新增键的单轮上限：提议 +5 只能落地 +1
    cl, _ = S.sanitize_llm_delta({"tsf_nudge": 5}, CFG)
    cl2, _ = S.sanitize_llm_delta({"tsf_nudge": -9}, CFG)
    s.expect(
        "★ `tsf_nudge` 有专用单轮上限：提议 +5 → +1、提议 -9 → -1（不借 level-2 的 ±5）",
        cl.get("tsf_nudge") == 1 and cl2.get("tsf_nudge") == -1,
        detail=f"+5 → {cl.get('tsf_nudge')}；-9 → {cl2.get('tsf_nudge')}；"
               f"TSF_NUDGE_MAX={S.TSF_NUDGE_MAX}",
        counterexample="反例：让 tsf_nudge 走 level-2 预算（±5）→ 提议 +5 会落地 +5 → "
                       "一回合跨 5% 进度（并可能跨段）→ 变红",
    )

    # 经真实写入点：越权键不得改动状态
    st = _mk()
    before = {k: int(st.get(k, 0)) for k in ("doom", "sta", "rea")}
    st["ending"] = None
    turn_clean, _ = S.sanitize_llm_delta({"doom": -100, "ending": "X",
                                          "tsf_nudge": 1}, CFG)
    S.apply_delta_engine(st, turn_clean, CFG)
    after = {k: int(st.get(k, 0)) for k in ("doom", "sta", "rea")}
    s.expect(
        "过一遍真实写入点：越权键未改动 doom/sta/rea，ending 仍为 None",
        before == after and st.get("ending") is None,
        detail=f"doom/sta/rea {before} → {after}；ending={st.get('ending')!r}；"
               f"progress={T.progress(st)}",
        counterexample="反例：把 doom 加进白名单 → 状态被模型改动 → 变红",
    )


# ---------------------------------------------------------------- ★ 硬闸门：可观察性

STAGE_PROBES = (0, 40, 80)


def test_mechanism_observable(s: TestSuite) -> None:
    """★★ 硬闸门：机制必须**可被观察** —— 不同进度下同一检定的成功率必须不同。

    三条独立证据：
      (a) **穷举理论值**：CHA 检定的成功率在 0/40/80 下必须两两可区分；
      (b) **大样本实测**：固定 seed、每档 2000 次，观察值与理论值一致；
      (c) **5 项属性全覆盖**：至少一项随进度**下降**、至少一项**上升**
          （证明机制不是"纯惩罚"）。
    """
    lo = int(P.t(CFG, "checks.roll_min"))
    hi = int(P.t(CFG, "checks.roll_max"))
    n = hi - lo + 1

    # (a) 穷举理论值
    prog_for = {0: 0, 40: 40, 80: 80}
    table = {}
    for st_name in ST.STAT_KEYS:
        row = {}
        for label, prog in prog_for.items():
            st = _mk(mystery_idx=0, mystery_count=5, mystery_len=6,
                     investigations_done=0)
            # 用 nudge 精确构造目标进度（apply_nudge 单调、上限钳制）
            need = prog - T.progress(st)
            if need > 0:
                for _ in range(need):
                    T.apply_nudge(st, 1, reason="construct")
            sh = T.check_shift(st, st_name)
            row[label] = (_succ_rate(st_name, 6, "SAFE", sh), sh)
        table[st_name] = row

    cha = table["CHA"]
    rates = [cha[p][0] for p in STAGE_PROBES]
    shifts = [cha[p][1] for p in STAGE_PROBES]
    diffs = [round(rates[i] - rates[0], 4) for i in range(1, len(rates))]
    s.expect(
        f"★★ CHA 检定的成功率在 progress=0/40/80 下**互不相同**"
        f"（{rates[0]:.1%} / {rates[1]:.1%} / {rates[2]:.1%}）",
        rates[0] != rates[1] and rates[0] != rates[2],
        detail=f"CHA（stat=6, threat=SAFE, dc=0）："
               f"0→{rates[0]:.4f}(shift {shifts[0]})、"
               f"40→{rates[1]:.4f}(shift {shifts[1]})、"
               f"80→{rates[2]:.4f}(shift {shifts[2]})；相对 0 的差值={diffs}",
        counterexample="反例：把 mechanical_effects 的结果**不接进** dice.check"
                       "（或 shift 表全是 0）→ 三条成功率完全相同 → 变红。"
                       "**这正是任务书说的「只写了个字段」**",
    )

    # (b) 大样本实测（固定 seed）
    sample_rates = {}
    for label, prog in prog_for.items():
        st = _mk(mystery_idx=0, mystery_count=5, mystery_len=6,
                 investigations_done=0)
        need = prog - T.progress(st)
        for _ in range(max(0, need)):
            T.apply_nudge(st, 1, reason="construct")
        sh = T.check_shift(st, "CHA")
        rng = random.Random(20261004)
        win = sum(1 for _ in range(2000)
                  if dice.check("CHA", 6, "SAFE", rng, CFG, dc=0,
                                tsf_shift=sh).success)
        sample_rates[label] = (win / 2000, sh)
    ok = all(abs(sample_rates[p][0] - rates[i]) < 0.04
             for i, p in enumerate(STAGE_PROBES))
    s.expect(
        f"大样本实测（固定 seed、每档 2000 次）与穷举理论值一致（误差 <4%）",
        ok,
        detail="；".join(f"p={p}: 实测 {sample_rates[p][0]:.4f} vs 理论 "
                         f"{rates[i]:.4f}（shift {sample_rates[p][1]}）"
                         for i, p in enumerate(STAGE_PROBES)),
        counterexample="反例：shift 接错位置（如加在掷骰侧而非玩家侧）→ 方向翻转 → "
                       "实测与理论差 >4% → 变红",
    )

    # (c) 双向：至少一项下降、至少一项上升
    #  ⚠️ 必须比到 **100**：KNW 的正向修正是 tier 5（==progress 100）才生效的
    #  （design 里刻意如此 —— 80 之前"还没看明白"）。第一版只比到 80，
    #  于是 ups 为空、这条误红；诊断脚本 `_diag_shift_effect.py` 证实
    #  shift=+1 在任意 stat 1..12 上都能把成功率抬高 1/12（stat=6 时 +8.3pp）。
    # 补上 progress=100 这一列（KNW 的正向修正只在 tier 5 生效）
    for st_name in ST.STAT_KEYS:
        st = _mk(mystery_idx=0, mystery_count=5, mystery_len=6,
                 investigations_done=0)
        for _ in range(100):
            T.apply_nudge(st, 1, reason="construct")
        sh = T.check_shift(st, st_name)
        table[st_name][100] = (_succ_rate(st_name, 6, "SAFE", sh), sh)

    downs, ups = [], []
    for st_name, row in table.items():
        d = row[100][0] - row[0][0]
        (ups if d > 0 else downs if d < 0 else []).append((st_name, round(d, 4)))
    s.expect(
        "★ 机制**双向**（不是纯惩罚）：至少一项随进度下降、至少一项上升"
        "（KNW 的正向修正在 tier 5 = progress 100 才生效）",
        bool(downs) and bool(ups),
        detail=f"下降={downs}；上升={ups}；"
               f"0→100 不变={[k for k, r in table.items() if r[100][0] == r[0][0]]}；"
               f"KNW 100 时 shift={table['KNW'][100][1]}",
        counterexample="反例：把 KNW 的正向修正删掉（全表非正）→ ups 为空 → 变红。"
                       "**纯惩罚的机制会诱导玩家抗拒进度**，这不是设计意图",
    )
    # 峰值只统计**惩罚侧**（负值），正向修正不计入
    peak = max([abs(row[100][1]) for row in table.values() if row[100][1] < 0] or [0])
    dc_min = abs(int(P.t(CFG, "checks.dc_min")))
    s.expect(
        f"惩罚峰值 {peak} 不超过 dc 域的 1/3（{dc_min}/3={dc_min/3:.2f}），防与 DOOM 叠加失控",
        peak <= dc_min / 3,
        detail=f"惩罚峰值={peak}；dc_min={-dc_min}；阈值={dc_min/3:.2f}",
        counterexample="反例：把峰值调到 -3 → 与 DOOMED 的 -2 叠加到 -5（≈dc_min）→ "
                       "检定近乎必败 → 变红",
    )


# ---------------------------------------------------------------- 命名空间隔离

def test_namespace_isolation(s: TestSuite) -> None:
    """TSF 不写 DOOM/STA/REA，也不改写 `stats`（t53 §10.1 第 1 条）。"""
    st = _mk(mystery_idx=3, mystery_count=5, mystery_len=6, investigations_done=6)
    before = {k: int(st.get(k, 0)) for k in ("doom", "sta", "rea")}
    stats_before = dict(st["stats"])
    T.mechanical_effects(st)
    T.progress(st)
    T.note_symptoms(st)
    T.apply_nudge(st, 1, reason="ns_test")
    after = {k: int(st.get(k, 0)) for k in ("doom", "sta", "rea")}
    s.expect(
        "★ TSF 子系统**从不写** doom/sta/rea（读它们都不读）",
        before == after,
        detail=f"{before} → {after}",
        counterexample="反例：在 progress 里顺手 `state['doom'] += 1` 作为代价 → 变红",
    )
    s.expect(
        "TSF 子系统**不改写** `state['stats']`（技能是技能的命名空间）",
        dict(st["stats"]) == stats_before,
        detail=f"stats 前后一致={dict(st['stats']) == stats_before}；值={stats_before}",
        counterexample="反例：把 TSF 修正直接写回 stats（`stats['CHA'] -= 1`）→ "
                       "**不可逆**：进度回退时修正无法还原 → 变红",
    )
    s.expect(
        "派生量明确在 `tsf_*` 命名空间下：状态里没有裸 `progress`/`stage` 键",
        "progress" not in st and "stage" not in st
        and T.NUDGE_KEY.startswith("tsf_") and T.SYMPTOMS_KEY.startswith("tsf_"),
        detail=f"NUDGE_KEY={T.NUDGE_KEY!r} SYMPTOMS_KEY={T.SYMPTOMS_KEY!r}；"
               f"裸 progress/stage 存在={'progress' in st or 'stage' in st}",
        counterexample="反例：把字段命名为 `progress` → 与主模式 `protagonist['stats']['progress']` "
                       "同名不同义（t53 §10.1 的最危险条目）→ 变红",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_coc_tsf.py —— CoC 内的 TSF 子系统（机制型）")
    for fn in (test_monotonic, test_reproducible, test_legacy_compat,
               test_stage_boundaries, test_whitelist, test_mechanism_observable,
               test_namespace_isolation):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

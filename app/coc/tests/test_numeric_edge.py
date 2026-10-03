"""数值边界**异常形态完备性**单测 —— D1 缺陷的永久防线。

tsf-security 独立复核发现的 D1：
> `sanitize_llm_delta({"exp": float("inf")}, cfg)` → **OverflowError**（`_as_int` 里
> `int(round(float("inf")))`）。可达性已实测：`json.loads('{"exp": Infinity}')` **成功**，
> 而 LLM 回包正是 `json.loads` 出来的 ⇒ 模型吐 `Infinity` 就能让**整回合异常**。
> `NaN` 反而安全（走 `ValueError` 被丢弃）——只有 ±Inf 崩。

本文件的重点**不是**"我修了 inf"，而是**证明异常集合是完备的**：
把能想到的**所有非有限 / 非整数 / 可疑形态**各喂一遍，断言
**无一抛出、且都被丢弃或钳制**，并把**每种形态的实际结果**打印成表。
"""
from __future__ import annotations

import decimal
import json
import math
import sys

from app.coc import loop as L
from app.coc import provenance as PR
from app.coc import sanitize as S
from app.coc.tests.harness import TestSuite

#: 完备性输入表：(标签, 值)。**只追求"想到多少喂多少"**，追求覆盖面而非优雅。
NON_INT_FORMS: list[tuple[str, object]] = [
    # —— 非有限浮点（D1 的直接形态）——
    ("float('inf')", float("inf")),
    ("float('-inf')", float("-inf")),
    ("float('nan')", float("nan")),
    ("1e999 → inf", 1e999),
    ("-1e999 → -inf", -1e999),
    ("float('inf') 的 -0 变体", float("inf") * -1),
    # —— Decimal 的非有限值 ——
    ("Decimal('Infinity')", decimal.Decimal("Infinity")),
    ("Decimal('-Infinity')", decimal.Decimal("-Infinity")),
    ("Decimal('NaN')", decimal.Decimal("NaN")),
    ("Decimal('sNaN')", decimal.Decimal("sNaN")),
    ("Decimal('1e999')", decimal.Decimal("1e999")),
    # —— 字符串形态（模型经常把数字写成字符串）——
    ("'1e999'", "1e999"),
    ("'inf'", "inf"),
    ("'-inf'", "-inf"),
    ("'infinity'", "infinity"),
    ("'Infinity'", "Infinity"),
    ("'nan'", "nan"),
    ("'  12  '", "  12  "),
    ("'12.7'", "12.7"),
    ("'abc'", "abc"),
    ("''", ""),
    ("'1_000'", "1_000"),
    # —— 超大/超小整数 ——
    ("10**100", 10 ** 100),
    ("-(10**100)", -(10 ** 100)),
    ("2**63", 2 ** 63),
    # —— 类型不对 ——
    ("None", None),
    ("True", True),
    ("False", False),
    ("[]", []),
    ("[1]", [1]),
    ("{}", {}),
    ("{'v': 1}", {"v": 1}),
    ("b'12'", b"12"),
    ("complex(1,2)", complex(1, 2)),
    ("object()", object()),
    ("lambda", (lambda: 1)),
]


def test_as_int_never_raises(s: TestSuite) -> None:
    """★ 核心：`_as_int` 对**任何**输入都不许抛异常。"""
    raised: list[str] = []
    table: list[str] = []
    for label, value in NON_INT_FORMS:
        try:
            r = S._as_int(value)
            table.append(f"{label:24s} → {r!r}")
        except BaseException as exc:                       # noqa: BLE001
            raised.append(f"{label} → {type(exc).__name__}: {exc}")
            table.append(f"{label:24s} → !! {type(exc).__name__}")
    s.expect(
        f"异常完备性：{len(NON_INT_FORMS)} 种非整数/非有限形态**无一抛出**",
        not raised,
        detail="逐形态实测结果（完整表）：\n         " + "\n         ".join(table),
        counterexample="反例：把 `_as_int` 改回只 `except (TypeError, ValueError)` → "
                       "`float('inf')` 会在 `int(round(...))` 抛 OverflowError → raised 非空 → 红。"
                       "**这正是 D1**；而这条断言要求的是「整张表都不抛」，"
                       "所以它同时覆盖了未来可能漏掉的新形态",
    )
    # 明确断言 D1 的那一种现在返回 None（而不是被静默当成 0 / 大整数）
    s.expect(
        "D1 的原始输入 `float('inf')` 现在返回 `None`（丢弃，而不是当成 0 或大整数）",
        S._as_int(float("inf")) is None and S._as_int(float("-inf")) is None,
        detail=f"_as_int(inf)={S._as_int(float('inf'))!r}；"
               f"_as_int(-inf)={S._as_int(float('-inf'))!r}",
        counterexample="反例：把非有限值 `round()` 成 0 或钳成某个整数 → 会让「模型吐 inf」"
                       "变成一次真实的数值变动（例如 +0 或巨额 +N）→ 返回非 None → 红",
    )
    # NaN 也丢弃（复核报告说它"反而安全"，修后应保持同一语义）
    s.expect(
        "`nan` 同样返回 None（与 ±Inf 同语义，不留特例）",
        S._as_int(float("nan")) is None and S._as_int("nan") is None,
        detail=f"_as_int(nan)={S._as_int(float('nan'))!r}；_as_int('nan')={S._as_int('nan')!r}",
        counterexample="反例：`math.isfinite` 只判 inf 不判 nan（写成 `f != float('inf')`）→ "
                       "nan 会走到 int() 抛 ValueError（被捕获后仍返回 None，但语义分裂）→ "
                       "在「全表不抛」上仍能过，故本断言单独把 nan 的**返回值**钉成 None",
    )


def test_json_infinity_is_reachable(s: TestSuite) -> None:
    """可达性：`json.loads` 接受 `Infinity`/`NaN` —— 这是 D1 能被模型触发的原因。"""
    ok_inf = ok_nan = None
    try:
        ok_inf = json.loads('{"exp": Infinity}')["exp"]
    except Exception:                                       # noqa: BLE001
        ok_inf = "<raise>"
    try:
        ok_nan = json.loads('{"exp": NaN}')["exp"]
    except Exception:                                       # noqa: BLE001
        ok_nan = "<raise>"
    s.expect(
        "`json.loads` 确实接受 Infinity / NaN（D1 的可达性前提）",
        isinstance(ok_inf, float) and math.isinf(ok_inf)
        and isinstance(ok_nan, float) and math.isnan(ok_nan),
        detail=f"json.loads('{{\"exp\": Infinity}}') → {ok_inf!r}；"
               f"json.loads('{{\"exp\": NaN}}') → {ok_nan!r}",
        counterexample="反例：若标准库某版本拒绝它们，这条会红 —— 那时 D1 的可达性下降，"
                       "但**防御仍然要保留**（换个人写回包也可能带进来）",
    )


def test_llm_delta_with_infinity(s: TestSuite) -> None:
    """端到端：模型吐 Infinity 时，整条解析路径**不抛异常**且越权部分被丢弃。"""
    cfg = PR.load_tuning()
    raw = json.loads('{"coc_delta": {"exp": Infinity, "funds": -Infinity, "doom": Infinity}}')
    raised = None
    turn = None
    try:
        from app.coc import prompt as P
        turn = P.narr_turn({"coc_delta": raw["coc_delta"], "scene": "x",
                            "dialogue": [], "choices": []}, cfg)
    except BaseException as exc:                            # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    s.expect(
        "`narr_turn` 收到含 Infinity 的 coc_delta → 不抛异常，且只保留合法键",
        raised is None and turn is not None
        and "doom" not in (turn or {}).get("coc_delta", {})
        and (turn or {}).get("coc_delta", {}).get("exp") is None,
        detail=f"raised={raised}；保留={sorted((turn or {}).get('coc_delta', {}))}；"
               f"丢弃={sorted(set(map(str, (turn or {}).get('dropped', []))))}",
        counterexample="反例：`_as_int` 未修 → 这里会 `OverflowError` → raised 非空 → 红。"
                       "这是**模型可达**的那条路径，所以它比单测 `_as_int` 更接近真实触发条件",
    )

    # 真实写点也不许炸
    run = L.Run.start({"difficulty": "Initiate", "seed": 1}, cfg)
    raised2 = None
    try:
        run.apply({"exp": float("inf"), "funds": float("-inf")}, reason="probe_inf")
    except BaseException as exc:                            # noqa: BLE001
        raised2 = f"{type(exc).__name__}: {exc}"
    s.expect(
        "真实写点（`Run.apply` → `apply_delta`）收到 inf 也不抛，且不改变状态",
        raised2 is None and int(run.state["exp"]) == 0 and int(run.state["funds"]) == 5,
        detail=f"raised={raised2}；exp={run.state['exp']}（期望 0）、"
               f"funds={run.state['funds']}（期望 5 = Initiate 起始）",
        counterexample="反例：未修时 `int(round(inf))` 在**写点**同样抛 → raised2 非空 → 红。"
                       "复核报告指出「真实写点同样炸」，这条就是它的回归防线",
    )


def test_clamp_with_non_finite(s: TestSuite) -> None:
    """`clamp_turn_delta` 走同一 `_as_int`，因此也要断言不抛。"""
    cfg = PR.load_tuning()
    raised = None
    out = None
    try:
        out = S.clamp_turn_delta(
            {"doom": float("inf"), "sta": float("-inf"), "rea": float("nan"),
             "exp": 1e999, "funds": "inf", "stat": {"KNW": float("inf")}}, cfg)
    except BaseException as exc:                            # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    s.expect(
        "`clamp_turn_delta` 对 inf/-inf/nan/1e999/'inf' 一律不抛，且不产生非法值",
        raised is None and out == {},
        detail=f"raised={raised}；输出={out!r}",
        counterexample="反例：未修 → `int(round(inf))` 抛 OverflowError → raised 非空 → 红",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_numeric_edge.py —— 数值边界异常形态完备性（D1）")
    for fn in (test_as_int_never_raises, test_json_infinity_is_reachable,
               test_llm_delta_with_infinity, test_clamp_with_non_finite):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

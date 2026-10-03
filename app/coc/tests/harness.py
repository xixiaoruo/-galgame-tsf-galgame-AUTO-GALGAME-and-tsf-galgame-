"""CoC 单测的极简框架（**零第三方依赖**）。

为什么不用 pytest：本机两个解释器都没有 pytest，而本任务的价值主张是
「不花 LLM 调用、不受模型随机性影响即可验证」—— 若验证本身还要装依赖，
这条就打了折扣。所以用标准库写一个够用的小框架：

  - 每个用例是一个函数，`TestCase` 收集「构造的反例输入 + 它确实变红了」；
  - `main()` 返回退出码（0=全绿），可直接被 CI 或 shell 调用。

全队规则（本轮采纳）：
> 如果一个断言我构造不出「让它变红」的输入，我就不会把它写进验收。

因此 `case.expect(...)` 强制要求传入 `counterexample=` —— 不写就抛 TypeError，
把「有没有想过反例」变成**框架级约束**，而不是靠自觉。
"""
from __future__ import annotations

import sys
import traceback


class Case:
    """一个小例子 + 它的反例说明。"""

    __slots__ = ("name", "ok", "detail", "counterexample", "demonstration")

    def __init__(self, name: str, ok: bool, detail: str,
                 counterexample: str, demonstration: str = ""):
        self.name = name
        self.ok = bool(ok)
        self.detail = detail
        self.counterexample = counterexample
        self.demonstration = demonstration


class TestSuite:
    """收集并运行用例。"""

    def __init__(self, title: str):
        self.title = title
        self.cases: list[Case] = []
        self.errors: list[str] = []

    def expect(self, name: str, ok: bool, *, detail: str = "",
               counterexample: str, demonstration: str = "") -> bool:
        """记一条断言。

        `counterexample` 是**必填**：写清「什么输入会让这条变红」。
        写不出反例的断言本身就不该进验收（那说明它测的不是被测路径）。
        """
        if not counterexample or not counterexample.strip():
            raise TypeError(
                f"[{name}] 必须提供 counterexample：构造不出反例的断言不许进验收"
            )
        self.cases.append(Case(name, ok, detail, counterexample, demonstration))
        return bool(ok)

    def run(self, fn) -> None:
        """运行一个用例函数；异常记为失败（而不是中断整套）。"""
        try:
            fn(self)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{fn.__name__}: {exc!r}\n{traceback.format_exc()}")
            self.cases.append(Case(
                f"{fn.__name__}（未捕获异常）", False, repr(exc),
                counterexample="反例：任意破坏前置条件的输入都会走到这条异常分支"))

    def report(self, stream=None) -> int:
        out = stream or sys.stdout
        total = len(self.cases)
        failed = [c for c in self.cases if not c.ok]
        out.write("=" * 78 + "\n")
        out.write(f"{self.title}\n")
        out.write("=" * 78 + "\n")
        for c in self.cases:
            mark = "PASS" if c.ok else "FAIL"
            out.write(f"  [{mark}] {c.name}\n")
            if c.detail:
                out.write(f"         证据：{c.detail}\n")
            out.write(f"         反例输入：{c.counterexample}\n")
            if c.demonstration:
                out.write(f"         变红实证：{c.demonstration}\n")
        for e in self.errors:
            out.write("\n!! 未捕获异常：\n" + e + "\n")
        out.write("-" * 78 + "\n")
        out.write(f"合计 {total} 条：PASS {total - len(failed)}，FAIL {len(failed)}\n")
        out.write("=" * 78 + "\n")
        return 1 if failed else 0

# -*- coding: utf-8 -*-
"""`app/llm.py::strip_json_block` 的形态回归测试（**离线、零依赖、不调 API**）。

## 为什么必须有这个文件

`app/llm.py` 的 `chat_json()` 里，解析层只在**非 mock** 分支上：

```python
if self.mock:
    return json.loads(raw)                 # 直接 return，不经过 strip_json_block
return json.loads(strip_json_block(raw))   # 只有真实调用才走这里
```

而 mock 的 `chat()` 自己产出**规整 JSON** ⇒ **整套既有自动化验证**
（`app.coc.tests.run_all` 的 9 个模块、四个探针、`acceptance_e2e` 的 11 个判据）
**全部跑在 mock 路径上，一个都测不到解析层**。

这解释了历史上那个缺陷为什么能长期存在：
`{"scene": "他说 ```你好```"}` 会被围栏正则啃成 `你好`，
`chat_json` 重试一次后抛 `LLMError`，被 `session.py` 兜底成默认文案
（`（一阵短暂的沉默。）`）—— **用户只看到 generic 文本，不知道发生了什么**。

修法已在（commit `397102d`），但**当时没有配测试**。本文件就是那个缺失的防线。

## 约定（与 测试工具/ 下其它平铺测试一致）

- 纯标准库；每个用例直接调用**真实** `app.llm.strip_json_block`（不 mock）
- 打印逐条结果与汇总；**全通过 exit 0，有失败 exit 1**
- **不写任何配置/存档**，不联网，不调真实 API

用法：
    python 测试工具/test_llm_strip_json.py
    python 测试工具/test_llm_strip_json.py -v      # 额外打印每条剥离结果
"""
from __future__ import annotations

import argparse
import io
import json
import sys

try:
    # 环境教训：`console=False` 的宿主里 sys.stdout 可能是 TextIOWrapper(encoding='gbk')，
    # 打印中文/emoji 会 UnicodeEncodeError。显式指定，避免在宿主里 exit=1。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, r"D:\TSF_Galgame")

from app.llm import strip_json_block          # noqa: E402

# ---------------------------------------------------------------- 用例表
# (标签, 原始文本, 期望解析出的对象)
# 前 14 条是"队长实测已通过"的形态（回归底线：一条都不许退化）；
# 第 15 条是本次缺陷的**原始形态**；16–18 是我补的同根因变体。
CASES: list[tuple[str, str, dict]] = [
    ("裸 JSON",
     '{"scene": "街角", "x": 1}', {"scene": "街角", "x": 1}),
    ("标准 ```json 围栏",
     '```json\n{"scene": "街角", "x": 1}\n```', {"scene": "街角", "x": 1}),
    ("无语言标记围栏",
     '```\n{"scene": "街角", "x": 1}\n```', {"scene": "街角", "x": 1}),
    ("围栏 + 前后说明",
     '好的，这是结果：\n```json\n{"scene": "街角", "x": 1}\n```\n希望有用。',
     {"scene": "街角", "x": 1}),
    ("前置说明无围栏",
     '这是 JSON：\n{"scene": "街角", "x": 1}', {"scene": "街角", "x": 1}),
    ("后置说明无围栏",
     '{"scene": "街角", "x": 1}\n以上。', {"scene": "街角", "x": 1}),
    ("前后都有说明",
     '说明前\n{"scene": "街角", "x": 1}\n说明后', {"scene": "街角", "x": 1}),
    ("BOM 开头",
     '\ufeff{"scene": "街角", "x": 1}', {"scene": "街角", "x": 1}),
    ("CRLF 换行",
     '```json\r\n{"scene": "街角", "x": 1}\r\n```', {"scene": "街角", "x": 1}),
    ("嵌套对象",
     '{"a": {"b": {"scene": "街角"}}, "x": 1}',
     {"a": {"b": {"scene": "街角"}}, "x": 1}),
    ("字符串值含单花括号",
     '{"scene": "他写了 { 就停了", "x": 1}', {"scene": "他写了 { 就停了", "x": 1}),
    ("字符串值含成对花括号",
     '{"scene": "用 {a} 表示", "x": 1}', {"scene": "用 {a} 表示", "x": 1}),
    ("内联到句中",
     '结果就是 {"scene": "街角", "x": 1} 这样。', {"scene": "街角", "x": 1}),
    # ★ 下面这条是本次缺陷的原始形态（commit 397102d 修的就是它）
    ("★ 字符串值内含三反引号（本次缺陷原始形态）",
     '{"scene": "他说 ```你好```", "x": 1}', {"scene": "他说 ```你好```", "x": 1}),
    ("字符串值含字面 ```json",
     '{"scene": "开头是 ```json 标记", "x": 1}', {"scene": "开头是 ```json 标记", "x": 1}),
    ("字符串值含反引号包裹的行",
     '{"scene": "他念道 ```abc``` 然后停下", "x": 2}',
     {"scene": "他念道 ```abc``` 然后停下", "x": 2}),
    ("围栏内层字符串含反引号（有围栏也被啃）",
     '```json\n{"scene": "他说 ```你好```", "x": 1}\n```',
     {"scene": "他说 ```你好```", "x": 1}),
]

# 数组根的两种情形分开断言 —— 如实钉住**既有**行为（与本次反引号修复无关）：
#   · 纯标量数组（无花括号）：原样保留 list
#   · 单对象数组（对象隐式转成 {"0": ...}）：被花括号截取展开成**那个对象**，不是 list
# 后者是花括号截取的**既有**副作用，且对真实消费方**无害**：
#   chat_json 的全部 17 个调用点都要 **dict**（`-> dict`），top-level 数组本就不是合法回包。
# 把这些写清楚，是为了避免以后有人看到 `[{"a":1}]` → dict 时以为是新 bug 而"修"坏它。
ARRAY_CASES: list[tuple[str, str, object]] = [
    ("数组根：纯标量数组保持 list", '[1, 2, 3]', [1, 2, 3]),
    ("数组根：嵌套数组保持 list", '[[1, 2], [3]]', [[1, 2], [3]]),
    ("数组根：单对象数组 → 展开为那个 dict（既有行为，对 dict 消费方无害）",
     '[{"scene": "街角"}]', {"scene": "街角"}),
]


def _run_one(label: str, raw: str, want, verbose: bool) -> tuple[bool, str]:
    """返回 (是否通过, 证据串)。异常一律捕获成失败，绝不让单个用例炸掉整轮。"""
    try:
        stripped = strip_json_block(raw)
    except Exception as e:                                     # noqa: BLE001
        return False, f"strip_json_block 抛出 {type(e).__name__}: {e}"
    try:
        got = json.loads(stripped)
    except json.JSONDecodeError as e:
        return False, (f"解析失败 {e}；剥离结果={stripped!r}"
                       f"（原始输入={raw!r}）")
    if got != want:
        return False, f"内容不符：得到 {got!r}，期望 {want!r}"
    if verbose:
        return True, f"→ {json.dumps(got, ensure_ascii=False)}"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="strip_json_block 形态回归测试")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="打印每条剥离/解析结果")
    args = ap.parse_args()

    print("=" * 74)
    print("app/llm.py::strip_json_block 形态回归测试（离线，不调 API）")
    print("=" * 74)

    all_cases = CASES + ARRAY_CASES
    fails: list[tuple[str, str, str]] = []
    for label, raw, want in all_cases:
        ok, ev = _run_one(label, raw, want, args.verbose)
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f"  {ev}" if ev else ""))
        if not ok:
            fails.append((label, raw, ev))

    # ---- 幂等性：对已剥离结果再剥一次必须不变（防"重复调用被啃"）----
    idem_bad = []
    for label, raw, _w in all_cases:
        try:
            once = strip_json_block(raw)
            twice = strip_json_block(once)
        except Exception as e:                                 # noqa: BLE001
            idem_bad.append((label, f"{type(e).__name__}: {e}", ""))
            continue
        if once != twice:
            idem_bad.append((label, once, twice))
    print(f"\n  幂等性（剥两次 == 剥一次）："
          f"{'全部一致' if not idem_bad else f'{len(idem_bad)} 条不一致'}")
    for label, a, b in idem_bad:
        print(f"    [FAIL] {label}\n      1次={a!r}\n      2次={b!r}")

    print("-" * 74)
    total = len(all_cases)
    print(f"形态用例：{total - len(fails)}/{total} 通过"
          f"；幂等性：{'通过' if not idem_bad else '失败'}")
    if fails:
        print("\n失败明细（含原始输入，便于直接复现）：")
        for label, raw, ev in fails:
            print(f"\n  [{label}]")
            print(f"    输入 : {raw!r}")
            print(f"    问题 : {ev}")
    print("=" * 74)

    ok = not fails and not idem_bad
    print("结论：" + ("全部通过 ✔" if ok else "存在失败 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

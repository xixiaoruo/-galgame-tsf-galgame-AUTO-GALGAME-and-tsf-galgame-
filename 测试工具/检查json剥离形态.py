# -*- coding: utf-8 -*-
"""对**真实的** `app/llm.py::strip_json_block` 跑 18 种形态矩阵（不调 API）。

与 `_repro_strip_json.py` 的区别：那个脚本内联了"修前/修后"两份 stub 做对照；
本脚本**直接 import 真代码**，验证"当前落地实现"是否真的通过 —— 避免出现
"报告通过、真代码没通过"这种假证。
"""
from __future__ import annotations

import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, r"D:\TSF_Galgame")

from app.llm import strip_json_block, LLMError   # noqa: E402

CASES: list[tuple[str, str]] = [
    ("裸 JSON", '{"scene": "街角", "x": 1}'),
    ("标准 ```json 围栏", '```json\n{"scene": "街角", "x": 1}\n```'),
    ("无语言标记围栏", '```\n{"scene": "街角", "x": 1}\n```'),
    ("围栏 + 前后说明", '好的，这是结果：\n```json\n{"scene": "街角", "x": 1}\n```\n希望有用。'),
    ("前置说明无围栏", '这是 JSON：\n{"scene": "街角", "x": 1}'),
    ("后置说明无围栏", '{"scene": "街角", "x": 1}\n以上。'),
    ("前后都有说明", '说明前\n{"scene": "街角", "x": 1}\n说明后'),
    ("BOM 开头", '\ufeff{"scene": "街角", "x": 1}'),
    ("CRLF 换行", '```json\r\n{"scene": "街角", "x": 1}\r\n```'),
    ("嵌套对象", '{"a": {"b": {"scene": "街角"}}, "x": 1}'),
    ("字符串值含单花括号", '{"scene": "他写了 { 就停了", "x": 1}'),
    ("字符串值含成对花括号", '{"scene": "用 {a} 表示", "x": 1}'),
    ("内联到句中", '结果就是 {"scene": "街角", "x": 1} 这样。'),
    ("数组里有对象", '[{"scene": "街角"}]'),
    ("字符串值内含三反引号", '{"scene": "他说 ```你好```", "x": 1}'),
    ("字符串值含字面 ```json", '{"scene": "开头是 ```json 标记", "x": 1}'),
    ("字符串值含反引号包裹的行", '{"scene": "他念道 ```abc``` 然后停下", "x": 2}'),
    ("围栏内层字符串含反引号",
     '```json\n{"scene": "他说 ```你好```", "x": 1}\n```'),
]


def main() -> int:
    print("=" * 78)
    print("对真实 app/llm.py::strip_json_block 的形态矩阵")
    print("=" * 78)
    fails = []
    for label, raw in CASES:
        try:
            stripped = strip_json_block(raw)
            obj = json.loads(stripped)
            print(f"  [PASS] {label:28s} → {json.dumps(obj, ensure_ascii=False)[:60]}")
        except Exception as e:                                # noqa: BLE001
            fails.append((label, type(e).__name__, str(e)))
            print(f"  [FAIL] {label:28s} → {type(e).__name__}: {e}")
    print("-" * 78)
    print(f"真实实现：通过 {len(CASES) - len(fails)}/{len(CASES)}")
    if fails:
        print("\n失败明细：")
        for label, kind, msg in fails:
            print(f"  {label}: {kind}: {msg}")
    # 幂等性检查：对已剥离结果再剥离一次，应不变（防"重复调用被啃"）
    idem = []
    for label, raw in CASES:
        try:
            once = strip_json_block(raw)
            twice = strip_json_block(once)
            if once != twice:
                idem.append((label, once, twice))
        except Exception:                                     # noqa: BLE001
            pass
    print(f"\n幂等性（剥两次 == 剥一次）：{'全部一致' if not idem else f'{len(idem)} 条不一致'}")
    for label, a, b in idem:
        print(f"  {label}:\n    1次={a!r}\n    2次={b!r}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""跑 `app/coc/tests/` 下的**全部**单测（零依赖，标准库即可）。

用法：
    python -m app.coc.tests.run_all          # 在项目根运行
    python app/coc/tests/run_all.py          # 直接运行文件也行

退出码：全绿 0；任一失败 1（可直接被 CI / shell 链式调用）。
"""
from __future__ import annotations

import io
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

MODULES = [
    "test_tuning_provenance",
    "test_dice",
    "test_sanitize",
    "test_state",
    "test_loop",
    "test_content",
    # P18-A（t51）接线期新增：三硬闸门（空串 + 最终 prompt 逐字节相等）
    "test_gate_empty",
    # 缺陷修复：两类路径（随包只读 vs 玩家可写）—— 防"存档写进安装目录"回归
    "test_paths_layout",
    # 复核 D1：数值边界异常形态完备性（inf/-inf/nan/Decimal/字符串/超大整数…）
    "test_numeric_edge",
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    import importlib

    results: list[tuple[str, int, int]] = []
    started = time.time()
    for name in MODULES:
        mod = importlib.import_module(f"app.coc.tests.{name}")
        print()
        rc = mod.main()
        results.append((name, rc, 0))

    print()
    print("#" * 78)
    print("# 汇总：app/coc/tests/ 全部单测")
    print("#" * 78)
    failed = [n for n, rc, _ in results if rc != 0]
    for name, rc, _ in results:
        print(f"  {'PASS' if rc == 0 else 'FAIL'}  {name}")
    print(f"\n  用时 {time.time() - started:.2f}s；模块 {len(results)} 个，"
          f"失败 {len(failed)} 个")
    if failed:
        print("  失败模块：" + "、".join(failed))
    print()
    print("  提示：接线期的另外三个门（在同一进程外跑，避免互相污染）：")
    print("    python -B 测试工具/coc_wiring_probe.py   # 三硬闸门/白名单/存读档/config")
    print("    python -B 测试工具/coc_http_probe.py     # 五处分发点（TestClient）")
    print("    python -B 测试工具/coc_frozen_probe.py   # 冻结布局数据落点")
    print("#" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

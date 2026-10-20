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

# ── 结构性隔离闸门（fail-fast）──────────────────────────────────────────────
# 为什么放在这里而不是"写文档提醒"：本会话反复出现"要求成员隔离、成员也照做了，
# 但某条路径仍写进真实数据目录"——包括一次孤儿回归运行器持续往用户目录写了 13 个档。
# 靠人记得已被证明失效，所以改成"忘记设置 TSF_DATA_DIR 就根本跑不起来"。
# 注意：这里只**拒绝未设**，不校验指向哪里（校验指向会误伤合法的绝对路径样本）。
if not os.environ.get("TSF_DATA_DIR"):
    sys.stderr.write(
        "\n[隔离闸门] 拒绝运行：未设置 TSF_DATA_DIR。\n"
        "  本测试套件会写存档；若 DATA_DIR 落在真实数据目录，就会污染玩家数据。\n"
        "  请先设置，例如：\n"
        '    set TSF_DATA_DIR=D:\\dsh创造\\coc-test-iso        (cmd)\n'
        '    $env:TSF_DATA_DIR="D:\\dsh创造\\coc-test-iso"    (powershell)\n'
        "  然后重跑。\n\n")
    sys.exit(3)

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
    # t54：CoC 内的 TSF 子系统（机制型）—— 单调/可复现/旧档/边界/白名单/可观察性
    "test_coc_tsf",
    # t63：dc 梯度 / 属性门槛（fail-closed）/ 物品栏 / 武器判定
    "test_stats_items",
    # t71：TSF×CoC 融合模式下的物品/装备/背包机制（属性修正进命中 / 服装演化 / 背包可用性）
    "test_tsf_items",
    # t73：自定义剧本 —— 世界观真正决定地点/谜团/古神/事件卡（含零影响与存档往返）
    "test_scenario",
    # t62：战斗 —— 200 时间单位 / ≤8 动作队列、武器属性检定、incorporeal、驱魔、可复现、账本
    "test_combat",
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

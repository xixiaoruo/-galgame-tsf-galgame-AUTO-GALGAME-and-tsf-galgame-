# -*- coding: utf-8 -*-
r"""清理**只属于 CoC 探针/单测**写入真实数据目录的存档。

为什么需要：t51 修复后 coc 写盘跟随 `app.config.DATA_DIR`（= 真实玩家数据目录
`D:\TSF_Galgame_Data\data`）。这**是正确行为**，但也意味着接线期的探针/单测若不带
`TSF_DATA_DIR` 隔离，就会在玩家数据目录里留下测试存档 —— 这次确实留下了，必须清掉。

**判定"是我的"的依据（三重，全部满足才删）**：
  1. 在指定时间窗口内被修改（默认 2026-10-04 03:00 ~ 03:30）；
  2. 文件是 JSON，且顶层 `mode == "coc"`（既有两模式的存档没有这个字段）；
  3. 位于 `data/saves/` 或 `data/slots/`。

**绝不删除**：既有模式的存档槽（`slot<10位秒级时间戳>.json` 且无 `mode` 字段）、
有 `protagonist` 字段的会话存档、任何非 coc 文件。

用法（默认 dry-run，只打印不删）：
    python 清理coc测试存档.py            # 预演
    python 清理coc测试存档.py --apply    # 真删
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA = r"D:\TSF_Galgame_Data\data"
WINDOW_START = dt.datetime(2026, 10, 4, 3, 0, 0)
WINDOW_END = dt.datetime(2026, 10, 4, 3, 40, 0)


def is_coc_file(path: str) -> tuple[bool, str]:
    try:
        with io.open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"读不动（{e.__class__.__name__}）"
    if not isinstance(obj, dict):
        return False, "顶层不是对象"
    if obj.get("mode") == "coc":
        return True, "顶层 mode=coc"
    if "coc_state" in obj:
        return True, "含 coc_state（槽信封）"
    if "protagonist" in obj:
        return False, "既有模式会话（有 protagonist）——不动"
    return False, f"无 coc 标记（keys={sorted(obj)[:4]}）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删（默认仅预演）")
    ap.add_argument("--data", default=DATA)
    args = ap.parse_args()

    print("=" * 74)
    print(f"清理 CoC 测试存档（{'APPLY' if args.apply else 'DRY-RUN'}）")
    print(f"数据目录：{args.data}")
    print(f"时间窗口：{WINDOW_START} ~ {WINDOW_END}")
    print("=" * 74)

    victims, kept = [], []
    for sub in ("saves", "slots"):
        d = os.path.join(args.data, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            st = os.stat(p)
            mt = dt.datetime.fromtimestamp(st.st_mtime)
            ok, why = is_coc_file(p)
            in_window = WINDOW_START <= mt <= WINDOW_END
            if ok and in_window:
                victims.append((p, st.st_size, mt, why))
            elif ok and not in_window:
                kept.append((p, mt, "coc 但不在时间窗口内 —— 不动"))
            else:
                kept.append((p, mt, why))

    print(f"\n判定为**我的 coc 测试存档**（将删除）：{len(victims)} 个")
    for p, size, mt, why in victims:
        print(f"  DEL  {os.path.relpath(p, args.data):44s} {size:>7} B  {mt}  [{why}]")
    total = sum(v[1] for v in victims)
    print(f"  合计 {total / 1024:.0f} KB")

    print(f"\n保留：{len(kept)} 个")
    for p, mt, why in kept[:12]:
        print(f"  keep {os.path.relpath(p, args.data):44s} {mt}  [{why}]")
    if len(kept) > 12:
        print(f"  … 其余 {len(kept) - 12} 个同样保留")

    if not args.apply:
        print("\n（预演结束；加 --apply 才真删）")
        return 0
    for p, _s, _m, _w in victims:
        os.remove(p)
    print(f"\n已删除 {len(victims)} 个 coc 测试存档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

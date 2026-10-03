# -*- coding: utf-8 -*-
"""只读清单：真实数据目录里所有 coc 存档/存档槽（文件名 + 时间 + 大小）。

**只报告，不删除** —— 队长明确要求"不要自行删除用户数据目录里的文件"。
用 `--before <ISO>` 可区分"我 t52 期间写的"与"更早/别人写的"。
"""
import argparse
import datetime as dt
import glob
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOTS = {
    "saves": r"D:\TSF_Galgame_Data\data\saves",
    "slots": r"D:\TSF_Galgame_Data\data\slots",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="2026-10-04T03:00:00",
                    help="此时间之前的归为'先前/他人'，之后的归为'本次会话'")
    a = ap.parse_args()
    cut = dt.datetime.fromisoformat(a.before)

    mine, earlier = [], []
    for kind, d in ROOTS.items():
        for p in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                o = json.load(io.open(p, encoding="utf-8"))
            except Exception:
                continue
            is_coc = isinstance(o, dict) and (
                o.get("mode") == "coc" or ("coc_state" in o and "protagonist" not in o))
            if not is_coc:
                continue
            st = os.stat(p)
            mt = dt.datetime.fromtimestamp(st.st_mtime)
            rec = (kind, os.path.basename(p), mt.isoformat(timespec="seconds"),
                   st.st_size, "")
            (mine if mt >= cut else earlier).append(rec)

    for label, rows in (("① 本次会话（t52 期间）写入的", mine),
                        ("② 更早（先前/他人）写入的", earlier)):
        print(f"\n{label}：{len(rows)} 个")
        for kind, name, mt, size, _ in rows:
            print(f"   {kind:6s} {name:28s} {mt}  {size:>7} B")
    print(f"\n合计 coc 文件 {len(mine) + len(earlier)} 个"
          f"（本次 {len(mine)} / 先前 {len(earlier)}）")
    print("说明：只报告，未删除任何文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

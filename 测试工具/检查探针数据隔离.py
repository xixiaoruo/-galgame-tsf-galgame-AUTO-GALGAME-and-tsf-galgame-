# -*- coding: utf-8 -*-
"""逐探针检查数据隔离：谁在往玩家真实数据目录写 coc 存档。

用途：t52 修好落点后，用它确认跑任何探针都不再污染真实数据目录。
做法：清点 coc 档 -> 逐个跑探针（每个之间重新清点）-> 打印增量。

做法：清点当前 coc 档 → 逐个跑探针（每个之间重新清点）→ 打印增量。
**不删任何文件**（本脚本只读；清理由 `清理coc测试存档.py` 负责且需人工确认）。
"""
import glob
import io
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"D:\TSF_Galgame"
REAL_SAVES = r"D:\TSF_Galgame_Data\data\saves"
REAL_SLOTS = r"D:\TSF_Galgame_Data\data\slots"
ENV = dict(os.environ)
ENV["PYTHONIOENCODING"] = "utf-8"
ENV["PYTHONUTF8"] = "1"


def coc_files(d):
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            o = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(o, dict) and o.get("mode") == "coc":
            out.append(p)
    return out


def snapshot():
    s = set(coc_files(REAL_SAVES))
    l = set(coc_files(REAL_SLOTS))
    return s, l


def main() -> int:
    s0, l0 = snapshot()
    print(f"起始：真实 saves 里 coc 档 {len(s0)} 个、slots 里 coc 槽 {len(l0)} 个")
    for p in sorted(s0):
        mt = os.path.getmtime(p)
        import datetime as dt
        print(f"  已有 {os.path.basename(p):26s} {dt.datetime.fromtimestamp(mt)}")

    probes = ["coc_wiring_probe", "coc_http_probe", "coc_frozen_probe", "coc_paths_probe"]
    for name in probes:
        script = os.path.join(ROOT, "测试工具", f"{name}.py")
        r = subprocess.run([sys.executable, "-B", script], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=ENV, timeout=300,
                           creationflags=0x08000000)
        s1, l1 = snapshot()
        new_s = sorted(set(s1) - s0)
        new_l = sorted(set(l1) - l0)
        flag = "!! 写了真实目录" if (new_s or new_l) else "ok"
        print(f"{name:20s} exit={r.returncode} 新增 saves={len(new_s)} slots={len(new_l)}  {flag}")
        for p in new_s + new_l:
            print(f"     NEW {p}")
        s0, l0 = s1, l1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

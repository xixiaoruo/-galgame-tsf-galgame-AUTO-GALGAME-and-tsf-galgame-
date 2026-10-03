# -*- coding: utf-8 -*-
"""验证「冻结产物里 data/coc 能被加载」——按 PyInstaller onedir 的真实布局做等价模拟。

为什么需要模拟而不是直接跑 exe：
  `dist/TSF_Galgame.exe` 的时间戳是 2026/9/26，**早于 CoC（t48/t51）**，
  所以它里面不可能有 `app/coc` 与 `data/coc`。重打包要十几分钟且属 t49 的范围，
  本任务不做。于是这里做**路径算术**的等价验证：

  1. 按 `TSF_Galgame.spec` 的 dat 规则造出 `_internal/data/coc/**`；
  2. 把 `app/`（含 coc 包）放到 `_internal/app/`；
  3. 用一个子进程把 `_internal` 放进 sys.path —— 此时
     `app/coc/paths.py` 的 `__file__` 与冻结后的位置**同构**；
  4. 断言 `paths.ROOT` 指向 `_internal`、且 `load_tuning()` 成功。

  这验证的是"路径推导 + 数据落点"这一对是否自洽 —— 正是冻结后最容易错的地方
  （t49 的 spec 注释也点明：onedir 下 `app.coc.paths.__file__` 解析到
   `_internal/app/coc/paths.py`，其 ROOT 即 `_internal`）。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJ = r"D:\TSF_Galgame"
SRC_COC = os.path.join(PROJ, "app", "coc")
SRC_DATA = os.path.join(PROJ, "data", "coc")

results = []


def check(cid, desc, ok, evidence, counterexample):
    results.append({"id": cid, "ok": bool(ok)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {cid} {desc}")
    print(f"         证据：{evidence}")
    print(f"         反例：{counterexample}")
    return bool(ok)


def main() -> int:
    print("=" * 78)
    print("冻结产物数据落点验证（onedir 布局等价模拟）")
    print("=" * 78)

    tmp = tempfile.mkdtemp(prefix="coc-frozen-")
    internal = os.path.join(tmp, "_internal")
    try:
        # 1) _internal/app/（含 __init__.py 与 coc 包）
        os.makedirs(os.path.join(internal, "app"), exist_ok=True)
        io.open(os.path.join(internal, "app", "__init__.py"), "w",
                encoding="utf-8").close()
        shutil.copytree(SRC_COC, os.path.join(internal, "app", "coc"),
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))

        # 2) _internal/data/coc/**（模拟 spec 里 _coc_datas 的落点）
        shutil.copytree(SRC_DATA, os.path.join(internal, "data", "coc"),
                        ignore=shutil.ignore_patterns("saves", "slots", "__pycache__"))

        # 3) 子进程：把 _internal 放进 sys.path，从那里导入
        probe = (
            "import sys, json, os\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from app.coc import paths, provenance\n"
            "out = {'ROOT': paths.ROOT, 'TUNING': paths.TUNING_PATH,\n"
            "       'exists': os.path.isfile(paths.TUNING_PATH)}\n"
            "try:\n"
            "    cfg = provenance.load_tuning()\n"
            "    out['loaded'] = True\n"
            "    out['difficulty_initiate'] = provenance.t(cfg, 'difficulty')['Initiate']['sta']\n"
            "except Exception as e:\n"
            "    out['loaded'] = False\n"
            "    out['err'] = repr(e)\n"
            "from app.coc import loop\n"
            "run = loop.Run.start({'difficulty': 'Initiate', 'seed': 1}, cfg) if out['loaded'] else None\n"
            "out['cards'] = len(run.card_pool) if run else 0\n"
            "print(json.dumps(out, ensure_ascii=False))\n"
        )
        p = subprocess.run([sys.executable, "-B", "-c", probe, internal],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        line = (p.stdout or "").strip().splitlines()[-1] if p.stdout.strip() else "{}"
        got = json.loads(line)

        check("FROZEN-ROOT", "冻结布局下 paths.ROOT 解析到 _internal",
              os.path.normcase(got.get("ROOT", "")) == os.path.normcase(internal),
              f"_internal={internal}；paths.ROOT={got.get('ROOT')}",
              "反例：把 spec 的落点写成 `app/data/coc`（而不是 `data/coc`）→ 则 "
              "ROOT/data/coc 不存在 → TUNING_PATH 指向不存在的文件 → TUNING-EXISTS 红")

        check("FROZEN-EXISTS", "冻结布局下 tuning.json 确实存在",
              bool(got.get("exists")),
              f"TUNING_PATH={got.get('TUNING')} exists={got.get('exists')}",
              "反例：spec 里不收集 data/coc → 该文件不存在 → 红（这正是 t49 spec 注释"
              "所警告的：data/ 被 .gitignore 忽略，必须显式打进包）")

        check("FROZEN-LOAD", "冻结布局下 load_tuning() 成功（含溯源校验）",
              bool(got.get("loaded")) and got.get("difficulty_initiate") == 21,
              f"loaded={got.get('loaded')} Initiate.sta={got.get('difficulty_initiate')} "
              f"err={got.get('err', '')}",
              "反例：漏打包 cards/ 或 tuning.json → TuningMissingError / ContentError → 红")

        check("FROZEN-ENGINE", "冻结布局下引擎可开局（卡池加载成功）",
              int(got.get("cards", 0)) == 24,
              f"卡池 {got.get('cards')} 张（期望 24）",
              "反例：spec 只收集了 *.json 顶层、漏了 cards/ 子目录 → 卡池 0 张 → 红")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r["ok"]]
    print("\n" + "=" * 78)
    print(f"合计 {len(results)} 条：PASS {len(results) - len(failed)}，FAIL {len(failed)}")
    print("限制：这是**布局等价模拟**，不是跑真实 exe。dist/TSF_Galgame.exe 早于本任务，")
    print("      未重打包（重打包属 t49 范围）。")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

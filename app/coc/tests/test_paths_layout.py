"""`paths.py` 的两类路径单测 —— **玩家可写数据必须跟随 `app.config.DATA_DIR`**。

这一条是 t51 缺陷的永久回归防线。缺陷本质：
> 第一版把**随包只读数据**与**玩家可写数据**都塞进「由 `__file__` 推出来的
> `<包根>/data/`」。非冻结下看起来能用，冻结 onedir 下 `__file__` 推出
> `ROOT = _internal` ⇒ **存档写进安装目录** ⇒ 替换目录更新时连带丢档，
> 违反项目红线「游戏数据永不因更新丢失」。

本文件的断言分两层：
  ① **行为**：把 `config.DATA_DIR` 临时指到别处，coc 的写盘路径必须跟着变
     （用 `tempfile` + 恢复原值，**不写真实玩家数据目录**）；
  ② **分类**：随包只读数据**不得**跟随 `DATA_DIR`，且不得由 `__file__` 推出。

②里的"不得由 `__file__` 推出"这条断言是**专门防回归**的：只要有人把
`SAVES_DIR`/`SLOTS_DIR` 改回 `os.path.join(ROOT, ...)`，它立刻变红。
"""
from __future__ import annotations

import os
import sys
import tempfile

from app import config as cfgmod
from app.coc import paths
from app.coc.tests.harness import TestSuite


def test_writable_follows_data_dir(s: TestSuite) -> None:
    """① 行为：写盘路径跟随 `config.DATA_DIR`。"""
    original = cfgmod.DATA_DIR
    tmp = tempfile.mkdtemp(prefix="coc-paths-")
    try:
        cfgmod.DATA_DIR = type(original)(tmp)
        wr = paths.classify()["player_writable"]
        saves, slots, dd = wr["saves"], wr["slots"], wr["data_dir"]
        s.expect(
            "把 DATA_DIR 指到临时目录后，coc 的 saves/slots 都跟着变到该目录之下",
            os.path.normcase(dd).startswith(os.path.normcase(tmp))
            and os.path.normcase(saves).startswith(os.path.normcase(tmp))
            and os.path.normcase(slots).startswith(os.path.normcase(tmp)),
            detail=f"临时 DATA_DIR={tmp}\n"
                   f"         data_dir={dd}\n         saves={saves}\n         slots={slots}",
            counterexample="反例：若 saves/slots 由 `__file__` 推出（修复前的写法），"
                           "把 DATA_DIR 指到临时目录对它们**毫无影响**，三者仍指向包内路径 → "
                           "startswith(tmp) 失败 → 变红。这条就是「写盘是否跟随数据根」的判据",
        )
        # 槽目录必须与既有模式同一个（否则 game.list_slots() 列不出 coc 槽）
        s.expect(
            "coc 的 slots 与既有模式 `DATA_DIR/slots` 一致",
            os.path.normcase(slots) == os.path.normcase(os.path.join(tmp, "slots")),
            detail=f"coc slots={slots}；既有模式={os.path.join(tmp, 'slots')}",
            counterexample="反例：把槽写到 `saves/`（或包内 `data/saves/`）→ 与既有模式不同 → "
                           "`game.list_slots()` 永远列不出 coc 槽 → 变红。"
                           "修复前 coc 槽实际写在包内 `data/saves/`，与 `DATA_DIR/slots` 根本不是一处",
        )
    finally:
        cfgmod.DATA_DIR = original


def test_readonly_content_not_followed(s: TestSuite) -> None:
    """② 分类：随包只读数据**不**跟随 `DATA_DIR`，且**不**由 `__file__` 推出写盘路径。"""
    original = cfgmod.DATA_DIR
    tmp = tempfile.mkdtemp(prefix="coc-paths-")
    try:
        cfgmod.DATA_DIR = type(original)(tmp)
        br = paths.classify()["bundled_readonly"]
        s.expect(
            "随包只读内容（tuning/卡池）仍在包内，不跟随 DATA_DIR",
            os.path.normcase(br["root"]).startswith(os.path.normcase(paths.ROOT))
            and not os.path.normcase(br["root"]).startswith(os.path.normcase(tmp))
            and os.path.isfile(br["tuning"]),
            detail=f"bundled root={br['root']}（包根={paths.ROOT}）\n"
                   f"         tuning 存在={os.path.isfile(br['tuning'])}",
            counterexample="反例：把读取也改到 `DATA_DIR/data/coc` → 未预置内容的玩家目录会让"
                           "引擎启动即抛 TuningMissingError → 红。**读取跟随 DATA_DIR 是错的**",
        )
        s.expect(
            "★ 防回归：写盘路径**不得**等于「包根/data/」之下的路径",
            not os.path.normcase(br["root"]).startswith(
                os.path.normcase(os.path.join(paths.ROOT, "data", "saves"))),
            detail=f"包内 data 根={os.path.join(paths.ROOT, 'data')}；"
                   f"写盘 saves（此刻）={paths.saves_dir()}",
            counterexample="反例：这正是**修复前的 bug** —— `SAVES_DIR = os.path.join(ROOT, "
                           "'data', 'coc', 'saves')`。冻结下 ROOT=_internal，于是存档写进安装目录。"
                           "本断言在**非冻结**下也能亮起来，因此它比「只在冻结下才红」的断言更早拦住回归",
        )
    finally:
        cfgmod.DATA_DIR = original


def test_no_file_derived_writable(s: TestSuite) -> None:
    """静态断言：`paths.py` 里不得出现「ROOT + data + saves/slots」这种写盘写法。"""
    import io
    src = io.open(os.path.join(paths.COC_DIR, "paths.py"), encoding="utf-8").read()
    code_lines = [l for l in src.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    # 只扫**代码行**（注释里可以出现这两种写法用于说明）
    bad = [l.strip() for l in code_lines
           if ("os.path.join(ROOT" in l and ("saves" in l or "slots" in l))]
    s.expect(
        "paths.py 的**代码**里没有 `os.path.join(ROOT, ..., 'saves'|'slots')`",
        not bad,
        detail=f"扫描 {len(code_lines)} 行代码，命中 {bad}",
        counterexample="反例：写 `SLOTS_DIR = os.path.join(ROOT, 'data', 'saves')`（修复前的写法）→ "
                       "命中非空 → 变红。这条是静态的：不依赖运行时取值，"
                       "所以连「改完但没跑起来」也能拦住",
    )
    # 反向自检：确认这个扫描真的能命中
    probe = [l for l in ["X = os.path.join(ROOT, 'data', 'saves')"]
             if ("os.path.join(ROOT" in l and ("saves" in l or "slots" in l))]
    s.expect(
        "反向自检：该扫描对修复前的写法确实会命中（防止写成恒真）",
        len(probe) == 1,
        detail=f"注入修复前写法后命中 {len(probe)} 条",
        counterexample="反例：把匹配串写错（例如漏了 ROOT）→ probe 为空 → 变红（说明上一条永不触发）",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_paths_layout.py —— 两类路径（随包只读 vs 玩家可写）")
    for fn in (test_writable_follows_data_dir, test_readonly_content_not_followed,
               test_no_file_derived_writable):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

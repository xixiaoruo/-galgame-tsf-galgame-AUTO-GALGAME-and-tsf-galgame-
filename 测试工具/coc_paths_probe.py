# -*- coding: utf-8 -*-
"""路径分类与两种形态的实测（t51 缺陷修复验收）。

验证四件事：
  ① **非冻结**下 `DATA_DIR`/`SAVES_DIR`/`SLOTS_DIR` 的实际取值；
  ② **冻结**（PyInstaller onedir 布局等价模拟）下**存档不落在安装目录内**；
  ③ **`TSF_DATA_DIR` 逃逸舱生效** —— 设了它之后 coc 存档落在该目录；
  ④ **随包只读数据不受 `DATA_DIR` 影响**（`data/coc` 仍在包内）。

为什么要分子进程做：`TSF_DATA_DIR` 是**进程启动时**由 `app.config` 读的，
同进程内改环境变量不会让 `app.config.DATA_DIR` 跟着变（它是模块级常量）。
所以只有起子进程才是**真实**的逃逸舱验证，而不是验证一个假场景。

用法：python 测试工具/coc_paths_probe.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_COC = os.path.join(PROJ, "app", "coc")
SRC_DATA = os.path.join(PROJ, "data", "coc")

CHECKS = []


def check(cid, desc, ok, evidence, counterexample):
    CHECKS.append({"id": cid, "ok": bool(ok)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {cid} {desc}")
    print(f"         证据：{evidence}")
    print(f"         反例：{counterexample}")
    return bool(ok)


#: 子进程里跑的探测脚本：打印两类路径 + 真写一次存档 + 校验 game 槽位是否一致
PROBE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from app.coc import paths, state, provenance
from app import config as cfg

cfgdata = provenance.load_tuning()
st = state.initial_states({"difficulty": "Initiate", "seed": 1}, cfgdata)
saved_path = state.save(st)

out = {
    "bundle_root": paths.ROOT,
    "config_DATA_DIR": str(cfg.DATA_DIR),
    "game_slots_dir": str(cfg.DATA_DIR / "slots"),
    "coc_slots_dir": paths.slots_dir(),
    "coc_saves_dir": paths.saves_dir(),
    "coc_toplevel_SAVES": paths.SAVES_DIR,
    "coc_toplevel_SLOTS": paths.SLOTS_DIR,
    "bundled_content_dir": paths.DATA_DIR,
    "tuning_path": paths.TUNING_PATH,
    "tuning_exists": os.path.isfile(paths.TUNING_PATH),
    "saved_at": saved_path,
    "save_inside_bundle": os.path.abspath(saved_path).startswith(
        os.path.abspath(paths.ROOT) + os.sep),
    "classify": paths.classify(),
}
print("PROBE_JSON:" + json.dumps(out, ensure_ascii=False))
"""


def run_probe(sys_path_root: str, tsf_data_dir: str = "") -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if tsf_data_dir:
        env["TSF_DATA_DIR"] = tsf_data_dir
    else:
        env.pop("TSF_DATA_DIR", None)
    p = subprocess.run([sys.executable, "-B", "-c", PROBE, sys_path_root],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=180,
                       creationflags=0x08000000)   # CREATE_NO_WINDOW（Start-Process 在本机抛异常）
    for line in (p.stdout or "").splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line[len("PROBE_JSON:"):])
    raise RuntimeError(f"子进程未产出结果。stdout={p.stdout!r} stderr={p.stderr[-800:]!r}")


def _build_internal(internal: str) -> None:
    """按 spec 的真实形态搭出 `_internal/app/**`。

    ⚠️ 必须把**整个 `app/` 包**放进去，不能只放 `app/coc/`：
    修复后 `paths.py` 会 `from .. import config`，只复制 coc 目录会
    `ImportError: cannot import name 'config' from 'app'`。
    （这一点本身说明：先前那版"只复制 app/coc"的冻结模拟是**不完整**的 ——
    它恰好绕过了 `paths → config` 这条依赖，因而对本次缺陷**假通过**。）
    """
    app_dst = os.path.join(internal, "app")
    os.makedirs(app_dst, exist_ok=True)
    app_src = os.path.join(PROJ, "app")
    for fn in sorted(os.listdir(app_src)):
        src = os.path.join(app_src, fn)
        dst = os.path.join(app_dst, fn)
        if os.path.isdir(src):
            if fn == "coc":
                shutil.copytree(src, dst,
                                ignore=shutil.ignore_patterns("__pycache__", "tests"))
            continue                      # 其它子目录（如 __pycache__）不需要
        if fn.endswith(".py"):
            shutil.copy2(src, dst)
    shutil.copytree(SRC_DATA, os.path.join(internal, "data", "coc"),
                    ignore=shutil.ignore_patterns("saves", "slots", "__pycache__"))
    # `app/config.py:93` 在 import 期就要读 DEFAULTS（= config.example.json），
    # 不复制它会在 import 阶段就 FileNotFoundError —— 真实 spec 会带上它。
    ex = os.path.join(PROJ, "config.example.json")
    if os.path.isfile(ex):
        shutil.copy2(ex, os.path.join(internal, "config.example.json"))


def main() -> int:
    print("=" * 78)
    print("CoC 路径分类与两种形态实测（t51 缺陷修复）")
    print("=" * 78)

    # ---------------- ① 非冻结 ----------------
    # ⚠️ 这一段**必须**也带 TSF_DATA_DIR 隔离：它内含 `state.save()` 的真写入。
    # 第一版这里传空串（=不设），于是真写进了**玩家真实数据目录**
    # `D:\TSF_Galgame_Data\data\saves\`（由 `_find_probe_leak.py` 定位到）。
    # 注意这与被测语义**不冲突**：本阶段要验的是"写盘跟随 config.DATA_DIR"，
    # 而 TSF_DATA_DIR 正是设置 DATA_DIR 的官方方式 —— 带着它反而更贴近真实用法。
    print("\n--- ① 非冻结形态（项目根在 sys.path 上；数据根隔离到临时目录）---")
    iso = tempfile.mkdtemp(prefix="coc-nf-")
    try:
        a = run_probe(PROJ, tsf_data_dir=iso)
    finally:
        shutil.rmtree(iso, ignore_errors=True)
    check("NF-CONFIG", "非冻结：coc 玩家可写路径跟随 app.config.DATA_DIR",
          os.path.normcase(a["coc_saves_dir"]).startswith(
              os.path.normcase(a["config_DATA_DIR"]))
          and os.path.normcase(a["coc_slots_dir"]).startswith(
              os.path.normcase(a["config_DATA_DIR"])),
          f"config.DATA_DIR={a['config_DATA_DIR']}；coc SAVES_DIR={a['coc_saves_dir']}；"
          f"coc SLOTS_DIR={a['coc_slots_dir']}",
          "反例：把 SAVES_DIR/SLOTS_DIR 改回 `由 __file__ 推的 ROOT/data/...` → "
          "它们会落在 D:\\TSF_Galgame\\data\\... 而**不在** config.DATA_DIR 下 → 红。"
          "这正是修复前的真实状态")

    check("NF-SLOTS-ALIGN", "非冻结：coc 槽目录 = 既有模式的槽目录（game._slots_dir）",
          os.path.normcase(a["coc_slots_dir"]) == os.path.normcase(a["game_slots_dir"]),
          f"coc={a['coc_slots_dir']}；game={a['game_slots_dir']}（同一目录）",
          "反例：把槽写到 saves/ 而不是 slots/ → 两个目录不同 → `game.list_slots()` "
          "列不出 coc 槽 → 红。修复前 coc 槽实际写在 ROOT/data/saves/，与 game 的 "
          "DATA_DIR/slots 根本不是一处 —— 我先前注释里的「共用」在磁盘上并不成立")

    check("NF-SAVED-LOCATION", "非冻结：真写一次存档，落点在 DATA_DIR 之下、**不在包内**",
          os.path.normcase(a["saved_at"]).startswith(os.path.normcase(a["config_DATA_DIR"]))
          and not a["save_inside_bundle"],
          f"state.save() → {a['saved_at']}；inside_bundle={a['save_inside_bundle']}",
          "反例：存档落在包根之下（`save_inside_bundle=True`）→ 更新替换目录会连带丢档 → 红")

    check("NF-CONTENT-BUNDLED", "非冻结：随包只读内容仍在包内（不受 DATA_DIR 影响）",
          os.path.normcase(a["bundled_content_dir"]).startswith(
              os.path.normcase(os.path.join(PROJ, "data", "coc")))
          and a["tuning_exists"],
          f"bundled content={a['bundled_content_dir']}；tuning 存在={a['tuning_exists']}",
          "反例：把读取也改到 DATA_DIR/data/coc → 未预置内容的玩家目录会让引擎启动即抛 "
          "TuningMissingError → 红。**读取跟随 DATA_DIR 是错的**，这正是两类路径要分开的原因")

    # ---------------- ③ TSF_DATA_DIR 逃逸舱（非冻结）----------------
    print("\n--- ② TSF_DATA_DIR 逃逸舱（非冻结，子进程）---")
    esc = tempfile.mkdtemp(prefix="coc-escape-")
    try:
        b = run_probe(PROJ, tsf_data_dir=esc)
        ok = (os.path.normcase(b["config_DATA_DIR"]).startswith(os.path.normcase(esc))
              and os.path.normcase(b["saved_at"]).startswith(os.path.normcase(esc))
              and os.path.normcase(b["coc_slots_dir"]).startswith(os.path.normcase(esc)))
        check("ESCAPE-ACTIVE", "设 TSF_DATA_DIR 后：DATA_DIR / coc 存档 / coc 槽**全部**落在该目录",
              ok,
              f"TSF_DATA_DIR={esc}\n"
              f"         config.DATA_DIR={b['config_DATA_DIR']}\n"
              f"         state.save() → {b['saved_at']}\n"
              f"         coc SLOTS_DIR={b['coc_slots_dir']}",
              "反例：coc 路径若来自 `__file__` 推法，则 TSF_DATA_DIR 对它无效、存档仍落在"
              "项目/安装目录 → 三个 startswith 之一失败 → 红。**修复前隔离验收看不到 coc 写盘，"
              "正是这条**")
    finally:
        shutil.rmtree(esc, ignore_errors=True)

    # ---------------- ③ 冻结形态（onedir 等价模拟）----------------
    print("\n--- ③ 冻结形态（onedir 布局等价模拟 + 外部数据根）---")
    tmp = tempfile.mkdtemp(prefix="coc-frozen-")
    internal = os.path.join(tmp, "_internal")
    external = os.path.join(tmp, "TSF_Galgame_Data")
    try:
        _build_internal(internal)
        c = run_probe(internal, tsf_data_dir=external)

        check("FROZEN-BUNDLE-ROOT", "冻结：packaged 根解析到 _internal（随包只读数据在那）",
              os.path.normcase(c["bundle_root"]) == os.path.normcase(internal)
              and c["tuning_exists"],
              f"_internal={internal}；paths.ROOT={c['bundle_root']}；"
              f"tuning 存在={c['tuning_exists']}",
              "反例：spec 漏收 data/coc → tuning_exists=False → 红")

        check("FROZEN-NOT-IN-INSTALL", "★ 冻结：存档**不落在安装目录内**（更新不再丢档）",
              not c["save_inside_bundle"]
              and os.path.normcase(c["saved_at"]).startswith(os.path.normcase(external)),
              f"安装目录={internal}\n"
              f"         state.save() → {c['saved_at']}\n"
              f"         inside_install_dir={c['save_inside_bundle']}",
              "反例：**修复前**的实际行为 —— `paths.SAVES_DIR` 由 `__file__` 推，"
              "冻结下 = `_internal/data/saves/`，`inside_install_dir=True` → 更新替换目录"
              "连带丢档 → 红。tsf-engineer 在真实 exe 里实测到的就是这个路径")

        check("FROZEN-SLOTS-ALIGN", "冻结：coc 槽目录 = game 的 `DATA_DIR/slots`（同一目录）",
              os.path.normcase(c["coc_slots_dir"]) == os.path.normcase(c["game_slots_dir"])
              and os.path.normcase(c["coc_slots_dir"]).startswith(
                  os.path.normcase(external)),
              f"coc={c['coc_slots_dir']}；game={c['game_slots_dir']}；外部数据根={external}",
              "反例：槽写在安装目录或另一目录 → 两者不等 → 红")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [x for x in CHECKS if not x["ok"]]
    print("\n" + "=" * 78)
    print(f"合计 {len(CHECKS)} 条：PASS {len(CHECKS) - len(failed)}，FAIL {len(failed)}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

r"""重打 TSF_Galgame 可分发交付包（产物进 release/，文件名带版本号）。

用法：
    python 测试工具\pack_tsf_zip.py            # 版本号自动读 server.py 的 GAME_VERSION
    python 测试工具\pack_tsf_zip.py 1.7.52     # 手动指定版本号
产物：
    release\TSF_Galgame_v<版本>.zip   ← 上传到 GitHub Release 当附件（不能进仓库：
    内含 129MB 单文件 exe，超过 GitHub 单文件 100MB 上限）

纳入（有效文件+库）：exe/spec/server/launcher/requirements/start.bat/开发者更新游戏状态.bat/
README/config.example.json/app/*.py/static/**（含界面内置样例）/使用说明.txt
剔除（私有与测试件）：config.json（含 API Key）/data/update/档案备份/dist/build/
各级 __pycache__/TSF_Galgame.zip 与 release/（分发产物本身）/测试工具/测试样本/_test/
角色库（含个人角色卡素材，用户指定不分享）/立绘预览_新布局.png/
项目档案与开发备忘.md（含 Key）/交接上下文.md（含本机路径等拓扑）/loop_*.log/
launcher_shell.py/_updater.bat/_probe.bat/一键适配ComfyUI.bat（它依赖
测试工具\comfy_adapt.py，而该脚本硬编码了本机 D:\ 绝对路径，不适合同包分发给他人）。
校验：包内任何条目不得出现 sk-xxxx / ark-xxxx 形式的 Key，命中即中止并删除产物。
"""
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 项目根目录（本脚本在 测试工具\ 下）
TOOLS = Path(__file__).resolve().parent
RELEASE_DIR = ROOT / "release"
DOC_TPL = TOOLS / "使用说明-整合包.txt"

EXCLUDE_DIRS = {
    "data", "update", "档案备份", "dist", "build", "__pycache__",
    "测试工具", "测试样本", "_test", "角色库",   # 角色库含个人角色卡素材，不进分享包（用户指定）
    ".zcode",   # 本机工作区（技能缓存等，含 Key 字样文档），不进分享包
    "release",  # 历次分发产物，不进包
    ".git",     # 版本库（含全部历史对象），绝不能进分发包
}
EXCLUDE_FILES = {
    "config.json", "TSF_Galgame.zip", "项目档案与开发备忘.md",
    "交接上下文.md",   # 开发交接文档（含本机路径/SD 目录等拓扑），不进分享包
    "立绘预览_新布局.png", "launcher_shell.py", "_updater.bat", "_probe.bat",
    "一键适配ComfyUI.bat",   # 见文件头说明：依赖的脚本含本机绝对路径，不随包分发
    ".gitignore", ".gitattributes",   # 仓库用文件，对终端用户无意义
    "GitHub上传教程.md",   # 本机发布流程说明，不随包分发
}
EXCLUDE_PREFIXES = {"loop_stdout.log", "loop_stderr.log", "debug.log"}

KEY_RE = re.compile(rb"(?:sk|ark)-[A-Za-z0-9]{12,}")


def leak_seeds() -> tuple:
    """从 config.json 读实际在用的 Key，取其前缀做校验种子。

    种子不进源码——本文件会被提交到公开仓库，写死 Key 片段等于把密钥公开。
    读不到 config.json（例如别人克隆后没有配置）时退化为纯正则校验。
    """
    try:
        cfg = json.loads((ROOT / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ()
    seeds = set()
    for section in cfg.values():
        if not isinstance(section, dict):
            continue
        for name, value in section.items():
            # 必须是 api_key / xxx_key 这类字段名；不能用 "key" in name（会把
            # uniform_keyword_en 这种普通关键词字段当成密钥）
            if not (name.lower() == "key" or name.lower().endswith("_key")):
                continue
            if isinstance(value, str) and len(value) >= 12:
                seeds.add(value[:8].encode())
    return tuple(seeds)


def game_version() -> str:
    """读 server.py 的 GAME_VERSION，让包名与程序版本始终一致。"""
    m = re.search(r'GAME_VERSION\s*=\s*"([^"]+)"', (ROOT / "server.py").read_text("utf-8"))
    return m.group(1) if m else "0.0.0"


def collect() -> list:
    entries = []
    for p in sorted(ROOT.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(ROOT)
        parts = rel.parts
        if any(part in EXCLUDE_DIRS for part in parts):
            continue
        if rel.name in EXCLUDE_FILES:
            continue
        if rel.name in EXCLUDE_PREFIXES:
            continue
        if any(part.endswith(".tmp.zip") for part in parts):
            continue
        entries.append(rel)
    return entries


def usage_doc(version: str) -> bytes:
    """终端用户使用说明：从模板填充版本号与日期，统一 CRLF（记事本友好）。"""
    text = DOC_TPL.read_text("utf-8")
    text = text.replace("{{VERSION}}", version).replace("{{DATE}}", date.today().isoformat())
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else game_version()
    entries = collect()

    # 硬校验：版本库、密钥文件、本机路径等一旦混入就没法补救，直接中止
    fatal = [str(r) for r in entries
             if any(p in (".git", ".ssh") for p in r.parts) or r.name == "config.json"]
    if fatal:
        raise SystemExit(f"打包内容含禁止项，已中止：{fatal}")

    RELEASE_DIR.mkdir(exist_ok=True)
    out = RELEASE_DIR / f"TSF_Galgame_v{version}.zip"
    tmp = out.with_suffix(".tmp.zip")

    has_doc = DOC_TPL.is_file()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in entries:
            z.write(ROOT / rel, rel.as_posix())
        if has_doc:
            z.writestr("使用说明.txt", usage_doc(version))
        else:
            print("！未找到", DOC_TPL, "，本次不含使用说明")

    seeds = leak_seeds()
    leak = []
    with zipfile.ZipFile(tmp) as z:
        for info in z.infolist():
            data = z.read(info.filename)
            if any(seed in data for seed in seeds) or KEY_RE.search(data):
                leak.append(info.filename)

    if leak:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"泄漏校验未通过，已中止：{leak}")

    tmp.replace(out)
    total = len(entries) + (1 if has_doc else 0)
    print(f"OK {out}")
    print(f"   {total} 条 / {out.stat().st_size / 1024 / 1024:.1f} MB / 版本 v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

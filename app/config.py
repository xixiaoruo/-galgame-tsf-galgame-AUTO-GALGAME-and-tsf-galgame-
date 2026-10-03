"""配置读写与校验。config.json 不存在时自动从模板生成。"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# PyInstaller 冻结（单文件 exe）时：BUNDLE 为包内只读解压目录，
# ROOT 为 exe 所在目录（写入区：config / data 都落在 exe 旁边，随带走随用）。
_BUNDLE = getattr(sys, "_MEIPASS", None)
if _BUNDLE:
    BUNDLE = Path(_BUNDLE)
    ROOT = Path(sys.executable).resolve().parent
else:
    BUNDLE = None
    ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = (BUNDLE if BUNDLE else ROOT) / "config.example.json"
# 数据目录：固定在 D 盘（D:\TSF_Galgame_Data），与 exe 放在哪无关——
# 档案/存档/缓存永远不会因为移动、替换、打包 exe 而丢失；
# D 盘不存在时自动回退到用户数据目录，旧位置的数据启动时自动迁移。
def _default_data_root() -> Path:
    # `TSF_DATA_DIR` 覆盖（可选）：窗口化宿主 / 隔离验收要在**不碰玩家真实数据**
    # 的前提下起一份自己的数据根（沙箱化的子进程也只能写工作区内的路径）。
    # 未设置时行为与从前逐字一致。
    _override = os.environ.get("TSF_DATA_DIR", "").strip()
    if _override:
        try:
            return Path(_override)
        except (OSError, ValueError):
            pass
    try:
        if os.path.isdir("D:/") or Path("D:/TSF_Galgame_Data").is_dir():
            return Path("D:/TSF_Galgame_Data")
    except OSError:
        pass
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TSF_Galgame"
    return Path.home() / ".tsf_galgame"


_DATA_ROOT = _default_data_root()
DATA_DIR = _DATA_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
SAVES_DIR = DATA_DIR / "saves"
CG_DIR = DATA_DIR / "cg"
CG_META_PATH = CG_DIR / "meta.json"


def _copy_tree(src: Path, dst: Path, name: str, moved: list[str]) -> None:
    if src.is_dir() and name in ("storylines", "storylines_trash", "slots",
                                 "saves", "cache", "cg"):
        dst.mkdir(parents=True, exist_ok=True)
        for f in os.listdir(src):
            sf, df = src / f, dst / f
            if sf.is_file() and not df.exists():
                try:
                    shutil.copy2(sf, df)
                    moved.append(f"{name}/{f}")
                except OSError:
                    continue
    elif src.is_file() and name in ("plugins.json", "injects.json", "debug.log"):
        if not dst.exists():
            try:
                shutil.copy2(src, dst)
                moved.append(name)
            except OSError:
                pass


def migrate_legacy_data() -> dict:
    """把历史位置的数据全部迁移到当前固定数据目录（只复制，不删除源）。

    历史位置包括：exe 目录 data/、旧版用户数据目录（LOCALAPPDATA 下的 TSF_Galgame）。
    目标已有同名文件时不覆盖（保留新数据）。
    """
    if not os.path.isdir("D:/"):   # 回退目标（非 D 盘机器）无需迁移
        return {}
    moved = []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for legacy in (ROOT / "data",
                   Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                   / "TSF_Galgame" / "data"):
        if legacy == DATA_DIR or not legacy.is_dir():
            continue
        for name in os.listdir(legacy):
            _copy_tree(legacy / name, DATA_DIR / name, name, moved)
    return {"migrated": moved}

DEFAULTS = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _clean_base_url(url: str | None) -> str | None:
    """去掉 URL 中的 query/fragment（如用户粘贴了 ?__theme=light）。"""
    if not url:
        return url
    return re.split(r"[?#]", str(url).strip())[0].rstrip("/")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        shutil.copy(EXAMPLE_PATH, CONFIG_PATH)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        user = {}
    cfg = _deep_merge(DEFAULTS, user)
    # 清洗服务地址：合并保存的旧值里可能带有查询串，会拼坏 API 路径
    for sec in ("llm", "image"):
        if sec in cfg and "base_url" in cfg[sec]:
            cfg[sec]["base_url"] = _clean_base_url(cfg[sec].get("base_url"))
    return cfg


def atomic_write_json(path, obj, *, indent: int | None = None) -> None:
    """原子写 JSON（B-04）：同目录临时文件 → flush + fsync → os.replace 覆盖。

    直接覆盖写目标文件时，若进程在写入中途被强杀（旧版关机是
    `threading.Timer(0.6, os._exit)`）或断电，会留下半个 JSON：
    存档被 load_all 静默跳过（整局从列表消失）、config.json 静默回退默认值
    （API Key 与全部设置丢失）。同一卷上 os.replace 是原子操作，
    因此目标文件要么是旧的完整内容、要么是新的完整内容，绝不出现截断态。

    临时文件与目标同目录（保证同卷）、文件名以 .tmp 结尾（不会被 *.json 扫到）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())   # 先落盘数据，再原子换名
        # Windows：目标文件被另一个线程/进程短暂打开时，os.replace 会抛
        # PermissionError（共享冲突）。读取方通常毫秒级就释放，重试几次即可；
        # 重试仍失败才抛出，由调用方按既有策略处理（不吞错）。
        delay = 0.02
        for _attempt in range(8):
            try:
                os.replace(tmp, p)
                break
            except PermissionError as e:
                last_error = e
                time.sleep(delay)
                delay *= 1.6
        else:
            raise last_error
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_config(cfg: dict) -> None:
    atomic_write_json(CONFIG_PATH, cfg, indent=2)


def masked(cfg: dict) -> dict:
    """返回给前端的安全视图：key 打码。"""
    safe = json.loads(json.dumps(cfg))

    def mask(key: str) -> str:
        if not key or len(key) <= 8:
            return "*" * len(key) if key else ""
        return key[:4] + "*" * 8 + key[-4:]

    for section in ("llm", "image"):
        if section in safe and "api_key" in safe[section]:
            safe[section]["api_key"] = mask(safe[section]["api_key"])
    return safe


def is_llm_configured(cfg: dict) -> bool:
    key = (cfg.get("llm") or {}).get("api_key", "")
    return bool(key) and "填入" not in key


def is_image_configured(cfg: dict) -> bool:
    img = cfg.get("image") or {}
    if (img.get("provider") or "openai").lower() == "sdwebui":
        # 本地 SD WebUI 只需地址，无需 Key
        return bool(img.get("base_url"))
    key = img.get("api_key", "")
    return bool(key) and "填入" not in key

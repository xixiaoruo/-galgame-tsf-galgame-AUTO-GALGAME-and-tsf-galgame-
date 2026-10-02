"""路径与标识符安全原语（单一连接器）——B-01 路径穿越封堵。

设计意图：**结构性正确**，而不是「挡住审计报告里那一个字符串」。

1. 拒绝优先于清洗：任何要拼进文件系统的「段」都先过 `safe_part()`
   —— 逐段拒绝（空串 / 父目录 `..` / 分隔符 `/` `\\` / 绝对路径 / Windows
   驱动器号 / UNC / 冒号（ADS）/ 控制字符 / 尾随空格与点 / Windows 设备保留名）：
   * `safe_id()` 再叠**严格 ASCII 正字符集**白名单 `^[A-Za-z0-9_-]{1,32}$`（id 用）；
   * `safe_name()` 允许汉字与中文标点（既有数据里就有「，」「·」「——」），
     但拒绝 Windows 非法字符与 NFKC 归一后变成 `/` `\\` `.` 的同形字符。
   **不做任何 Unicode/URL 规整后再放行**：不规整就不会把 `／`(U+FF0F)、`％`、
   `‥`(U+2025) 之类的同形字符「变成」分隔符或点号（归一后的字符串绝不用于拼路径）。
2. 拼接后必须 `resolve()`（跟随符号链接与 junction）并断言仍在 root 之内，
   越界即 `ValueError` —— 这是兜底断言，不是唯一防线。
3. **调用方只使用 `safe_join` / `safe_relpath` 返回值（已 resolve 的绝对路径）
   做 I/O**，绝不用拼接前的路径。
4. 只抛 `ValueError`（由端点翻译成 400/403），错误信息里**不放绝对路径**，
   避免把服务器目录结构回显给调用方，也不把内部路径写进日志/响应。
5. 仅依赖标准库（不 import app.config），可在任何层安全导入。

用法：
    sid = safe_id(payload.id, what="剧本 id")          # 请求里的 id
    p = safe_join(cfgmod.DATA_DIR / "storylines", f"{sid}.json", what="剧本文件")
    p.read_text(encoding="utf-8")                      # 只用返回值做 I/O
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PureWindowsPath

#: id 长度上限（存档 id / 剧本 id / 插件 id / CG id / 会话 id）
MAX_ID_LEN = 32
#: 单段文件名/目录名长度上限（远小于 Windows 单段 255 上限，留足余量）
MAX_NAME_LEN = 200

#: id 白名单：字母、数字、下划线、短横（严格 ASCII，同形字符天然进不来）
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % MAX_ID_LEN)
ID_CHARS_HINT = "只允许字母、数字、下划线、短横"
#: 文件名/目录名禁止字符：Windows 非法字符（结构性禁止，不依赖正字符集，
#: 以免误杀「主角_标准学园制服，袖口干净利.png」这类既有中文标点文件名）
NAME_ILLEGAL = frozenset('*?"<>|%')
NAME_CHARS_HINT = ("不允许路径分隔符 / \\、冒号、控制字符、Windows 非法字符 "
                   '(* ? " < > |)、百分号，以及「..」与尾随空格/点')
#: 同形字符归一后命中这些字符即拒绝（`／` U+FF0F、`．` U+FF0E、`‥` U+2025、`…` U+2026、
#: `％` U+FF05、`：` U+FF1A 等）。只看非 ASCII 字符，ASCII 本身由 `_reject_unsafe`
#: 与 `NAME_ILLEGAL` 覆盖；命中即拒绝，绝不把归一结果用于拼路径。
_CONFUSABLE_TARGETS = ("/", "\\", ".", ":", "%", "*", "?", '"', "<", ">", "|")
#: NFKC 不会归一、但外形就是斜杠/点号的字符（Windows 视其为普通字符，仍一律拒绝）
_LOOKALIKE_CHARS = frozenset(
    "\u2044\u2215\u29f8\u29f5\ufe68\u2571\u2572\u2573"   # ⁄ ∕ ⧸ ⧵ ﹨ ╱ ╲ ╳
    "\u2024\u2025\u2026\uff0e\ufe52\uff61"               # ․ ‥ … ． ﹒ ｡
    "\uff1a\ufe55\u2236\ua789"                            # ： ﹕ ∶ ꞉
    "\uff05\ufe6a"                                        # ％ ﹪
    "\uff0f\uff3c\uff1c\uff1e\uff5c\uff1f\uff0a\uff02"    # ／ ＼ ＜ ＞ ｜ ？ ＊ ＂
)

#: Windows 设备保留名（带扩展名同样是设备，例如 CON.json）
_RESERVED: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _text(value, what: str) -> str:
    """取字符串；None/非字符串一律视为非法（不做 str() 隐式转换，避免误放行）。"""
    if value is None:
        raise ValueError(f"{what}不能为空")
    if not isinstance(value, str):
        raise ValueError(f"{what}类型不合法（应为字符串）")
    return value


def _reject_unsafe(raw: str, what: str, max_len: int) -> None:
    """与字符集无关的逐段拒绝（长度 → 控制字符 → 分隔符 → 结构 → 设备名）。"""
    if raw == "":
        raise ValueError(f"{what}不能为空")
    if len(raw) > max_len:
        raise ValueError(f"{what}过长（最多 {max_len} 个字符）")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise ValueError(f"{what}含控制字符")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"{what}含路径分隔符")
    if ":" in raw:
        raise ValueError(f"{what}含冒号（驱动器号 / NTFS 备用数据流不允许）")
    if ".." in raw:
        raise ValueError(f"{what}含「..」父目录成分")
    if raw.strip(".") == "":
        raise ValueError(f"{what}不能只有点号")
    # Windows 会吃掉尾随空格与点（`.. ` / `x.` 形态会被系统归一成 `..` / `x`）
    if raw != raw.rstrip(" ."):
        raise ValueError(f"{what}不允许尾随空格或点")
    # 绝对路径 / 驱动器号 / UNC：分隔符与冒号已拒，这里再显式判一次（防将来放宽字符集）
    try:
        win = PureWindowsPath(raw)
        posix = Path(raw)
    except (TypeError, ValueError) as e:  # pragma: no cover - 极端输入
        raise ValueError(f"{what}无法解析") from e
    if win.is_absolute() or win.drive or win.root or posix.is_absolute():
        raise ValueError(f"{what}不允许绝对路径 / 驱动器号 / UNC")
    stem = raw.split(".", 1)[0].upper()
    if stem in _RESERVED:
        raise ValueError(f"{what}命中 Windows 设备保留名")


def _reject_name_chars(raw: str, what: str) -> None:
    """文件名/目录名专有的字符级拒绝（结构性，不依赖正字符集白名单）。

    允许汉字与中文标点（既有数据里有「，」「·」「——」等），只禁止：Windows 非法
    字符、百分号（URL 编码残留）、以及 NFKC 归一后会变成 `/` `\\` `.` 的同形字符
    （`／` U+FF0F、`．` U+FF0E、`‥` U+2025、`…` U+2026 等）。**只判定不改写**：
    归一后的字符串绝不用于拼路径，避免「归一造出 ..」这类反向漏洞。
    """
    for ch in raw:
        if ch in NAME_ILLEGAL:
            raise ValueError(f"{what}含非法字符（{NAME_CHARS_HINT}）")
        if ch in _LOOKALIKE_CHARS:
            raise ValueError(f"{what}含同形字符（分隔符/点号外观）")
        if ord(ch) < 0x80:
            continue
        if any(t in unicodedata.normalize("NFKC", ch) for t in _CONFUSABLE_TARGETS):
            raise ValueError(f"{what}含同形字符（归一后是分隔符/点号）")


def safe_part(value, *, what: str = "名称", pattern: re.Pattern | None = None,
              max_len: int = MAX_NAME_LEN,
              chars_hint: str = NAME_CHARS_HINT) -> str:
    """校验单个路径段（目录名/文件名/id），返回校验后的**原样**字符串。

    结构性拒绝一律 `ValueError`；`pattern` 给定时再做正字符集白名单；
    返回值为原字符串（**不做任何规整/替换/归一**）。
    """
    raw = _text(value, what)
    _reject_unsafe(raw, what, max_len)
    _reject_name_chars(raw, what)
    if pattern is not None and not pattern.fullmatch(raw):
        raise ValueError(f"{what}含非法字符（{chars_hint}）")
    return raw


def safe_id(value, *, what: str = "id", pattern: re.Pattern | None = ID_RE,
            max_len: int = MAX_ID_LEN) -> str:
    """id 白名单：默认 `^[A-Za-z0-9_-]{1,32}$`（可传 pattern 覆盖字符集）。

    即使调用方传了宽松 pattern，`_reject_unsafe` / `_reject_name_chars` 仍会拒绝
    分隔符/`..`/驱动器号/设备名/同形字符/尾随点等危险形态，因此
    `safe_id(...) + ".json"` 拼路径是结构性安全的。
    """
    return safe_part(value, what=what, pattern=pattern, max_len=max_len,
                     chars_hint=ID_CHARS_HINT)


def safe_name(value, *, what: str = "文件名", pattern: re.Pattern | None = None,
              max_len: int = MAX_NAME_LEN) -> str:
    """文件/目录名单段校验：允许汉字与中文标点，拒绝一切分隔符与越界形态。"""
    return safe_part(value, what=what, pattern=pattern, max_len=max_len,
                     chars_hint=NAME_CHARS_HINT)


def _resolve(path) -> Path:
    """resolve（跟随符号链接/junction）失败也当作非法，绝不静默放行。"""
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, ValueError) as e:
        raise ValueError("路径无法解析") from e


def is_within(root, path) -> bool:
    """`path` 是否**严格位于** `root` 之内（先 resolve；Windows 大小写不敏感）。"""
    try:
        base = _resolve(root)
        target = _resolve(path)
    except ValueError:
        return False
    if target == base:
        return False
    if target.is_relative_to(base):
        return True
    # 大小写/盘符写法差异兜底：按路径组件比较（不做字符串前缀判断）
    b = [os.path.normcase(p) for p in base.parts]
    t = [os.path.normcase(p) for p in target.parts]
    return len(t) > len(b) and t[:len(b)] == b


def safe_join(root, *parts, what: str = "路径", max_parts: int | None = None) -> Path:
    """把 `parts` 逐段校验后安全拼到 `root` 下，返回**已 resolve 的绝对路径**。

    - 每段：拒绝空串、`..`、`/`、`\\`、绝对路径、驱动器号、UNC、冒号、控制字符、
      尾随空格/点、Windows 设备保留名，并做字符集白名单；
    - 整体：`max_parts` 限制层级；拼接后 `resolve()` 并断言仍在 `root` 之内
      （跟随符号链接/junction 之后判定），越界抛 `ValueError`；
    - 错误信息不含绝对路径。
    """
    if max_parts is not None and len(parts) > max_parts:
        raise ValueError(f"{what}层级过深（最多 {max_parts} 层）")
    if not parts:
        raise ValueError(f"{what}不能为空")
    base = _resolve(root)
    cur = base
    for part in parts:
        cur = cur / safe_name(part, what=what)
    target = _resolve(cur)
    if not is_within(base, target):
        raise ValueError(f"{what}越出允许的目录")
    return target


def safe_relpath(rel, root, *, max_parts: int = 3, what: str = "路径") -> Path:
    """校验 `a/b/c.png` 形态的相对路径后安全拼接（**保留既有多层语义**）。

    与 `safe_join` 的差别：先按 `/` 切段，显式拒绝前导/尾随分隔符与空段
    （`a//b`、`a/`），再逐段走 `safe_join`。层数上限由 `max_parts` 指定。
    """
    raw = _text(rel, what)
    if raw.startswith("/") or raw.startswith("\\"):
        raise ValueError(f"{what}不允许绝对路径")
    parts = raw.split("/")
    if len(parts) > max_parts:
        raise ValueError(f"{what}层级过深（最多 {max_parts} 层）")
    if any(p == "" for p in parts):
        raise ValueError(f"{what}含空路径段")
    return safe_join(root, *parts, what=what)

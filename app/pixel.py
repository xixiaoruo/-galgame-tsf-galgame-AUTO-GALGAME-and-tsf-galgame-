# -*- coding: utf-8 -*-
"""确定性像素画后处理（P10-A）。

纯 CPU、无模型、无随机：同一输入 + 同一参数 → 逐字节相同的输出。

管线（三段固定）::

    BOX 下采样 blocks 倍  →  MEDIANCUT 调色板量化 colors 色 (dither=NONE)
                          →  NEAREST 放大回原尺寸

设计要点（每条都有实测依据，见 D:\\dsh创造\\pixel-research\\PIXEL-ART.md）：

1. **为什么必须后处理**：纯提示词出的「像素风」没有对齐网格——512px 图里实测
   34700~45000 种颜色、平坦像素对比率仅 0.33~0.48（原生抗锯齿），而且 48px 缩图
   上的块方差 0.55~0.79 会**必然命中**项目的「高频纹理异常」判据（阈值 0.23）。
   后处理后：唯一色数压到 16、平坦比 0.94、块方差 0.13~0.16（不命中）。
2. **alpha 与 RGB 同步走 NEAREST**：只对 RGB 做量化、alpha 只做「BOX 下采样 +
   NEAREST 放大」，绝不羽化放大——否则 RGB 是硬边、alpha 是渐变，合成时出半透明
   镶边。**不二值化 alpha**：保留项目 `edge_cleanup` 刻意留下的发丝半透明语义。
3. **尺寸不整除时补边**：抠图后立绘是 bbox 裁剪的不规则尺寸（实测 301 张成品里
   **0 张**是 768×1344；divisible-by-4 者亦极少），所以先向右/下**边缘复制**补到
   blocks 的整数倍、量化放大后再**裁回原尺寸**——对外尺寸零变化，网格从左上角
   对齐；被裁掉的只有右侧/下侧 ≤ blocks-1 像素（该处实测恒为透明边距）。
4. **颜色数下限 16**：实测 `colors=8` 会把整图均值 RGB 抬高约 6%（明显泛白掉色，
   B 样本 184→194），故配置里写 8 会被抬到 16；默认 24（保脸），求强像素感用
   blocks=8。
5. **必须走线程池**：本模块是同步 CPU 代码（一次整图解码 + 逐像素循环）。调用方
   一律用 `apply_async()`（内部 `asyncio.to_thread`），不要直接同步调用——B-05
   已经因为同步阻塞事件循环吃过一次亏。
"""

from __future__ import annotations

import asyncio
import io
import logging

from PIL import Image

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BLOCKS", "DEFAULT_COLORS",
    "is_enabled", "normalize_params", "params_from_config",
    "tag", "key_parts", "pixelate", "pixelate_png", "apply_async", "apply_png_sync",
]

DEFAULT_BLOCKS = 4
DEFAULT_COLORS = 24

# 实测边界：blocks=16 时脸部开始碎掉（角色辨识度受损）；colors=8 明显掉色。
_MIN_BLOCKS, _MAX_BLOCKS = 2, 16
_MIN_COLORS, _MAX_COLORS = 16, 256
_FALLBACK_BLOCKS, _FALLBACK_COLORS = DEFAULT_BLOCKS, DEFAULT_COLORS


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_params(blocks, colors) -> tuple[int, int]:
    """把外部参数**钳制**到实测安全区间（越界不报错，只记录并夹到最近边界）。

    - blocks ∈ [2, 16]：16 已实测会碎脸，更大的值无意义；
    - colors ∈ [16, 256]：8 色实测整体泛白掉色约 6%，写 8 会被夹到 16；
    - 非数字（None/字符串/空）→ 落回默认 (4, 24)。
    """
    b, c = _as_int(blocks, 0), _as_int(colors, 0)
    if b <= 0:
        b = _FALLBACK_BLOCKS
    elif b < _MIN_BLOCKS or b > _MAX_BLOCKS:
        log.warning("pixel: blocks=%s 越界 [%d,%d] → 夹到边界",
                    blocks, _MIN_BLOCKS, _MAX_BLOCKS)
        b = min(max(b, _MIN_BLOCKS), _MAX_BLOCKS)
    if c <= 0:
        c = _FALLBACK_COLORS
    elif c < _MIN_COLORS or c > _MAX_COLORS:
        log.warning("pixel: colors=%s 越界 [%d,%d]（8 色实测掉色约 6%%）→ 夹到边界",
                    colors, _MIN_COLORS, _MAX_COLORS)
        c = min(max(c, _MIN_COLORS), _MAX_COLORS)
    return b, c


def is_enabled(image_cfg: dict | None) -> bool:
    """画风开关（默认关：配置里没有这个键 = 现行行为完全不变）。"""
    try:
        return bool((image_cfg or {}).get("pixel_art", False))
    except Exception:
        return False


def params_from_config(image_cfg: dict | None) -> tuple[int, int] | None:
    """从 config["image"] 取 (blocks, colors)；开关关掉时返回 None。"""
    if not is_enabled(image_cfg):
        return None
    cfg = image_cfg or {}
    return normalize_params(cfg.get("pixel_blocks"), cfg.get("pixel_colors"))


def tag(image_cfg: dict | None) -> str:
    """缓存标签：``""``（关）/ ``pix{blocks}-{colors}``（开，如 ``pix4-24``）。"""
    p = params_from_config(image_cfg)
    return "" if p is None else "pix%d-%d" % p


def key_parts(image_cfg: dict | None) -> tuple:
    """拼进 `asset_key()` 的缓存键片段（**开关关闭时是空元组**）。

    为什么不是「永远多一个片段」：`image._hash_key()` 是
    ``sha256("|".join(parts))``，**多一个空串也会改变 key**——那样默认关闭时
    所有历史对局的立绘 key 都会变一次，已有存档里的成品立绘会被判定「未命中」
    而全部重画。空元组 = 关闭时 key 与改动前**逐位相同**（见 PIXEL-STYLE.md
    的缓存键证明），开启后同一角色不同 blocks/colors 得到不同 key。
    """
    t = tag(image_cfg)
    return (t,) if t else ()


def _pad_edge(im: Image.Image, width: int, height: int) -> Image.Image:
    """向右/下**边缘复制**补齐到 (width, height)。

    边缘复制（而非补黑/补白）是为了不让补边像素污染 MEDIANCUT 的调色板，也避免
    跨边界的块取平均时把人物最外侧一列压暗。
    """
    w, h = im.size
    if (w, h) == (width, height):
        return im
    out = Image.new(im.mode, (width, height))
    out.paste(im, (0, 0))
    if width > w:
        out.paste(im.crop((w - 1, 0, w, h)).resize((width - w, h),
                                                   Image.Resampling.NEAREST), (w, 0))
    if height > h:
        out.paste(im.crop((0, h - 1, w, h)).resize((w, height - h),
                                                   Image.Resampling.NEAREST), (0, h))
    if width > w and height > h:
        out.paste(im.crop((w - 1, h - 1, w, h)).resize((width - w, height - h),
                                                       Image.Resampling.NEAREST),
                  (w, h))
    return out


def pixelate(img: Image.Image, blocks: int | None = None,
             colors: int | None = None) -> Image.Image:
    """确定性像素化；返回**与原图同尺寸**的 RGBA 图。

    :param img: 任意模式（内部按 RGBA 处理）
    :param blocks: 下采样倍率（像素格边长），走 `normalize_params` 钳制
    :param colors: 调色板颜色数（MEDIANCUT 自适应，逐图独立计算）
    """
    b, c = normalize_params(blocks, colors)
    src = img.convert("RGBA")
    w, h = src.size
    if w < b or h < b:
        return src
    # 补到 blocks 整数倍 → 整数倍下采样/放大（避免最后一行/列出现变宽像素）
    pw = ((w + b - 1) // b) * b
    ph = ((h + b - 1) // b) * b
    tw, th = max(1, pw // b), max(1, ph // b)

    rgb = _pad_edge(src.convert("RGB"), pw, ph)
    alpha = Image.new("L", (pw, ph), 0)
    alpha.paste(src.getchannel("A"), (0, 0))

    small_rgb = rgb.resize((tw, th), Image.Resampling.BOX)
    small_a = alpha.resize((tw, th), Image.Resampling.BOX)

    # 量化只针对 RGB：dither=NONE 才是真·硬色块（默认 dither 会引入抖动噪点）
    quant = small_rgb.quantize(colors=c, method=Image.Quantize.MEDIANCUT,
                               dither=Image.Dither.NONE)

    out = quant.convert("RGB").resize((pw, ph), Image.Resampling.NEAREST)
    out_a = small_a.resize((pw, ph), Image.Resampling.NEAREST)
    out = out.convert("RGBA")
    out.putalpha(out_a)
    # 裁回原尺寸：对外尺寸零变化；被裁的只有右/下 ≤ blocks-1 像素（实测恒透明）
    if (pw, ph) != (w, h):
        out = out.crop((0, 0, w, h))
    return out


def pixelate_png(data: bytes, blocks: int | None = None,
                 colors: int | None = None) -> bytes:
    """bytes → bytes 入口（PNG 进 PNG 出）。解码失败时**原样返回**，绝不中断生成。"""
    try:
        src = Image.open(io.BytesIO(data))
        src.load()
    except Exception as e:  # 坏图不归本模块管，交给既有质检
        log.warning("pixel: 解码失败，原样放行: %s", e)
        return data
    try:
        buf = io.BytesIO()
        pixelate(src, blocks, colors).save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.warning("pixel: 像素化失败，原样放行: %s", e)
        return data


def apply_png_sync(data: bytes, image_cfg: dict | None) -> bytes:
    """按配置决定是否像素化（同步版；调用方须在线程池里跑）。"""
    p = params_from_config(image_cfg)
    if p is None or not data:
        return data
    return pixelate_png(data, p[0], p[1])


async def apply_async(data: bytes, image_cfg: dict | None) -> bytes:
    """线程池版入口（热路径专用）：开关关闭时零开销直接返回。"""
    if not data or params_from_config(image_cfg) is None:
        return data
    return await asyncio.to_thread(apply_png_sync, data, image_cfg)

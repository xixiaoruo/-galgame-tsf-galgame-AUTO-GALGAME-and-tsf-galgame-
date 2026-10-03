"""像素画风端到端验证：真实立绘 → 开启像素化 → 测三个可测量指标。

为什么不重新出图：SD 出图要占 GPU 且慢；而像素化是**纯 CPU 后处理**，
输入一张真实立绘即可完整验证这条链 —— 这才是被测对象本身。

判据（可测量，不靠"看起来像像素画"）：
  · 尺寸不变（对外契约：像素化不改尺寸 —— t40 的"边缘复制补边 → 裁回原尺寸"）
  · 唯一色数显著下降（真像素网格的特征）
  · 平坦比上升（相邻像素相同的比例）
  · alpha 通道保持（立绘有透明区，不能丢）
"""
import io
import json
import pathlib
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\TSF_Galgame")

from PIL import Image  # noqa: E402
from app import pixel as P  # noqa: E402

CACHE = pathlib.Path(r"D:\TSF_Galgame_Data\data\cache")


def metrics(im: Image.Image) -> dict:
    rgba = im.convert("RGBA")
    w, h = rgba.size
    # 唯一色数（RGB 部分）
    colors = len(set(rgba.convert("RGB").getdata()))
    # 平坦比：相邻像素相同的比例（水平方向）
    px = rgba.convert("RGB").load()
    same = 0
    total = 0
    for y in range(0, h, 3):          # 抽样，避免大图太慢
        for x in range(1, w):
            total += 1
            if px[x, y] == px[x - 1, y]:
                same += 1
    flat = same / total if total else 0.0
    alpha_vals = set(rgba.getchannel("A").getdata())
    return {"size": (w, h), "colors": colors, "flat": round(flat, 4),
            "alpha_levels": len(alpha_vals), "has_transparent": 0 in alpha_vals}


# 找一张真实立绘（缓存里的成品）
cands = sorted([p for p in CACHE.rglob("*.png") if "_raw" not in p.name],
               key=lambda p: p.stat().st_size, reverse=True)
if not cands:
    print("[FATAL] 找不到真实立绘样本")
    sys.exit(2)
src = cands[0]
print(f"样本: {src}")
print(f"  {src.stat().st_size} B")
data = src.read_bytes()

im0 = Image.open(io.BytesIO(data))
m0 = metrics(im0)
print(f"\n原图:  size={m0['size']}  colors={m0['colors']}  flat={m0['flat']}  "
      f"alpha 层级={m0['alpha_levels']}  含透明={m0['has_transparent']}")

print()
print("=" * 74)
print("开启像素化（cfg 形式，与运行时一致）")
print("=" * 74)
results = {}
for blocks, colors in ((4, 32), (8, 24), (2, 64)):
    cfg = {"image": {"pixel_art": True, "pixel_blocks": blocks, "pixel_colors": colors}}
    icfg = cfg["image"]
    print(f"\n--- blocks={blocks} colors={colors}")
    print(f"    is_enabled      = {P.is_enabled(icfg)}")
    print(f"    params_from_cfg = {P.params_from_config(icfg)}")
    print(f"    cache key_parts = {P.key_parts(icfg)}")
    out = P.apply_png_sync(data, icfg)
    im1 = Image.open(io.BytesIO(out))
    m1 = metrics(im1)
    print(f"    产物 {len(out)} B  size={m1['size']}  colors={m1['colors']}  "
          f"flat={m1['flat']}  alpha 层级={m1['alpha_levels']}  含透明={m1['has_transparent']}")
    ok_size = m1["size"] == m0["size"]
    ok_color = m1["colors"] < m0["colors"]
    ok_flat = m1["flat"] > m0["flat"]
    ok_alpha = m1["has_transparent"] == m0["has_transparent"]
    print(f"    尺寸不变={ok_size}  色数下降={ok_color}  平坦比上升={ok_flat}  透明保持={ok_alpha}")
    results[(blocks, colors)] = dict(ok_size=ok_size, ok_color=ok_color,
                                     ok_flat=ok_flat, ok_alpha=ok_alpha, m=m1)

print()
print("=" * 74)
print("关闭时是否逐字节不变（默认关 ⇒ 不得改动任何东西）")
print("=" * 74)
for off in ({"pixel_art": False}, {}, None, {"pixel_art": True, "pixel_blocks": 0}):
    cfg = {"image": off} if off is not None else {"image": {}}
    icfg = cfg["image"]
    try:
        out = P.apply_png_sync(data, icfg)
        same = out == data
        print(f"  image_cfg={off!s:<42} enabled={P.is_enabled(icfg)!s:<5} "
              f"输出==输入: {same}")
    except Exception as exc:  # noqa: BLE001
        print(f"  image_cfg={off!s:<42} 抛 {type(exc).__name__}: {exc}")

print()
print("=" * 74)
fails = []
for k, v in results.items():
    for name in ("ok_size", "ok_color", "ok_flat", "ok_alpha"):
        if not v[name]:
            fails.append(f"blocks={k[0]} colors={k[1]}: {name} 失败")
print("VERDICT:", "PASS" if not fails else "FAIL")
for f in fails:
    print("  ✗", f)
print("=" * 74)
sys.exit(0 if not fails else 1)

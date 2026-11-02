"""1.9.21 实机验证：ComfyUI 线路的换装 / 脸部细化 / 渐变链（真实出图）。

背景：1.9.20 之前 `refine_outfit/refine_face/refine_transition` 只实现 SD
WebUI 的 img2img/inpaint，ComfyUI 线一律抛错 → 上层回退「全量重画」，
换装后脸/身材/发型全变（用户反馈的「换装系统失效」）。

本脚本用本机正在运行的 ComfyUI（默认 127.0.0.1:8188）真实跑三条链路，
并对结果做**可复核的像素断言**：
  · 换装：alpha 轮廓逐像素不变（硬核承接）、头部区域基本不变、躯干区域
    有可见变化（说明衣服真的被重画了）；
  · 脸部细化：掩膜外几乎不变、脸部区域有变化；
  · 渐变链：输出仍是合法 PNG、尺寸与原图一致、alpha 不变。

用法：python 测试工具/test_comfy_i2i_live.py [--quick]
产物：测试样本/comfy_i2i/ 下的 *.png
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(r"D:\TSF_Galgame")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.image import ImageClient, ImageGenError  # noqa: E402

SRC = ROOT / "测试样本" / "1.6.74_差分终验" / "千代_happy.png"
OUT = ROOT / "测试样本" / "comfy_i2i"

PROMPT_OUTFIT = (
    "same character, same face, same hair, same pose, wearing a black "
    "turtleneck sweater and long pleated skirt, fully dressed, outfit "
    "covering the body, detailed clothing texture, crisp fabric folds, "
    "anime visual novel illustration, clean lineart")


def _mean_diff(a: Image.Image, b: Image.Image, box=None) -> float:
    if box:
        a, b = a.crop(box), b.crop(box)
    d = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    return float(ImageStat.Stat(d.convert("L")).mean[0])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = {"image": {"provider": "comfyui",
                     "base_url": "http://127.0.0.1:8188",
                     "comfy_checkpoint": "waiIllustriousSDXL_v170.safetensors",
                     "auto_local_failover": False,
                     "comfy_max_wait": 1200,
                     "sd_steps": 24, "sd_cfg": 6.0,
                     "sd_sampler": "DPM++ 2M", "sd_scheduler": "karras"}}
    client = ImageClient(cfg)
    print("provider =", client.provider, "| mock =", client.mock)
    src = Image.open(SRC).convert("RGBA")
    print("source:", SRC.name, src.size)
    (OUT / "src.png").write_bytes(SRC.read_bytes())

    # ---------------- 1) 换装 ----------------
    print("\n[1] refine_outfit via ComfyUI ...")
    out = asyncio.run(client.refine_outfit(
        SRC.read_bytes(), PROMPT_OUTFIT, 424242, nude_mode="dressed"))
    (OUT / "outfit.png").write_bytes(out)
    im = Image.open(io.BytesIO(out)).convert("RGBA")
    assert im.size == src.size, f"换装后尺寸变了：{im.size} != {src.size}"
    alpha_same = ImageChops.difference(
        im.getchannel("A"), src.getchannel("A")).getbbox() is None
    w, h = src.size
    head_box = (0, 0, w, int(h * 0.22))
    torso_box = (int(w * 0.2), int(h * 0.35), int(w * 0.8), int(h * 0.85))
    head_diff = _mean_diff(im, src, head_box)
    torso_diff = _mean_diff(im, src, torso_box)
    # 颜色崩坏检测（1.9.21 定论：长负面里的 (x:1.35) 权重会让 ComfyUI 整幅
    # 红通道塌陷成青蓝；换装结果的人物区域必须保持中性色）。
    # 注意：合成图的背景是透明（RGB 为 0），所以只统计「不透明人物区域」。
    person = im.convert("RGB").resize((96, 96))
    pmask = im.getchannel("A").resize((96, 96))
    ppx = [p for p, a in zip(list(person.getdata()), list(pmask.getdata()))
           if a > 200]
    rgb_mean = tuple(sum(c[i] for c in ppx) / max(1, len(ppx))
                     for i in range(3)) if ppx else (0, 0, 0)
    print(f"  alpha 完全一致 = {alpha_same}")
    print(f"  mean RGB = ({rgb_mean[0]:.0f},{rgb_mean[1]:.0f},{rgb_mean[2]:.0f})")
    print(f"  头部区域平均差 = {head_diff:.2f}（应很小）")
    print(f"  躯干区域平均差 = {torso_diff:.2f}（应有明显变化）")
    assert alpha_same, "换装把 alpha 轮廓改掉了（硬核承接契约被破坏）"
    assert rgb_mean[0] > 120, "换装结果红通道塌陷（颜色崩坏）"
    assert torso_diff > 3.0, "躯干区域几乎没变化——换装没生效"
    assert head_diff < torso_diff, "头部变化比躯干还大——头部锚定失效"

    # ---------------- 2) 脸部细化 ----------------
    print("\n[2] refine_face via ComfyUI inpaint ...")
    face = asyncio.run(client.refine_face(
        out, out, "same character, same hairstyle, same outfit, "
        "beautiful detailed face, refined facial features, delicate eyes, "
        "same person", 424243, denoise=0.30))
    (OUT / "face.png").write_bytes(face)
    fim = Image.open(io.BytesIO(face)).convert("RGBA")
    assert fim.size == im.size
    face_box = (int(w * 0.25), int(h * 0.02), int(w * 0.75), int(h * 0.25))
    body_box = (int(w * 0.15), int(h * 0.45), int(w * 0.85), int(h * 0.95))
    fd = _mean_diff(fim, im, face_box)
    bd = _mean_diff(fim, im, body_box)
    print(f"  脸部区域平均差 = {fd:.2f}（应有变化）")
    print(f"  身体区域平均差 = {bd:.2f}（应几乎为 0）")
    assert fd > 1.0, "脸部没有变化——inpaint 没生效"
    assert bd < max(2.0, fd), "身体被改动过多——掩膜外泄漏"

    # ---------------- 3) TSF 渐变链 ----------------
    print("\n[3] refine_transition via ComfyUI (2 passes) ...")
    trans = asyncio.run(client.refine_transition(
        out, "same character, adult woman, feminine features, "
        "longer hair, softer jawline, same pose, same outfit, gradual change",
        424244, denoise=0.32, body_pass=True, body_denoise=0.40,
        hair_words="hair growing longer, fuller hair"))
    (OUT / "transition.png").write_bytes(trans)
    tim = Image.open(io.BytesIO(trans)).convert("RGBA")
    assert tim.size == im.size
    assert ImageChops.difference(tim.getchannel("A"),
                                 im.getchannel("A")).getbbox() is None
    print(f"  输出尺寸 {tim.size}，alpha 不变，平均变化 "
          f"{_mean_diff(tim, im):.2f}")
    print("\nALL OK ->", OUT)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ImageGenError as e:
        print("ImageGenError:", e)
        raise SystemExit(2)

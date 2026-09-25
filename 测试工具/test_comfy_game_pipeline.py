"""隔离测试剧本：验证「游戏生图客户端 × 本机 ComfyUI」全链路。

不写任何配置/存档，只连 http://127.0.0.1:8000 出图：
  A) 主角立绘：生成 → 质检 → AI 抠图（rembg）→ 成品复检
  B) 场景背景：生成 → 质检
产物：D:\\TSF_Galgame\\测试样本\\comfy_*_raw.png / *_cut.png / bg.png
"""
import asyncio
import time
import sys
from pathlib import Path

sys.path.insert(0, r"D:\TSF_Galgame")
from PIL import Image
from app.game import _final_check_issue
from app.image import (ImageClient, _bg_check, process_portrait_async)

CFG = {"provider": "comfyui", "base_url": "http://127.0.0.1:8000",
       "comfy_checkpoint": "Qpipi.com_zukiAnimeILL_v50.safetensors",
       "sd_sampler": "euler", "sd_scheduler": "normal",
       "sd_steps": 24, "sd_cfg": 5.5, "vram_mode": "mid",
       "sd_portrait_size": "512x768", "sd_background_size": "768x512",
       "uniform_keyword_en": "", "uniform_keyword_neg": "",
       "face_refine": False, "outfit_inpaint": False, "pink_rim": True}

OUT = Path(r"D:\TSF_Galgame\测试样本")


async def main() -> int:
    t0 = time.time()
    cl = ImageClient({"image": CFG})
    assert not cl.mock
    print(f"[A] 生成立绘（{CFG['sd_portrait_size']}…）")
    raw = await cl.generate_portrait(
        "凛", "黑色短发，深色眼瞳，白色衬衫与深色长裤", "neutral",
        "平静自然的表情", "日系动漫视觉小说立绘风格，精美赛璐璐上色，干净线条",
        extra_negative="", seed=20260824)
    (OUT / "comfy_portrait_raw.png").write_bytes(raw)
    iraw = Image.open(__import__("io").BytesIO(raw))
    print(f"    原始立绘 {iraw.size} {len(raw)}B，用时 {time.time()-t0:.0f}s")
    t1 = time.time()
    cut = await process_portrait_async(raw, "ai")
    (OUT / "comfy_portrait_cut.png").write_bytes(cut)
    icut = Image.open(__import__("io").BytesIO(cut))
    print(f"    抠图成品 {icut.size} {icut.mode}，质检：{_final_check_issue(cut) or '通过'}"
          f"（{time.time()-t1:.0f}s）")
    print("[B] 生成背景（768x512…）")
    bg = await cl.generate_background("黄昏的学园图书馆，落地窗，书架与暖光",
                                      "日系动漫游戏背景插画风格，无人物")
    (OUT / "comfy_bg.png").write_bytes(bg)
    ibg = Image.open(__import__("io").BytesIO(bg))
    print(f"    背景 {ibg.size}，质检：{_bg_check(bg) or '通过'}")
    print(f"=== 全链路 OK（总用时 {time.time()-t0:.0f}s）===")
    print("样本：", OUT)
    return 0


sys.exit(asyncio.run(main()))

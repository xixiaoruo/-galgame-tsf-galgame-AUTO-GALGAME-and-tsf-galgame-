"""ComfyUI(本地桌面版) 实时连通测试：
① /system_stats 健康检查与版本
② /object_info/CheckpointLoaderSimple 枚举可用模型
③ 用游戏客户端 ImageClient(provider=comfyui) 真实提交一张立绘并取回
产物：D:\\TSF_Galgame\\测试样本\\comfy_test_portrait.png
先决：ComfyUI 桌面端已在界面点「启动」（监听 127.0.0.1:8188）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, r"D:\TSF_Galgame")
import httpx
from PIL import Image
from app.image import ImageClient

BASE = "http://127.0.0.1:8000"
OUT = Path(r"D:\TSF_Galgame\测试样本")
OUT.mkdir(exist_ok=True)
CFG = {
    "provider": "comfyui", "base_url": BASE,
    "sd_steps": 20, "sd_cfg": 6.0, "sd_sampler": "euler",
    "sd_scheduler": "normal", "comfy_checkpoint": "Qpipi.com_zukiAnimeILL_v50.safetensors",
    "vram_mode": "mid", "sync_webui": True, "auto_sdxl_styles": True,
    "pink_rim": True, "uniform_keyword_en": "",
    "sd_portrait_size": "512x768", "sd_background_size": "768x512",
    "face_refine": True, "outfit_inpaint": True,
}


async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{BASE}/system_stats")
        r.raise_for_status()
        stats = r.json()
        sys_info = stats.get("system", {})
        print("① system_stats OK | ComfyUI:", sys_info.get("comfyui_version"),
              "| GPU:", (stats.get("devices") or [{}])[0].get("name"))
        r = await c.get(f"{BASE}/object_info/CheckpointLoaderSimple")
        names = (r.json().get("CheckpointLoaderSimple", {})
                 .get("input", {}).get("required", {})
                 .get("ckpt_name", [None, []])[0] or [])
        print("② 可用模型:", (names or ["（无）"])[:5])
    if not names:
        print("ComfyUI 无可用模型，请先在 ComfyUI 界面放入模型；测试中止")
        return

    client = ImageClient({"image": CFG})
    assert not client.mock
    print("③ 使用游戏客户端真实生成一张立绘（请稍候…）")
    png = await client.generate_portrait(
        "凛", "黑色短发，深色眼瞳，常服", "neutral", "平静自然的表情",
        "日系动漫视觉小说立绘风格，精美赛璐璐上色，干净线条，高分辨率",
        extra_negative="", seed=1001, strict=True)
    path = OUT / "comfy_test_portrait.png"
    path.write_bytes(png)
    im = Image.open(path)
    print("④ 生成成功:", im.size, im.mode, len(png), "bytes →", path)


asyncio.run(main())

"""真实素材出图测试：用学院局现有立绘 + 实 SD WebUI 验证
① 脸部 img2img 细化（表情清晰不糊） ② 换装限定区域图生图（脸不变）。

产出（供目视查看）：D:\\TSF_Galgame\\测试样本\\face_before.png / face_after.png /
outfit_before.png / outfit_after.png。
"""
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, r"D:\TSF_Galgame")
from PIL import Image
from app.image import ImageClient

OUT = Path(r"D:\TSF_Galgame\测试样本")
OUT.mkdir(exist_ok=True)

CACHE = Path(r"D:\TSF_Galgame_Data\data\cache")
SESS_DIRS = [d for d in CACHE.iterdir() if d.is_dir() and "学院" in d.name]
print("可用对局目录：", [d.name for d in SESS_DIRS])


def pick_portrait():
    """选一张人物占画面足够大、有完整头肩的主立绘作为测试素材。"""
    for d in SESS_DIRS:
        pd = d / "portraits"
        if not pd.is_dir():
            continue
        for p in sorted(pd.glob("*.png")):
            if p.stat().st_size < 200000:
                continue
            try:
                im = Image.open(p)
                bw_ratio = im.width / max(1, im.height)
            except Exception:
                continue
            # 全身/大半身竖图更接近真实立绘
            if 0.2 < bw_ratio < 0.8 and im.width >= 500:
                return p, im.size
    return None, None


async def main():
    p, size = pick_portrait()
    if not p:
        print("未找到合适素材，中止")
        return
    print("测试素材：", p, "size", size)
    data = p.read_bytes()
    (OUT / "face_before.png").write_bytes(data)
    (OUT / "outfit_before.png").write_bytes(data)

    cl = ImageClient({"image": {
        "provider": "sdwebui", "base_url": "http://127.0.0.1:7860",
        "sd_steps": 20, "sd_cfg": 6, "sd_sampler": "DPM++ 2M",
        "pink_rim": True, "sync_webui": True, "auto_sdxl_styles": True,
    }})

    # ① 脸部细化：denoise 0.28，参照图用自身
    face = await cl.refine_face(
        data, data,
        "anime girl school uniform, identical character, same person", 777)
    (OUT / "face_after.png").write_bytes(face)
    print("face refine:", len(face), "bytes")

    # ② 换装限定区域重绘：掩膜服装区，提示词换装
    outfit = await cl.refine_outfit(
        data,
        "anime girl, same character, same standing pose, wearing school uniform: "
        "white blouse, dark navy blazer jacket, pleated skirt, ribbon tie, "
        "knee-high socks, detailed clothing texture, crisp fabric folds", 888)
    (OUT / "outfit_after.png").write_bytes(outfit)
    print("outfit inpaint:", len(outfit), "bytes")

    # 简单数值校验：原图/结果尺寸一致、脸区方差（清晰度代理指标）不塌
    before = Image.open(io.BytesIO(data))
    fa = Image.open(io.BytesIO(face)).convert("RGB")
    oa = Image.open(io.BytesIO(outfit)).convert("RGB")
    assert fa.size == before.size and oa.size == before.size, "尺寸不一致"
    from PIL import ImageStat
    px = fa.load()
    w, h = fa.size
    face = fa.crop((int(w * 0.38), int(h * 0.05), int(w * 0.62), int(h * 0.20)))
    print("face sharpness(std):", ImageStat.Stat(face).stddev or [0])
    print("DONE")


asyncio.run(main())

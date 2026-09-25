# -*- coding: utf-8 -*-
"""1.7.32 转变技能内建·六段实机出图验证：
复刻 game._start_protagonist_portrait「阶段跃迁=全量生成」路径的提示词组装
（tsf.stage_portrait_prompt 段特征 + 段负面 + TRANSFORM_IMPROVE 词），
带转变判定器（BLIP 打标 + 段词集打分 + 同人色差），六段逐段各出一张。

产出：D:\\TSF_Galgame\\测试样本\\1.7.32_六段\\s{0..5}.png + info.json
（每段 prompt / negative / caption / 判定分 / 尺寸 / 耗时）
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\TSF_Galgame")
from app.image import ImageClient, tsf_stage_score
from app import tsf
from app import game as gmod

OUT = Path(r"D:\TSF_Galgame\测试样本\1.7.32_六段")
OUT.mkdir(parents=True, exist_ok=True)
CFG = json.load(open(r"D:\TSF_Galgame\config.json", encoding="utf-8"))

ANCHOR = "成年男性，微翘黑色短发，黑色眼眸，白色T恤黑色长裤"
STYLE = ("日系动漫视觉小说立绘风格，精美赛璐璐上色，干净线条，"
         "全身像，面向镜头，浅灰色纯色背景，高质量")
PROGRESS = [10, 30, 50, 70, 90, 100]  # 六段段内代表进度


async def main():
    client = ImageClient(CFG)
    stages = tsf.build_stages(6)
    session = {"stages": stages, "tsf_target": {}}
    info = []
    # 支持只跑指定段：python test_stage_tsf_132.py 4 5 (段序号 0-5)
    argv = [a for a in sys.argv[1:] if a.isdigit()]
    wanted = [int(a) for a in argv] if argv else list(range(len(PROGRESS)))
    for i in wanted:
        if not (0 <= i < len(PROGRESS)):
            continue
        p = PROGRESS[i]
        si = tsf.stage_of(p, stages)
        feat = tsf.stage_feat(si, stages)
        prompt = (tsf.stage_portrait_prompt(ANCHOR, p, "", stages)
                  + "，" + gmod.TRANSFORM_IMPROVE_EN
                  # 与 game._start_protagonist_portrait 同步：发色锁
                  + "，(same hair color:1.2), (keep original hair color:1.15), "
                    "same hair color as before")
        neg = (tsf.stage_negative_relaxed_stage(si, stages)
               + "，" + gmod.TRANSFORM_IMPROVE_NEG
               + "，" + gmod.PROT_AGE_NEG + "，no camera, no phone, no lens")
        # 1.7.32 判定器全链路：每段 3 候选变体 + A1111 打标挑「转变正确、
        # 着装正确（nude_ok=False 裸体大罚）+ 发色统一 + 同人」的最优种子；
        # prev=上一段成品（发区色差罚分参照）
        prev_b = None
        if i > 0 and (OUT / f"s{i - 1}.png").is_file():
            prev_b = (OUT / f"s{i - 1}.png").read_bytes()
        matcher = gmod._tsf_stage_matcher(client, session, "主角", si, prev_b)
        t0 = time.time()
        seed = gmod._stable_seed(f"prot|主角|{si}")
        print(f"[{i}] 段{si + 1} {stages[si]['name']} p={p} seed={seed} ...")
        try:
            data = await client.generate_portrait(
                "主角", prompt, f"阶段{si + 1}",
                f"主角的阶段{si + 1}立绘", STYLE,
                extra_negative=neg, seed=seed, strict=True,
                matcher=matcher, identity_lock=False)
            (OUT / f"s{i}.png").write_bytes(data)
            cap = await client._interrogate(
                __import__("base64").b64encode(data).decode(), model="clip")
            score = tsf_stage_score(cap, feat)
            info.append({
                "stage": si, "name": stages[si]["name"], "progress": p,
                "file": f"s{i}.png", "caption": cap, "score": score,
                "core": feat["core"], "forbidden": feat["forbidden"],
                "seconds": round(time.time() - t0, 1),
            })
            print(f"    OK {data and len(data) / 1024:.0f}KB "
                  f"{time.time() - t0:.0f}s caption={cap[:90]!r} score={score}")
        except Exception as e:
            print(f"    FAIL {type(e).__name__}: {e}")
            info.append({"stage": si, "name": stages[si]["name"],
                         "progress": p, "error": str(e)})
    (OUT / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print("done ->", OUT)


if __name__ == "__main__":
    asyncio.run(main())

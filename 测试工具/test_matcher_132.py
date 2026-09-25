# -*- coding: utf-8 -*-
"""1.7.32 转变判定器校准：对六段实机成品（s0..s5.png）跑 A1111 interrogate
打标（BLIP+CLIP 链路），输出**交叉打分矩阵**——每张图对每个段语义层级的
判定分。预期：对角高分、非对角（段错位）低分/负分 = 判定器具备挑图能力。

产出：D:\\TSF_Galgame\\测试样本\\1.7.32_六段\\matcher_matrix.txt / info.json
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\TSF_Galgame")
from app.image import ImageClient, tsf_stage_score
from app import tsf

OUT = Path(r"D:\TSF_Galgame\测试样本\1.7.32_六段")
CFG = json.load(open(r"D:\TSF_Galgame\config.json", encoding="utf-8"))


async def main():
    client = ImageClient(CFG)
    caps = {}
    matrix = []
    for i in range(6):
        p = OUT / f"s{i}.png"
        if not p.is_file():
            print(f"s{i}.png 不存在，跳过")
            continue
        b64 = base64.b64encode(p.read_bytes()).decode()
        t0 = time.time()
        cap = await client._interrogate(b64, model="clip")
        caps[i] = cap
        row = {"pic": i, "caption": cap,
               "seconds": round(time.time() - t0, 1)}
        scores = {}
        for tier in range(6):
            feat = tsf.STAGE_FEAT_TIERS[tier]
            s = tsf_stage_score(cap, feat)
            scores[tier] = s
        row["scores"] = scores
        matrix.append(row)
        print(f"s{i}  {time.time() - t0:5.1f}s  {cap[:110]!r}")
        print("     scores:", scores)
    (OUT / "matcher_matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    print("done")


if __name__ == "__main__":
    asyncio.run(main())

# -*- coding: utf-8 -*-
"""1.9.21 实机：用真实 DeepSeek 跑一次一致性检查（合成测试数据，不碰玩家存档）。

只读：不写任何会话文件；使用临时会话 dict。会给 DeepSeek 发一次请求
（约 1~3k tokens），用于验证：请求可用、JSON 可解析、问题清单结构正确。
"""
from __future__ import annotations

import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import config as cfgmod  # noqa: E402
from app import consistency as CM  # noqa: E402


def session() -> dict:
    return {
        "sid": "live1921", "title": "镜中人",
        "world": "现代都市，旧校舍的镜子会映出另一个自己；被映照者会逐渐变成镜中人的模样。",
        "outline": "◆ 初遇：主角在旧校舍遇见镜中的少女。\n◆ 渐变：身体开始改变。",
        "protagonist": {
            "name": "阿明", "anchor": "黑色短发，琥珀色眼瞳，身高 175cm",
            "identity": "开始女性化的高中生",
            "stats": {"progress": 35, "physique": 30, "immersion": 20},
        },
        "characters": [{
            "name": "小雪", "role": "同班同学",
            "personality": "外冷内热，说话简洁",
            "appearance": "银白长发，冰蓝眼瞳，水手服",
            "age": 17, "height_cm": 162,
            "locked_fields": ["height_cm", "appearance"],
        }],
        "ai_directive": "", "writing_style": "default", "custom_prompt": "",
        "log": [
            {"turn": {"scene": "旧校舍走廊", "dialogue": [
                {"character": "旁白", "text": "夕阳把走廊染成橘红，镜面泛着冷光。"},
                {"character": "小雪", "text": "你也看见了吧，镜子里那个和你一模一样的人。"}],
                "outfit": {}}, "choice": {"text": "走近镜子"}},
            {"turn": {"scene": "旧校舍走廊", "dialogue": [
                {"character": "旁白", "text": "镜中的少女抬手，和你做了同一个动作。"},
                {"character": "小雪", "text": "她比昨天更像你了。"},
                {"character": "小雪", "text": "她比昨天更像你了。"}],
                "outfit": {"小雪": "水手服"}}, "choice": None},
        ],
        "consistency": None, "consistency_auto": 0,
    }


async def main() -> int:
    cfg = cfgmod.load_config()
    print("LLM:", cfg["llm"].get("base_url"), "|", cfg["llm"].get("model"))
    s = session()
    try:
        rep = await CM.check(s, mode="recent", turns=12)
    except CM.ConsistencyError as e:
        print("FAIL:", e)
        return 1
    print("score:", rep["score"], "| model:", rep["model"],
          "| elapsed:", rep["elapsed"], "s")
    print("summary:", rep["summary"])
    print("counts:", rep["counts"])
    for it in rep["issues"]:
        print(f"  - [{it['severity']}/{it['category']}] turn={it['turn']} "
              f"{it['problem']}\n      原文：{it['evidence']}\n      修复：{it['fix']}")
    ok = (isinstance(rep["score"], int) and 0 <= rep["score"] <= 100
          and isinstance(rep["issues"], list)
          and all(i["category"] in CM.CATEGORIES for i in rep["issues"]))
    print("结构校验:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

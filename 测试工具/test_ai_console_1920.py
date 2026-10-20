# -*- coding: utf-8 -*-
"""1.9.20 AI 控制台回归：请求留痕 / 原始输出 / 修改对话 / 导演指令。

不联网：临时数据根 + 空 config → LLM 走 mock。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

# 强制隔离：绝不因为外部已设置 TSF_DATA_DIR 而写进真实配置目录
os.environ["TSF_DATA_DIR"] = tempfile.mkdtemp(prefix="ai1920-test-")
os.makedirs(os.environ["TSF_DATA_DIR"], exist_ok=True)
with open(os.path.join(os.environ["TSF_DATA_DIR"], "config.json"), "w",
          encoding="utf-8") as _fh:
    _fh.write("{}")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import ai_console as AC
from app import game
from app.coc import prompt as COCP
from app.coc import session as COCS
from app.llm import LLMClient

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [PASS] " if ok else "  [FAIL] ") + name
          + (f"\n         证据：{detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


async def main() -> int:
    print("=" * 78)
    print("1.9.20 AI 控制台 / 改对话 / 导演指令")
    print("=" * 78)

    # ① LLMClient 真实留痕（mock 也留一条，说明“未调用真实模型”）
    s: dict = {}
    c = LLMClient({"llm": {}}, session=s, label="测试调用")
    await c.chat_json([{"role": "system", "content": "系统"},
                       {"role": "user", "content": "用户"}], temperature=0.5)
    rows = AC.entries(s)
    check("LLM 调用写入控制台（含请求/原始输出/解析状态）",
          len(rows) == 1 and rows[0]["status"] == "ok"
          and bool(rows[0]["raw"]) and rows[0]["parse"]["ok"]
          and len(rows[0]["messages"]) == 2,
          detail=f"entries={len(rows)} label={rows[0].get('label')} "
                 f"raw={len(rows[0].get('raw',''))} parse={rows[0].get('parse')}")
    check("mock 模式不伪造 reasoning（明确写演示模式）",
          "演示模式" in str(rows[0].get("reasoning")),
          detail=str(rows[0].get("reasoning"))[:60])

    # ② TSF 会话：导演指令进入 system；改对话不改数值
    st = await game.start_game({
        "mode": "tsf", "title": "AI控制台", "world": "现代都市的雨夜。",
        "protagonist": {"name": "阿明", "anchor": "黑色短发"},
        "characters": [], "content_rating": "all", "r18_enabled": False,
        "stat_config": [],
    })
    sid = st["sid"]
    sess = game.get_session(sid)
    check("TSF 开局产生 AI 控制台记录",
          len(sess.get("ai_console") or []) >= 1,
          detail=f"entries={len(sess.get('ai_console') or [])}")
    game.set_ai_directive(sid, "保持冷峻，不要替主角做决定")
    check("导演指令进入 TSF system prompt",
          "保持冷峻" in game.build_story_system(sess),
          detail="build_story_system contains 保持冷峻")
    d = json.loads(json.dumps(st["dialogue"], ensure_ascii=False))
    d[0]["text"] = "（玩家改写的旁白）"
    st2 = game.edit_dialogue(sid, d)
    check("手动修改 TSF 对话后立即生效",
          st2["dialogue"][0]["text"] == "（玩家改写的旁白）",
          detail=st2["dialogue"][0]["text"])

    # ③ CoC：开局系统记录 + 导演指令进入 CoC prompt + 改对话
    cs = COCS.start({"difficulty": "Initiate", "seed": 11,
                     "character": "Kirie", "scenario_id": "manor"})
    csid = cs["sid"]
    ss = COCS.get(csid)
    check("CoC 开局留下系统开场记录（不假装调用过模型）",
          any(x.get("label") == "CoC 开场（系统）"
              for x in (ss["run"].state.get("ai_console") or [])),
          detail=str([x.get("label") for x in (ss["run"].state.get("ai_console") or [])]))
    COCS.set_ai_directive(csid, "CoC 叙述保持冷峻")
    check("导演指令进入 CoC system prompt",
          "CoC 叙述保持冷峻" in COCP.coc_system(ss["run"]),
          detail="coc_system contains directive")
    cd = [dict(x) for x in (cs.get("dialogue") or [])]
    if cd:
        cd[0]["text"] = "（CoC 玩家改写）"
        ce = COCS.edit_dialogue(csid, cd)
        check("手动修改 CoC 对话后立即生效",
              ce["dialogue"][0]["text"] == "（CoC 玩家改写）",
              detail=ce["dialogue"][0]["text"])
    else:
        check("CoC 对话编辑前置：有对话可编辑", False, "dialog 为空")

    # ④ 记录上限（防止存档被控制台撑爆）
    s2: dict = {"ai_console": []}
    for i in range(AC.MAX_ENTRIES + 5):
        e = AC.begin(s2, [{"role": "user", "content": "x" * 5000}],
                     model="m", label=f"t{i}")
        AC.finish(s2, e, "y" * 5000)
    check(f"控制台最多保留 {AC.MAX_ENTRIES} 条",
          len(s2["ai_console"]) == AC.MAX_ENTRIES,
          detail=f"len={len(s2['ai_console'])}")
    check("长消息/回复被截断（不无限膨胀）",
          len(s2["ai_console"][-1]["messages"][0]["content"]) <= AC.MAX_TEXT + 40,
          detail=str(len(s2["ai_console"][-1]["messages"][0]["content"])))

    game.SESSIONS.pop(sid, None)
    COCS.drop(csid)
    print("-" * 78)
    print(f"合计：{'PASS' if not FAILS else 'FAIL'}；失败 {len(FAILS)}")
    print("=" * 78)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

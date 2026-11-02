# -*- coding: utf-8 -*-
"""1.9.21 API 冒烟：新端点走真实 FastAPI 路由（隔离数据根 + 空 config = mock）。

覆盖：
  GET  /api/version                       → 1.9.21
  GET  /api/prompt-presets                → 8 种写作风格
  POST /api/game/start                    → 建会话（mock LLM）
  GET  /api/game/{sid}/consistency        → report=None, auto_turns=0
  POST /api/game/{sid}/consistency/check  → 未配置模型必须 503（明确报错）
  POST /api/game/{sid}/prompt-style       → 保存风格 + 自定义提示词
  GET  /api/game/{sid}                    → 状态里带 writing_style/consistency
  POST /api/game/{sid}/consistency/auto   → 自动检查间隔
  GET  /                                  → index.html 200
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["TSF_DATA_DIR"] = tempfile.mkdtemp(prefix="api1921-test-")
with open(os.path.join(os.environ["TSF_DATA_DIR"], "config.json"), "w",
          encoding="utf-8") as fh:
    fh.write("{}")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [PASS] " if ok else "  [FAIL] ") + name
          + (f"\n         证据：{detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    # local_access_guard 会校验 Host（防 DNS rebinding）——TestClient 默认
    # host=testclient 会被 403，这里显式用本机地址构造请求。
    c = TestClient(server.app, base_url="http://127.0.0.1:8765")
    print("=" * 78)
    print("1.9.21 API 冒烟")
    print("=" * 78)

    r = c.get("/api/version")
    check("GET /api/version = 1.9.21",
          r.status_code == 200 and r.json().get("version") == "1.9.21",
          detail=f"{r.status_code} {r.text[:80]}")

    r = c.get("/api/prompt-presets")
    styles = (r.json() or {}).get("styles") or []
    check("GET /api/prompt-presets 返回 8 种风格且默认零风格词",
          r.status_code == 200 and len(styles) >= 8
          and styles[0]["id"] == "default" and styles[0]["prompt"] == "",
          detail=f"{r.status_code} n={len(styles)}")

    r = c.post("/api/game/start", json={
        "mode": "tsf", "title": "API冒烟", "world": "现代都市的雨夜。",
        "protagonist": {"name": "阿明", "anchor": "黑色短发"},
        "characters": [{"name": "小雪", "personality": "外冷内热",
                        "appearance": "银白长发"}],
        "outline": "◆ 一\n开始。", "content_rating": "18",
        "r18_enabled": True, "allow_forced": False,
    })
    sid = ((r.json() or {}).get("sid") or "") if r.status_code == 200 else ""
    check("POST /api/game/start 建会话（mock）", bool(sid),
          detail=f"{r.status_code} sid={sid} {r.text[:100]}")

    r = c.get(f"/api/game/{sid}/consistency")
    d = r.json() if r.status_code == 200 else {}
    check("GET consistency 初始为空报告 + auto=0",
          r.status_code == 200 and d.get("report") is None
          and d.get("auto_turns") == 0, detail=f"{r.status_code} {d}")

    r = c.post(f"/api/game/{sid}/consistency/check",
               json={"mode": "recent", "turns": 12})
    check("未配置模型时 consistency/check 明确 503（不伪造结果）",
          r.status_code == 503, detail=f"{r.status_code} {r.text[:120]}")

    r = c.post(f"/api/game/{sid}/prompt-style",
               json={"style_id": "literary", "custom_prompt": "每幕写气味"})
    check("POST prompt-style 保存成功",
          r.status_code == 200 and r.json().get("writing_style") == "literary"
          and r.json().get("custom_prompt") == "每幕写气味",
          detail=f"{r.status_code} {r.text[:120]}")

    r = c.get(f"/api/game/{sid}")
    st = r.json() if r.status_code == 200 else {}
    check("GET 状态带 writing_style / custom_prompt / consistency",
          st.get("writing_style") == "literary"
          and st.get("custom_prompt") == "每幕写气味"
          and "consistency" in st,
          detail=f"{r.status_code} ws={st.get('writing_style')}")

    r = c.post(f"/api/game/{sid}/consistency/auto", json={"turns": 5})
    check("POST consistency/auto 保存间隔",
          r.status_code == 200 and r.json().get("auto_turns") == 5,
          detail=f"{r.status_code} {r.text[:80]}")

    r = c.post(f"/api/game/{sid}/consistency/fix")
    check("无可修复建议时 fix 安全返回（不写坏指令）",
          r.status_code == 200 and "directive" in (r.json() or {}),
          detail=f"{r.status_code} {(r.text or '')[:100]}")

    r = c.post(f"/api/game/{sid}/outfit",
               json={"character": "小雪", "outfit": "casual"})
    body = r.json() if r.status_code == 200 else {}
    check("换装端点返回 outfits 状态（ComfyUI 线路也走同一入口）",
          r.status_code == 200 and body.get("outfits", {}).get("小雪") == "便服",
          detail=f"{r.status_code} outfits={body.get('outfits')}")

    r = c.get("/")
    check("GET / 返回 index.html（含两个新页签）",
          r.status_code == 200 and "ai-tab-consistency" in r.text
          and "ai-tab-prompt" in r.text, detail=str(r.status_code))

    print("=" * 78)
    print("结果：" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项"))
    for f in FAILS:
        print("  - " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())

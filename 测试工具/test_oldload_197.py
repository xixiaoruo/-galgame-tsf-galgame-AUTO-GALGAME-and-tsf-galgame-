"""1.9.7 旧存档恢复：pending 资产转 error、场景重试预算重置、可重新入队。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import game  # noqa: E402

session = {
    "sid": "old", "title": "旧档", "mode": "tsf",
    "protagonist": {"name": "主角", "stats": {}},
    "characters": [], "assets": {"portraits": {}, "backgrounds": {
        "dead": {"status": "pending", "file": "", "error": ""},
        "alive": {"status": "pending", "file": "x.png", "error": ""},
    }},
    "scenes": {"scene1": {"name": "庭院", "bg_id": "bg0", "hint": "", "visits": 1}},
    "bg_map": {"bg0": "dead"},
    "bg_retry": {"scene1": {"n": 3, "first_bg": "bg0", "last_ts": 1, "state": "unavailable"}},
    "log": [{"turn": {"scene": "庭院", "background_id": "bg0",
                      "bg_unavailable": True, "scene_revisit": True}, "choice": None}],
}
game._recover_interrupted_assets(session)
assert session["assets"]["backgrounds"]["dead"]["status"] == "error"
assert "中断" in session["assets"]["backgrounds"]["dead"]["error"]
assert session["bg_retry"] == {}
assert "bg_unavailable" not in session["log"][-1]["turn"]
assert "scene_revisit" not in session["log"][-1]["turn"]

# 重试预算清零后，_assign_scene_bg 应重新入队（不真的调 SD：替换工厂）
called = {}
game._start_background_task = lambda s, hint, base_name="背景": called.setdefault("bg", "bg9")
turn = {"scene": "庭院", "background_hint": "安静的日式庭院", "background_id": "bg0"}
game._assign_scene_bg(session, turn)
assert called.get("bg") == "bg9", called
assert turn["background_id"] == "bg9", turn
print("OK 1.9.7 old save pending recovery + scene requeue")

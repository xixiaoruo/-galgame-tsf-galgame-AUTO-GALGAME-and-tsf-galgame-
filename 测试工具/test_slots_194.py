"""1.9.4 存档回归：同秒连存不得互相覆盖；列表能看到两份；裸读可回读。"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / ".devdata" / "slots-test"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ["TSF_DATA_DIR"] = str(TMP)

from app import game  # noqa: E402

sid = "slottest001"
game.SESSIONS[sid] = {
    "sid": sid, "title": "存档测试", "mode": "tsf",
    "protagonist": {"name": "主角", "anchor": "黑发", "stats": {}},
    "characters": [], "log": [], "assets": {"portraits": {}, "backgrounds": {}},
    "scenes": {}, "bg_map": {}, "char_states": {},
}

r1 = game.save_slot(sid, "第一份")
r2 = game.save_slot(sid, "第二份")
assert r1["slot_id"] != r2["slot_id"], (r1, r2)
items = game.list_slots()
assert len(items) == 2, items
names = {x["name"] for x in items}
assert names == {"第一份", "第二份"}, names
for it in items:
    raw = game.load_slot_raw(it["slot_id"])
    assert raw["slot_name"] == it["name"]
assert game.delete_slot(r1["slot_id"]) is True
assert len(game.list_slots()) == 1
print("OK 1.9.4 slot uniqueness/list/raw-load")
shutil.rmtree(TMP, ignore_errors=True)

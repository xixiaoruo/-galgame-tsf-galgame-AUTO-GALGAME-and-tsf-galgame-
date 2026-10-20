"""1.9.0 新功能单测：随机制服 / 场景归一 / 角色卡背景 / 感情机制。

不启动服务、不写玩家真实数据、不调用真实 LLM/SD：
  · uniforms/scenes/characters 都是纯函数或可在 mock 下运行
  · vn 的感情机制在内存 session 上验证
  · legacy 只测字段适配函数，不执行 restore
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import characters as charm          # noqa: E402
from app import game                        # noqa: E402
from app import legacy                      # noqa: E402
from app import scenes                      # noqa: E402
from app import story                       # noqa: E402
from app import uniforms                    # noqa: E402
from app import vn                          # noqa: E402


def test_uniforms():
    presets = uniforms.list_presets()
    assert len(presets) >= 20, len(presets)
    ids = {p["id"] for p in presets}
    assert len(ids) == len(presets), "制服 id 重复"
    for p in presets:
        assert p["name"] and p["en"] and p["neg"], p
        assert len(p["en"]) <= uniforms.MAX_EN, p["id"]
        assert len(p["neg"]) <= uniforms.MAX_NEG, p["id"]
        low = p["en"].lower()
        assert "nude" not in low and "naked" not in low, p["id"]
    # 学园场景优先校服池，且随机可复现
    pick = uniforms.random_preset("星冠学园 教室", seed=7)
    assert pick["matched"] is True
    assert pick["scene_tag"] == "school"
    assert pick["id"] in uniforms.TAG_POOLS["school"]
    p1 = uniforms.random_preset("神社", seed=11)
    p2 = uniforms.random_preset("神社", seed=11)
    assert p1["id"] == p2["id"]
    # 排除项必须生效
    banned = {u["id"] for u in uniforms.UNIFORMS}
    only = uniforms.random_preset("学园", exclude=list(banned - {"sailor"}),
                                  seed=1)
    assert only["id"] == "sailor", only
    en, neg = uniforms.normalize_pair("  a  b ", " c ")
    assert en == "a b" and neg == "c"
    print("OK uniforms")


def test_scenes():
    assert scenes.scenes_similar("星冠学园·操场（午后）", "星冠学园·操场")
    assert scenes.scenes_similar("操场", "星冠学园·操场")
    assert scenes.scenes_similar("教室", "教室（放课后）")
    assert not scenes.scenes_similar("教室", "体育馆")
    assert scenes.canonical_scene("操场（午后）", ["星冠学园·操场"]) == "星冠学园·操场"
    assert scenes.canonical_scene("完全不同的地方", ["教室"]) is None
    assert scenes.guess_initial_scene("故事发生在星冠学园的高中校园")
    assert scenes.guess_initial_scene("") == ""
    print("OK scenes")


def test_characters():
    c = charm.random_character(world="学园故事", seed=3)
    assert c["name"] and c["role"] and c["appearance"] and c["personality"]
    assert len(c["background"]) >= 60
    dup = charm.normalize_character_list(
        [{"name": "A"}, {"name": "A"}, {"name": "主角"},
         {"name": "B", "role": "同班同学", "background": "x"}],
        protagonist="主角")
    assert [x["name"] for x in dup] == ["A", "B"]
    assert dup[1]["role"] == "同班同学"
    assert dup[1]["background"] == "x"
    # 背景生成：mock LLM 走本地模板，不联网
    class FakeLLM:
        mock = True
    old = charm.LLMClient
    charm.LLMClient = lambda cfg: FakeLLM()
    try:
        r = asyncio.run(charm.write_background(
            name="测试", role="同班同学", personality="安静", world="学园"))
    finally:
        charm.LLMClient = old
    assert r["background"] and r["mock"] is True
    # 档案清洗保留新字段
    st = story._clean({
        "name": "档案", "world": "world", "protagonist": {"name": "主角"},
        "characters": [{"name": "A", "role": "学姐", "background": "背景"}],
        "stage_count": 10, "uniform_name": "水手服",
    })
    assert st["characters"][0]["role"] == "学姐"
    assert st["characters"][0]["background"] == "背景"
    assert st["stage_count"] == 10
    assert st["uniform_name"] == "水手服"
    assert st["schema"] == 2
    print("OK characters")


def test_emotions():
    assert vn.bond_stage(0)["label"] == "陌生"
    assert vn.bond_stage(20)["label"] == "认识"
    assert vn.bond_stage(45)["label"] == "亲近"
    assert vn.bond_stage(95)["label"] == "恋人"
    session = {
        "mode": "tsf", "protagonist": {"name": "主角"},
        "characters": [{"name": "A"}, {"name": "B"}],
        "char_states": {}, "log": [],
    }
    vn.ensure_char_states(session)
    assert all(v["affection"] == 20 for v in session["char_states"].values())
    session["char_states"]["A"]["affection"] = 44
    turn = {"scene": "教室", "present": ["A"],
            "states_delta": {"A": {"affection": 2, "mood": 2}}}
    vn.apply_char_delta(session, turn["states_delta"])
    ups = vn.record_turn_emotions(session, turn)
    assert len(ups) == 1 and ups[0]["label"] == "亲近", ups
    assert vn.emotion_note(session, "A")["text"].startswith("在「教室」")
    prompt = vn.char_states_block(session)
    assert "关系" in prompt and "亲近" in prompt
    # 缺席 4 幕：好感 -1、高潮/敏感回落，主角不衰减
    session["log"] = [{"turn": turn, "choice": None}]
    for _ in range(4):
        vn.apply_absence_decay(session, ["A"])
    assert session["char_states"]["B"]["affection"] == 19
    assert session["char_absence"]["B"] == 4
    assert session["char_states"]["主角"]["affection"] == 20
    # 在场即清零缺席计数
    vn.apply_absence_decay(session, ["B"])
    assert "B" not in session["char_absence"]
    print("OK emotions")


def test_uniform_portrait_lock():
    session = {"uniform_en": "", "uniform_neg": "",
               "current_scene": "学园教室"}
    en, neg = game._uniform_for_portrait(session)
    assert en and "school uniform" in en and neg
    en2, neg2 = game._uniform_for_portrait(session, "白色连衣裙")
    assert en2 == "" and neg2 == ""
    en3, _ = game._uniform_for_portrait(session, "水手服")
    assert en3 == en
    assert game._uniform_text_for(session, "白色连衣裙") == ""
    print("OK uniform portrait lock")


def test_legacy_adapt():
    raw = {
        "id": "abc123", "name": "旧档案", "title": "旧剧本",
        "world": "world", "mode": "tsf", "protagonist": {"name": "主角"},
        "characters": [{"name": "A", "appearance": "黑发"}],
    }
    out = legacy._adapt_story(raw, "档案备份")
    assert out["schema"] == 2 and out["stage_count"] == 6
    assert out["characters"][0]["role"] == "NPC"
    assert "background" in out["characters"][0]
    assert out["recovered_from"] == "档案备份"
    assert out["recovered_at"]
    # 新 id 副本
    out2 = legacy._adapt_story(raw, "档案备份", new_id=True)
    assert out2["id"] != "abc123"
    print("OK legacy adapt")


def main():
    test_uniforms()
    test_scenes()
    test_characters()
    test_emotions()
    test_uniform_portrait_lock()
    test_legacy_adapt()
    print("ALL 1.9.0 TESTS PASSED")


if __name__ == "__main__":
    main()

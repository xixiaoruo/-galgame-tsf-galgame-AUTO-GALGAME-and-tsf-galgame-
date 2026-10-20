# -*- coding: utf-8 -*-
"""t73：**自定义剧本**（世界观真正决定地点/谜团/古神/事件卡）。

用户报告："coc_tsf 模式完全不遵循背景剧情"。根因有二：
  ① `app/coc/*.py` 对 world/outline/lorebook 的引用数是 **0**（没有输入通道）；
  ② 前端还把世界替换成 `world || "盐川町"`。
本模块验证新增的 scenario 通道：
  1. **零影响**：无剧本时地点/谜团/古神/卡池与随包内容逐项相同、prompt 无剧本段；
  2. **生效**：给了剧本 ⇒ 地点/谜团/古神/卡池**真的换成剧本的**；
  3. **部分给**：只给 locations ⇒ 只有地点被换，谜团仍回落随包（不是全有全无）；
  4. **文本注入**：world/outline/storybook/directives 出现在 prompt 里；
  5. **边界声明**：prompt 里必须声明"数值仍由系统决定"（不许剧本改写数值）；
  6. **拒绝非法**：非法 threat / 缺 id / 非数组 ⇒ 抛错（绝不静默忽略）；
  7. **存档往返**：剧本随存档保留，读档后地点表仍是剧本的。

每条断言都带 `counterexample`（harness 强制）。
"""
from __future__ import annotations

import io
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.coc import content
from app.coc import prompt as PR
from app.coc import scenario as SCN
from app.coc import session as SESS
from app.coc.loop import Run
from app.coc.tests.harness import TestSuite

CFG = None
from app.coc.provenance import load_tuning  # noqa: E402
CFG = load_tuning()

SCEN = {
    "title": "星冠学园",
    "world": "这是一所封闭的女子学园，夜里会点名。",
    "outline": "主角转学进来，逐渐发现自己被登记成了别人。",
    "storybook": ["学园不承认男性学生", "午夜铃响后不要出宿舍"],
    "directives": ["叙述保持冷峻克制"],
    "locations": [
        {"id": "Dorm", "name": "女生宿舍", "threat": "SAFE", "home": True},
        {"id": "OldWing", "name": "旧校舍", "threat": "DOOMED"},
    ],
    "mysteries": [
        {"id": "MY-X", "name": "夜里的点名", "locations": ["Dorm", "OldWing"], "free_roam": True},
    ],
    "old_gods": [{"id": "TheWatcher", "name": "观者", "global_rule": "none"}],
}


def _run(scn=None):
    return Run.start({"difficulty": "Initiate", "seed": 4242,
                      "old_god": (scn or {}).get("old_gods", [{}])[0].get("id", "Goizo"),
                      "character": "Kirie"}, CFG, scenario=SCN.normalize(scn) if scn else None)


def test_zero_impact(s: TestSuite) -> None:
    """无剧本 ⇒ 一切与随包内容一致。"""
    run = _run(None)
    s.expect("无剧本时 loc_table == content.locations()",
             [x.get("id") for x in run.loc_table] == [x.get("id") for x in content.locations()],
             detail=str([x.get("id") for x in run.loc_table]),
             counterexample="若 Run.start 无条件用 scenario 覆盖，无剧本时表会变空")
    s.expect("无剧本时 mystery_table == content.mysteries()",
             [x.get("id") for x in run.mystery_table] == [x.get("id") for x in content.mysteries()],
             counterexample="同上：无剧本却替换 ⇒ 谜团表与随包不一致")
    s.expect("无剧本时卡池非空且等于随包卡池",
             len(run.card_pool) == len(content.cards()) and len(run.card_pool) > 0,
             detail=f"{len(run.card_pool)} vs {len(content.cards())}",
             counterexample="若 scenario.cards 回落写错成空列表，卡池会变成 0")
    s.expect("无剧本时 scenario 字段为 None", run.scenario is None,
             counterexample="若把 {} 存成 scenario，后续 prompt_block 可能误判为有剧本")
    s.expect("无剧本时 prompt 无【剧本名】段",
             "【剧本名】" not in PR.coc_system(run),
             counterexample="若 prompt_block 对空剧本仍拼标题，会多出空段 ⇒ 破坏逐字节闸门")
    s.expect("scenario.normalize(None/空) 返回 None",
             SCN.normalize(None) is None and SCN.normalize({}) is None,
             counterexample="若 normalize({}) 返回 {}，会让'留空'被当成'有剧本'")


def test_override(s: TestSuite) -> None:
    """给了剧本 ⇒ 四张表真的换成剧本的。"""
    run = _run(SCEN)
    s.expect("地点表来自剧本", [x["id"] for x in run.loc_table] == ["Dorm", "OldWing"],
             detail=str([x["id"] for x in run.loc_table]),
             counterexample="若 Run.start 仍读 content.locations()，这里会是随包的 4 个地点")
    s.expect("地点名来自剧本", run.loc_table[0]["name"] == "女生宿舍",
             detail=str(run.loc_table[0].get("name")),
             counterexample="若只换 id 不换 name，玩家看到的还是随包地名")
    s.expect("谜团表来自剧本", [x["id"] for x in run.mystery_table] == ["MY-X"],
             detail=str([x["id"] for x in run.mystery_table]),
             counterexample="若 mystery_table 未接 scenario，这里会是随包的 MY-01/MY-02")
    s.expect("古神表来自剧本", [x["id"] for x in SCN.old_gods(run.scenario)] == ["TheWatcher"],
             detail=str([x["id"] for x in SCN.old_gods(run.scenario)]),
             counterexample="若 old_gods 未接 scenario，会回落到随包的两位古神")
    s.expect("剧本地点被标了来源", run.loc_table[0].get("_source", "").startswith("scenario"),
             detail=str(run.loc_table[0].get("_source")),
             counterexample="若不给 _source，就无法区分'随包内容'与'玩家剧本'（溯源纪律）")
    s.expect("剧本没有 home 时自动补一个",
             SCN.normalize({"locations": [{"id": "A"}, {"id": "B"}]})["locations"][0]["home"] is True,
             counterexample="都不标 home ⇒ 休息/回血无落脚点，属可用性缺口")


def test_partial(s: TestSuite) -> None:
    """只给一部分表 ⇒ 只换那一部分，其余回落（不是全有全无）。"""
    sc = SCN.normalize({"locations": [{"id": "OnlyOne", "name": "唯一地点"}]})
    s.expect("只给 locations ⇒ 地点换成剧本的",
             [x["id"] for x in SCN.locations(sc)] == ["OnlyOne"],
             counterexample="若缺表时返回空列表而不是回落，地点会变空 ⇒ 引擎无法开局")
    s.expect("只给 locations ⇒ 谜团回落随包",
             [x["id"] for x in SCN.mysteries(sc)] == [x["id"] for x in content.mysteries()],
             detail=str([x["id"] for x in SCN.mysteries(sc)]),
             counterexample="若逐表独立回落写错成整体替换，谜团会变空")
    s.expect("只给 locations ⇒ 古神回落随包",
             [x["id"] for x in SCN.old_gods(sc)] == [x["id"] for x in content.old_gods()],
             counterexample="同上")
    s.expect("只给 locations ⇒ 卡池回落随包",
             len(SCN.cards(sc)) == len(content.cards()),
             detail=f"{len(SCN.cards(sc))} vs {len(content.cards())}",
             counterexample="同上")


def test_prompt_text(s: TestSuite) -> None:
    """世界观/大纲/故事书/指令真的进了 prompt。"""
    run = _run(SCEN)
    txt = PR.coc_system(run)
    for key, needle in (("剧本名", "星冠学园"),
                        ("世界观", "封闭的女子学园"),
                        ("大纲", "被登记成了别人"),
                        ("故事书", "不承认男性学生"),
                        ("指令", "冷峻克制")):
        s.expect(f"prompt 含{key}", needle in txt,
                 counterexample=f"若 prompt_block 漏掉该字段，{key} 就不会出现 ⇒ 模型看不到设定")
    s.expect("prompt 含剧本边界声明（数值不受剧本影响）",
             "仍由系统决定" in txt and "不得因为设定而改写任何数值" in txt,
             counterexample="缺边界声明 ⇒ 模型可能按设定擅自改写数值/跳过检定（破坏确定性层）")


def test_reject_invalid(s: TestSuite) -> None:
    """非法剧本必须**抛错**，不能静默忽略。"""
    cases = [
        ("非法 threat", {"locations": [{"id": "X", "threat": "VERY_BAD"}]}),
        ("地点缺 id", {"locations": [{"name": "无 id"}]}),
        ("locations 非数组", {"locations": {"id": "X"}}),
        ("谜团缺 id", {"mysteries": [{"name": "无 id"}]}),
        ("古神缺 id", {"old_gods": [{"name": "无 id"}]}),
        ("cast 缺 name", {"cast": [{"display": "无 name"}]}),
        ("scenario 本身非对象", "not-a-dict"),
    ]
    for label, bad in cases:
        raised = None
        try:
            SCN.normalize(bad)
        except SCN.ScenarioError as e:
            raised = str(e)
        s.expect(f"拒绝非法：{label}", raised is not None, detail=str(raised)[:60],
                 counterexample=f"若静默忽略 {label}，玩家会以为设定生效了，实际没有")


def test_save_roundtrip(s: TestSuite) -> None:
    """剧本随存档往返：存→读后地点表仍是剧本的。"""
    sess = SESS.start({"difficulty": "Initiate", "seed": 99, "old_god": "TheWatcher",
                       "character": "Kirie", "scenario": SCEN})
    sid = sess["sid"]
    r = SESS.save_slot(sid, "剧本测试")
    slot_id = r.get("slot_id")
    s.expect("存档成功", bool(slot_id), detail=str(r),
             counterexample="若 save_slot 对本局抛错（如 scenario 不可 JSON 化），这里会失败")
    if slot_id:
        back = SESS.load_slot(slot_id)
        s2 = SESS.get(back["sid"])
        s.expect("读档后会话带回剧本", bool((s2 or {}).get("scenario")),
                 detail=str(bool((s2 or {}).get("scenario"))),
                 counterexample="若 save_slot 没写 scenario，读档后 scenario 为 None ⇒ 地点表回落随包")
        s.expect("读档后地点表仍是剧本的",
                 [x["id"] for x in ((s2 or {}).get("run").loc_table or [])] == ["Dorm", "OldWing"],
                 detail=str([x["id"] for x in ((s2 or {}).get("run").loc_table or [])]),
                 counterexample="若 load_slot 没把 scenario 传回 Run.resume，地点表会变成随包的 4 个")
        SESS.drop(back["sid"])
    SESS.drop(sid)


def test_plain_text_scenario(s: TestSuite) -> None:
    """玩家只填了世界观/大纲（没有结构化剧本）⇒ 也应被遵循（叙述层）。"""
    sess = SESS.start({"difficulty": "Initiate", "seed": 7, "old_god": "Goizo",
                       "character": "Kirie",
                       "world": "这是一所女校", "outline": "主角被登记为女生",
                       "lorebook": [{"text": "学园不承认男性"}],
                       "directives": [{"text": "保持冷峻"}]})
    sc = (SESS.get(sess["sid"]) or {}).get("scenario") or {}
    s.expect("纯文本也生成剧本对象", bool(sc), detail=str(sorted(sc))[:80],
             counterexample="若 _scenario_of 不组装纯文本，只填世界观的玩家仍然不被遵循（原报告场景）")
    s.expect("world 被收进剧本", "女校" in str(sc.get("world")),
             counterexample="world 未收集 ⇒ prompt 里看不到世界观")
    s.expect("outline 被收进剧本", "登记为女生" in str(sc.get("outline")),
             counterexample="outline 未收集")
    s.expect("lorebook 被收进 storybook", any("不承认男性" in x for x in (sc.get("storybook") or [])),
             detail=str(sc.get("storybook")),
             counterexample="lorebook 未收集 ⇒ 世界规则不进 prompt")
    s.expect("directives(list[dict]) 被收进剧本",
             any("冷峻" in x for x in (sc.get("directives") or [])),
             detail=str(sc.get("directives")),
             counterexample="directives 是 list[dict]，若按字符串处理会取到整个 dict 而丢内容")
    txt = PR.coc_system((SESS.get(sess["sid"]) or {}).get("run"))
    s.expect("prompt 含该世界观", "女校" in txt,
             counterexample="剧本对象建了但没进 prompt ⇒ 等于没接")
    SESS.drop(sess["sid"])


def main() -> int:
    s = TestSuite("app/coc/tests/test_scenario.py —— 自定义剧本（世界观决定内容）")
    for fn in (test_zero_impact, test_override, test_partial, test_prompt_text,
               test_reject_invalid, test_save_roundtrip, test_plain_text_scenario):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

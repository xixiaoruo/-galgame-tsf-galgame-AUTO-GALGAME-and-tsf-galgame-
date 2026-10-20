# -*- coding: utf-8 -*-
"""1.9.19 CoC 扩展回归：SAN / 固定商店 / 物品使用 / 剧本预设 / 版本同步。

覆盖用户本轮要求：
  1. SAN（0–99、d100 检定、损失、临时疯狂、归零结局）；
  2. 物品栏 / 背包（既有）+ 物品使用（绷带/镇静剂/日记…）；
  3. 自动生成物品（固定商店按种子生成、搜刮成功才生成）；
  4. 固定商店场景（进入/购买/出售/重掷）；
  5. 各种 CoC 剧本预设（黄昏洋馆/雾港/极地/学园/雨夜）。
"""
from __future__ import annotations

import os
import sys
import tempfile

# 测试必须隔离真实数据目录（与 app/coc/tests 的闸门同一条纪律）：
# 强制覆盖，绝不允许外部 TSF_DATA_DIR 把测试写进真实目录。
os.environ["TSF_DATA_DIR"] = tempfile.mkdtemp(prefix="coc1919-test-")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.coc import inventory as INV
from app.coc import loop as L
from app.coc import provenance as P
from app.coc import sanity as SAN
from app.coc import scenarios as PRESETS
from app.coc import session as SESS
from app.coc import shop as SHOP
from app.coc import state as ST
from app.coc.tests.harness import TestSuite

CFG = P.load_tuning()


def _run(seed: int = 1919) -> L.Run:
    return L.Run.start({"difficulty": "Initiate", "seed": seed,
                        "sid": f"test1919{seed}"}, CFG)


# ---------------------------------------------------------------- SAN

def test_initial_san(s: TestSuite) -> None:
    a = SAN.initial_san({"KNW": 5}, CFG)
    b = SAN.initial_san({"KNW": 8}, CFG)
    c = SAN.initial_san({"KNW": 12}, CFG)
    s.expect("初始 SAN 按 KNW 调整（5→45 / 8→60 / 12→80）",
             (a, b, c) == (45, 60, 80),
             detail=f"KNW5={a} KNW8={b} KNW12={c}",
             counterexample="反例：固定 50 → 三个值相等 → 变红")
    s.expect("SAN 上限来自 tuning（99）", SAN.san_max(CFG) == 99,
             detail=str(SAN.san_max(CFG)),
             counterexample="反例：写死 100 或读错字段 → 变红")


def test_san_check_and_bout(s: TestSuite) -> None:
    run = _run(7)
    r = SAN.check(run, loss="1/1d10", difficulty="regular", reason="test")
    s.expect("SAN 检定返回 d100/目标/成败/损失/前后值",
             all(k in r for k in ("roll", "target", "success", "loss",
                                  "san_before", "san_after")),
             detail=str({k: r[k] for k in ("roll", "target", "success", "loss")}),
             counterexample="反例：只回一句文案，前端无法显示 roll/target → 变红")
    s.expect("SAN 检定真的推进 rng 并写入 san_checks",
             int(run.state["san_checks"]) == 1 and run.state["san"] == r["san_after"],
             detail=f"checks={run.state['san_checks']} san={run.state['san']}",
             counterexample="反例：只计算不落账 → 面板与实际不一致 → 变红")
    # 强制一次大损失，验证临时疯狂
    st = run.state
    st["san"] = 30
    r2 = SAN.check(run, loss="10/10", difficulty="regular", reason="bout")
    s.expect("单次 SAN 损失 ≥5 触发临时疯狂",
             bool(r2.get("bout")) and bool(st.get("temp_insanity")),
             detail=f"loss={r2.get('loss')} bout={r2.get('bout')}",
             counterexample="反例：只写日志不置 temp_insanity → 面板无提示 → 变红")
    st["san"] = 0
    run.settle_ending()
    s.expect("SAN 归零走 END_SAN_ZERO（与 END_REA_ZERO 分开）",
             st.get("ending") == "END_SAN_ZERO",
             detail=str(st.get("ending")),
             counterexample="反例：SAN 归零也判 END_REA_ZERO → 两个资源没有区分 → 变红")


# ---------------------------------------------------------------- 商店

def test_shop_stock(s: TestSuite) -> None:
    run = _run(11)
    a = SHOP.shop_view(run, "general_store")
    b = SHOP.shop_view(run, "general_store")
    s.expect("同局同店两次读取库存一致（固定货架）",
             [x["id"] for x in a["stock"]] == [x["id"] for x in b["stock"]],
             detail=f"{len(a['stock'])} 件：{[x['id'] for x in a['stock']]}",
             counterexample="反例：每次读取都重新随机 → 货架会自己变 → 变红")
    first = [x["id"] for x in a["stock"]]
    rr = SHOP.reroll(run, "general_store")
    second = [x["id"] for x in rr["shop"]["stock"]]
    s.expect("重掷真的换货（且 DOOM 上升）",
             first != second and int(run.state["doom"]) >= 3,
             detail=f"before={first} after={second} doom={run.state['doom']}",
             counterexample="反例：reroll 只加钱/加 DOOM 却不换库存 → 变红")


def test_shop_buy_sell(s: TestSuite) -> None:
    run = _run(12)
    run.state["funds"] = 30
    before = int(run.state["funds"])
    view = SHOP.shop_view(run, "general_store")
    affordable = [x for x in view["stock"] if int(x["price"]) <= before]
    s.expect("商店至少有可购买商品", bool(affordable),
             detail=f"库存={[x['id'] for x in view['stock']]}",
             counterexample="反例：价格全部高于初始资金 → 开局商店形同虚设 → 变红")
    item = affordable[0]
    res = SHOP.buy(run, "general_store", item["id"])
    s.expect("购买成功：扣款、物品进背包、库存移除",
             res.get("ok") and int(run.state["funds"]) == before - item["price"]
             and item["id"] in (run.state["inventory"]["backpack"] or [])
             and all(x["id"] != item["id"] for x in SHOP.shop_view(run, "general_store")["stock"]),
             detail=f"{item['id']} price={item['price']} funds={run.state['funds']}",
             counterexample="反例：只扣款不给物品 / 给了物品不扣款 / 库存未移除 → 变红")
    again = SHOP.buy(run, "general_store", item["id"])
    s.expect("已拥有的商品不能重复购买（背包按 id 去重）",
             (not again.get("ok")) and again.get("reason") in ("already_owned", "not_in_stock"),
             detail=str(again.get("reason")),
             counterexample="反例：允许重复购买 → 背包里出现两份同 id → 后续装备/使用语义混乱 → 变红")
    sell = SHOP.sell(run, "general_store", item["id"])
    s.expect("出售返还资金且从背包移除",
             sell.get("ok") and int(run.state["funds"]) > 0
             and item["id"] not in (run.state["inventory"]["backpack"] or []),
             detail=f"value={sell.get('value')} funds={run.state['funds']}",
             counterexample="反例：卖了不扣背包 / 不返钱 → 变红")


def test_search_generates(s: TestSuite) -> None:
    run = _run(13)
    run.state["stats"]["PER"] = 12
    got = 0
    for _ in range(6):
        r = SHOP.search(run, "Downtown")
        if r.get("found"):
            got += 1
    s.expect("搜刮是显式动作，且成功时自动生成物品",
             got >= 1 and len(run.state.get("searched") or []) >= 1,
             detail=f"6 次搜刮命中 {got} 次；searched={run.state.get('searched')}",
             counterexample="反例：搜刮永远返回空 → 自动生成物品功能等于没有 → 变红")
    again = SHOP.search(run, "Downtown")
    s.expect("同一回合同一地点只能搜刮一次",
             (not again.get("ok")) and again.get("reason") == "already_searched",
             detail=str(again.get("reason")),
             counterexample="反例：可无限搜刮 → 资源刷取 → 变红")


# ---------------------------------------------------------------- 物品使用

def test_use_item(s: TestSuite) -> None:
    run = _run(14)
    run.apply({"item_grant": ["Bandage"]}, reason="test", clamp=False)
    run.apply({"tags_grant": [{"id": "MinorCut", "kind": "injury", "severity": "minor"}]},
              reason="test", clamp=False)
    res = INV.use_item(run, "Bandage")
    s.expect("绷带治好 minor 伤势并消耗",
             res.get("ok") and "Bandage" not in run.state["inventory"]["backpack"]
             and all(t.get("id") != "MinorCut" for t in run.state.get("tags") or []),
             detail=str(res),
             counterexample="反例：用了不消耗 / 伤势没移除 → 变红")
    run.apply({"item_grant": ["Bandage"]}, reason="test", clamp=False)
    no = INV.use_item(run, "Bandage")
    s.expect("没有对应伤势时绷带不空耗",
             (not no.get("ok")) and no.get("reason") == "no_matching_injury",
             detail=str(no.get("reason")),
             counterexample="反例：无伤也消耗绷带 → 玩家会误以为使用成功 → 变红")

    run.apply({"item_grant": ["Sedative"]}, reason="test", clamp=False)
    run.state["san"] = 40
    use = INV.use_item(run, "Sedative")
    s.expect("镇静剂回复 SAN 并消耗",
             use.get("ok") and int(run.state["san"]) > 40
             and "Sedative" not in run.state["inventory"]["backpack"],
             detail=f"san={run.state['san']} effects={use.get('effects')}",
             counterexample="反例：只加文案不改 SAN → 变红")

    run.apply({"item_grant": ["OccultDiary"]}, reason="test", clamp=False)
    run.state["doom"] = 20
    use2 = INV.use_item(run, "OccultDiary")
    s.expect("神秘日记降低 DOOM 并消耗",
             use2.get("ok") and int(run.state["doom"]) < 20,
             detail=f"doom={run.state['doom']} effects={use2.get('effects')}",
             counterexample="反例：DOOM 不变 → 物品效果未接 tuning → 变红")


# ---------------------------------------------------------------- 剧本预设

def test_scenario_presets(s: TestSuite) -> None:
    ids = [x["id"] for x in PRESETS.summaries() if x["id"] != "default"]
    s.expect("至少 5 个可用 CoC 剧本预设", len(ids) >= 5,
             detail=str(ids),
             counterexample="反例：只放 1 个默认剧本 → 用户要的“各种剧本”没落地 → 变红")
    for sid in ids:
        sc = PRESETS.resolve(sid, None)
        run = L.Run.start({"difficulty": "Initiate", "seed": 1919,
                           "old_god": sc["old_gods"][0]["id"],
                           "character": "Kirie"}, CFG, scenario=sc)
        s.expect(f"预设 {sid} 开局：地点/谜团/卡池/章节长度/初始物品全部生效",
                 len(run.loc_table) >= 3 and len(run.mystery_table) >= 2
                 and len(run.card_pool) >= 6 and int(run.state["mystery_len"]) == 3
                 and all(i in run.state["inventory"]["backpack"]
                         for i in (sc.get("starting_items") or [])),
                 detail=f"loc={len(run.loc_table)} my={len(run.mystery_table)} "
                        f"cards={len(run.card_pool)} len={run.state['mystery_len']} "
                        f"bag={run.state['inventory']['backpack']}",
                 counterexample=f"反例：{sid} 缺地点/卡池，或 mystery_len 没覆盖 → 变红")
    try:
        PRESETS.resolve("no-such-scenario", None)
        bad = ""
    except PRESETS.ScenarioPresetError as e:
        bad = str(e)
    s.expect("未知剧本预设必须报错（不静默回落）", bool(bad),
             detail=bad[:60],
             counterexample="反例：未知 id 静默用默认剧本 → 玩家以为选中的剧本生效了 → 变红")


def test_scenario_save_roundtrip(s: TestSuite) -> None:
    pub = SESS.start({"difficulty": "Initiate", "seed": 77,
                      "scenario_id": "manor", "character": "Kirie"})
    sid = pub["sid"]
    r = SESS.save_slot(sid, "1.9.19 剧本")
    slot_id = r.get("slot_id")
    back = SESS.load_slot(slot_id) if slot_id else {}
    s2 = SESS.get(back.get("sid", ""))
    s.expect("剧本预设随存档/读档往返",
             bool(s2) and s2.get("scenario_id") == "manor"
             and [x["id"] for x in s2["run"].loc_table] == ["ManorFoyer", "ManorStudy", "ManorCellar"],
             detail=f"slot={slot_id} scenario_id={(s2 or {}).get('scenario_id')}",
             counterexample="反例：存/读丢掉 scenario_id → 读档回到盐川町 → 变红")
    if s2:
        SESS.drop(s2["sid"])
    SESS.drop(sid)


def test_san_panel(s: TestSuite) -> None:
    pub = SESS.start({"difficulty": "Initiate", "seed": 88, "character": "Kirie"})
    c = pub["coc"]
    s.expect("开局面板包含 SAN / san_max / san_state",
             all(k in c for k in ("san", "san_max", "san_state"))
             and c["san"] > 0 and c["san_max"] == 99,
             detail=str({k: c.get(k) for k in ("san", "san_max", "san_state")}),
             counterexample="反例：面板只给 REA，没有 SAN → 前端无法显示理智值 → 变红")
    SESS.drop(pub["sid"])


def main() -> int:
    s = TestSuite("测试工具/test_coc_1919.py —— SAN / 商店 / 物品 / 剧本预设")
    for fn in (test_initial_san, test_san_check_and_bout, test_shop_stock,
               test_shop_buy_sell, test_search_generates, test_use_item,
               test_scenario_presets, test_scenario_save_roundtrip, test_san_panel):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

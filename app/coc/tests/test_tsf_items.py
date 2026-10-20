# -*- coding: utf-8 -*-
"""t71：TSF×CoC 融合模式下**物品/装备/背包的机制后果**。

用户要求（2026-10-05）：融合模式里
  ① 武器/装备要吃 TSF 修正（此前 `combat.stat_of` 读原始属性 ⇒ 转变对命中零影响）
  ② 服装随阶段演化
  ③ 背包与所得物品的机制真正生效

覆盖：
  1. **零影响对照**：转变进度 0 时所有新增路径必须与改动前**逐字相同**（否则就是回归）；
  2. **① 属性修正**：`stat_of` 随档位下降、KNW 正向、下限 1；且**用单手武器隔离**验证
     `hit_pct` 的下降**来自属性被压**（而不是"武器没装上"——那是另一件事，必须排除）；
  3. **② 服装演化**：衣物状态随档位变化；双手武器在档 4/5 被拒且原因可读；C 槽不被一刀切；
  4. **③ 背包可用性**：每件物品带 `wearable`/`block_reason`/`use_penalty`；视图带 `tsf_mech`；
  5. **数值纪律**：新增数值**全部**来自 `tuning.json` 的 `coc_tsf_items`（代码里无兜底常量）。

每条断言都带 `counterexample`（harness 强制）：写清"什么输入会让它变红"。
"""
from __future__ import annotations

import io
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.coc import combat as CB
from app.coc import item_effects as IE
from app.coc import provenance as P
from app.coc import state as ST
from app.coc.tests.harness import TestSuite

CFG = P.load_tuning()
TIERS = (0, 1, 2, 3, 4, 5)


def _mk(mystery_idx: int = 0):
    """档位可复现的状态：Initiate + mystery_count 5 ⇒ 每章 +20 ⇒ 档 = mystery_idx。"""
    s = ST.initial_states({"difficulty": "Initiate", "old_god": "Goizo",
                           "character": "Yashiro", "death_mode": "last_stand",
                           "seed": 7}, CFG)
    s["mystery_count"] = 5
    s["mystery_idx"] = int(mystery_idx)
    s["mystery_len"] = 6
    s["investigations_done"] = 0
    return s


def _bag(state, ids):
    state.setdefault("inventory", {"backpack": [], "storage": []})["backpack"] = list(ids)
    return state


def test_zero_impact(s: TestSuite) -> None:
    """档 0（无转变）时：属性 = 原始、无 block、惩罚 0 —— 新路径零影响。"""
    st = _bag(_mk(0), ["FireAxe", "SteakKnife", "GruesomeTotem", "Bandage"])
    s.expect("档0 progress=0", IE.progress_of(st) == 0,
             detail=f"progress={IE.progress_of(st)}",
             counterexample="mystery_idx 设成 1（或 investigations_done>0）时 progress 不为 0")
    raw = dict(st["stats"])
    for k in ("STR", "DEX", "PER", "CHA", "KNW"):
        got = CB.stat_of(st, k)
        s.expect(f"档0 {k} 有效值==原始值", got == int(raw[k]),
                 detail=f"{got} vs {raw[k]}",
                 counterexample=f"若把 tsf shift 无条件叠加（不看档位），档0 的 {k} 就会偏离原始值")
    v = ST.inventory_view(st)
    s.expect("档0 全部 block_reason 为空",
             all(it["block_reason"] == "" for it in v["backpack"]),
             detail=str([(it["id"], it["block_reason"]) for it in v["backpack"]]),
             counterexample="wear_table 的档0 若被写成 False，这里就会出现非空 block_reason")
    s.expect("档0 use_penalty == 0",
             all(it["use_penalty"] == 0 for it in v["backpack"]),
             counterexample="use_penalty 表首项若不是 0，这里会 >0")
    r = ST.equip_item(st, "FireAxe")
    s.expect("档0 双手斧可装备（放行）", r.get("ok") is True, detail=str(r),
             counterexample="item_hand_tier_max.two 若设成 0，档0 就会被拒")


def test_stat_shift_applied(s: TestSuite) -> None:
    """① `stat_of` 随档位下降；KNW 正向；下限 1。"""
    lo, hi = CB.stat_of(_mk(0), "STR"), CB.stat_of(_mk(5), "STR")
    s.expect("STR 有效值随档位下降", lo > hi, detail=f"档0={lo} 档5={hi}",
             counterexample="若 stat_of 退回读原始 stats（本次修复前的行为），档0 与档5 会相等")
    s.expect("KNW 最高档为正向修正", IE.tsf_shift_for(_mk(5), "KNW") >= 1,
             detail=f"shift={IE.tsf_shift_for(_mk(5), 'KNW')}",
             counterexample="_MECH_TABLE 的 KNW 行若全为 0，这里会是 0")
    s.expect("属性有效值下限 >= 1",
             all(CB.stat_of(_mk(m), k) >= 1 for m in TIERS
                 for k in ("STR", "DEX", "PER", "CHA", "KNW")),
             counterexample="原始属性为 1 且 shift 为 -2 时，若不做下限钳制会得到 -1")
    seq = [CB.stat_of(_mk(m), "STR") for m in TIERS]
    s.expect("STR 有效值随档位单调不增",
             all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1)), detail=str(seq),
             counterexample="_MECH_TABLE 的 STR 行若不是 (0,-1,-1,-2,-2,-2) 这种单调序列")


def test_weapon_hit_isolated(s: TestSuite) -> None:
    """① 的关键隔离：**单手武器**（不会被手限制拒装）的命中率下降，必须来自属性被压。

    若用双手 FireAxe，档 5 会被 `tsf_hand_unfit` 拒装 ⇒ `stat_value=0` ⇒
    命中的下降变成"没装备"，测的就不是属性修正了。这里显式排除该混淆。
    """
    a, b = _bag(_mk(0), ["SteakKnife"]), _bag(_mk(5), ["SteakKnife"])
    ra, rb = ST.equip_item(a, "SteakKnife"), ST.equip_item(b, "SteakKnife")
    s.expect("单手武器在档0与档5**都**能装上（排除'没装备'混淆）",
             ra.get("ok") is True and rb.get("ok") is True,
             detail=f"档0={ra.get('reason')} 档5={rb.get('reason')}",
             counterexample="若 item_hand_tier_max 也限制了 one（单手），档5 就会拒装，本用例的隔离前提即失效")
    act = {"id": "AttackWeapon", "scale_stat": "weapon"}
    enemy = {"PWR": 0, "id": "Foe"}
    ha = CB.hit_pct(a, act, enemy, cfg=CFG)
    hb = CB.hit_pct(b, act, enemy, cfg=CFG)
    da = ha[1] if isinstance(ha, tuple) else ha
    db = hb[1] if isinstance(hb, tuple) else hb
    s.expect("命中率随转变下降", da["pct"] > db["pct"],
             detail=f"档0={da['pct']} 档5={db['pct']}",
             counterexample="若 action_stat 仍读原始 stats（修复前），两档命中率相同")
    s.expect("下降来自 stat_value 变小（不是 base_hit）",
             db["stat_value"] < da["stat_value"],
             detail=f"stat_value {da['stat_value']}→{db['stat_value']}",
             counterexample="武器未装备时 stat_value 会是 0 而 base_hit 不变 —— 那正是本用例要排除的混淆")
    s.expect("tsf_shift 项真的进入命中公式",
             db["tsf_shift"] < da["tsf_shift"],
             detail=f"tsf_shift {da['tsf_shift']}→{db['tsf_shift']}",
             counterexample="若 hit_pct 不接受 stat_value/不把 shift 计入，两项都会是 0")


def test_wardrobe_evolution(s: TestSuite) -> None:
    """② 服装随阶段演化 + 装备合法性。"""
    states = [IE.wardrobe_state(_mk(m))["state"] for m in TIERS]
    s.expect("衣物状态随档位变化（≥3 种）", len(set(states)) >= 3, detail=str(states),
             counterexample="wardrobe_states 若只登记 1~2 档，这里就只有 1~2 种状态")
    s.expect("档0 衣物 normal", states[0] == "normal", detail=states[0],
             counterexample="最低档的 state 名若被改成别的（如 'none'），此断言失败")
    s.expect("档5 衣物非 normal", states[5] != "normal", detail=states[5],
             counterexample="若选取逻辑总取第一行，档5 也会是 normal")
    s.expect("档0 双手斧可装备", IE.wearable(_mk(0), "FireAxe")[0] is True,
             counterexample="item_hand_tier_max.two ≤ 0 时档0 即被拒")
    ok5, why5 = IE.wearable(_mk(5), "FireAxe")
    s.expect("档5 双手斧被拒且原因为 tsf_hand_unfit",
             (ok5, why5) == (False, "tsf_hand_unfit"), detail=f"{ok5},{why5}",
             counterexample="若 item_hand_tier_max 未生效（读不到该键），档5 会放行")
    s.expect("C 槽（护符）全程可穿",
             all(IE.wearable(_mk(m), "GruesomeTotem")[0] for m in TIERS),
             counterexample="wear_table.C 若在某档写成 False，该档会被拒")
    st5 = _bag(_mk(5), ["FireAxe", "Camera"])
    r = ST.equip_item(st5, "FireAxe")
    s.expect("档5 equip_item 真的拒绝", r.get("ok") is False, detail=str(r),
             counterexample="若 equip_item 没接 wearable 检查，这里会 ok=True（即本次修复前）")
    s.expect("拒绝原因可读（tsf_hand_unfit）", r.get("reason") == "tsf_hand_unfit",
             detail=str(r.get("reason")),
             counterexample="若把原因统一成 'not_equippable'，玩家无法区分'不能用'与'不合身'")
    s.expect("档5 C 槽仍可装备（非一刀切拒绝）",
             ST.equip_item(st5, "Camera").get("ok") is True,
             counterexample="若 wearable 对任何物品都返回 False（写死），C 槽也会被拒")


def test_inventory_effects(s: TestSuite) -> None:
    """③ 背包可用性真正生效。"""
    st = _bag(_mk(4), ["Bandage", "OccultDiary", "FireAxe", "SteakKnife",
                       "GruesomeTotem", "Map"])
    v = ST.inventory_view(st)
    bag = v["backpack"]
    s.expect("每件物品带 wearable", all("wearable" in it for it in bag),
             counterexample="若 inventory_view 仍用基础 inventory_list（修复前），这些键都不存在")
    s.expect("每件物品带 block_reason", all("block_reason" in it for it in bag),
             counterexample="同上：未接 item_effects 时无此键")
    s.expect("每件物品带 use_penalty", all("use_penalty" in it for it in bag),
             counterexample="同上")
    s.expect("视图带 tsf_mech 汇总", "tsf_mech" in v, detail=str(list(v)),
             counterexample="若 mech_view 抛异常被吞，此键会缺失")
    s.expect("tsf_mech 反映当前档位", (v.get("tsf_mech") or {}).get("tier") == 4,
             detail=str(v.get("tsf_mech")),
             counterexample="若档位换算用了别的除数（不是 20），档位不会是 4")
    axe = [it for it in bag if it["id"] == "FireAxe"][0]
    s.expect("档4 双手斧 block_reason=tsf_hand_unfit",
             axe["block_reason"] == "tsf_hand_unfit", detail=str(axe),
             counterexample="item_hand_tier_max.two 若 > 4（如 6），档4 不会被拒")
    knife = [it for it in bag if it["id"] == "SteakKnife"][0]
    s.expect("档4 单手武器不被手限制拦", knife["block_reason"] == "",
             detail=str(knife.get("block_reason")),
             counterexample="若 item_hand_tier_max 也登记了 'one'，单手武器会被误拒")
    band = [it for it in bag if it["id"] == "Bandage"][0]
    s.expect("消耗品不占槽 ⇒ wearable=True", band["wearable"] is True,
             counterexample="若 wearable 对没有 equip_slot 的物品也去查槽位表，会误判")
    s.expect("档4 使用惩罚 > 0", bag[0]["use_penalty"] > 0,
             detail=str(bag[0]["use_penalty"]),
             counterexample="use_penalty 表若全 0，此处为 0")
    txt = ST.dumps(st)
    back = ST.loads(txt)
    same, why = ST.state_equal(st, back)
    s.expect("带物品机制的 state 存档往返一致", same is True, detail=str(why),
             counterexample="若新字段被写进 state 本体（而非仅在视图层计算），往返可能不一致")


def test_numeric_discipline(s: TestSuite) -> None:
    """新增数值必须全部来自 tuning.json；且带 _source（COC-DESIGN §4.2 强制格式）。"""
    raw = P.t(CFG, "coc_tsf_items")
    s.expect("tuning 存在 coc_tsf_items 段", isinstance(raw, dict),
             counterexample="tuning.json 未加入该段时 raw 为 None/缺失")
    for key in ("wear_table", "item_hand_tier_max", "use_penalty", "wardrobe_states"):
        node = raw.get(key) if isinstance(raw, dict) else None
        s.expect(f"{key} 带 _source", isinstance(node, dict) and bool(node.get("_source")),
                 detail=str(type(node).__name__),
                 counterexample=f"若 {key} 直接写成裸值（不带包装），_source 就取不到 —— 违反溯源纪律")
        s.expect(f"{key} 带 _unverified 标注",
                 isinstance(node, dict) and "_unverified" in node,
                 counterexample=f"{key} 若省略 _unverified，无法区分'已查实'与'本项目设计'")
    s.expect("use_penalty 长度为 6（与六档对齐）",
             len(IE._seg(CFG).get("use_penalty") or []) == 6,
             detail=str(IE._seg(CFG).get("use_penalty")),
             counterexample="若只写 3 个值，高档位会越界取到末项（静默错档）")
    s.expect("wardrobe_states 覆盖 6 档",
             len(IE._seg(CFG).get("wardrobe_states") or []) == 6,
             counterexample="少于 6 档时高档位会沿用较低档的衣物描述")


def test_time_cost_penalty(s: TestSuite) -> None:
    """`use_penalty` 真的进战斗耗时（t71 第二处机制落点）。

    用真实动作表里的 `Kick`（base_time=80、scale_coef=0 ⇒ 无属性缩放，
    因此耗时的变化**只可能**来自 use_penalty，与属性修正无关 —— 这是隔离）。
    """
    from app.coc import combat as _CB
    kick = None
    try:
        idx = _CB.action_index()
        kick = idx.get("Kick") if isinstance(idx, dict) else None
    except Exception:
        kick = None
    s.expect("取到 Kick 动作（base_time/scaling 已知）", isinstance(kick, dict),
             detail=str(type(kick).__name__),
             counterexample="动作表若把 id 从 'Kick' 改名，这里取不到（测试应随之更新）")
    if not isinstance(kick, dict):
        return
    c0 = CB.time_cost(kick, _mk(0), cfg=CFG)
    c5 = CB.time_cost(kick, _mk(5), cfg=CFG)
    s.expect("档0 耗时不受 use_penalty 影响", c0 == int(kick.get("base_time") or 0),
             detail=f"档0={c0} base={kick.get('base_time')}",
             counterexample="use_penalty 表首项若不是 0，档0 的耗时也会被加上去")
    s.expect("档5 耗时比档0 长（惩罚真的进了结算）", c5 > c0,
             detail=f"档0={c0} 档5={c5}",
             counterexample="若 time_cost 没接 use_penalty（本次修复前），两档耗时完全相同")
    s.expect("档5 多出的量 == use_penalty[5]",
             c5 - c0 == IE._seg(CFG).get("use_penalty", [])[5],
             detail=f"差={c5 - c0}",
             counterexample="若把 use_penalty 乘了某个系数，差值就不再等于表值")


def main() -> int:
    s = TestSuite("app/coc/tests/test_tsf_items.py —— TSF×CoC 物品/装备/背包机制")
    for fn in (test_zero_impact, test_stat_shift_applied, test_weapon_hit_isolated,
               test_wardrobe_evolution, test_inventory_effects, test_numeric_discipline,
               test_time_cost_penalty):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

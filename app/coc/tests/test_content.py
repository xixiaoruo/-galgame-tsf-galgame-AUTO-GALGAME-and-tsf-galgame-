"""内容层单测 —— 事件卡 schema、平衡约束、成年白名单（G1 的数据面）。

对应设计：`COC-DESIGN.md` §4.1 事件卡 JSON Schema、§2.5 抽取、§2.6 战斗平衡、
§5.3 G1 成年闸门；上游：`MECHANICS.md` §4.1（1~4 个选项）、§4.3（正文 2~4 句）、
§9.4 坑 1/2（真实差评：重复劳作、战斗拖沓）。

**schema 校验是手写的**（不引 jsonschema 依赖）：本任务的价值主张是"零依赖即可验证"，
所以只实现本模式真正需要的字段/取值约束，并保证每条约束都有反例。
"""
from __future__ import annotations

import os
import re
import sys

from app.coc import content as C
from app.coc import paths
from app.coc import provenance as P
from app.coc.tests.harness import TestSuite

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ID_RE = re.compile(r"^C-\d{3,4}$")
STAT_NAMES = {"STR", "DEX", "PER", "KNW", "CHA"}
THREATS = {"SAFE", "LOW", "DANGEROUS", "DOOMED"}
TAG_KINDS = {"injury", "curse", "town"}


def validate_card(card: dict, loc_ids: set[str], item_ids: set[str],
                  mystery_ids: set[str]) -> list[str]:
    """手写 schema 校验，返回问题列表（空 = 合法）。"""
    errs: list[str] = []

    def need(key, typ, pred=None, msg=""):
        v = card.get(key)
        if not isinstance(v, typ) or (pred and not pred(v)):
            errs.append(f"{key}: {msg or '类型/取值不符'}（实际 {v!r}）")

    need("id", str, lambda v: bool(ID_RE.match(v)), "须匹配 ^C-\\d{3,4}$")
    locs = card.get("locations")
    if not isinstance(locs, list) or not (1 <= len(locs) <= 6):
        errs.append(f"locations: 须是 1~6 个地点的列表（实际 {locs!r}）")
    else:
        for loc in locs:
            if loc not in loc_ids:
                errs.append(f"locations: 未知地点 {loc!r}")
    if card.get("mystery_only") and card["mystery_only"] not in mystery_ids:
        errs.append(f"mystery_only: 未知谜团 {card['mystery_only']!r}")

    body = card.get("body")
    if not isinstance(body, str) or not (20 <= len(body) <= 600):
        errs.append(f"body: 长度须在 20~600（实际 {len(body) if isinstance(body, str) else type(body)}）")

    ch = card.get("choices")
    if not isinstance(ch, list) or not (1 <= len(ch) <= 4):
        errs.append(f"choices: 须是 1~4 个（MECHANICS.md §4.1『1 到 4 个选项』，实际 "
                    f"{len(ch) if isinstance(ch, list) else type(ch)}）")
    else:
        for i, c in enumerate(ch):
            if not isinstance(c, dict) or not str(c.get("text", "")).strip():
                errs.append(f"choices[{i}].text 必填")
                continue
            if len(str(c["text"])) > 60:
                errs.append(f"choices[{i}].text 超长（{len(str(c['text']))}）")
            spec = c.get("check")
            if spec is not None:
                if not isinstance(spec, dict):
                    errs.append(f"choices[{i}].check 须是对象或 null")
                else:
                    if spec.get("stat") not in STAT_NAMES:
                        errs.append(f"choices[{i}].check.stat 非法：{spec.get('stat')!r}")
                    if spec.get("threat") not in THREATS:
                        errs.append(f"choices[{i}].check.threat 非法：{spec.get('threat')!r}")
                    dc = spec.get("dc", 0)
                    if not isinstance(dc, int) or not (-6 <= dc <= 6):
                        errs.append(f"choices[{i}].check.dc 越界：{dc!r}")
            for branch in ("success", "failure"):
                out = c.get(branch)
                if out is None:
                    continue
                if not isinstance(out, dict):
                    errs.append(f"choices[{i}].{branch} 须是对象")
                    continue
                for k in ("doom", "sta", "rea", "exp", "funds"):
                    if k in out and not isinstance(out[k], int):
                        errs.append(f"choices[{i}].{branch}.{k} 须是整数")
                for k in ("item_grant", "item_take"):
                    if k in out:
                        if not isinstance(out[k], list):
                            errs.append(f"choices[{i}].{branch}.{k} 须是列表")
                        else:
                            for it in out[k]:
                                if it not in item_ids:
                                    errs.append(f"choices[{i}].{branch}.{k}: 未登记物品 {it!r}")
                if "tags_grant" in out:
                    for tid in out["tags_grant"]:
                        if not isinstance(tid, str):
                            errs.append(f"choices[{i}].{branch}.tags_grant 元素须是 id 字符串"
                                        f"（实际 {tid!r}）")
                if "combat" in out:
                    cb = out["combat"]
                    if not isinstance(cb, dict) or not cb.get("enemy"):
                        errs.append(f"choices[{i}].{branch}.combat 缺 enemy")
    if not card.get("_source"):
        errs.append("_source 缺失")
    return errs


def test_card_schema(s: TestSuite) -> None:
    cards = C.cards()
    loc_ids = {l["id"] for l in C.locations()}
    item_ids = {i["id"] for i in C.items()}
    mystery_ids = {m["id"] for m in C.mysteries()}
    bad = {c.get("id", "?"): validate_card(c, loc_ids, item_ids, mystery_ids) for c in cards}
    bad = {k: v for k, v in bad.items() if v}
    s.expect(
        f"全部 {len(cards)} 张事件卡通过 schema 校验",
        cards and not bad,
        detail=f"卡数 {len(cards)}（4 地点 × 6）；不合法的卡 {len(bad)} 张"
               + (f"：{bad}" if bad else ""),
        counterexample="反例：把某张卡的 choices 加到 5 个（超过 §4.1 的 1~4）→ 该卡进 bad → 变红。"
                       "**这条必须扫全部卡**：只挑一张「看起来对」的卡来断言，"
                       "等于没测另外 23 张",
        demonstration=f"实测：bad={list(bad)}",
    )
    s.expect(
        "卡 id 全局唯一且形如 C-nnn",
        len({c["id"] for c in cards}) == len(cards) and all(ID_RE.match(c["id"]) for c in cards),
        detail=f"唯一 id {len({c['id'] for c in cards})} / 总数 {len(cards)}；"
               f"样例 {sorted(c['id'] for c in cards)[:4]}",
        counterexample="反例：把两张卡都命名成 C-101 → 唯一性断言失败 → 变红",
    )
    s.expect(
        "每张卡的 locations 都在 locations.json 里登记（抽取才能命中）",
        all(set(c["locations"]) <= loc_ids for c in cards),
        detail=f"登记地点 {sorted(loc_ids)}；"
               f"每卡地点数 "
               f"{sorted({len(c['locations']) for c in cards})}",
        counterexample="反例：某卡写 locations=['Rooftop'] 而地点表没有 Rooftop → 校验失败 → 变红。"
                       "（正文能写任何地方，但 locations 必须可解析）",
    )


def test_schema_rejects_broken_cards(s: TestSuite) -> None:
    """★ 反例法：逐条破坏，确认校验器**真的**会报错（而不是永远返回空列表）。

    这是本文件最关键的一条：如果 validate_card 永远返回 []，上面那条"全部通过"
    就成了废话。所以这里对**每一条**约束各造一个反例。
    """
    cards = C.cards()
    base = dict(cards[0])
    loc_ids = {l["id"] for l in C.locations()}
    item_ids = {i["id"] for i in C.items()}
    mystery_ids = {m["id"] for m in C.mysteries()}

    import copy
    mutations = {
        "选项数 5 个（超过 1~4）": lambda c: c.__setitem__("choices", c["choices"] * 5),
        "选项数 0 个": lambda c: c.__setitem__("choices", []),
        "正文过短（<20 字）": lambda c: c.__setitem__("body", "太短"),
        "正文过长（>600 字）": lambda c: c.__setitem__("body", "长" * 700),
        "未知地点": lambda c: c.__setitem__("locations", ["Nowhere"]),
        "id 格式非法": lambda c: c.__setitem__("id", "X-1"),
        "检定属性非法": lambda c: (c["choices"][0].__setitem__(
            "check", {"stat": "LUCK", "threat": "SAFE", "dc": 0})),
        "威胁等级非法": lambda c: (c["choices"][0].__setitem__(
            "check", {"stat": "KNW", "threat": "SPICY", "dc": 0})),
        "dc 越界": lambda c: (c["choices"][0].__setitem__(
            "check", {"stat": "KNW", "threat": "SAFE", "dc": 99})),
        "未登记物品": lambda c: (c["choices"][0].setdefault("success", {}).__setitem__(
            "item_grant", ["NotAnItem"])),
        "缺 _source": lambda c: c.pop("_source", None),
        "选项文本为空": lambda c: c["choices"][0].__setitem__("text", "   "),
        "谜团 id 非法": lambda c: c.__setitem__("mystery_only", "MY-99"),
    }
    undetected = []
    for label, mutate in mutations.items():
        c = copy.deepcopy(base)
        mutate(c)
        if not validate_card(c, loc_ids, item_ids, mystery_ids):
            undetected.append(label)
    s.expect(
        f"逐条破坏 {len(mutations)} 种非法输入，校验器**每一种都报错**",
        not undetected,
        detail=f"破坏方式 {len(mutations)} 种；未被检出 {len(undetected)} 种"
               + (f"：{undetected}" if undetected else "（全部被检出）"),
        counterexample="反例：把 validate_card 里 body 长度那段删掉 → 「正文过短/过长」两种会变成"
                       "未检出 → undetected 非空 → 变红",
        demonstration=f"实测：undetected={undetected}",
    )


def test_balance_invariants(s: TestSuite) -> None:
    """§2.6 的硬性平衡约束 + §9.4 坑 2（战斗拖沓）的机读化。"""
    enemies = C.enemies()
    non_boss_bad = [e["id"] for e in enemies
                    if not e.get("boss") and int(e.get("min_turns_to_kill", 99)) > 2]
    s.expect(
        "非 Boss 敌人 min_turns_to_kill ≤ 2（§9.4 坑 2『战斗拖沓』的机读化）",
        not non_boss_bad,
        detail=f"敌人 {len(enemies)} 只；非 Boss 超标的 {non_boss_bad}",
        counterexample="反例：把某个杂兵写成 min_turns_to_kill=8 → 进 bad → 变红。"
                       "上游 §5.6 设计建议：『不要给每个杂兵都设计 8 步编排，"
                       "普通遭遇应能在 1–2 回合内解决』",
    )
    has_incorporeal = [e["id"] for e in enemies if "incorporeal" in (e.get("traits") or [])]
    s.expect(
        "存在幽灵类敌人（incorporeal）—— §5.4 的技能克制是零成本高收益机制",
        len(has_incorporeal) > 0,
        detail=f"幽灵敌人 {len(has_incorporeal)} 只：{has_incorporeal}",
        counterexample="反例：把全部敌人去掉 incorporeal → 幽灵免疫普通攻击这条机制就没有载体 "
                       "→ 变红",
    )

    # DOOM 不对称：上涨总量必须远大于单章减免（§2.5 设计建议「扣钱容易，赚钱极难」）
    cfg = P.load_tuning()
    per_inv = int(P.t(cfg, "doom_up.investigate"))
    length = int(P.t(cfg, "mystery.len_default"))
    relief = int(P.t(cfg, "difficulty.Initiate.doom_relief_per_mystery"))
    s.expect(
        "DOOM 不对称：一章的调查上涨总量 > 该章减免（否则游戏越玩越轻松）",
        per_inv * length > relief,
        detail=f"每调查 +{per_inv} × 每章 {length} 次 = +{per_inv * length}；"
               f"章完成 -{relief} → 净 +{per_inv * length - relief}",
        counterexample=f"反例：把 doom_relief_per_mystery 提到 {per_inv * length + 5} → "
                       f"章节净收益变负、DOOM 越玩越低 → 本断言变红。"
                       f"（§2.1 的下降途径全表加起来的量级远小于上涨）",
    )


def test_adult_gate_data(s: TestSuite) -> None:
    """G1 的数据面：成年白名单必须存在且每个条目都显式标 adult。"""
    cast = C.cast()
    s.expect(
        "cast.json 里每个角色都显式标 age_flag=adult",
        cast and all(c.get("age_flag") == "adult" for c in cast.values()),
        detail=f"白名单 {len(cast)} 人："
               f"{sorted((n, c.get('age_flag')) for n, c in cast.items())}",
        counterexample="反例：加一个不带 age_flag 的角色 → 断言失败 → 变红。"
                       "COC-DESIGN §5.3 G1 要求「不在此表内即拒」，"
                       "所以缺失必须表现为**拒绝**而不是**默认放行**",
    )
    missing = [n for n, c in cast.items() if not str(c.get("age_flag", "")).strip()]
    s.expect(
        "没有 age_flag 缺失的条目（缺失=未登记=应被 G1 拒绝）",
        not missing,
        detail=f"缺 age_flag 的 {len(missing)} 个：{missing}",
        counterexample="反例：把 age_flag 键删掉一个 → 该角色会落进'未登记'，"
                       "而任何按 `c.get('age_flag') == 'adult'` 判定的闸门都会拒它 → 变红",
    )


def test_erotic_rule_layer_structure(s: TestSuite) -> None:
    """双轴三档的**结构**必须就位（本任务只做结构，不做文本；闸门验收在接线任务）。"""
    cfg = P.load_tuning()
    et = P.t(cfg, "erotic.erotic_tiers")
    ht = P.t(cfg, "erotic.horror_tiers")
    s.expect(
        "情色三档 = [subtle, intense, explicit]",
        et == ["subtle", "intense", "explicit"],
        detail=f"erotic_tiers={et}",
        counterexample="反例：只定义两档（删掉 explicit）→ 变红。"
                       "COC-DESIGN §5.2 是**双轴 × 三档**，"
                       "单轴表达不出「低理智 × 清醒亲密」这类组合，这正是保留双轴的理由",
    )
    s.expect(
        "恐怖三档 = [dread, visceral, cosmic]（本模式新增的第二轴）",
        ht == ["dread", "visceral", "cosmic"],
        detail=f"horror_tiers={ht}",
        counterexample="反例：把恐怖档删掉、只留情色档 → 变红（就退回「只沿用现有分级注入」了，"
                       "而用户明确要求**新增**专用规则层）",
    )
    th = P.t(cfg, "erotic.horror_tier_thresholds")
    s.expect(
        "恐怖档的生效阈值与 sta/rea/doom 挂钩（不是凭空的档位）",
        th["visceral"]["doom_min"] == 40 and th["cosmic"]["doom_min"] == 75
        and th["cosmic"]["rea_max"] == 9,
        detail=f"thresholds={th}",
        counterexample="反例：把 cosmic 的条件删掉 → 恐怖档永远不升级 → 变红。"
                       "阈值对齐 MECHANICS.md §3.2（REA ≤9 立绘变阴郁）",
    )
    s.expect(
        "保守档钳制值已登记（温和档 → 强制 subtle，只降不升）",
        P.t(cfg, "erotic.conservative_intensity") == "温和"
        and P.t(cfg, "erotic.gate_rating_required") == "18",
        detail=f"conservative_intensity={P.t(cfg, 'erotic.conservative_intensity')!r}；"
               f"gate_rating_required={P.t(cfg, 'erotic.gate_rating_required')!r}",
        counterexample="反例：把 conservative_intensity 改成「浓烈」→ 保守档不再收紧 → 变红。"
                       "G3 的语义是**只能降不能升**（COC-DESIGN §5.3）",
    )


def main() -> int:
    s = TestSuite("app/coc/tests/test_content.py —— 卡 schema / 反例检验 / 平衡约束 / 成年白名单")
    for fn in (test_card_schema, test_schema_rejects_broken_cards,
               test_balance_invariants, test_adult_gate_data,
               test_erotic_rule_layer_structure):
        s.run(fn)
    return s.report()


if __name__ == "__main__":
    sys.exit(main())

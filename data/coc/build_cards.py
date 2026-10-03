# -*- coding: utf-8 -*-
"""一次性生成 data/coc/cards/*.json（24 张事件卡）。

为什么用脚本生成而不是手写 24 段 JSON：
  - 结构完全同形，手写 24 遍只会引入不一致；
  - 便于把"每张卡都带 _source"变成循环里的断言，而不是靠人眼检查。

用法：python build_cards.py   （幂等：覆盖写 cards/*.json）
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR = os.path.join(HERE, "cards")

SRC_BASE = ("MECHANICS.md §4.1 事件结构（一小段描述 + 1~4 个选项 + 脚本化结果）、"
            "§4.3 建议 schema {id, locations, requirements, body, choices[{text, check, success, failure}]}；"
            "§7.2 Steam 官方把游戏定义为“一套事件卡组”（一手证据，可信度高）。"
            "卡面文本为本项目新写（不照搬上游任何卡片原文）。")

# 24 张卡：4 地点 × 6
SPEC = [
    # ---------------- Downtown（商店街） ----------------
    ("C-101", "Downtown", "SAFE", "check", "KNW",
     "橱窗里那排模型换了位置。你确定昨天那个穿校服的还在最左边。",
     [("凑近看玻璃后面的价签", "KNW", {"exp": 5}, {"rea": -1}, None),
      ("推门进去问店员", None, {"funds": -1, "exp": 3}, {"doom": 1}, None),
      ("记下来，转身走开", None, {"exp": 2}, {}, None)]),
    ("C-102", "Downtown", "SAFE", "plain",
     "收银台后面的老人一直在数同一叠零钱，数到第七张就开始重来。",
     [("问他今天几号", None, {"exp": 3}, {}, None),
      ("买一包烟", None, {"funds": -1}, {"exp": 2}, None)]),
    ("C-103", "Downtown", "DANGEROUS", "combat",
     "巷口有个人背对着你站着。他的肩膀动得不太对，像是有人在里面数拍子。",
     [("先出声", None, {"flags": {"alerted": True}}, {"combat": "Thin Man"}, "Thin Man"),
      ("绕路", None, {"doom": 1}, {}, None)]),
    ("C-104", "Downtown", "SAFE", "check", "PER",
     "电线杆上贴着一张寻人启事。照片被人用指甲抠过，只剩下一个模糊的轮廓。",
     [("把启事揭下来", "PER", {"item_grant": ["Map"], "exp": 5}, {"sta": -1}, None),
      ("拍照存档", None, {"exp": 3}, {}, None)]),
    ("C-105", "Downtown", "SAFE", "plain",
     "自动售货机的灯牌闪了一下。你按了两次同样的键，掉出来的是同一个牌子的两罐。",
     [("喝掉一罐", None, {"funds": -1}, {"sta": 1}, None),
      ("把它扔进垃圾桶", None, {}, {}, None)]),
    ("C-106", "Downtown", "SAFE", "check", "CHA",
     "街角蹲着一个女孩，正用粉笔在地上画一扇门。她画得很仔细，连门轴都画了。",
     [("和她聊两句", "CHA", {"exp": 8, "flags": {"met_girl": True}}, {"rea": -2}, None),
      ("从她旁边绕过去", None, {}, {"doom": 1}, None)]),

    # ---------------- Apartments（公寓） ----------------
    ("C-201", "Apartments", "SAFE", "check", "KNW",
     "你出门前明明锁了门。回来的时候，锁是从里面反锁的。",
     [("贴着门听里面的动静", "KNW", {"exp": 6}, {"rea": -2}, None),
      ("找管理员要备用钥匙", None, {"funds": -1, "exp": 3}, {"doom": 1}, None),
      ("坐在楼梯上等", None, {"exp": 2, "doom": 1}, {}, None)]),
    ("C-202", "Apartments", "SAFE", "plain",
     "水槽下面的柜子里多了一双鞋。尺码不是你的，也不是家里任何一个人的。",
     [("拿去扔掉", None, {"exp": 4}, {"doom": 1}, None),
      ("留在原地不动它", None, {"tags_grant": ["Fearful"]}, {}, None)]),
    ("C-203", "Apartments", "DANGEROUS", "combat",
     "隔壁传来的声音像有人在来回踱步，但步子踩得太密，密到不可能是两条腿。",
     [("敲门", None, {}, {"combat": "Roommate"}, "Roommate"),
      ("锁上自己的门，不出声", None, {"doom": 2}, {}, None)]),
    ("C-204", "Apartments", "SAFE", "check", "STR",
     "衣柜后面露出一截墙纸，颜色比周围新。你试着推了一下，它动了。",
     [("用力把它整块掀开", "STR", {"item_grant": ["OccultDiary"], "exp": 8}, {"sta": -2}, None),
      ("只用手指抠开一角", None, {"exp": 3}, {"sta": -1}, None)]),
    ("C-205", "Apartments", "SAFE", "plain",
     "冰箱里那盒牛奶是你上周买的。日期还好好的。你记得自己已经扔过它一次。",
     [("倒掉", None, {"exp": 2}, {}, None),
      ("打开闻一下", None, {"exp": 3}, {"sta": -1}, None)]),
    ("C-206", "Apartments", "SAFE", "check", "PER",
     "天花板上有一小片水渍，形状和昨天不一样了。",
     [("站到正下方仔细看", "PER", {"exp": 6}, {"rea": -1}, None),
      ("搬开床，不管它", None, {}, {}, None)]),

    # ---------------- School（学校） ----------------
    ("C-301", "School", "LOW", "check", "KNW",
     "教室后面的课程表被撕掉了一角。缺的正好是星期三下午。",
     [("去问值班老师", "KNW", {"exp": 6, "flags": {"asked_teacher": True}}, {"doom": 1}, None),
      ("翻讲台抽屉找备份", None, {"item_grant": ["Bandage"], "exp": 4}, {"sta": -1}, None)]),
    ("C-302", "School", "LOW", "plain",
     "操场上没有人，但秋千在动，幅度稳定，像被人推着。",
     [("走过去数荡了几次", None, {"exp": 5, "doom": 1}, {}, None),
      ("从教学楼另一侧离开", None, {"doom": 1}, {}, None)]),
    ("C-303", "School", "DANGEROUS", "combat",
     "厕所最里面那间隔间的门虚掩着。里面的光比外面亮。",
     [("推门", None, {}, {"combat": "Toilet Ghost"}, "Toilet Ghost"),
      ("洗手走人", None, {"exp": 2}, {}, None)]),
    ("C-304", "School", "LOW", "check", "DEX",
     "走廊尽头的窗户开着一道缝，风从那边吹过来，带着水汽。三楼不该有这种味道。",
     [("伸手把窗推上", "DEX", {"exp": 5}, {"sta": -1, "rea": -1}, None),
      ("让它开着", None, {"doom": 1}, {}, None)]),
    ("C-305", "School", "LOW", "plain",
     "图书室的借阅卡上，有一本书被借走过四次，每次都是同一天。",
     [("去找那本书", None, {"item_grant": ["DustyGrimoire"], "exp": 6}, {"doom": 1}, None),
      ("把卡片放回去", None, {"exp": 2}, {}, None)]),
    ("C-306", "School", "LOW", "check", "CHA",
     "天台上坐着个女生，背对着栏杆。她说她能告诉你该怎么拍手。",
     [("听她说完", "CHA", {"flags": {"ritual_hint": True}, "exp": 8}, {"doom": 1}, None),
      ("说不用了", None, {}, {"rea": -1}, None)]),

    # ---------------- Hospital（医院） ----------------
    ("C-401", "Hospital", "LOW", "check", "KNW",
     "挂号处的号机自己吐了一张票，号码是 000。",
     [("拿着票去诊室", "KNW", {"exp": 5}, {"sta": -1}, None),
      ("把票撕了", None, {"exp": 2}, {"doom": 1}, None)]),
    ("C-402", "Hospital", "LOW", "plain",
     "走廊尽头的长椅上放着一件叠好的外套。它的袖口是湿的。",
     [("翻一下口袋", None, {"item_grant": ["MedicalKit"], "exp": 4}, {"doom": 1}, None),
      ("原样放好", None, {}, {}, None)]),
    ("C-403", "Hospital", "DANGEROUS", "combat",
     "药房门上的玻璃从里面蒙了一层白雾。里面有人影在数药瓶，数得很慢。",
     [("推门进去", None, {}, {"combat": "Doll Nurse"}, "Doll Nurse"),
      ("回头叫护士", None, {"funds": -1}, {"sta": 1}, None)]),
    ("C-404", "Hospital", "LOW", "check", "PER",
     "观察窗后面那间屋子亮着灯，但登记本上写着这一层今天停用。",
     [("贴着窗往里看", "PER", {"exp": 6}, {"rea": -2}, None),
      ("记下房间号就走", None, {"exp": 3}, {}, None)]),
    ("C-405", "Hospital", "LOW", "plain",
     "自动贩卖机里的咖啡是热的。你投币之前它就是热的。",
     [("投币买一杯", None, {"funds": -1, "sta": 1}, {}, None),
      ("摸一下出货口的温度", None, {"exp": 3}, {"rea": -1}, None)]),
    ("C-406", "Hospital", "LOW", "check", "STR",
     "消防通道的门被从里面顶住了。门缝里有头发。",
     [("用力顶开", "STR", {"exp": 8, "item_grant": ["BronzeCrucifix"]}, {"sta": -2}, None),
      ("放弃，走楼梯", None, {"doom": 1}, {}, None)]),
]


def build_card(cid, loc, threat, *rest):
    """SPEC 元组 → 一张卡（**支持两种书写形态**）。

    形态 A（7 字段，含主检定属性）：`(id, loc, threat, kind, stat, body, choices)`
    形态 B（6 字段，非检定卡）  ：`(id, loc, threat, kind, body, choices)`

    为什么允许两种：`stat` 只对 `kind=check` 有意义，对 plain/combat 卡写个占位
    `None` 只会让人怀疑"这里是不是漏填了"。**函数按 `len(rest)` 分派**，
    并把"第 4 位是不是 choices 列表"作为结构断言 —— 这样形态写错会立刻炸，
    而不是把正文当成 choices 静默塞进去。

    `kind` 与 `stat` 是**书写时便于人读**的分类标签，不进入卡片 JSON
    —— schema 里检定属性只出现在 `choices[].check.stat`。
    """
    if len(rest) == 4:
        kind, stat, body, choices = rest
    elif len(rest) == 3:
        kind, body, choices = rest
        stat = None
    else:
        raise TypeError(f"{cid}: SPEC 必须是 6 或 7 字段，实际 {len(rest) + 3}")
    if not isinstance(choices, (list, tuple)):
        raise TypeError(f"{cid}: 最后一个字段必须是 choices 列表，实际 {type(choices).__name__}")
    if kind == "check" and not stat:
        raise TypeError(f"{cid}: kind=check 必须给出主检定属性")

    # 选项四元组：(选项文本, 检定属性或 None, 成功结果 dict, 失败结果 dict)
    #
    # **注意**：SPEC 里每项写的是 5 个值，但**第 5 个位置是留给人的可读备注**（历史上
    # 想表达"这个分支触发战斗"）；战斗已在第 4 个 dict 里用 `"combat": "敌人id"` 表达。
    # 所以这里用 `*rest5` 接住 4~5 个值，只取前四个 —— 否则
    # `combat` 会意外吃掉整个结果 dict（本文件第一版就是这么错的，
    # 产出的卡出现 `"failure": {"combat": ...}` 被 schema 校验正确地拒掉）。
    out_choices = []
    for ch in choices:
        if not (4 <= len(ch) <= 5):
            raise TypeError(f"{cid}: 选项必须是 4 或 5 元组，实际 {len(ch)}")
        text, check_stat, success, failure = ch[0], ch[1], ch[2], ch[3]
        c = {"text": text}
        if check_stat:
            c["check"] = {"stat": check_stat, "threat": threat, "dc": 0}
        # 只在**真的有内容**时才写 success/failure：
        # 空对象 {} 会被卡 schema 判为非法结果段（空对象没有语义，
        # 「什么也不发生」应当由字段缺省表达，不该留空壳）。
        for branch, out in (("success", success), ("failure", failure)):
            if not out:
                continue
            out = dict(out)
            # 把可读形态 `"combat": "<敌人id>"` 归一成 schema 形态
            # `{"enemy": id, "is_boss": bool}`（COC-DESIGN §4.1 choice.outcome.combat）
            cb = out.get("combat")
            if isinstance(cb, str):
                out["combat"] = {"enemy": cb, "is_boss": cb in ("Thin Man", "Doll Nurse")}
            c[branch] = out
        out_choices.append(c)
    return {
        "id": cid,
        "title": "",
        "pack": "base",
        "locations": [loc],
        "mystery_only": None,
        "weight": 10,
        "requirements": {},
        "body": body,
        "choices": out_choices,
        "erotic_hook": None,
        "_source": SRC_BASE,
        "_unverified": True,
    }


def main():
    os.makedirs(CARDS_DIR, exist_ok=True)
    by_loc = {}
    for spec in SPEC:
        cid, loc = spec[0], spec[1]
        card = build_card(*spec)
        assert card["_source"] and card["_unverified"] is True, cid
        assert 1 <= len(card["choices"]) <= 4, cid
        assert 20 <= len(card["body"]) <= 600, (cid, len(card["body"]))
        by_loc.setdefault(loc, []).append(card)

    total = 0
    for loc, cards in sorted(by_loc.items()):
        path = os.path.join(CARDS_DIR, f"{loc.lower()}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"_source": SRC_BASE, "cards": cards}, fh,
                      ensure_ascii=False, indent=1)
            fh.write("\n")
        total += len(cards)
        print(f"wrote {os.path.basename(path)}: {len(cards)} cards")
    print("total cards:", total)


if __name__ == "__main__":
    main()

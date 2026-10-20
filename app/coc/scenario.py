"""自定义剧本（scenario）：**让玩家的世界观/大纲真正决定地点、谜团、古神与事件卡**。

## 为什么需要这个模块

在它之前，CoC 系（`coc` / `coc_tsf`）的场景**完全来自随包的固定内容**
（`data/coc/locations.json` 的 4 个地点、`mysteries.json` 的 2 个谜团、`old_gods.json` 的 2 位古神、
`cards/*.json` 的卡池）。`app/coc/*.py` 对 `world` / `outline` / `lorebook` 的引用数是 **0** ——
玩家在开局界面填的世界观**根本没有任何输入通道**，所以"完全不遵循背景剧情"。

本模块给出一条**显式通道**：把玩家给的剧本收进 `session["scenario"]`，
构造 `Run` 时用它替换那几张表（**缺失的表逐项回落到随包内容**，所以"不填 = 现行行为完全不变"）。

## 分层（与 COC-DESIGN 的骨架/外壳一致）

```
骨架层（机制，仍由本项目决定）  : DOOM/STA/REA、d12 属性检定、时间槽战斗、sanitize 白名单
内容层（**这里可以整体替换**）  : locations / mysteries / old_gods / cards
外壳层（叙述）                  : world / outline / storybook 文本注入 prompt
```

数值与判定**一律不受剧本影响**：剧本只能换"有哪些地点、哪些谜团、哪些古神、哪些事件卡"，
不能改 `tuning.json` 里的任何数值 —— 这是与既有纪律一致的硬边界。

## 剧本 JSON 形状（全部字段可选；缺哪张表就回落随包内容）

```jsonc
{
  "title":   "星冠学园",
  "world":   "世界观文本（注入 prompt）",
  "outline": "故事大纲（注入 prompt）",
  "storybook": ["故事书条目1", "..."],
  "directives": ["指令1", "..."],
  "locations": [ {"id":"Dorm","name":"女生宿舍","threat":"SAFE","home":true} ],
  "mysteries": [ {"id":"MY-X","name":"夜里的点名","locations":["Dorm"],"free_roam":true} ],
  "old_gods":  [ {"id":"TheWatcher","name":"观者","global_rule":"none"} ],
  "cast":      [ {"name":"Kirie","display":"白河心春","age_flag":"adult","role":"investigator"} ],
  "cards":     [ {"id":"...","body":"...","choices":[...]} ]
}
```

`threat` 必须落在 `dice.THREATS` 内；`cards` 的 schema 由 `content` 的既有校验负责（本模块只做轻校验）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import content
from .dice import THREATS

#: 会被剧本替换的几张表（键名 → 随包默认来源函数名）
_REPLACEABLE: dict[str, str] = {
    "locations": "locations",
    "mysteries": "mysteries",
    "old_gods": "old_gods",
    "cards": "cards",
}

#: 只进 prompt 的文本字段（不参与机制）
_TEXT_KEYS: tuple[str, ...] = ("title", "world", "outline", "storybook", "directives")

#: 1.9.19：剧本预设可以压缩章节长度，并可给调查员发初始物品。
#: 它们只改变“这一局有多少调查/带什么进场”，不改任何检定公式。
_MECH_KEYS: tuple[str, ...] = ("mystery_len", "mystery_count")


class ScenarioError(ValueError):
    """剧本结构非法（拒绝加载，绝不"容错采用" —— 与 content 层同一纪律）。"""


def _err(msg: str) -> None:
    raise ScenarioError(msg)


def normalize(raw: Any) -> dict | None:
    """校验并归一化剧本。`None`/空 ⇒ 返回 `None`（= 用随包内容，零影响）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _err(f"scenario 必须是对象，实际 {type(raw).__name__}")
    out: dict[str, Any] = {}

    for k in _TEXT_KEYS:
        v = raw.get(k)
        if v in (None, ""):
            continue
        if k == "storybook" or k == "directives":
            if isinstance(v, str):
                v = [x.strip() for x in v.splitlines() if x.strip()]
            if not isinstance(v, list):
                _err(f"scenario.{k} 必须是字符串或字符串数组")
            out[k] = [str(x).strip() for x in v if str(x).strip()][:50]
        else:
            out[k] = str(v)[:20000]

    # 1.9.19：短篇剧本可把单章调查数压到 2~6，并把「钥匙数」压到 1~5；
    # 只在**显式给了**的情况下覆盖难度表，缺省时保持原版行为。
    for key in _MECH_KEYS:
        if raw.get(key) in (None, ""):
            continue
        try:
            v = int(raw[key])
        except (TypeError, ValueError):
            _err(f"scenario.{key} 必须是整数")
        if key == "mystery_len" and not (1 <= v <= 15):
            _err(f"scenario.mystery_len={v} 越界（1~15）")
        if key == "mystery_count" and not (1 <= v <= 10):
            _err(f"scenario.mystery_count={v} 越界（1~10）")
        out[key] = v

    st_items = raw.get("starting_items")
    if st_items not in (None, [], {}):
        if not isinstance(st_items, list) or not all(isinstance(x, str) for x in st_items):
            _err("scenario.starting_items 必须是物品 id 字符串数组")
        valid_items = {str(x.get("id")) for x in content.items() if x.get("id")}
        bad = [x for x in st_items if str(x) not in valid_items]
        if bad:
            _err(f"scenario.starting_items 含未登记物品 {bad}")
        out["starting_items"] = [str(x) for x in st_items][:8]

    for key in ("locations", "mysteries", "old_gods", "cast", "cards"):
        v = raw.get(key)
        if v in (None, [], {}):
            continue
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            _err(f"scenario.{key} 必须是对象数组")
        out[key] = v

    if "locations" in out:
        for i, loc in enumerate(out["locations"]):
            lid = str(loc.get("id") or "").strip()
            if not lid:
                _err(f"scenario.locations[{i}] 缺 id")
            th = str(loc.get("threat") or "SAFE").upper()
            if th not in THREATS:
                _err(f"scenario.locations[{i}].threat={th!r} 非法（合法：{list(THREATS)}）")
            loc["id"] = lid
            loc["threat"] = th
            loc.setdefault("name", lid)
            loc.setdefault("home", False)
            loc["_source"] = "scenario（玩家自定义剧本）"
        if not any(bool(x.get("home")) for x in out["locations"]):
            # 一个"家"都没有会让休息/回血路径无落脚点 ⇒ 把第一处标为家（显式、可预期）
            out["locations"][0]["home"] = True

    if "mysteries" in out:
        for i, my in enumerate(out["mysteries"]):
            if not str(my.get("id") or "").strip():
                _err(f"scenario.mysteries[{i}] 缺 id")
            my.setdefault("name", str(my["id"]))
            my.setdefault("locations", [])
            my.setdefault("free_roam", True)
            my["_source"] = "scenario（玩家自定义剧本）"

    if "old_gods" in out:
        for i, g in enumerate(out["old_gods"]):
            if not str(g.get("id") or "").strip():
                _err(f"scenario.old_gods[{i}] 缺 id")
            g.setdefault("name", str(g["id"]))
            g.setdefault("global_rule", "none")
            g["_source"] = "scenario（玩家自定义剧本）"

    if "cast" in out:
        for i, c in enumerate(out["cast"]):
            if not str(c.get("name") or "").strip():
                _err(f"scenario.cast[{i}] 缺 name")
            c.setdefault("display", str(c["name"]))
            c.setdefault("age_flag", "adult")
            c.setdefault("role", "investigator")
            c["_source"] = "scenario（玩家自定义剧本）"

    return out or None


def load_file(path: str | Path) -> dict | None:
    """从磁盘读一份剧本 JSON（供"玩家自带剧本文件"用）。"""
    p = Path(path)
    if not p.is_file():
        raise ScenarioError(f"剧本文件不存在：{p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ScenarioError(f"剧本不是合法 JSON：{p}（{exc}）") from exc
    return normalize(raw)


def from_session(session: dict | None) -> dict | None:
    s = (session or {}).get("scenario")
    return s if isinstance(s, dict) and s else None


# ------------------------------------------------------------------ 取表（带回落）


def locations(scenario: dict | None) -> list[dict]:
    """地点表：剧本给了就用剧本的，否则回落到随包 `content.locations()`。"""
    got = (scenario or {}).get("locations")
    return list(got) if got else content.locations()


def mysteries(scenario: dict | None) -> list[dict]:
    got = (scenario or {}).get("mysteries")
    return list(got) if got else content.mysteries()


def old_gods(scenario: dict | None) -> list[dict]:
    got = (scenario or {}).get("old_gods")
    return list(got) if got else content.old_gods()


def cards(scenario: dict | None, extra: list[dict] | None = None) -> list[dict]:
    """卡池：剧本给了就**替换**（不是叠加）—— 作者要能完全掌控事件卡。

    `extra` 是测试注入口（与 `Run.start(extra_cards=…)` 同义），永远附加在最后。
    """
    got = (scenario or {}).get("cards")
    pool = list(got) if got else list(content.cards())
    if extra:
        pool = pool + list(extra)
    return pool


def catalog(scenario: dict | None) -> dict:
    """开局界面用的选项清单（有剧本时以剧本为准）。"""
    sc = scenario or {}
    return {
        "old_gods": [str(g.get("id")) for g in old_gods(scenario)],
        "characters": [str(c.get("name")) for c in
                       (sc.get("cast") or content.cast())] if isinstance(sc.get("cast"), list)
                      else list(content.cast()),
        "locations": [str(x.get("id")) for x in locations(scenario)],
        "mysteries": [str(x.get("id")) for x in mysteries(scenario)],
        "scenario": bool(sc),
    }


def prompt_block(scenario: dict | None) -> str:
    """注入 LLM 的剧本段（空剧本 ⇒ 空串 ⇒ 提示词逐字节不变）。"""
    sc = scenario or {}
    if not sc:
        return ""
    parts: list[str] = []
    if sc.get("title"):
        parts.append(f"【剧本名】{sc['title']}")
    if sc.get("world"):
        parts.append("【世界观设定】（作者指定，叙述必须贴合）\n" + str(sc["world"]))
    if sc.get("outline"):
        parts.append("【故事大纲】（作者指定，剧情按此脉络推进）\n" + str(sc["outline"]))
    sb = sc.get("storybook")
    if isinstance(sb, list) and sb:
        parts.append("【故事书（世界规则）】\n" + "\n".join(f"- {x}" for x in sb[:50]))
    dv = sc.get("directives")
    if isinstance(dv, list) and dv:
        parts.append("【强制指令】（最高优先级）\n" + "\n".join(f"- {x}" for x in dv[:50]))
    if not parts:
        return ""
    # 边界声明：剧本换内容，不换数值
    parts.append(
        "【剧本边界】以上设定只改变**地点/谜团/古神/事件卡与叙述措辞**；"
        "DOOM、体力、理智、属性、掷骰结果与战斗结算**仍由系统决定**，"
        "不得因为设定而改写任何数值或跳过检定。"
    )
    return "\n\n".join(parts)

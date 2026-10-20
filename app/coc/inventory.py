"""背包物品的**使用**结算（1.9.19）。

战斗内消耗品由 `combat.py` 处理；本模块补的是城镇/调查场景下的显式使用：
绷带治轻伤、医疗包治重伤、镇静剂回复 SAN、日记/魔典降 DOOM、地图/笔记换 EXP。

所有数值仍只通过 `Run.apply` → `sanitize.apply_delta` 落账；本模块只判定
“这件物品能不能用、用了会发生什么”，不直接写权威数值。
"""
from __future__ import annotations

from typing import Any

from . import sanity as _san
from . import state as _state
from .provenance import t


def _cure_tag(state: dict, severity: str) -> str | None:
    for tg in state.get("tags") or []:
        if str(tg.get("kind")) == "injury" and str(tg.get("severity")) == str(severity):
            return str(tg.get("id") or "")
    return None


def use_item(run, item_id: str) -> dict:
    """使用一件背包物品。返回 `{ok, item, name, effects, text, reason}`。"""
    st = run.state
    iid = str(item_id or "")
    bag = (st.get("inventory") or {}).get("backpack") or []
    if iid not in bag:
        return {"ok": False, "reason": "not_in_backpack", "item": iid}
    meta = _state.item_table().get(iid)
    if not meta:
        return {"ok": False, "reason": "unregistered", "item": iid}
    if not meta.get("usable"):
        if meta.get("kind") == "equipment":
            return {"ok": False, "reason": "not_usable_equipment", "item": iid,
                    "name": meta.get("name", iid)}
        return {"ok": False, "reason": "not_usable", "item": iid,
                "name": meta.get("name", iid)}

    delta: dict[str, Any] = {}
    effects: dict[str, Any] = {}
    parts: list[str] = []

    cures = str(meta.get("cures") or "")
    if cures:
        tid = _cure_tag(st, cures)
        if not tid:
            return {"ok": False, "reason": "no_matching_injury", "item": iid,
                    "name": meta.get("name", iid),
                    "text": "没有需要处理的这类伤势。"}
        delta["tags_remove"] = [tid]
        effects["cured"] = tid
        parts.append("处理了伤势")

    dk = str(meta.get("doom_down_key") or "")
    if dk:
        try:
            amount = int(t(run.cfg, f"doom_down.{dk}"))
        except KeyError:
            amount = 0
        if amount:
            delta["doom"] = -amount
            effects["doom"] = -amount
            parts.append(f"DOOM −{amount}%")

    san_expr = meta.get("san_restore")
    if san_expr:
        amount = _san.roll_expr(run.rng, san_expr)
        if amount:
            delta["san"] = amount
            effects["san"] = amount
            parts.append(f"SAN +{amount}")

    sta = int(meta.get("sta_restore", 0) or 0)
    if sta:
        delta["sta"] = sta
        effects["sta"] = sta
        parts.append(f"STA +{sta}")

    exp = int(meta.get("exp_bonus", 0) or 0)
    if exp:
        delta["exp"] = exp
        effects["exp"] = exp
        parts.append(f"EXP +{exp}")

    if not delta:
        return {"ok": False, "reason": "no_effect", "item": iid,
                "name": meta.get("name", iid),
                "text": "这件东西现在派不上用场。"}

    # 消耗品/可用品用掉；装备类不会走到这里（usable=false）
    delta["item_take"] = [iid]
    applied = run.apply(delta, reason=f"item_use:{iid}", clamp=False)
    got = applied.get("applied") or {}
    if not got.get("item_take"):
        return {"ok": False, "reason": "use_failed", "item": iid,
                "name": meta.get("name", iid),
                "text": "使用没有生效（物品仍在背包里）。"}
    run.log("item_use", {"item": iid, "effects": effects,
                         "san": int(st.get("san", 0)),
                         "doom": int(st.get("doom", 0))})
    name = meta.get("name", iid)
    return {"ok": True, "reason": "ok", "item": iid, "name": name,
            "effects": effects,
            "text": f"你使用了「{name}」：" + "，".join(parts) + "。"}

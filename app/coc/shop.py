"""固定商店场景与自动生成物品。

## 设计

商店不是新地图，而是城镇里**固定的一处场景**：`data/coc/shops.json` 定义店主、
场景文本、候选货架与加价。每次进入/补货时，用 `sha256(sid|shop|reroll)` 作为种子
从候选池抽取固定数量商品 —— 同一局、同一商店、同一轮进货的结果**可复现**，
读档后仍是同一批货；只有手动重掷或自然补货才会变。

## 边界

· 商店只改 `funds` / `inventory` / `doom`，不改变属性、检定公式或 SAN；
· 购买/重掷走 `Run.apply`（唯一写入点），商店库存本身是**内容状态**，由本模块落账；
· 所有价格来自 `items.json` 的 `price` × 商店 `markup`，没有任何代码内兜底价格；
· 搜刮是显式动作（每回合每地点一次），成功才生成物品，不会在普通调查里静默塞东西。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from typing import Any

from . import paths
from . import state as _state
from .provenance import t

_RARITY_WEIGHT = {"common": 6, "uncommon": 3, "rare": 1}
_CACHE: list[dict] | None = None


class ShopError(ValueError):
    """商店参数非法（未知商店/商品）。"""


def _load() -> list[dict]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not os.path.isfile(paths.SHOPS_PATH):
        _CACHE = []
        return _CACHE
    try:
        with open(paths.SHOPS_PATH, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ShopError(f"商店数据读取失败：{exc}") from exc
    rows = blob.get("shops", blob) if isinstance(blob, dict) else blob
    if not isinstance(rows, list):
        raise ShopError("shops.json 结构非法：期望 shops 数组")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("id"):
            raise ShopError(f"shops[{i}] 缺 id")
        if not isinstance(row.get("items"), list) or not row["items"]:
            raise ShopError(f"shops[{i}] 缺 items 候选池")
        out.append(dict(row))
    _CACHE = out
    return _CACHE


def table() -> dict[str, dict]:
    return {str(s["id"]): s for s in _load()}


def summaries() -> list[dict]:
    return [{"id": s["id"], "name": s.get("name", s["id"]),
             "owner": s.get("owner", ""), "scene": s.get("scene", ""),
             "greeting": s.get("greeting", ""),
             "tags": list(s.get("tags") or []),
             "item_count": len(s.get("items") or [])}
            for s in _load()]


def _cfg_int(cfg: dict, key: str) -> int:
    return int(t(cfg, f"shop.{key}"))


def _price(meta: dict, shop: dict) -> int:
    base = int(meta.get("price", 1) or 1)
    return max(1, base + int(shop.get("markup", 0) or 0))


def _stable_rng(state: dict, shop_id: str, reroll: int) -> random.Random:
    seed = f"{state.get('sid')}|{shop_id}|{int(reroll)}".encode("utf-8")
    return random.Random(int(hashlib.sha256(seed).hexdigest()[:16], 16))


def _weighted_pick(rng: random.Random, ids: list[str], n: int,
                   items: dict[str, dict]) -> list[str]:
    """按稀有度加权**不放回**抽取 n 件（权重不因已抽走而重算，简单且可复现）。"""
    pool = list(dict.fromkeys(str(x) for x in ids))
    picked: list[str] = []
    while pool and len(picked) < n:
        weights = [_RARITY_WEIGHT.get(str((items.get(i) or {}).get("rarity") or "common"), 6)
                   for i in pool]
        choice = rng.choices(pool, weights=weights, k=1)[0]
        picked.append(choice)
        pool.remove(choice)
    return picked


def _shop_state(state: dict, shop_id: str) -> dict:
    shops = state.setdefault("shops", {})
    if not isinstance(shops, dict):
        shops = {}
        state["shops"] = shops
    node = shops.get(shop_id)
    if not isinstance(node, dict):
        node = {"stock": [], "visited": 0, "reroll": 0, "last_stock_turn": -999,
                "last_open_turn": -999}
        shops[shop_id] = node
    return node


def ensure_stock(run, shop_id: str, *, force: bool = False) -> dict:
    """确保该商店有库存；返回商店节点。"""
    shop = table().get(str(shop_id))
    if not shop:
        raise ShopError(f"未知商店 {shop_id!r}；可选 {sorted(table())}")
    st = run.state
    node = _shop_state(st, str(shop_id))
    cur_turn = int(st.get("turn", 0))
    size = max(1, _cfg_int(run.cfg, "stock_size"))
    restock = max(1, _cfg_int(run.cfg, "restock_turns"))
    empty = not node.get("stock")
    stale = (cur_turn - int(node.get("last_stock_turn", -999))) >= restock
    if force or empty or stale:
        items = _state.item_table()
        ids = [i for i in (shop.get("items") or []) if str(i) in items]
        picked = _weighted_pick(_stable_rng(st, str(shop_id), int(node.get("reroll", 0))),
                                ids, size, items)
        node["stock"] = [{"id": i, "qty": 1} for i in picked]
        node["last_stock_turn"] = cur_turn
    return node


def shop_view(run, shop_id: str) -> dict:
    shop = table().get(str(shop_id))
    if not shop:
        raise ShopError(f"未知商店 {shop_id!r}")
    node = ensure_stock(run, str(shop_id))
    items = _state.item_table()
    bag = set(((run.state.get("inventory") or {}).get("backpack") or []))
    stock = []
    for row in node.get("stock") or []:
        iid = str(row.get("id"))
        meta = items.get(iid)
        if not meta:
            continue
        stock.append({
            "id": iid, "name": meta.get("name", iid),
            "kind": meta.get("kind", "unknown"),
            "rarity": meta.get("rarity", "common"),
            "desc": meta.get("desc", ""),
            "price": _price(meta, shop),
            "qty": int(row.get("qty", 1) or 1),
            "owned": iid in bag,
            "usable": bool(meta.get("usable")),
            "san_restore": meta.get("san_restore", ""),
            "cures": meta.get("cures", ""),
        })
    return {
        "id": shop["id"], "name": shop.get("name", shop["id"]),
        "owner": shop.get("owner", ""), "scene": shop.get("scene", ""),
        "greeting": shop.get("greeting", ""),
        "markup": int(shop.get("markup", 0) or 0),
        "stock": stock,
        "funds": int(run.state.get("funds", 0)),
        "reroll": int(node.get("reroll", 0)),
        "visited": int(node.get("visited", 0)),
        "restock_turns": max(1, _cfg_int(run.cfg, "restock_turns")),
    }


def catalog(run) -> list[dict]:
    """所有固定商店的当前视图（不触发进入 DOOM 结算）。"""
    out = []
    for shop in _load():
        try:
            out.append(shop_view(run, str(shop["id"])))
        except ShopError:
            continue
    return out


def open_shop(run, shop_id: str) -> dict:
    """进入固定商店场景：首次进入本回合按 tuning 计 DOOM。"""
    shop = table().get(str(shop_id))
    if not shop:
        raise ShopError(f"未知商店 {shop_id!r}")
    node = ensure_stock(run, str(shop_id))
    cur_turn = int(run.state.get("turn", 0))
    if int(node.get("last_open_turn", -999)) != cur_turn:
        doom = int(t(run.cfg, "doom_up.shop_enter"))
        if doom:
            run.apply({"doom": doom}, reason="shop_enter", clamp=False)
        node["last_open_turn"] = cur_turn
    node["visited"] = int(node.get("visited", 0)) + 1
    run.log("shop_open", {"shop": shop_id, "turn": cur_turn,
                          "funds": int(run.state.get("funds", 0)),
                          "doom": int(run.state.get("doom", 0))})
    view = shop_view(run, str(shop_id))
    view["entered"] = True
    return view


def buy(run, shop_id: str, item_id: str) -> dict:
    shop = table().get(str(shop_id))
    if not shop:
        return {"ok": False, "reason": "unknown_shop", "shop": shop_id, "item": item_id}
    node = ensure_stock(run, str(shop_id))
    iid = str(item_id or "")
    row = next((x for x in (node.get("stock") or []) if str(x.get("id")) == iid), None)
    if row is None:
        return {"ok": False, "reason": "not_in_stock", "shop": shop_id, "item": iid}
    meta = _state.item_table().get(iid)
    if not meta:
        return {"ok": False, "reason": "unregistered", "shop": shop_id, "item": iid}
    bag = (run.state.get("inventory") or {}).get("backpack") or []
    if iid in bag:
        return {"ok": False, "reason": "already_owned", "shop": shop_id, "item": iid}
    price = _price(meta, shop)
    if int(run.state.get("funds", 0)) < price:
        return {"ok": False, "reason": "insufficient_funds", "shop": shop_id,
                "item": iid, "price": price,
                "funds": int(run.state.get("funds", 0))}
    # 先扣款，再授予；授予失败则退款（避免“钱没了、货没到”）
    before_funds = int(run.state.get("funds", 0))
    run.apply({"funds": -price}, reason="shop_buy", clamp=False)
    got = run.apply({"item_grant": [iid]}, reason="shop_buy", clamp=False)
    if not (got.get("applied") or {}).get("item_grant"):
        run.apply({"funds": price}, reason="shop_refund", clamp=False)
        return {"ok": False, "reason": "grant_failed", "shop": shop_id, "item": iid,
                "funds": before_funds}
    # 买走：本店本轮货架移除
    node["stock"] = [x for x in (node.get("stock") or []) if str(x.get("id")) != iid]
    run.log("shop_buy", {"shop": shop_id, "item": iid, "price": price,
                         "funds": int(run.state.get("funds", 0))})
    return {"ok": True, "reason": "ok", "shop": shop_id, "item": iid,
            "price": price, "name": meta.get("name", iid),
            "funds": int(run.state.get("funds", 0))}


def sell(run, shop_id: str, item_id: str) -> dict:
    """出售背包物品（标价的 50%，向下取整，最低 1）。"""
    shop = table().get(str(shop_id))
    if not shop:
        return {"ok": False, "reason": "unknown_shop"}
    iid = str(item_id or "")
    bag = (run.state.get("inventory") or {}).get("backpack") or []
    if iid not in bag:
        return {"ok": False, "reason": "not_in_backpack", "item": iid}
    meta = _state.item_table().get(iid) or {}
    # 已装备的物品先卸下（否则卖完装备槽会指向不存在的物品）
    eq = _state.equipped(run.state)
    for slot, cur in list(eq.items()):
        if str(cur) == iid:
            run.apply({"unequip": slot}, reason="shop_sell_unequip", clamp=False)
    take = run.apply({"item_take": [iid]}, reason="shop_sell", clamp=False)
    if not (take.get("applied") or {}).get("item_take"):
        return {"ok": False, "reason": "take_failed", "item": iid}
    ratio = float(t(run.cfg, "shop.sell_ratio"))
    value = max(1, int(int(meta.get("price", 1) or 1) * ratio))
    run.apply({"funds": value}, reason="shop_sell", clamp=False)
    # 卖出的商品回到货架（若本店候选池包含它）
    node = _shop_state(run.state, str(shop_id))
    if iid in (shop.get("items") or []) and all(
            str(x.get("id")) != iid for x in (node.get("stock") or [])):
        node.setdefault("stock", []).append({"id": iid, "qty": 1})
    run.log("shop_sell", {"shop": shop_id, "item": iid, "value": value,
                          "funds": int(run.state.get("funds", 0))})
    return {"ok": True, "reason": "ok", "shop": shop_id, "item": iid,
            "value": value, "funds": int(run.state.get("funds", 0))}


def reroll(run, shop_id: str) -> dict:
    shop = table().get(str(shop_id))
    if not shop:
        return {"ok": False, "reason": "unknown_shop"}
    node = _shop_state(run.state, str(shop_id))
    doom = int(t(run.cfg, "doom_up.shop_reroll"))
    if doom:
        run.apply({"doom": doom}, reason="shop_reroll", clamp=False)
    node["reroll"] = int(node.get("reroll", 0)) + 1
    node["last_stock_turn"] = -999
    ensure_stock(run, str(shop_id), force=True)
    run.log("shop_reroll", {"shop": shop_id, "reroll": node["reroll"],
                            "doom": int(run.state.get("doom", 0))})
    return {"ok": True, "reason": "ok", "doom": int(run.state.get("doom", 0)),
            "shop": shop_view(run, str(shop_id))}


def _loot_pool(run, shop: dict) -> list[str]:
    items = _state.item_table()
    pool = [i for i in (shop.get("items") or []) if str(i) in items]
    # 已拥有的物品不再重复搜出（本模式背包按 id 去重）
    bag = set(((run.state.get("inventory") or {}).get("backpack") or []))
    return [i for i in pool if str(i) not in bag]


def search(run, location: str, shop_id: str = "general_store") -> dict:
    """在当前地点搜刮一次：成功自动生成一件物品，失败只留线索。"""
    loc = str(location or "").strip() or "unknown"
    st = run.state
    key = f"{int(st.get('turn', 0))}:{loc}"
    searched = st.setdefault("searched", [])
    if key in searched:
        return {"ok": False, "reason": "already_searched", "location": loc}
    shop = table().get(str(shop_id)) or next(iter(table().values()), None)
    if not shop:
        return {"ok": False, "reason": "no_shop_data"}
    doom = int(t(run.cfg, "shop.loot_doom"))
    if doom:
        run.apply({"doom": doom}, reason="search", clamp=False)
    searched.append(key)
    # 只保留最近 40 条，避免存档无限膨胀
    if len(searched) > 40:
        del searched[:-40]
    per = int((st.get("stats") or {}).get("PER", 5) or 5)
    chance = float(t(run.cfg, "shop.loot_base_chance")) + per * float(
        t(run.cfg, "shop.loot_per"))
    roll = run.rng.random()
    if roll >= chance:
        run.log("search", {"location": loc, "found": None, "roll": round(roll, 4),
                           "chance": round(chance, 4)})
        return {"ok": True, "found": None, "location": loc, "roll": round(roll, 4),
                "chance": round(chance, 4),
                "text": "你翻了一遍，只找到一些无法带走的东西。"}
    pool = _loot_pool(run, shop)
    if not pool:
        run.log("search", {"location": loc, "found": None, "reason": "pool_empty"})
        return {"ok": True, "found": None, "location": loc,
                "text": "这里能拿的东西都已经在你包里了。"}
    iid = _weighted_pick(run.rng, pool, 1,
                         _state.item_table())[0]
    run.apply({"item_grant": [iid]}, reason="search_loot", clamp=False)
    name = (_state.item_table().get(iid) or {}).get("name", iid)
    run.log("search", {"location": loc, "found": iid, "roll": round(roll, 4),
                       "chance": round(chance, 4)})
    return {"ok": True, "found": iid, "name": name, "location": loc,
            "roll": round(roll, 4), "chance": round(chance, 4),
            "text": f"你在角落里找到了「{name}」。"}

"""CoC 模式的会话层：把引擎（`loop.Run`）变成可在 `server.py` 里服务的对象。

## 为什么需要这一层

t48 的 `loop.Run` 是纯引擎（构造/推进/结算/存档）。接线需要的是"会话"：
一个能被 `server.py` 按 `sid` 取到、能推进、能读档的对象。

## 会话表为什么**不**放进 `game.SESSIONS`

`game.get_session(sid)` 抛 `GameError` 的语义是"会话不存在"，而
`game.public_state()` / `game.ensure_scene_bg()` 会读 `prot["stats"]`（TSF 字段）。
把 coc 会话塞进 `game.SESSIONS` 会让**所有**走 `game.get_session` 的既有端点
（assets / tasks / history / portrait…）都可能撞上缺字段的 coc 会话。

所以 coc 自带一个会话表，并在 `server.py` 的五个分发点**先**判 coc
（`coc_session(sid) is not None`）——这样既有分支一行不动，coc 也拿到自己的路径。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .. import ai_console as _ai_console
from . import content
from . import inventory as _inventory
from . import paths
from . import prompt as _prompt
from . import sanity as _sanity
from . import scenarios as _presets
from . import shop as _shop
from . import state as _state
from .loop import Resolution, Run
from .provenance import load_tuning, t

MODE = "coc"

#: 会话表（进程内；与既有模式共用"按 sid 取会话"的概念，但物理隔离）
_SESSIONS: dict[str, dict] = {}

#: 难度/死亡模式的合法值（前端下拉用）
DIFFICULTIES = ("Sceptic", "Initiate", "Cultist")
DEATH_MODES = ("last_stand", "sudden_death")


class CocError(RuntimeError):
    """会话层错误（未知 sid / 参数非法）。"""


# ---------------------------------------------------------------- 会话表

def sessions() -> dict[str, dict]:
    return _SESSIONS


def get(sid: str) -> dict | None:
    return _SESSIONS.get(str(sid or ""))


def drop(sid: str) -> None:
    _SESSIONS.pop(str(sid or ""), None)


def is_coc(sid: str) -> bool:
    """该 sid 是否属于 CoC 模式（供 `server.py` 的分发点判断）。"""
    return str(sid or "") in _SESSIONS


# ---------------------------------------------------------------- 开局


def _scenario_of(payload: dict) -> dict | None:
    """从开局 payload 取并校验自定义剧本（t73）。

    支持两种给法：
      · `payload["scenario"]` = 剧本对象（前端/API 直接传结构化剧本）
      · `payload["world"]/["outline"]/["lorebook"]/["directives"]` = 文本，
        在**没有结构化剧本**时自动组装成一个"纯文本剧本"
        （只影响叙述，不改地点/谜团 —— 让"只填了世界观"的玩家也能被遵循）

    非法剧本**直接抛** `ScenarioError`（绝不静默忽略：静默会让玩家以为设定生效了）。
    """
    from . import scenario as _scn
    raw = payload.get("scenario")
    struct = _scn.normalize(raw) if raw else None
    if struct:
        return struct
    text: dict = {}
    # 玩家在开局界面填的**游戏标题**也当作剧本名 —— 否则 prompt 里"这一局叫什么"是空的
    # （实测缺口：只传 title 时 【剧本名】 不出现）。
    title = str(payload.get("title") or "").strip()
    if title:
        text["title"] = title
    world = str(payload.get("world") or "").strip()
    if world:
        text["world"] = world
    outline = str(payload.get("outline") or "").strip()
    if outline:
        text["outline"] = outline
    lore = payload.get("lorebook")
    if isinstance(lore, list):
        items = []
        for it in lore:
            if isinstance(it, dict):
                t = str(it.get("text") or it.get("value") or "").strip()
                if t:
                    items.append(t)
            elif str(it).strip():
                items.append(str(it).strip())
        if items:
            text["storybook"] = items
    dv = payload.get("directives")
    if isinstance(dv, list) and dv:
        items = []
        for it in dv:
            if isinstance(it, dict):
                # StartPayload 的 directives 是 list[dict]（既有模式用 text/value 两种键名）
                t = str(it.get("text") or it.get("value") or it.get("content") or "").strip()
                if t:
                    items.append(t)
            elif str(it).strip():
                items.append(str(it).strip())
        if items:
            text["directives"] = items
    return _scn.normalize(text) if text else None


def start(payload: dict | None = None, cfg: dict | None = None) -> dict:
    """开局：造 Run → 落盘 → 返回 `public_state`。"""
    if cfg is None:
        cfg = load_tuning()
    p = dict(payload or {})
    seed = p.get("seed")
    if seed in ("", None):
        seed = int(time.time() * 1000) % (2 ** 31)
    # 1.9.19：剧本预设 + 玩家自定义 JSON（自定义字段覆盖预设；都不给 = 原版）。
    preset_id = str(p.get("scenario_id") or "").strip()
    scen = _presets.resolve(preset_id, p.get("scenario"))
    if scen is None:
        # 兼容旧路径：只填 world/outline/lorebook 的纯文本剧本
        scen = _scenario_of(p)
    old_god = str(p.get("old_god") or "Goizo")
    if scen and isinstance(scen.get("old_gods"), list) and scen["old_gods"]:
        ids = [str(g.get("id")) for g in scen["old_gods"] if g.get("id")]
        if ids and old_god not in ids:
            old_god = ids[0]
    run = Run.start({
        "difficulty": p.get("difficulty") or "Initiate",
        "death_mode": p.get("death_mode") or "last_stand",
        "old_god": old_god,
        "character": p.get("character") or "Kirie",
        "background": p.get("background") or "Commoner",
        "seed": int(seed),
        "stats": p.get("stats") or None,
    }, cfg, scenario=scen)
    session = {
        "mode": MODE,
        "sid": run.state["sid"],
        "run": run,
        "cfg": cfg,
        # t73：自定义剧本随会话保存（存档信封里也带一份，见 save_slot/load_slot）
        "scenario": scen,
        "scenario_id": preset_id,
        "content_rating": str(p.get("content_rating") or "all"),
        "r18_enabled": bool(p.get("r18_enabled")),
        "adult_intensity": str(p.get("adult_intensity") or "成熟"),
        "allow_forced": bool(p.get("allow_forced")),
        "created": time.time(),
    }
    _SESSIONS[session["sid"]] = session
    # 开局即推进一次：让玩家第一眼就有场面与选项（否则前端只会看到空场面）。
    prepare_first_scene(session)
    res = session.get("_last_resolution")
    session["_last_text"] = (_fallback_text(res, error="opening:no_llm")
                             if res is not None else _fallback_text(None))
    # 开局没有走 LLM（这是既有行为，不是缺陷）；给控制台留一条“系统开场”记录，
    # 让玩家第一次打开 AI 控制台时能看懂“为什么还没有模型调用”。
    try:
        entry = _ai_console.begin(
            run.state,
            [{"role": "system", "content": "（CoC 开局由系统场景生成，未调用真实模型）"},
             {"role": "user", "content": "生成开场场面"}],
            model="system", temperature=None, mock=True, label="CoC 开场（系统）")
        _ai_console.finish(
            run.state, entry, json.dumps(session["_last_text"], ensure_ascii=False),
            reasoning="开局使用系统兜底场景；第一次推进时才会调用模型。",
            duration_ms=0)
    except Exception:
        pass
    return public_state(session)


# ---------------------------------------------------------------- 推进

def prepare_first_scene(session: dict) -> None:
    """开局后立刻做一次调查，让玩家第一眼就有场面可看。

    为什么需要：`Run.start()` 之后 `state["phase"]` 还是 `town`、且没有任何卡面——
    前端若不先推进就会看到空场面。这里推进一次并把结果挂到会话上，
    由 `advance` 之后的首次叙事复用它。
    """
    if session.get("_first_done"):
        return
    run: Run = session["run"]
    locs = run.available_locations()
    if locs:
        res = run.advance_town(locs[0])
        session["_pending_resolution"] = res
    run.settle_ending()
    session["_first_done"] = True


def _resolve_once(session: dict, choice_index: int) -> Resolution:
    """**确定性结算**（同步、无 LLM）：玩家对上一轮抽到的卡做选择 → 出结果。

    与 `loop.advance_town` 的关系：`advance_town` 是"抽一张新卡并结算"，
    而玩家流程是"对**已经看到**的卡做选择"。两者共用 `loop.resolve_card`
    （同一个结算器、同一套钳制与不变式），所以确定性没有第二条路径。
    """
    run: Run = session["run"]
    pending: Resolution | None = session.pop("_pending_resolution", None)
    if pending is not None and pending.card_id:
        card = next((c for c in run.card_pool if c["id"] == pending.card_id), None)
        # t63：属性门槛会把选项锁住，而本函数（探针 / 无 LLM 环境）只有一个
        # 整数下标，**不可能知道**玩家会选哪项。若该下标恰好被锁，
        # `resolve_card` 会抛 LoopError ⇒ 探索**卡死**（实测 C-201#0 PER 5<6）。
        # 所以：请求的下标不可用时，退到**第一个可选项**；这样"玩家属性低"
        # 只是"能做的事更少"，不是"游戏走不下去"。
        # 注意这是**路由的兜底**，不是放松门槛 —— 被锁的选项仍然不可选，
        # 真实玩家路径（前端传明确下标）依旧会被 `resolve_card` 拒绝。
        want = max(0, int(choice_index or 0))
        ok, _why = run.check_requirements(
            ((card or {}).get("choices") or [{}])[
                min(want, max(0, len((card or {}).get("choices") or [{}]) - 1))
            ].get("requirements") or {})
        if not ok:
            want = run.first_playable_choice(card)
        res = run.resolve_card(card, want)
        run.state["investigations_done"] = int(run.state.get("investigations_done", 0)) + 1
        run.state["investigations_total"] = int(run.state.get("investigations_total", 0)) + 1
        if int(run.state["investigations_done"]) >= int(run.state["mystery_len"]):
            if int(run.state["keys"]) < int(run.state.get("mystery_count", 5)):
                run.complete_mystery()
    else:
        locs = run.available_locations()
        res = run.advance_town(locs[int(choice_index or 0) % len(locs)])
    session["_last_resolution"] = res
    run.settle_ending()
    return res


def advance_without_llm(session: dict, choice_index: int = 0) -> dict:
    """**只结算、不请 LLM**（同步）。给探针/测试/无 LLM 环境用。

    正常游戏路径走 `advance_async`；这条存在的意义是让"接线后的端到端"
    可以在**不花 LLM 调用**的前提下被验证（与 t48 的价值主张一致）。
    """
    run: Run = session["run"]
    if run.state.get("ending"):
        return public_state(session, note="本局已结束")
    res = _resolve_once(session, choice_index)
    session["_last_text"] = dict(_fallback_text(res), _degraded=True, _error="no_llm")
    _persist(session)
    return public_state(session)


async def advance_async(session: dict, choice_index: int = 0) -> dict:
    """**正式**推进：结算（确定性、同步） + 叙事（await LLM）。

    为什么把两步分开：E1「结算先于叙事」要求结算在 LLM 之前**定稿**；
    写成两个语句比写成一个"边算边问"的函数更容易被审查与验证。
    """
    run: Run = session["run"]
    if run.state.get("ending"):
        return public_state(session, note="本局已结束")
    res = _resolve_once(session, choice_index)
    session["_last_text"] = await _narrate(session, res)
    _persist(session)
    return public_state(session)


async def _narrate(session: dict, res: Resolution) -> dict:
    """请 LLM 写叙述；任何异常都降级为系统原文（游戏不卡死）。

    **没有 prompt 代码时也能玩**：`client.mock` 为真（未配置 API Key）时直接走
    降级文案——这保证了"接线完成但没配 Key"也能开局与推进。
    """
    run: Run = session["run"]
    cfg = session["cfg"]
    try:
        from ..llm import LLMClient
        client = LLMClient(_llm_cfg(), session=run.state, label="CoC 叙述")
        if getattr(client, "mock", False):
            # 演示模式不调用真实模型；仍留一条控制台记录，避免玩家打开后完全空白。
            entry = _ai_console.begin(
                run.state,
                [{"role": "system", "content": "（演示模式：未调用真实模型）"},
                 {"role": "user", "content": "CoC 叙述"}],
                model="mock", temperature=0.9, mock=True, label="CoC 叙述（演示）")
            fallback = dict(_fallback_text(res), _degraded=True, _error="llm.mock")
            _ai_console.finish(
                run.state, entry,
                json.dumps(fallback, ensure_ascii=False),
                reasoning="演示模式没有 reasoning_content。",
                duration_ms=0)
            return fallback
        parts = _participants_for(run, res)
        gate = _prompt.rhe.resolve_gate(
            session.get("content_rating", "all"), session.get("r18_enabled", False),
            session.get("adult_intensity", "成熟"), parts, cfg,
            horror_hint=_prompt.rhe.horror_tier_for(run.state, cfg))
        messages = _prompt.build_messages(
            run, resolution=res,
            content_rating=session.get("content_rating", "all"),
            r18_enabled=session.get("r18_enabled", False),
            adult_intensity=session.get("adult_intensity", "成熟"),
            allow_forced=session.get("allow_forced", False),
            gate=gate, participants=parts)
        raw = await client.chat_json(messages, temperature=0.9)
        turn = _prompt.narr_turn(raw, cfg)
        # E2/E3 再走一次：提案只能落在白名单内，且只能叠加在已定稿结果之上
        if turn.get("coc_delta"):
            # ⚠️ 必须显式 trust="llm"：`Run.apply` 的默认是 engine（引擎结算用），
            # 而这里拿到的是**模型原值**。即使 `narr_turn` 已经过白名单，也要在这一层
            # 把边界写成结构化事实——这样任何绕过解析层的改动都会**当场抛
            # TrustViolation**（被下面的 except 降级为系统文案），而不是静默获得引擎权限。
            run.apply(turn["coc_delta"], reason="llm_proposal", trust="llm")
        return turn
    except Exception as exc:   # 兜底：绝不因叙事失败让对局崩
        return dict(_fallback_text(res, error=repr(exc)), _degraded=True)


def _persist(session: dict) -> None:
    """落盘当前会话（best-effort；失败只记不抛）。"""
    try:
        session["run"].save()
    except OSError:
        pass


# ---------------------------------------------------------------- 物品栏动作（t63）

def equip_action(sid: str, action: str, *, item: str = "",
                 slot: str = "") -> dict:
    """玩家动作：装备 / 卸下。返回 `{ok, action, item, slot, reason, inventory}`。

    **为什么走 `Run.apply`（唯一写点）而不是直接改 `state`**：
      · 只有经过 `apply` → `sanitize.apply_delta` 才会被 `persist` 落盘、
        并跑 `invariants()`；直接改 state 会绕过这两件事（t48 立下的 E1/E6 契约）；
      · `equip` / `unequip` 已加进 `ENGINE_ALLOWED_KEYS` 家族（`PLAYER_ACTION_KEYS`）——
        它们是**玩家**的动作，所以**刻意不进 LLM 白名单**：模型无权替玩家决定装备。
        这样"模型能碰什么"与"玩家能碰什么"在代码里是两份不同的清单。

    `action="equip"` 需要 `item`（背包里的物品 id）；`action="unequip"` 需要 `slot`
    （`R`/`C`/`B`）。失败**不抛**，用 `ok=False` + `reason` 表达，便于端点直接回给前端。
    """
    session = get(sid)
    if session is None:
        raise CocError("会话不存在或已过期")
    run: Run = session["run"]
    act = str(action or "").strip().lower()
    if act not in ("equip", "unequip"):
        return {"ok": False, "action": act, "item": item, "slot": slot,
                "reason": "unknown_action",
                "inventory": _state.inventory_view(run.state)}
    delta = {act: (str(item) if act == "equip" else str(slot))}
    try:
        res = run.apply(delta, reason=f"player:{act}", trust="engine")
    except Exception as exc:              # 信任层/校验异常都不该让端点 500
        return {"ok": False, "action": act, "item": item, "slot": slot,
                "reason": f"{type(exc).__name__}:{exc}",
                "inventory": _state.inventory_view(run.state)}
    applied = res.get("applied", {}).get(act) or []
    dropped = res.get("dropped") or []
    _persist(session)
    item_id = applied[0].get("item") if applied else None
    slot_used = applied[0].get("slot") if applied else None
    return {
        "ok": bool(applied) and not dropped,
        "action": act,
        "item": item_id,
        "slot": slot_used,
        "reason": ("ok" if (applied and not dropped)
                   else (";".join(map(str, dropped)) or "no_effect")),
        "inventory": _state.inventory_view(run.state),
    }


# ---------------------------------------------------------------- 1.9.19：SAN / 商店 / 搜刮 / 物品使用

def _require(sid: str) -> dict:
    session = get(sid)
    if session is None:
        raise CocError("会话不存在或已过期")
    return session


def _attach_view(session: dict, payload: dict) -> dict:
    """把整段 coc 状态挂到写操作响应上，前端一次拿全，无需二次 GET。"""
    try:
        payload["coc"] = public_state(session)
    except Exception:
        pass
    return payload


def shop_list(sid: str) -> dict:
    session = _require(sid)
    run: Run = session["run"]
    shops = _shop.catalog(run)
    return _attach_view(session, {"ok": True, "shops": shops,
                                  "funds": int(run.state.get("funds", 0))})


def shop_open(sid: str, shop_id: str) -> dict:
    session = _require(sid)
    run: Run = session["run"]
    view = _shop.open_shop(run, shop_id)
    _persist(session)
    return _attach_view(session, {"ok": True, "shop": view})


def shop_buy(sid: str, shop_id: str, item_id: str) -> dict:
    session = _require(sid)
    run: Run = session["run"]
    res = _shop.buy(run, shop_id, item_id)
    _persist(session)
    if res.get("ok"):
        try:
            res["shop"] = _shop.shop_view(run, shop_id)
        except Exception:
            pass
    return _attach_view(session, res)


def shop_sell(sid: str, shop_id: str, item_id: str) -> dict:
    session = _require(sid)
    run: Run = session["run"]
    res = _shop.sell(run, shop_id, item_id)
    _persist(session)
    if res.get("ok"):
        try:
            res["shop"] = _shop.shop_view(run, shop_id)
        except Exception:
            pass
    return _attach_view(session, res)


def shop_reroll(sid: str, shop_id: str) -> dict:
    session = _require(sid)
    run: Run = session["run"]
    res = _shop.reroll(run, shop_id)
    _persist(session)
    return _attach_view(session, res)


def search_loot(sid: str, location: str = "") -> dict:
    session = _require(sid)
    run: Run = session["run"]
    loc = str(location or "").strip()
    if not loc:
        # 最近一次调查地点；没有就取当前可选地点第一处
        for ev in reversed(run.events):
            if ev.get("kind") == "investigation" and ev.get("loc"):
                loc = str(ev["loc"])
                break
        if not loc:
            locs = run.available_locations()
            loc = str(locs[0]) if locs else "unknown"
    res = _shop.search(run, loc)
    _persist(session)
    return _attach_view(session, res)


def use_item(sid: str, item_id: str) -> dict:
    session = _require(sid)
    run: Run = session["run"]
    res = _inventory.use_item(run, item_id)
    _persist(session)
    try:
        res["inventory"] = _state.inventory_view(run.state)
    except Exception:
        pass
    return _attach_view(session, res)


def san_check(sid: str, *, loss: str = "0/1d6", difficulty: str = "regular",
              reason: str = "") -> dict:
    session = _require(sid)
    run: Run = session["run"]
    res = _sanity.check(run, loss=loss, difficulty=difficulty, reason=reason)
    _persist(session)
    res["ok"] = True
    res["inventory"] = _state.inventory_view(run.state)
    return _attach_view(session, res)


# ---------------------------------------------------------------- 1.9.20：AI 控制台 / 手动改对话

def set_ai_directive(sid: str, text: str) -> str:
    session = _require(sid)
    run: Run = session["run"]
    run.state["ai_directive"] = str(text or "").strip()[:500]
    _persist(session)
    return str(run.state["ai_directive"])


def edit_dialogue(sid: str, dialogue: Any) -> dict:
    """手动修改当前 CoC 叙述的对话行（不改卡面选项与任何数值）。"""
    session = _require(sid)
    text = session.get("_last_text")
    if not isinstance(text, dict):
        # 读档后 _last_text 可能还没重建；用当前公开视图兜底，让"改对话"永远可用。
        pub = public_state(session)
        text = {"dialogue": pub.get("dialogue") or [],
                "choices": pub.get("choices") or []}
        session["_last_text"] = text
    cleaned = []
    for d in (dialogue or [])[:6]:
        if not isinstance(d, dict):
            continue
        body = str(d.get("text") or "").strip()
        if not body:
            continue
        cleaned.append({
            "character": str(d.get("character") or "旁白").strip()[:20] or "旁白",
            "emotion": str(d.get("emotion") or "neutral").strip()[:12] or "neutral",
            "text": body[:800],
        })
    if not cleaned:
        raise CocError("对话不能为空")
    text = dict(text)
    text["dialogue"] = cleaned
    text["_edited_by_user"] = True
    session["_last_text"] = text
    _persist(session)
    return public_state(session)


def _llm_cfg() -> dict:
    """LLM 配置：优先复用既有 config（`llm` 段），失败则回退 mock。

    不 import app.config 的模块级对象，只调它的 load_config —— 与既有模式同源。
    """
    try:
        from .. import config as cfgmod
        return cfgmod.load_config()
    except Exception:
        return {"llm": {"base_url": "", "api_key": ""}}


def _participants_for(run: Run, res: Resolution) -> list[str]:
    """本幕的**参与者**（G1 成年闸门的输入）。

    取卡面若显式声明了 `_erotic.participants` 就用它；否则退化为
    "本局登记的角色 + 在场盟友"。**空名单 ⇒ G1 直接拒**（不默认放行）。
    """
    card = None
    if res.card_id:
        card = next((c for c in run.card_pool if c["id"] == res.card_id), None)
    if card:
        er = card.get("_erotic") or {}
        if er.get("participants"):
            return [str(x) for x in er["participants"]]
    names = [run.state.get("character")]
    names += [str(a) for a in (run.state.get("allies") or [])]
    return [n for n in names if n]


def _fallback_text(res: Resolution | None, error: str = "") -> dict:
    """LLM 不可用时的系统文案（保证前端永远有东西可显示）。"""
    if res is None:
        return {
            "scene": "盐川町",
            "dialogue": [{"character": "旁白", "emotion": "neutral",
                          "text": "（街上没有人。你还是得去查点什么。）"}],
            "choices": [{"text": "开始调查", "effect": ""}],
            "narrator": {"unreliable": False, "inserted_glitch": ""},
            "coc_delta": {}, "dropped": [],
            "_degraded": True, "_error": error,
        }
    who = res.choice_text or "你做出了选择"
    return {
        "scene": "街头一角",
        "dialogue": [
            {"character": "旁白", "emotion": "neutral",
             "text": f"（{who}。）"},
            {"character": "旁白", "emotion": "neutral",
             "text": res.text or "事情就这样发生了，没有更多解释。"},
        ],
        "choices": [{"text": "继续", "effect": ""}],
        "narrator": {"unreliable": False, "inserted_glitch": ""},
        "coc_delta": {},
        "dropped": [],
        "_degraded": True,
        "_error": error,
    }


# ---------------------------------------------------------------- 状态视图

def public_state(session: dict, note: str = "") -> dict:
    """给前端的完整状态（形状对齐既有两模式，前端可复用大部分渲染）。"""
    run: Run = session["run"]
    st = run.state
    card = None
    res = session.get("_last_resolution")
    if res is not None and res.card_id:
        card = next((c for c in run.card_pool if c["id"] == res.card_id), None)

    panel = run.panel()
    panel["extra"] = _state_extra(st)
    # ★ t63：物品栏视图进 coc 段（前端物品栏 UI 的数据源，契约见报告 §5）
    panel["inventory"] = _state.inventory_view(st)
    text = session.get("_last_text")
    if text is None and res is not None:
        text = _fallback_text(res)

    scene = (text or {}).get("scene") or (card or {}).get("body", "（街上没有人。）")
    dialogue = (text or {}).get("dialogue") or [
        {"character": "旁白", "emotion": "neutral",
         "text": (card or {}).get("body", "（街上没有人。）")}]
    # ★ t63：选项带**门槛可见性**。
    #   · LLM 给了 choices（`text.choices`）时，用卡片的真实选项来补 `available`
    #     —— 因为 LLM 的 choices 只是文案，真正决定可选性的是卡片数据；
    #   · 没走卡片的开场（`_last_resolution` 为 None）则退回纯文案列表。
    _card_choices = (card or {}).get("choices")
    if _card_choices:
        choices = run.choices_view(card)
    else:
        choices = (text or {}).get("choices") or [
            {"text": str(c.get("text", "")), "effect": ""}
            for c in (_card_choices or [])] or [{"text": "继续", "effect": ""}]

    title = f"CoC · {st.get('old_god')} · {st.get('difficulty')}"
    sc = session.get("scenario") or {}
    if session.get("scenario_id") and sc.get("title"):
        title = f"CoC · {sc['title']} · {st.get('difficulty')}"
    return {
        "sid": st["sid"],
        "mode": MODE,
        "title": title,
        "scene": scene,
        "background_id": None,
        "dialogue": dialogue,
        "choices": choices,
        "present": _participants_for(run, res) if res else [],
        "scene_id": "coc",
        "scene_visits": 1,
        "scene_roster": [],
        "scene_revisit": False,
        "cg_active": False,
        "cg_id": "",
        "cg_title": "",
        "characters_meta": [],
        "catchphrases": [],
        "outfits": {},
        "content_rating": session.get("content_rating", "all"),
        "r18_enabled": bool(session.get("r18_enabled")),
        "adult_intensity": session.get("adult_intensity", "成熟"),
        "allow_forced": bool(session.get("allow_forced")),
        "cg_saved": False,
        "cmd_cg": False,
        "cmd_cg_saved": False,
        "llm_degraded": bool((session.get("_last_text") or {}).get("_degraded")),
        "protagonist": st.get("character"),
        "npcs": [],
        # ★ 前端与探针都读这一项
        "coc": panel,
        "stat_config": [],
        "assets": {"portraits": {}, "backgrounds": {}},
        "bg_map": {},
        "cg_url": "",
        "turn_no": int(st.get("turn", 0)),
        "mock": True,
        "note": note,
        "ending": st.get("ending"),
        "ending_title": run.ending_title(st["ending"]) if st.get("ending") else "",
        "ai_directive": str(st.get("ai_directive") or ""),
    }


def _state_extra(st: dict) -> list[dict]:
    from . import state as _st
    return _st.extra_dimensions(st)


# ---------------------------------------------------------------- 存档槽

def _slots_dir() -> str:
    """存档槽目录：**与既有模式同一个**（`DATA_DIR/slots`，见 `app/game._slots_dir`）。

    每次调用重新取 `paths.slots_dir()`（跟随 `app.config.DATA_DIR` / `TSF_DATA_DIR`），
    不再读顶层常量 —— 顶层常量是 import 时的快照，进程内换数据根会拿到旧路径。
    """
    d = paths.slots_dir()
    os.makedirs(d, exist_ok=True)
    return d


def save_slot(sid: str, name: str) -> dict:
    """存进**全局存档槽**（与既有模式同一目录，故 `/api/slots` 自动列出）。

    信封与 `app.game.save_slot` 对齐（`slot_name` / `slot_created` / `sid` / `log`），
    这样 `game.list_slots()` **无需改动**就能列出 coc 存档。
    """
    session = get(sid)
    if session is None:
        raise CocError("会话不存在或已过期")
    run: Run = session["run"]
    snap = run.snapshot()
    slot_id = f"slot{int(time.time() * 1000)}"
    envelope = {
        "mode": MODE,
        "sid": snap["sid"],
        "slot_name": (str(name or "存档").strip() or "存档")[:30],
        "slot_created": time.time(),
        "title": (f"CoC · {(session.get('scenario') or {}).get('title')} · {snap.get('difficulty')}"
                  if session.get("scenario_id") and (session.get("scenario") or {}).get("title")
                  else f"CoC · {snap.get('old_god')} · {snap.get('difficulty')}"),
        "log": snap.get("log", []),
        "content_rating": session.get("content_rating", "all"),
        "r18_enabled": bool(session.get("r18_enabled")),
        "adult_intensity": session.get("adult_intensity", "成熟"),
        "allow_forced": bool(session.get("allow_forced")),
        # ★ 完整 coc 状态（含 rng_state），读档据此原样恢复
        "coc_state": snap,
        # t73：自定义剧本随存档走（读档要能复原同一套地点/谜团/古神/卡池）
        "scenario": session.get("scenario") or None,
        "scenario_id": session.get("scenario_id") or "",
    }
    path = os.path.join(_slots_dir(), f"{slot_id}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(envelope, fh, ensure_ascii=False, indent=1, sort_keys=True)
    return {"slot_id": slot_id, "name": envelope["slot_name"],
            "turn_no": int(snap.get("turn", 0)), "mode": MODE}


def peek_slot_mode(slot_id: str) -> str:
    """只读 envelope 的 `mode`（用于分发点判断，**不注册会话、不产生副作用**）。"""
    path = os.path.join(paths.slots_dir(), f"{str(slot_id)}.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("mode") or "")
    except (json.JSONDecodeError, OSError):
        return ""


def load_slot(slot_id: str) -> dict:
    """从全局存档槽恢复 coc 会话（重建 Run 与 rng，注册进 coc 会话表）。"""
    path = os.path.join(paths.slots_dir(), f"{str(slot_id)}.json")
    if not os.path.isfile(path):
        raise CocError("存档不存在")
    with open(path, "r", encoding="utf-8") as fh:
        env = json.load(fh)
    snap = env.get("coc_state")
    if not isinstance(snap, dict):
        raise CocError("这不是 CoC 存档")
    cfg = load_tuning()
    # t73：读档时把剧本取回并重新校验（老档没有该键 ⇒ None ⇒ 回落随包内容，行为不变）
    from . import scenario as _scn
    scen = _scn.normalize(env.get("scenario")) if env.get("scenario") else None
    run = Run.resume(snap, cfg, scenario=scen)
    session = {
        "mode": MODE,
        "sid": run.state["sid"],
        "run": run,
        "cfg": cfg,
        "scenario": scen,
        "scenario_id": str(env.get("scenario_id") or ""),
        "content_rating": env.get("content_rating", "all"),
        "r18_enabled": bool(env.get("r18_enabled")),
        "adult_intensity": env.get("adult_intensity", "成熟"),
        "allow_forced": bool(env.get("allow_forced")),
        "created": time.time(),
        "_restored": True,
        "_first_done": True,
    }
    _SESSIONS[session["sid"]] = session
    return public_state(session)


# ---------------------------------------------------------------- 面板/配置

def list_slots() -> list[dict]:
    """coc 的存档槽（读**与既有模式同一个**目录，只挑 `mode=="coc"`）。"""
    out = []
    d = paths.slots_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                env = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if env.get("mode") != MODE:
            continue
        out.append({
            "slot_id": os.path.splitext(fn)[0],
            "name": env.get("slot_name") or os.path.splitext(fn)[0],
            "title": env.get("title", ""),
            "turn_no": int((env.get("coc_state") or {}).get("turn", 0)),
            "created": env.get("slot_created", 0),
            "sid": env.get("sid", ""),
            "mode": MODE,
        })
    out.sort(key=lambda x: x.get("created", 0), reverse=True)
    return out


def config_view(cfg: dict | None = None) -> dict:
    """前端开局面板需要的可选值（不进 LLM 提示）。"""
    if cfg is None:
        cfg = load_tuning()
    diff_all = t(cfg, "difficulty")
    return {
        "difficulties": list(DIFFICULTIES),
        "death_modes": list(DEATH_MODES),
        "old_gods": [g["id"] for g in content.old_gods()],
        "characters": list(content.cast()),
        "mystery_count": {k: diff_all[k]["mystery_count"] for k in DIFFICULTIES},
        "erotic_tiers": list(t(cfg, "erotic.erotic_tiers")),
        "horror_tiers": list(t(cfg, "erotic.horror_tiers")),
        "llm_delta_whitelist": list(t(cfg, "llm_delta_whitelist")),
        # 1.9.19：剧本预设 / 固定商店 / SAN 开局说明
        "scenarios": _presets.summaries(),
        "shops": _shop.summaries(),
        "sanity": {"max": int(t(cfg, "sanity.max")),
                   "bout_threshold": int(t(cfg, "sanity.bout_threshold"))},
    }

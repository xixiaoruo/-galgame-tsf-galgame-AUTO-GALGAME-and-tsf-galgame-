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

from . import content
from . import paths
from . import prompt as _prompt
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

def start(payload: dict | None = None, cfg: dict | None = None) -> dict:
    """开局：造 Run → 落盘 → 返回 `public_state`。"""
    if cfg is None:
        cfg = load_tuning()
    p = dict(payload or {})
    seed = p.get("seed")
    if seed in ("", None):
        seed = int(time.time() * 1000) % (2 ** 31)
    run = Run.start({
        "difficulty": p.get("difficulty") or "Initiate",
        "death_mode": p.get("death_mode") or "last_stand",
        "old_god": p.get("old_god") or "Goizo",
        "character": p.get("character") or "Kirie",
        "background": p.get("background") or "Commoner",
        "seed": int(seed),
        "stats": p.get("stats") or None,
    }, cfg)
    session = {
        "mode": MODE,
        "sid": run.state["sid"],
        "run": run,
        "cfg": cfg,
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
        res = run.resolve_card(card, max(0, min(int(choice_index or 0), 3)))
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
        client = LLMClient(_llm_cfg())
        if getattr(client, "mock", False):
            return dict(_fallback_text(res), _degraded=True, _error="llm.mock")
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
            run.apply(turn["coc_delta"], reason="llm_proposal")
        return turn
    except Exception as exc:   # 兜底：绝不因叙事失败让对局崩
        return dict(_fallback_text(res, error=repr(exc)), _degraded=True)


def _persist(session: dict) -> None:
    """落盘当前会话（best-effort；失败只记不抛）。"""
    try:
        session["run"].save()
    except OSError:
        pass


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
    text = session.get("_last_text")
    if text is None and res is not None:
        text = _fallback_text(res)

    scene = (text or {}).get("scene") or (card or {}).get("body", "（街上没有人。）")
    dialogue = (text or {}).get("dialogue") or [
        {"character": "旁白", "emotion": "neutral",
         "text": (card or {}).get("body", "（街上没有人。）")}]
    choices = (text or {}).get("choices") or [
        {"text": str(c.get("text", "")), "effect": ""}
        for c in ((card or {}).get("choices") or [])] or [{"text": "继续", "effect": ""}]

    return {
        "sid": st["sid"],
        "mode": MODE,
        "title": f"CoC · {st.get('old_god')} · {st.get('difficulty')}",
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
    }


def _state_extra(st: dict) -> list[dict]:
    from . import state as _st
    return _st.extra_dimensions(st)


# ---------------------------------------------------------------- 存档槽

def _slots_dir() -> str:
    os.makedirs(paths.SLOTS_DIR, exist_ok=True)
    return paths.SLOTS_DIR


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
        "title": f"CoC · {snap.get('old_god')} · {snap.get('difficulty')}",
        "log": snap.get("log", []),
        "content_rating": session.get("content_rating", "all"),
        "r18_enabled": bool(session.get("r18_enabled")),
        "adult_intensity": session.get("adult_intensity", "成熟"),
        "allow_forced": bool(session.get("allow_forced")),
        # ★ 完整 coc 状态（含 rng_state），读档据此原样恢复
        "coc_state": snap,
    }
    path = os.path.join(_slots_dir(), f"{slot_id}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(envelope, fh, ensure_ascii=False, indent=1, sort_keys=True)
    return {"slot_id": slot_id, "name": envelope["slot_name"],
            "turn_no": int(snap.get("turn", 0)), "mode": MODE}


def peek_slot_mode(slot_id: str) -> str:
    """只读 envelope 的 `mode`（用于分发点判断，**不注册会话、不产生副作用**）。"""
    path = os.path.join(paths.SLOTS_DIR, f"{str(slot_id)}.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("mode") or "")
    except (json.JSONDecodeError, OSError):
        return ""


def load_slot(slot_id: str) -> dict:
    """从全局存档槽恢复 coc 会话（重建 Run 与 rng，注册进 coc 会话表）。"""
    path = os.path.join(paths.SLOTS_DIR, f"{str(slot_id)}.json")
    if not os.path.isfile(path):
        raise CocError("存档不存在")
    with open(path, "r", encoding="utf-8") as fh:
        env = json.load(fh)
    snap = env.get("coc_state")
    if not isinstance(snap, dict):
        raise CocError("这不是 CoC 存档")
    cfg = load_tuning()
    run = Run.resume(snap, cfg)
    session = {
        "mode": MODE,
        "sid": run.state["sid"],
        "run": run,
        "cfg": cfg,
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
    """coc 的存档槽（读同一目录，只挑 `mode=="coc"`）。"""
    out = []
    try:
        names = sorted(os.listdir(paths.SLOTS_DIR))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(paths.SLOTS_DIR, fn), "r", encoding="utf-8") as fh:
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
    }

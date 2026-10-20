"""普通 Galgame 模式会话：心情 / 好感 / 性敏感 / 高潮度 数值系统。

与 TSF 模式（app/game.py）共用同一套会话表、资产管线、场景/CG/换装机制；
本模块负责普通模式的协议解析、数值应用与命令系统（TSF 模式代码零改动）。
"""
import hashlib
import json
import logging
import time
import uuid

import asyncio   # B-05/M-02：命令场面 CG 合成走 asyncio.to_thread（t15 授权改动）

from . import characters as charm
from . import config as cfgmod
from . import game
from . import prompts
from . import scenes as scenemod
from . import vn_prompts
from .image import ImageClient
from .llm import LLMClient, LLMError, MockSetup

log = logging.getLogger("galgame.vn")

MODE = "vn"

STATE_KEYS = ["mood", "affection", "arousal", "climax"]

STATE_META = {
    "mood": {"name": "心情", "icon": "♪", "start": 45,
             "hint": "当前心情愉悦度：高=轻松满足，低=低落压抑"},
    "affection": {"name": "好感", "icon": "❣", "start": 20,
                  "hint": "角色对玩家的好感度，随互动起伏"},
    "arousal": {"name": "性敏感", "icon": "❀", "start": 0,
                "hint": "性兴奋/敏感度：低=心如止水，高=轻易动情"},
    "climax": {"name": "高潮度", "icon": "◉", "start": 0,
               "hint": "高潮累积与达成度：每次高潮后应回落"},
}

# 命令面板数值影响（沿用 game.COMMAND_ACTIONS 的动作名）
VN_COMMAND_DELTA = {
    "kiss": {"affection": 2, "arousal": 4},
    "hug": {"affection": 2, "arousal": 3},
    "sex": {"arousal": 10, "climax": 8},
    "blowjob": {"arousal": 8, "climax": 6},
}


def initial_states(config: list[dict] | None = None) -> dict:
    cfg = config if isinstance(config, list) and config else None
    if cfg is None:
        cfg = _default_config()
    return {c["key"]: int(c["start"]) for c in cfg}


async def random_setup() -> dict:
    """普通模式的随机开局设定（非 TSF 题材）。"""
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return MockSetup.random()
    try:
        data = await llm.chat_json([
            {"role": "system", "content": vn_prompts.VN_RANDOM_SETUP_SYSTEM},
            {"role": "user", "content": "请生成一个全新的开局设定。"},
        ], temperature=1.0)
        if (not isinstance(data, dict)
                or not str(data.get("world") or "").strip()
                or not isinstance(data.get("characters"), list)
                or not data.get("characters")):
            raise LLMError("随机设定缺少 world/characters")
        return data
    except LLMError as e:
        log.warning("VN random_setup 失败，回退本地随机设定：%s", e)
        return MockSetup.random()


def sanitize_delta(delta, keys: list[str] | None = None) -> dict:
    """清洗 LLM 返回的数值变化：只认会话状态键（含玩家自定义），单项 ±15。"""
    known = list(keys) if keys is not None else STATE_KEYS
    out = {}
    if isinstance(delta, dict):
        for k, v in delta.items():
            if k in known:
                try:
                    out[k] = max(-15, min(15, int(round(float(v)))))
                except (TypeError, ValueError):
                    continue
    return out


def apply_delta(states: dict, delta: dict) -> dict:
    for k, v in delta.items():
        if k in states:
            states[k] = max(0, min(100, states[k] + v))
    return states


def panel_view(states: dict, config: list[dict] | None = None) -> dict:
    cfg = config if isinstance(config, list) and config else _default_config()
    return {
        "states": [
            {"key": c["key"], "name": c["name"], "icon": c["icon"],
             "value": int(states.get(c["key"], c["start"])), "hint": c["hint"]}
            for c in cfg
        ],
    }


def _default_config() -> list[dict]:
    """普通模式内置状态默认配置（可被玩家开局前编辑覆盖）。"""
    return [{"key": k, "name": m["name"], "icon": m["icon"],
             "start": int(m["start"]), "hint": m["hint"]}
            for k, m in STATE_META.items()]


def states_block(states: dict, config: list[dict] | None = None) -> str:
    cfg = config if isinstance(config, list) and config else _default_config()
    lines = []
    for c in cfg:
        lines.append(f"- {c['name']}：{int(states.get(c['key'], c['start']))}/100"
                     f"（{c['hint']}）")
    return "\n".join(lines)


# ---------- 每角色独立数值（情绪 / 好感 / 身体敏感度 / 高潮度）----------
#
# 两种模式共用：每个出场角色（含主角）都有一套自己的数值，存在
# session["char_states"] = {"角色名": {key: int}}。TSF 模式下主角另外还有
# 一套转变数值（prot["stats"]），两者互不影响。

def char_names(session: dict) -> list[str]:
    """全部角色名：主角在前，其后为其他角色（去重保序）。"""
    names = [session["protagonist"]["name"]]
    names += [c["name"] for c in (session.get("characters") or [])]
    return list(dict.fromkeys(n for n in names if n))


def _char_stat_config(session: dict) -> list[dict]:
    """逐角色数值的键集合：普通模式沿用会话配置（玩家可自定义编辑），
    TSF 模式固定用这四项（TSF 的转变数值是另一套，不混进来）。"""
    if session.get("mode") == MODE:
        return game.session_stat_config(session)
    return _default_config()


def resolve_char(name, names: list[str], prot: str) -> str | None:
    """把 LLM 给的角色名归一到会话内角色；支持「主角/玩家/我」这类指代。"""
    name = str(name or "").strip()
    if not name:
        return None
    if name in names:
        return name
    if name in ("主角", "玩家", "我", "主人公", "ME", "me") or name == prot:
        return prot
    return None


def ensure_char_states(session: dict) -> dict:
    """确保 session["char_states"] 覆盖全部角色，并返回该存储。

    迁移：老会话只有一份 session["states"]（语义是「角色对玩家的好感」，
    即第一位其他角色），首次调用时归给它；其余角色取初始值。
    """
    store = session.get("char_states")
    if not isinstance(store, dict):
        store = {}
    cfg = _char_stat_config(session)
    legacy = session.get("states")
    legacy = dict(legacy) if isinstance(legacy, dict) else None
    for idx, name in enumerate(char_names(session)):
        cur = store.get(name)
        if not isinstance(cur, dict):
            cur = {}
        if not cur:
            if legacy is not None and idx > 0:
                cur = {k: int(v) for k, v in legacy.items()
                       if isinstance(v, (int, float))}
                legacy = None
            else:
                cur = initial_states(cfg)
        for c in cfg:                 # 会话中途改过状态配置时补齐新键
            cur.setdefault(c["key"], int(c["start"]))
        store[name] = cur
    session["char_states"] = store
    return store


def char_states_block(session: dict, config: list[dict] | None = None) -> str:
    """逐角色状态块（注入剧情提示词）。"""
    cfg = config if isinstance(config, list) and config else _char_stat_config(session)
    store = ensure_char_states(session)
    prot = session["protagonist"]["name"]
    lines = ["每个角色一套独立数值（0-100），描写必须与各自数值相符："]
    for name in char_names(session):
        st = store.get(name) or {}
        vals = "、".join(f"{c['name']} {int(st.get(c['key'], c['start']))}" for c in cfg)
        lines.append(f"- {name}{'（主角）' if name == prot else ''}：{vals}")
    lines.append("（含义：" + "；".join(f"{c['name']}={c['hint']}" for c in cfg) + "）")
    lines.append("主角的「好感」指 TA 对当前互动对象的亲近度，其余角色的「好感」"
                 "指该角色对主角的好感度。")
    rel = relationship_block(session)
    if rel:
        lines.append("【关系阶段与情感记忆（称呼、距离感与主动性必须与此相符；"
                     "关系升级要写出铺垫，不能凭空跳到恋人）】")
        lines.append(rel)
    return "\n".join(lines)


def sanitize_char_delta(delta, keys: list[str] | None = None,
                        names: list[str] | None = None,
                        prot: str = "") -> dict:
    """清洗逐角色数值变化：{"角色名": {"affection": 2, ...}}，单项 ±15。

    兼容旧版扁平格式 {"affection": 2, ...}：按旧语义归到第一位其他角色。
    """
    known = list(keys) if keys is not None else STATE_KEYS
    names = list(names) if names else []
    out: dict[str, dict] = {}
    if not isinstance(delta, dict):
        return out
    if delta and not any(isinstance(v, (dict, list)) for v in delta.values()) \
            and any(k in known for k in delta):
        target = next((n for n in names if n != prot), None) or prot
        clean = sanitize_delta(delta, known)
        return {target: clean} if (target and clean) else {}
    for name, d in delta.items():
        target = resolve_char(name, names, prot)
        if target is None:
            continue
        clean = sanitize_delta(d, known)
        if not clean:
            continue
        merged = out.setdefault(target, {})
        for k, v in clean.items():
            merged[k] = max(-15, min(15, merged.get(k, 0) + v))
    return out


def apply_char_delta(session: dict, delta: dict) -> dict:
    """把逐角色变化应用到 char_states，返回实际应用的部分。"""
    store = ensure_char_states(session)
    applied: dict[str, dict] = {}
    if not isinstance(delta, dict):
        return applied
    for name, d in delta.items():
        if name not in store or not isinstance(d, dict):
            continue
        apply_delta(store[name], d)
        applied[name] = dict(d)
    return applied


def char_schema(session: dict) -> str:
    """逐角色数值的 JSON schema 片段（供提示词使用）。"""
    return ", ".join(f'"{c["key"]}": 0' for c in _char_stat_config(session))


# ---------- 关系阶段 / 情感记忆（1.9.0）----------
#
# 旧版只有四个 0-100 数值：心情、好感、性敏感、高潮度。数值本身能变，
# 但 AI 不知道「现在算什么关系」，玩家也只在面板上看到四个数字。1.9.0
# 在不增加新数值键（不破坏旧档/自定义状态栏）的前提下补了三层：
#   1) 关系阶段：由「好感」映射成 陌生→认识→熟悉→亲近→心动→依恋→恋人，
#      注入提示词，要求 AI 的称呼、距离感、主动性随阶段变化；
#   2) 情感记忆：每幕有实际变化时记一条「场景 + 原因 + 数值」，注入最近几条，
#      让 AI 能回扣前文，而不是每幕重新认识一次；
#   3) 缺席衰减：长期不出场的角色，心情回归平静、身体反应与好感缓慢回落，
#      避免「放置半年还是满好感」。
#
# 所有派生状态都存 session（旧档首次访问时懒初始化），不改 char_states 的键。

BOND_STAGES: list[tuple[int, str, str]] = [
    (0, "陌生", "彼此还不熟，保持礼貌距离"),
    (15, "认识", "知道名字与日常身份，会点头打招呼"),
    (30, "熟悉", "能自然搭话，开始互相留意"),
    (45, "亲近", "愿意分享私事，有了一些默契"),
    (60, "心动", "目光会为对方停留，容易在意细节"),
    (75, "依恋", "情绪被对方牵动，会主动靠近"),
    (90, "恋人", "彼此确认了特别的地位"),
]

_MEMORY_LIMIT = 80
_BOND_EVENT_LIMIT = 60


def bond_stage(affection: int) -> dict:
    """好感值 → 关系阶段（纯函数，供面板/提示词/测试共用）。"""
    try:
        v = max(0, min(100, int(affection)))
    except (TypeError, ValueError):
        v = 0
    idx = 0
    for i, (threshold, _label, _hint) in enumerate(BOND_STAGES):
        if v >= threshold:
            idx = i
    threshold, label, hint = BOND_STAGES[idx]
    nxt = BOND_STAGES[idx + 1][0] if idx + 1 < len(BOND_STAGES) else None
    return {"index": idx, "label": label, "hint": hint,
            "threshold": threshold, "next_at": nxt, "score": v}


def _bond_store(session: dict) -> dict:
    store = session.get("bond_stage")
    if not isinstance(store, dict):
        store = {}
        session["bond_stage"] = store
    return store


def _emotion_notes(session: dict) -> dict:
    notes = session.get("emotion_notes")
    if not isinstance(notes, dict):
        notes = {}
        session["emotion_notes"] = notes
    return notes


def _emotion_memory(session: dict) -> list[dict]:
    mem = session.get("emotion_memory")
    if not isinstance(mem, list):
        mem = []
        session["emotion_memory"] = mem
    return mem


def _absence_store(session: dict) -> dict:
    store = session.get("char_absence")
    if not isinstance(store, dict):
        store = {}
        session["char_absence"] = store
    return store


def ensure_bonds(session: dict) -> dict:
    """确保每个角色都有关系阶段记录（旧档懒迁移，不覆盖玩家进度）。"""
    store = ensure_char_states(session)
    bonds = _bond_store(session)
    for name in char_names(session):
        st = store.get(name) or {}
        stage = bond_stage(int(st.get("affection", 0)))
        cur = bonds.get(name)
        if not isinstance(cur, dict):
            bonds[name] = {"index": stage["index"], "label": stage["label"],
                           "score": stage["score"]}
        else:
            # 数值被玩家/读档改动时，以当前好感重算，但保留历史最高阶段
            cur["label"] = stage["label"]
            cur["score"] = stage["score"]
            cur["index"] = int(cur.get("index", stage["index"]))
            if stage["index"] > cur["index"]:
                cur["index"] = stage["index"]
    return bonds


def bond_view(session: dict, name: str) -> dict:
    ensure_bonds(session)
    store = ensure_char_states(session)
    st = store.get(name) or {}
    stage = bond_stage(int(st.get("affection", 0)))
    cur = (_bond_store(session).get(name) or {})
    # 面板展示「当前好感对应阶段」；历史最高阶段单独给出（不覆盖显示）
    return dict(stage, peak_index=int(cur.get("index", stage["index"])),
                peak_label=BOND_STAGES[min(int(cur.get("index", stage["index"])),
                                           len(BOND_STAGES) - 1)][1])


def emotion_note(session: dict, name: str) -> dict | None:
    note = _emotion_notes(session).get(name)
    return dict(note) if isinstance(note, dict) else None


def _choice_text(session: dict) -> str:
    try:
        choice = session["log"][-1].get("choice") or {}
    except (KeyError, IndexError):
        return ""
    return str(choice.get("text", "")).strip()[:60]


def record_turn_emotions(session: dict, turn: dict) -> list[dict]:
    """记录本幕情感变化 + 关系里程碑；返回本幕升级的角色列表。

    必须在 states_delta 已应用、scene 已确定之后调用。
    """
    store = ensure_char_states(session)
    bonds = _bond_store(session)
    delta = turn.get("states_delta") if isinstance(turn.get("states_delta"), dict) else {}
    scene = str(turn.get("scene", "") or "……")[:30]
    choice = _choice_text(session)
    prot = session["protagonist"]["name"]
    ups: list[dict] = []
    notes = _emotion_notes(session)
    mem = _emotion_memory(session)
    for name in char_names(session):
        d = delta.get(name)
        meaningful = {}
        if isinstance(d, dict) and d:
            meaningful = {k: int(v) for k, v in d.items()
                          if k in ("mood", "affection", "arousal", "climax")
                          and int(v) != 0}
            if meaningful:
                bits = []
                for k, label in (("affection", "好感"), ("mood", "心情"),
                                 ("arousal", "敏感"), ("climax", "高潮")):
                    if meaningful.get(k):
                        v = meaningful[k]
                        bits.append(f"{label}{'+' if v > 0 else ''}{v}")
                reason = choice or "剧情推进"
                text = f"在「{scene}」：{reason}（{'、'.join(bits)}）"
                note = {"text": text[:160], "scene": scene,
                        "turn": len(session.get("log") or []) + 1,
                        "delta": meaningful}
                notes[name] = note
                mem.append({"name": name, **note})
                if len(mem) > _MEMORY_LIMIT:
                    del mem[:len(mem) - _MEMORY_LIMIT]
        st = store.get(name) or {}
        cur_aff = int(st.get("affection", 0))
        d_aff = int(meaningful.get("affection", 0)) if meaningful else 0
        before_stage = bond_stage(cur_aff - d_aff)
        stage = bond_stage(cur_aff)
        old = bonds.get(name) or {}
        old_idx = int(old.get("index", before_stage["index"]))
        # 关系里程碑：只有「本幕跨过阈值」才算升级；历史峰值已更高时不重复
        if stage["index"] > old_idx:
            bonds[name] = {"index": stage["index"], "label": stage["label"],
                           "score": stage["score"]}
            ev = {"name": name, "label": stage["label"],
                  "from": BOND_STAGES[old_idx][1], "turn": len(session.get("log") or []) + 1,
                  "scene": scene, "protagonist": name == prot}
            ups.append(ev)
            events = session.setdefault("bond_events", [])
            events.append(ev)
            if len(events) > _BOND_EVENT_LIMIT:
                del events[:len(events) - _BOND_EVENT_LIMIT]
    if ups:
        turn["bond_up"] = ups
    return ups


def apply_absence_decay(session: dict, present: list[str] | None = None,
                        *, is_opening: bool = False) -> dict:
    """长期不出场的角色缓慢回归：心情向 45、敏感/高潮回落、好感每 4 幕 -1。

    只在非开局幕调用；主角永不衰减。返回实际衰减的角色与数值（便于测试）。
    """
    if is_opening or not session.get("log"):
        return {}
    store = ensure_char_states(session)
    absent = _absence_store(session)
    present = {str(x) for x in (present or [])}
    prot = session["protagonist"]["name"]
    changed: dict[str, dict] = {}
    for name in char_names(session):
        if name == prot:
            absent.pop(name, None)
            continue
        if name in present:
            absent.pop(name, None)
            continue
        n = int(absent.get(name, 0) or 0) + 1
        absent[name] = n
        st = store.get(name)
        if not isinstance(st, dict):
            continue
        before = dict(st)
        # 每 2 幕：心情向 45 靠 1，敏感 -1；每幕：高潮 -5；每 4 幕：好感 -1
        if n % 2 == 0:
            mood = int(st.get("mood", 45))
            if mood > 45:
                st["mood"] = mood - 1
            elif mood < 45:
                st["mood"] = mood + 1
            if int(st.get("arousal", 0)) > 0:
                st["arousal"] = max(0, int(st["arousal"]) - 1)
        if int(st.get("climax", 0)) > 0:
            st["climax"] = max(0, int(st["climax"]) - 5)
        if n % 4 == 0 and int(st.get("affection", 0)) > 0:
            st["affection"] = max(0, int(st["affection"]) - 1)
        diff = {k: st[k] - before.get(k, st[k]) for k in st
                if st[k] != before.get(k, st[k])}
        if diff:
            changed[name] = {"absent_turns": n, "delta": diff}
    return changed


def relationship_block(session: dict) -> str:
    """关系阶段 + 最近情感记忆（注入提示词）。空会话返回空串。"""
    names = char_names(session)
    if not names:
        return ""
    ensure_bonds(session)
    mem = _emotion_memory(session)
    lines = []
    for name in names:
        b = bond_view(session, name)
        lines.append(f"- {name}：关系 {b['label']}（好感 {b['score']}/100，"
                     f"{b['hint']}）")
    # 每个角色只保留最近一条记忆，避免长局提示词膨胀
    latest = {}
    for m in mem:
        latest[m.get("name")] = m
    recent = [m for m in latest.values() if m.get("text")]
    if recent:
        lines.append("最近的情感记忆（相关时可自然回扣，不要机械复述）：")
        for m in recent[-6:]:
            lines.append(f"- {m['text']}")
    return "\n".join(lines)


def panel_view_chars(session: dict, config: list[dict] | None = None) -> dict:
    """前端右侧面板数据：每个角色一组数值 + 关系阶段 + 最近情感。"""
    cfg = config if isinstance(config, list) and config else _char_stat_config(session)
    store = ensure_char_states(session)
    prot = session["protagonist"]["name"]
    ensure_bonds(session)
    return {
        "characters": [
            {"name": name,
             "is_protagonist": name == prot,
             "role": next((c.get("role", "") for c in
                           ([session["protagonist"]] + (session.get("characters") or []))
                           if c.get("name") == name), ""),
             "bond": bond_view(session, name),
             "emotion_note": emotion_note(session, name),
             "states": [
                 {"key": c["key"], "name": c["name"], "icon": c["icon"],
                  "value": int((store.get(name) or {}).get(c["key"], c["start"])),
                  "hint": c["hint"]}
                 for c in cfg
             ]}
            for name in char_names(session)
        ]
    }


# ---------- 轮次清洗 ----------

def _sanitize_turn(turn: dict, emotions: list[str], characters: list[dict],
                   pname: str, adult: bool, rename_hint: str = "",
                   stat_keys: list[str] | None = None) -> dict:
    """校验并规范化 VN 轮次 JSON（协议与 TSF 相同，数值键为 states_delta）。"""
    import re
    known = {c["name"] for c in characters} | {pname}
    if rename_hint:
        known.add(rename_hint)   # 本幕主角新名字：不再当作新 NPC
    name_re = re.compile(r"^[\u4e00-\u9fa5·]{2,5}$")
    dialkeys = list(stat_keys) if stat_keys is not None else STATE_KEYS
    dialogue = []
    new_names = []
    for d in turn.get("dialogue") or []:
        if not isinstance(d, dict):
            continue
        text = str(d.get("text", "")).strip()
        if not text:
            continue
        name = str(d.get("character", "旁白")).strip() or "旁白"
        emotion = str(d.get("emotion", "neutral")).strip()
        if name not in known and name != "旁白":
            if name_re.fullmatch(name):
                new_names.append(name)
            else:
                name = "旁白"
        if name == pname or name == "旁白":
            emotion = "neutral"
        elif emotion not in emotions:
            emotion = "neutral"
        dialogue.append({"character": name, "emotion": emotion, "text": text})
    if not dialogue:
        dialogue = [{"character": "旁白", "emotion": "neutral",
                     "text": "（一阵短暂的沉默。）"}]

    present = [str(x).strip() for x in (turn.get("present") or [])
               if str(x).strip() in (known - {pname})
               or (str(x).strip() != pname and name_re.fullmatch(str(x).strip()))]
    # 场景名单按 LLM 判定严格呈现：名单为空 = 无其他角色出场（不兜底全员）

    choices = []
    for c in turn.get("choices") or []:
        if isinstance(c, dict) and str(c.get("text", "")).strip():
            bias = str(c.get("bias", "")).strip()
            if bias not in dialkeys:
                bias = ""
            choices.append({
                "text": str(c["text"]).strip(),
                "effect": str(c.get("effect", "")).strip(),
                "bias": bias,
            })
    if not choices:
        choices = [{"text": "继续", "effect": "", "bias": ""}]

    cg = turn.get("cg") or {}
    if not isinstance(cg, dict):
        cg = {}
    cg_active = bool(cg.get("active"))
    cg_title = str(cg.get("title", "")).strip()[:20]
    cg_prompt = str(cg.get("prompt", "")).strip()[:300]
    if cg_active and not cg_prompt:
        cg_active = False

    outfit = {}
    raw_outfit = turn.get("outfit") or {}
    if isinstance(raw_outfit, dict):
        for k, v in raw_outfit.items():
            k = str(k).strip()
            v = str(v).strip()[:60]
            if v and (k in known or name_re.fullmatch(k)):
                outfit[k] = v

    return {
        "scene": str(turn.get("scene", "")).strip()[:30] or "……",
        "background_hint": str(turn.get("background_hint", "")).strip()[:200],
        "is_new_background": bool(turn.get("is_new_background")),
        "dialogue": dialogue[:6],
        "choices": choices[:4],
        "present": present,
        "new_names": list(dict.fromkeys(new_names)),
        "new_characters": [
            charm.sanitize_character(c)
            for c in (turn.get("new_characters") or [])
            if isinstance(c, dict)
            and str(charm.sanitize_character(c).get("name", "")).strip()
        ][:3],
        "outfit": outfit,
        "cg": {"active": cg_active, "title": cg_title, "prompt": cg_prompt},
        "states_delta": sanitize_char_delta(
            turn.get("states_delta"), dialkeys,
            [pname] + [c["name"] for c in characters], pname),
    }


# ---------- 会话消息 ----------

def _vn_system(session: dict) -> str:
    cfg = cfgmod.load_config()["game"]
    adult = session.get("adult", False)
    outfit_cn = cfg.get("outfit_cn", {})
    world = session["world"]
    if session.get("summary"):
        world = f"{world}\n\n【前情提要】\n{session['summary']}"
    stat_cfg = game.session_stat_config(session)
    stat_keys = [c["key"] for c in stat_cfg]
    states_schema = ", ".join(f'"{k}": 0' for k in stat_keys)
    bias_hint = "/".join(stat_keys[:5]) or "（自定义状态）"
    system = vn_prompts.VN_STORY_SYSTEM.format(
        world=world,
        outline=(session.get("outline")
                 if cfg.get("inject_outline", True) else "（大纲已关闭，剧情自由发挥）"),
        pname=session["protagonist"]["name"],
        anchor=session["protagonist"].get("anchor", ""),
        outfit_state=game._outfit_state_block(session),
        lore=prompts.format_lore(
            session.get("lorebook", []),
            prompts.recent_dialogue_text(session["log"]),
        ),
        content_policy=game.content_policy_text(
            session.get("content_rating", "18"),
            bool(session.get("r18_enabled")),
            session.get("adult_intensity", "浓烈"),
            bool(session.get("allow_forced"))),
        directives=("\n".join(f"- {d['text']}"
                              for d in session.get("directives", []) if d.get("enabled"))
                    or "（暂无，按默认风格创作）"),
        characters=vn_prompts.format_characters(session["characters"]),
        emotions="、".join(session["emotions"]),
        states=char_states_block(session, stat_cfg),
        states_schema=states_schema,
        bias_hint=bias_hint,
    )
    # 1.9.20：玩家导演指令（持续生效；放在模板之后，避免 format 的花括号误伤）
    _dir = str(session.get("ai_directive") or "").strip()
    if _dir:
        system += ("\n\n【玩家导演指令（本局持续生效，优先级高于默认风格；"
                   "但不得违反内容分级政策）】\n" + _dir[:500])
    # API 前置注入：启用中的注入文本拼到 system prompt 最前面（全局生效）
    from . import inject as inject_mod
    return inject_mod.inject_block(system)


def build_messages(session: dict) -> list[dict]:
    messages = [{"role": "system", "content": _vn_system(session)}]
    log_entries = session["log"]
    for i, entry in enumerate(log_entries):
        if i == 0:
            user = ("请生成游戏的开场剧情段落（第一段 dialogue 之前可以有一两句话交代"
                    "时间地点与主角的状态）。") if not session.get("summary") \
                else "【前情提要已并入设定】请直接继续推进剧情，不要重新开场。"
        else:
            prev_choice = log_entries[i - 1]["choice"] or {"text": "继续"}
            user = f"玩家选择了：{prev_choice['text']}"
            if prev_choice.get("effect"):
                user += f"（走向：{prev_choice['effect']}）"
            if prev_choice.get("bias"):
                user += (f"（此选项倾向影响「{game._stat_display_name(session, prev_choice['bias'])}」，"
                         "请优先体现）")
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant",
                         "content": json.dumps(entry["turn"], ensure_ascii=False)})
    last_choice = log_entries[-1]["choice"]
    if last_choice:
        user = f"玩家选择了：{last_choice['text']}"
        if last_choice.get("effect"):
            user += f"（走向：{last_choice['effect']}）"
        if last_choice.get("bias"):
            user += (f"（此选项倾向影响「{game._stat_display_name(session, last_choice['bias'])}」，"
                     "请优先体现）")
        messages.append({"role": "user", "content": user})
    return messages


async def _maybe_compress(session: dict) -> None:
    limit = int(cfgmod.load_config()["game"].get("history_rounds", 12))
    log_entries = session["log"]
    if len(log_entries) <= limit:
        return
    old, keep = log_entries[: limit // 2], log_entries[limit // 2:]

    def render(entries):
        parts = []
        for e in entries:
            for d in e["turn"]["dialogue"]:
                parts.append(f"{d['character']}：{d['text']}")
            if e["choice"]:
                parts.append(f"（玩家选择了：{e['choice']['text']}）")
        return "\n".join(parts)

    summary_text = (session.get("summary", "") + "\n" + render(old)).strip()
    try:
        llm = LLMClient(cfgmod.load_config())
        result = await llm.chat([
            {"role": "system", "content": vn_prompts.VN_SUMMARY_SYSTEM},
            {"role": "user", "content": summary_text},
        ], temperature=0.3)
        session["summary"] = result.strip()
    except LLMError as e:
        log.warning("vn summary failed: %s", e)
        session["summary"] = summary_text[:600]
    session["log"] = keep


# ---------- 立绘 ----------

def _char_of(session: dict, name: str) -> dict:
    prot = session["protagonist"]
    if name == prot["name"]:
        return {"name": name,
                "appearance": prot.get("anchor", "") or "成年角色，着装整洁",
                "personality": ""}
    return next((c for c in session["characters"] if c["name"] == name),
                {"name": name, "appearance": "成年角色，着装整洁", "personality": ""})


def _vn_portrait(session: dict, name: str, nonce: int = 0, outfit: str = "",
                 note: str = "") -> None:
    """普通模式角色立绘：按表情差分生成（主角与 NPC 同管线）。"""
    is_prot = name == session["protagonist"]["name"]
    game._start_npc_portrait(session, _char_of(session, name), nonce=nonce,
                             outfit=outfit, note=note,
                             file_base=(session.get("prot_handle") or name)
                             if is_prot else "")


def _start_all_portraits(session: dict) -> None:
    prot = session["protagonist"]
    _vn_portrait(session, prot["name"])
    for c in session["characters"]:
        _vn_portrait(session, c["name"])


def _apply_outfit_now(session: dict, name: str, outfit: str) -> None:
    session[f"{name}|outfit"] = outfit
    _vn_portrait(session, name, outfit=outfit)


# ---------- 数值与轮次收尾 ----------

def _apply_turn_states(session: dict, turn: dict, mock: bool) -> None:
    delta = turn.get("states_delta") or {}
    if mock and not delta:
        # 演示模式下每个角色都缓慢增长，保证面板可见变化
        cfg = _char_stat_config(session)
        delta = {n: {c["key"]: 1 for c in cfg} for n in char_names(session)}
        turn["states_delta"] = delta
    apply_char_delta(session, delta)
    # 1.9.0 感情机制（与 TSF 同源）：缺席回归 + 情感记忆/关系里程碑
    apply_absence_decay(session, turn.get("present") or [],
                        is_opening=not bool(session.get("log")))
    record_turn_emotions(session, turn)


def _post_turn(session: dict, turn: dict) -> None:
    """动态角色池 / 场景登记 / 桥段 CG / 换装差分（TSF 版本之外的 VN 版）。"""
    pool = session["characters"]
    existing = {c["name"] for c in pool}
    prot_name = session["protagonist"]["name"]
    adult = session.get("adult", False)
    # 1.9.0：与 TSF 同一条「只认真正在场的新角色」规则——LLM 幻觉出来的
    # new_characters/new_names（不在 present 里）不再入池、不再白画一张立绘。
    present = set(turn.get("present") or [])
    candidates = [c for c in turn.get("new_characters", [])
                  if c.get("name") in present]
    for name in turn.get("new_names", []):
        if name in present and name not in {c["name"] for c in candidates} \
                and name != prot_name:
            candidates.append({"name": name, "appearance": "", "personality": ""})
    for c in candidates:
        c = charm.sanitize_character(c)
        name = c["name"]
        if not name or name in existing or name in ("旁白", prot_name):
            continue
        if len(pool) >= 10:
            log.warning("vn character pool full (10), ignoring %s", name)
            continue
        appearance = c.get("appearance", "").strip() or "成年角色，着装整洁，外貌待补充"
        entry = dict(c)
        entry["appearance"] = appearance
        entry["auto_added"] = True
        entry["first_seen"] = turn.get("scene", "")
        pool.append(entry)
        existing.add(name)
        _vn_portrait(session, name)

    scenes = session.setdefault("scenes", {})
    scene_name = turn.get("scene", "……")
    scene_id = hashlib.sha256(scene_name.encode("utf-8")).hexdigest()[:10]
    if scene_id not in scenes:
        scenes[scene_id] = {"name": scene_name, "hint": turn.get("background_hint", ""),
                            "bg_id": turn.get("background_id"), "roster": [], "visits": 0}
    sc = scenes[scene_id]
    sc["visits"] += 1
    sc["bg_id"] = turn.get("background_id") or sc.get("bg_id")
    for who in turn.get("present", []):
        if who not in sc["roster"]:
            sc["roster"].append(who)
    turn["scene_id"] = scene_id
    turn["scene_visits"] = sc["visits"]
    turn["scene_roster"] = list(sc["roster"])

    cg = turn.get("cg") or {}
    if cg.get("active"):
        turn["cg_id"] = game._start_cg_task(session, cg)

    # 换装差分（非 18+ 拦截裸体/内衣声明）
    outfits = turn.get("outfit", {})
    for name in [prot_name] + [c["name"] for c in pool]:
        new_outfit = outfits.get(name, "")
        if not new_outfit or session.get(f"{name}|outfit") == new_outfit:
            continue
        if not adult and any(k in new_outfit for k in ("裸体", "内衣", "内裤")):
            continue
        _apply_outfit_now(session, name, new_outfit)


# ---------- 对外流程 ----------

def public_state(session: dict) -> dict:
    turn = session["log"][-1]["turn"]
    char_panel = panel_view_chars(session)
    return {
        "sid": session["sid"],
        "mode": MODE,
        "title": session["title"],
        "scene": turn["scene"],
        "background_id": turn.get("background_id"),
        "dialogue": turn["dialogue"],
        "choices": turn["choices"],
        "present": turn.get("present", []),
        "scene_id": turn.get("scene_id", ""),
        "scene_visits": turn.get("scene_visits", 1),
        "scene_roster": turn.get("scene_roster", []),
        "scene_revisit": bool(turn.get("scene_revisit")),
        # 背景补生成可见性（t45，与 game.public_state 同口径，计数口径复用
        # game._bg_retry_count 以免两处漂移）：普通模式必须一样能看到
        # "本场景背景永久不可用"+已尝试次数，否则前端只能一直转圈。
        "bg_unavailable": bool(turn.get("bg_unavailable")),
        "bg_retry_attempts": game._bg_retry_count(session, turn.get("scene")),
        "cg_active": bool(turn.get("cg_id")),
        "cg_id": turn.get("cg_id", ""),
        "cg_title": (turn.get("cg") or {}).get("title", ""),
        "cg_key": ((session.get("cg_map") or {}).get(turn.get("cg_id")) or {}).get("key", ""),
        "characters_meta": [
            {"name": c["name"], "role": c.get("role", ""),
             "background": c.get("background", ""),
             "auto_added": bool(c.get("auto_added"))}
            for c in session["characters"]
        ],
        "catchphrases": session.get("catchphrases", []),
        "outfits": {
            c["name"]: session.get(f"{c['name']}|outfit", "")
            for c in session["characters"]
        } | {session["protagonist"]["name"]:
             session.get(f"{session['protagonist']['name']}|outfit", "")},
        "content_rating": session.get("content_rating", "18"),
        "r18_enabled": bool(session.get("r18_enabled")),
        "adult_intensity": session.get("adult_intensity", "浓烈"),
        "allow_forced": bool(session.get("allow_forced")),
        "cg_saved": bool(turn.get("cg_saved")),
        "cmd_cg": bool(turn.get("cmd_cg")),
        "cmd_cg_saved": bool(turn.get("cmd_cg_saved")),
        "llm_degraded": bool(session.get("llm_degraded")),
        "protagonist": session["protagonist"]["name"],
        "npcs": [c["name"] for c in session["characters"]],
        "vn": char_panel,
        "char_stats": char_panel,
        "stat_config": game.session_stat_config(session),
        "uniform_en": session.get("uniform_en", ""),
        "uniform_neg": session.get("uniform_neg", ""),
        "uniform_name": session.get("uniform_name", ""),
        "bond_up": list(turn.get("bond_up") or []),
        "assets": session["assets"],
        "bg_map": session["bg_map"],
        "cg_url": game._cg_url(session, turn),
        "turn_no": len(session["log"]),
        "mock": session["mock"],
        # 1.9.20：AI 控制台用（导演指令；记录走独立端点）
        "ai_directive": str(session.get("ai_directive") or ""),
    }


async def start(payload: dict) -> dict:
    world = str(payload.get("world", "")).strip()
    if not world:
        raise game.GameError("请先填写世界观/剧情设定")
    prot_in = payload.get("protagonist") or {}
    pname = str(prot_in.get("name", "")).strip()[:12] or "主角"
    characters = charm.normalize_character_list(
        payload.get("characters") or [], limit=4, protagonist=pname)
    content_rating = str(payload.get("content_rating", "18")).strip()[:8]
    if content_rating not in ("all", "16", "18"):
        content_rating = "18"
    r18 = bool(payload.get("r18_enabled", True)) and content_rating == "18"
    adult = content_rating == "18" and r18

    cfg = cfgmod.load_config()
    game_cfg = cfg["game"]
    emotions = list(game_cfg.get("emotions", ["neutral", "happy", "shy", "sad"]))
    if adult:
        for e in game_cfg.get("adult_emotions", ["aroused", "flustered"]):
            if e not in emotions:
                emotions.append(e)

    stat_cfg = game.session_stat_config(
        {"mode": MODE, "stat_config": payload.get("stat_config")})
    # 本局统一制服关键词（会话级，同局所有角色同款；留空回退全局 config）
    uniform_en = str(payload.get("uniform_en", "") or "").strip()[:200]
    uniform_neg = str(payload.get("uniform_neg", "") or "").strip()[:200]
    uniform_name = str(payload.get("uniform_name", "") or "").strip()[:40]
    session = {
        "sid": uuid.uuid4().hex[:12],
        "mode": MODE,
        "title": str(payload.get("title", "")).strip() or "未命名之物语",
        "world": world,
        "outline": str(payload.get("outline", "")).strip()[:2000],
        "lorebook": [
            {"keys": str(e.get("keys", ""))[:100], "content": str(e.get("content", ""))[:500],
             "always": bool(e.get("always"))}
            for e in (payload.get("lorebook") or [])
            if str(e.get("content", "")).strip()
        ],
        "catchphrases": [str(x).strip()[:60] for x in (payload.get("catchphrases") or [])
                         if str(x).strip()][:5],
        "content_rating": content_rating,
        "r18_enabled": r18,
        "adult_intensity": str(payload.get("adult_intensity", "浓烈")).strip()[:4] or "浓烈",
        "allow_forced": bool(payload.get("allow_forced", True)) and r18,
        "directives": game.merge_directives(payload.get("directives") or []),
        "characters": characters,
        "protagonist": {"name": pname,
                        "anchor": str(prot_in.get("anchor", "")).strip()[:200]
                        or "黑色短发，深色眼瞳，日常便装"},
        "prot_handle": f"{pname}_{uuid.uuid4().hex[:4]}",   # 主角固定随机文档名
        "stat_config": stat_cfg,
        "uniform_en": uniform_en,
        "uniform_neg": uniform_neg,
        "uniform_name": uniform_name,
        # 1.9.0：开场场景推断（制服映射在开场立绘即生效）
        "current_scene": scenemod.guess_initial_scene(
            world, str(payload.get("outline", "")),
            str(payload.get("title", ""))),
        "schema": 2,
        "states": initial_states(stat_cfg),
        "char_states": {},        # 每角色独立数值，建会话后由 ensure_char_states 填充
        "emotions": emotions,
        "summary": "",
        # 1.9.20：AI 控制台 / 玩家导演指令
        "ai_console": [],
        "ai_directive": "",
        "log": [],
        "assets": {"portraits": {}, "backgrounds": {}},
        "bg_map": {},
        "bg_count": 0,
        "mock": {"llm": LLMClient(cfg).mock, "image": ImageClient(cfg).mock},
        "created": time.time(),
    }
    game.SESSIONS[session["sid"]] = session
    ensure_char_states(session)      # 每个角色一套独立数值（含主角）
    try:
        # 角色库导入：已有立绘直接复用（生成管线自动跳过匹配变体）
        game.inject_library_assets(session, payload.get("library_imports") or [], cfg)
        _start_all_portraits(session)
        llm = LLMClient(cfg, session=session, label="VN 开场")
        messages = [{"role": "system", "content": _vn_system(session)},
                    {"role": "user",
                     "content": "请生成游戏的开场剧情段落（第一段 dialogue 之前可以有一两句话交代时间地点与主角的状态）。"}]
        try:
            turn_raw = await llm.chat_json(messages, temperature=0.8)
        except LLMError as e:
            log.warning("vn LLM unavailable (%s), degraded to demo story", e)
            session["llm_degraded"] = True
            llm = LLMClient({"llm": {"base_url": "", "api_key": ""}},
                            session=session, label="VN 开场（演示回退）")
            turn_raw = await llm.chat_json(messages, temperature=0.8)
        if llm.mock:
            game._mock_localize(turn_raw, pname)
        turn = _sanitize_turn(turn_raw, emotions, characters, pname, adult,
                              stat_keys=game.stat_keys_of(session))
        _apply_turn_states(session, turn, mock=llm.mock)
        game._assign_scene_bg(session, turn)
        _post_turn(session, turn)
        session["log"].append({"turn": turn, "choice": None})
        game.persist(session)
        return public_state(session)
    except LLMError as e:
        game.SESSIONS.pop(session["sid"], None)
        raise game.GameError(str(e)) from e


async def _next_turn(session: dict, temperature: float = 0.85) -> tuple[dict, bool]:
    """取出下一轮（LLM 失败降级演示剧情，游戏永不卡死）。返回 (JSON, 是否mock)。"""
    try:
        await _maybe_compress(session)
        llm = LLMClient(cfgmod.load_config(), session=session, label="VN 剧情推进")
        turn_raw = await llm.chat_json(build_messages(session), temperature=temperature)
    except LLMError as e:
        log.warning("vn LLM unavailable (%s), degraded to demo", e)
        session["llm_degraded"] = True
        llm = LLMClient({"llm": {"base_url": "", "api_key": ""}},
                        session=session, label="VN 剧情推进（演示回退）")
        turn_raw = await llm.chat_json(build_messages(session), temperature=temperature)
    if llm.mock:
        game._mock_localize(turn_raw, session["protagonist"]["name"])
    return turn_raw, llm.mock


async def advance(sid: str, choice_index: int) -> dict:
    session = game.get_session(sid)
    current = session["log"][-1]
    choices = current["turn"]["choices"]
    if choice_index < 0 or choice_index >= len(choices):
        raise game.GameError("无效的选项")
    current["choice"] = choices[choice_index]
    try:
        turn_raw, is_mock = await _next_turn(session)
    except LLMError as e:
        current["choice"] = None
        raise game.GameError(str(e)) from e
    adult = session.get("adult", False)
    _rename = game._clean_rename(turn_raw)
    turn = _sanitize_turn(turn_raw, session["emotions"], session["characters"],
                          session["protagonist"]["name"], adult, rename_hint=_rename,
                          stat_keys=game.stat_keys_of(session))
    game._apply_rename(session, _rename)
    _apply_turn_states(session, turn, mock=is_mock)
    prev_hint = session["log"][-1]["turn"].get("background_hint", "")
    game._assign_scene_bg(session, turn)
    turn["background_hint"] = turn["background_hint"] or prev_hint
    _post_turn(session, turn)
    session["log"].append({"turn": turn, "choice": None})
    game.persist(session)
    return public_state(session)


async def speak(sid: str, text: str) -> dict:
    session = game.get_session(sid)
    text = text.strip()[:200]
    if not text:
        raise game.GameError("话语内容为空")
    session["log"][-1]["choice"] = {"text": f"💬 {text}", "effect": "固定话语", "bias": ""}
    try:
        turn_raw, is_mock = await _next_turn(session)
    except LLMError as e:
        session["log"][-1]["choice"] = None
        raise game.GameError(str(e)) from e
    adult = session.get("adult", False)
    _rename = game._clean_rename(turn_raw)
    turn = _sanitize_turn(turn_raw, session["emotions"], session["characters"],
                          session["protagonist"]["name"], adult, rename_hint=_rename,
                          stat_keys=game.stat_keys_of(session))
    game._apply_rename(session, _rename)
    _apply_turn_states(session, turn, mock=is_mock)
    prev_hint = session["log"][-1]["turn"].get("background_hint", "")
    game._assign_scene_bg(session, turn)
    turn["background_hint"] = turn["background_hint"] or prev_hint
    _post_turn(session, turn)
    session["log"].append({"turn": turn, "choice": None})
    game.persist(session)
    return public_state(session)


def _fallback_command_turn(pname: str, name: str, desc: str) -> dict:
    return {
        "scene": "命令之间",
        "background_hint": "室内的私密空间，灯光柔和暧昧",
        "is_new_background": False,
        "present": [name],
        "new_characters": [],
        "dialogue": [
            {"character": "旁白", "emotion": "neutral",
             "text": f"（你把命令一字一句地说出口：{desc}。）"},
            {"character": name, "emotion": "flustered",
             "text": "……你明知道，我没办法拒绝你……"},
            {"character": "旁白", "emotion": "neutral",
             "text": f"（{name}依从了你的命令，气氛骤然升温。）"},
        ],
        "choices": [
            {"text": "继续加深这次体验", "effect": "推进", "bias": "arousal"},
            {"text": "命令她做下一步", "effect": "继续命令", "bias": ""},
            {"text": "停下，回到平静", "effect": "克制", "bias": ""},
        ],
        "cg": {"active": True, "title": f"命令 · {name}",
               "prompt": (f"室内暧昧的光线中，{name}服从命令的瞬间，"
                          "电影感广角构图，氛围张力拉满")},
        "states_delta": {},
    }


async def apply_command(sid: str, character: str, action: str) -> dict:
    session = game.get_session(sid)
    known = {c["name"] for c in session["characters"]} | {session["protagonist"]["name"]}
    if character not in known:
        raise game.GameError("命令目标角色不存在")
    if action not in game.COMMAND_ACTIONS:
        raise game.GameError("未知的命令")
    action_cn, desc_tpl, outfit, _ = game.COMMAND_ACTIONS[action]
    r18 = game._is_r18(session)
    if not r18 and outfit in ("underwear", "nude"):
        raise game.GameError("该更衣命令仅限 18+ R18 模式使用")
    if action in ("kiss", "hug", "sex", "blowjob") and not (r18 and session.get("allow_forced")):
        raise game.GameError("该命令需在 18+ 模式下开启「允许强制/支配内容」")

    desc = desc_tpl.format(name=character)
    if outfit is not None:
        _apply_outfit_now(session, character,
                          ("默认" if outfit == "default" else
                           game.OUTFIT_CN.get(outfit, outfit)))
    command_msg = (
        f"【命令指令】玩家立即对「{character}」下达命令：{desc}。"
        f"请立刻呈现她听到命令后的反应、被执行的过程与后果，"
        "细节生动、有画面感、不要回避；本幕是值得收藏的重要场面，"
        "请把 cg 设为 active=true 并给出标题与画面描述。"
    )
    messages = build_messages(session)
    if messages and messages[-1]["role"] == "user":
        messages[-1] = {"role": "user", "content": command_msg}
    else:
        messages.append({"role": "user", "content": command_msg})
    adult = session.get("adult", False)
    try:
        llm = LLMClient(cfgmod.load_config(), session=session, label="VN 命令")
        turn_raw = await llm.chat_json(messages, temperature=0.9)
        _rename = game._clean_rename(turn_raw)
        turn = _sanitize_turn(turn_raw, session["emotions"], session["characters"],
                              session["protagonist"]["name"], adult,
                              rename_hint=_rename,
                              stat_keys=game.stat_keys_of(session))
    except (LLMError, ValueError) as e:
        log.warning("vn command story failed (%s), use fallback turn", e)
        _rename = ""
        turn = _sanitize_turn(
            _fallback_command_turn(session["protagonist"]["name"], character, desc),
            session["emotions"], session["characters"],
            session["protagonist"]["name"], adult,
            stat_keys=game.stat_keys_of(session))
    game._apply_rename(session, _rename)

    turn["cg"]["active"] = True
    if not turn["cg"].get("title"):
        turn["cg"]["title"] = f"命令 · {action_cn}"
    if not turn["cg"].get("prompt"):
        turn["cg"]["prompt"] = (f"室内暧昧光线中，{character}服从命令的瞬间，"
                                "电影感广角构图，氛围张力拉满")
    # 命令本身的数值影响：算在【被下令的角色】那一份上，与 LLM 给的变化叠加
    cmd_delta = VN_COMMAND_DELTA.get(action, {})
    if cmd_delta:
        entry = turn["states_delta"].setdefault(character, {})
        for k, v in cmd_delta.items():
            entry[k] = max(-15, min(15, entry.get(k, 0) + v))

    _apply_turn_states(session, turn, mock=False)
    prev_hint = session["log"][-1]["turn"].get("background_hint", "")
    game._assign_scene_bg(session, turn)
    turn["background_hint"] = turn["background_hint"] or prev_hint
    _post_turn(session, turn)
    turn["cmd_cg"] = True
    session["log"].append({"turn": turn, "choice": None})
    game.persist(session)
    # B-05/M-02：命令场面 CG 合成是 CPU 密集同步操作，必须丢线程池
    # （本行在 t15 原 inScope 之外，改动由队长书面授权，语义等价：
    #  原来直接在 async 上下文里同步调用 → 合成期间事件循环被独占）
    await asyncio.to_thread(game.save_command_cg, session, turn,
                            f"命令·{action_cn}")
    return public_state(session)


def apply_outfit(sid: str, character: str, outfit: str) -> dict:
    session = game.get_session(sid)
    known = {c["name"] for c in session["characters"]} | {session["protagonist"]["name"]}
    if character not in known:
        raise game.GameError("角色不存在")
    outfit = (outfit or "").strip()[:40]
    if not outfit:
        raise game.GameError("请填写服装")
    if outfit in ("underwear", "nude") and not game._is_r18(session):
        raise game.GameError("内衣/裸体着装仅限 18+ R18 模式使用")
    if outfit in game.OUTFIT_KEYS:
        outfit = game.OUTFIT_CN[outfit]
    _apply_outfit_now(session, character, outfit)
    game.persist(session)
    return public_state(session)


def regenerate(sid: str, target: str, note: str = "") -> dict:
    session = game.get_session(sid)
    nonces = session.setdefault("regen_nonce", {})
    if target == "all":
        game._drop_old_assets(session, session["protagonist"]["name"])
        for npc in session["characters"]:
            game._drop_old_assets(session, npc["name"])
        nonces["__prot__"] = nonces.get("__prot__", 0) + 1
        _vn_portrait(session, session["protagonist"]["name"],
                     nonce=nonces["__prot__"], note=note)
        for npc in session["characters"]:
            nonces[npc["name"]] = nonces.get(npc["name"], 0) + 1
            _vn_portrait(session, npc["name"], nonce=nonces[npc["name"]], note=note)
        label = f"全部角色立绘（{len(session['characters']) + 1} 个角色）"
    elif target == "protagonist":
        game._drop_old_assets(session, session["protagonist"]["name"])
        nonces["__prot__"] = nonces.get("__prot__", 0) + 1
        _vn_portrait(session, session["protagonist"]["name"],
                     nonce=nonces["__prot__"], note=note)
        label = f"{session['protagonist']['name']}"
    else:
        if target not in {c["name"] for c in session["characters"]}:
            raise game.GameError(f"角色不存在：{target}")
        game._drop_old_assets(session, target)
        nonces[target] = nonces.get(target, 0) + 1
        _vn_portrait(session, target, nonce=nonces[target], note=note)
        label = f"{target}"
    game.persist(session)
    return {"ok": True, "regenerating": label}

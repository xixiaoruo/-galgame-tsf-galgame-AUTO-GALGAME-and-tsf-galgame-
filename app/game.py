"""游戏会话管理：开局、剧情推进、立绘/背景后台生成、缓存与存档（TSF 版本）。"""
import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from . import characters as charm
from . import config as cfgmod
from . import pixel as pixel_mod
from . import prompts
from . import scenes as scenemod
from . import story_rules
from . import tsf
from . import cgbook
from .image import (ImageClient, ImageGenError, apply_style_mode, asset_key,
                    gen_params_tag, process_portrait, process_portrait_async,
                    append_appearance_en, emotion_en_tags,
                    # t61 画风预设层（默认无风格 ⇒ 三者都是零影响）
                    style_suffix, style_negative, style_key_part,
                    # t70 画风绑定的 LoRA 词（无绑定/非像素风 ⇒ 空串，零影响）
                    lora_tag)
from .llm import LLMClient, LLMError, MockSetup

log = logging.getLogger("galgame.game")

# 提示词版本：随提示词/负面词的升级而递增，用于缓存键——旧缓存自动失效，
# 保证角色立绘始终用最新提示词管线生成
# v6：立绘背景白→浅灰影棚色（白大褂等浅色服装在纯白底上会被漂白/隐身），
# 负面词加防漂白（white-on-white, washed out）；v5 起并入生成参数签名。
# v7：style_suffix 去除"半身像/纯白色背景"矛盾指令（与"必须全身像/浅灰
# 背景"冲突导致构图随机崩坏），补银灰发/眼镜/长发英文外貌标签。
PROMPT_VERSION = "v37-tsf-cards-scene-190"

#: 1.8.0「六段演变工坊」：正在跑的预演任务（sid → asyncio.Task）。
#: 只用于防重入与取消；进度状态落在 session["stage_tour"] 并由 persist 落盘。
_STAGE_TOUR_TASKS: dict[str, "asyncio.Task"] = {}


def _perf(session: dict) -> dict:
    """本局性能计数（惰性初始化，随存档持久化；缺失键自动补齐）。"""
    p = session.setdefault("perf", {})
    for k, v in (("images", 0), ("failed", 0), ("previews", 0),
                 ("image_seconds", 0.0), ("tour_runs", 0),
                 ("cache_hits", 0), ("saved_seconds", 0.0),
                 ("llm_calls", 0), ("llm_tokens_est", 0)):
        p.setdefault(k, v)
    return p


def _params_tag(cfg: dict) -> str:
    """生成参数签名缓存标记：步数/CFG/采样器/HR 变化时旧图自动作废。"""
    return gen_params_tag(cfg)


SESSIONS: dict[str, dict] = {}
# 持有后台任务引用，防止被垃圾回收
_TASKS: set[asyncio.Task] = set()
# 服务端主事件循环（由 server.py 在首个请求/启动时登记）。
# 需要事件循环的后台生成任务若从同步端点（FastAPI 跑在线程池）发起，
# 必须投递到它上面——否则 asyncio.create_task 在无循环的线程里必抛
# RuntimeError: no running event loop（B-02/B-03 的根因）。
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """登记主事件循环（幂等；可在任意 async 上下文里调用）。"""
    global _MAIN_LOOP
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
    _MAIN_LOOP = loop


def _spawn_task(coro) -> bool:
    """把后台协程排进事件循环；成功返回 True，失败返回 False。

    当前线程有循环（async 端点）→ 直接 create_task；
    当前线程是线程池 worker（同步端点）→ 用 run_coroutine_threadsafe
    投递到主循环；两者都不可用（例如服务尚未起来）→ 返回 False，
    由调用方回滚刚登记的 pending 资产（不能留下永不被推进的条目）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(coro)
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return True
    main = _MAIN_LOOP
    if main is not None and not main.is_closed():
        fut = asyncio.run_coroutine_threadsafe(coro, main)
        _TASKS.add(fut)
        fut.add_done_callback(_TASKS.discard)
        return True
    coro.close()   # 没能排上：显式关闭协程，避免 "never awaited" 警告
    return False

# 预置创作指令（开始界面「AI 指令注入」与游戏内「⚡ 指令」共用）
PRESET_DIRECTIVES = [
    {"id": "slow", "group": "节奏", "text": "剧情节奏放缓，多用细腻的日常与环境描写"},
    {"id": "fast", "group": "节奏", "text": "剧情节奏加快，尽快推进到下一个转折点"},
    {"id": "style_poetic", "group": "文风", "text": "文风细腻文艺，多用比喻与意象"},
    {"id": "style_light", "group": "文风", "text": "文风轻快幽默，台词多于旁白"},
    {"id": "style_hard", "group": "文风", "text": "文风冷峻简练，克制留白"},
    {"id": "inner", "group": "描写", "text": "增加主角的内心独白与心理挣扎描写"},
    {"id": "daily", "group": "氛围", "text": "营造轻松的日常氛围，多安排生活化场景"},
    {"id": "tense", "group": "氛围", "text": "营造紧张悬疑的氛围，制造危机与悬念"},
    {"id": "limit_subtle", "group": "限度", "text": "描写尺度保持含蓄，点到即止，不出现直白露骨内容"},
    {"id": "conservative", "group": "限度", "text": "数值变化保守，转变缓慢渐进（stats_delta 每项 0~±3）"},
    {"id": "bold", "group": "限度", "text": "数值变化更大胆，选项后果更显著（stats_delta 可用 ±10~15）"},
    {"id": "npc-active", "group": "互动", "text": "让其他角色更主动介入剧情，增加互动"},
]


def initial_directives() -> list[dict]:
    return [{"id": d["id"], "text": d["text"], "group": d.get("group", ""),
             "enabled": False, "preset": True} for d in PRESET_DIRECTIVES]


def merge_directives(incoming: list) -> list[dict]:
    """按 set_directives 同样的规则合并：预置只改勾选，自定义校验后收编。"""
    preset_ids = {p["id"] for p in PRESET_DIRECTIVES}
    result = []
    for p in PRESET_DIRECTIVES:
        item = next((d for d in incoming
                     if isinstance(d, dict) and d.get("id") == p["id"]), {})
        result.append({"id": p["id"], "text": p["text"],
                       "group": p.get("group", ""), "preset": True,
                       "enabled": bool(item.get("enabled", False))})
    customs = []
    for d in incoming:
        if not isinstance(d, dict) or d.get("id") in preset_ids:
            continue
        text = str(d.get("text", "")).strip()[:200]
        if text:
            customs.append({"id": str(d.get("id") or uuid.uuid4().hex[:8]),
                            "text": text, "group": "自定义", "preset": False,
                            "enabled": bool(d.get("enabled", True))})
    result.extend(customs[:12])
    return result


class GameError(Exception):
    pass


# ---------- 持久化 ----------


def _stable_seed(key: str) -> int:
    """由角色名等生成稳定的 31 位种子：同角色跨会话/跨表情形象一致。"""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4],
                          "big") % (2 ** 31)


def _seed_for(session: dict, key: str) -> int:
    """会话级种子（1.7.39 「确定性劣化簇」根治）：稳定种子 + 会话
    seed_offset × 1000003 大偏移——某（提示词×种子）组合跨实例固定产出
    劣化图（绿噪/双人，SD 重启也同结果：实机主角阶段簇 1318580382
    验证）；失败自动重画时 offset+1 → 永久避开该劣化簇。"""
    off = int(session.get("seed_offset", 0) or 0)
    return (_stable_seed(key) + off * 1000003) % (2 ** 31)



def _save_path(sid: str) -> Path:
    return cfgmod.SAVES_DIR / f"{sid}.json"


def persist(session: dict) -> None:
    session["saved_at"] = time.time()
    try:
        # B-04：原子写（临时文件 + os.replace）——中途被强杀也不会留下半个
        # JSON，否则下次启动 load_all 会静默跳过该局（整局从列表消失）
        cfgmod.atomic_write_json(_save_path(session["sid"]), session)
    except OSError as e:
        log.warning("persist failed: %s", e)


def persist_all() -> int:
    """把手里的全部会话落盘（关机前调用），返回成功条数。"""
    n = 0
    for s in list(SESSIONS.values()):
        try:
            persist(s)
            n += 1
        except Exception as e:   # 单局失败不能挡住关机
            log.warning("persist_all failed sid=%s: %s", s.get("sid"), e)
    return n



def load_all() -> None:
    migrate_legacy_slots()
    for p in cfgmod.SAVES_DIR.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            # 服务重启/读档共用：pending 任务中断恢复、死路径校正、场景重试预算重置
            _recover_interrupted_assets(s)
            # 兼容旧存档：补主角字段与新增指数（genital 等）
            if "protagonist" not in s:
                continue
            prot = s["protagonist"]
            if "stats" in prot:
                for k, meta in tsf.STAT_META.items():
                    prot["stats"].setdefault(k, meta["start"])
            if "catchphrases" not in s:
                s["catchphrases"] = []
            SESSIONS[s["sid"]] = s
            _ensure_asset_paths(s)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            log.warning("skip broken save %s: %s", p.name, e)


def get_session(sid: str) -> dict:
    s = SESSIONS.get(sid)
    if not s:
        raise GameError("会话不存在或已过期，请重新开始")
    return s


def session_stat_config(session: dict) -> list[dict]:
    """会话级状态配置（开局前自定义编辑）：旧会话/无配置时返回内置默认。

    TSF 模式锁定 progress（阶段引擎核心，不允许删除）。
    """
    raw = session.get("stat_config")
    if session.get("mode") == "vn":
        from . import vn as vn_mod
        meta, locks = vn_mod.STATE_META, []
    else:
        meta, locks = tsf.STAT_META, ["progress"]
    return tsf.merge_stat_config(raw, meta, locks)


def stat_keys_of(session: dict) -> list[str]:
    """会话当前状态键列表（bias 校验 / delta 清洗 / schema 用）。"""
    return [c["key"] for c in session_stat_config(session)]


# ---------- 剧情轮次处理 ----------

def _mock_fallback_turn() -> dict:
    """LLM 不可用时的演示轮次（保证游戏永不卡死）。"""
    import copy
    from . import llm as llm_mod
    return copy.deepcopy(llm_mod.MOCK_TURN)


def _mock_localize(turn_raw: dict, pname: str) -> None:
    """演示模式下把 mock 剧本中的默认角色名替换为实际主角名。"""
    for d in turn_raw.get("dialogue", []) or []:
        if isinstance(d, dict) and d.get("character") == "小雪":
            d["character"] = pname


def _sanitize_turn(turn: dict, emotions: list[str], characters: list[dict],
                   pname: str, rename_hint: str = "",
                   stat_keys: list[str] | None = None) -> dict:
    """校验并规范化 LLM 返回的轮次 JSON。

    旁白修复：只有字面「旁白」才映射为旁白；其他未知名（2~5 字中文名）保留
    原样并标记 new，交由 _post_turn 自动加入角色池，绝不静默变成旁白。
    rename_hint：本幕主角改名后的新名字（允许对话直接使用新名）。
    stat_keys：会话级状态键（含玩家自定义），决定 bias 与 delta 白名单。
    """
    import re
    from . import vn as vn_mod      # 逐角色数值清洗（两种模式共用）
    known = {c["name"] for c in characters} | {pname}
    if rename_hint:
        known.add(rename_hint)   # 本幕主角新名字：对话中的新名不再当作新 NPC
    name_re = re.compile(r"^[\u4e00-\u9fa5·]{2,5}$")
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
                new_names.append(name)  # 新角色：保留，稍后自动入池
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
    known_keys = stat_keys if stat_keys is not None else tsf.STAT_KEYS
    for c in turn.get("choices") or []:
        if isinstance(c, dict) and str(c.get("text", "")).strip():
            bias = str(c.get("bias", "")).strip()
            if bias not in known_keys:
                bias = ""
            choices.append({
                "text": str(c["text"]).strip(),
                "effect": str(c.get("effect", "")).strip(),
                "bias": bias,
            })
    if not choices:
        choices = [{"text": "继续", "effect": "", "bias": ""}]

    # 换装检测：LLM 每轮可给出各角色当前服饰（仅常规服饰，禁止内衣内裤类）
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
        "stats_delta": tsf.sanitize_delta(turn.get("stats_delta"), known_keys),
        # 各角色自己的情绪/好感/身体数值（与主角转变数值相互独立）
        "states_delta": vn_mod.sanitize_char_delta(
            turn.get("states_delta"), None,
            [pname] + [c["name"] for c in characters], pname),
        "identity": str(turn.get("identity", "")).strip()[:60],
    }


def _outfit_state_block(session: dict) -> str:
    """当前服装状态：主角 + NPC 已换装的显示，未换装显示「初始着装」。"""
    lines = []
    prot = session["protagonist"]
    pname = prot["name"]
    p_outfit = session.get(f"{pname}|outfit", "") or "初始着装"
    lines.append(f"- {pname}（主角）：{p_outfit}")
    for c in session["characters"]:
        c_outfit = session.get(f"{c['name']}|outfit", "") or "初始着装"
        lines.append(f"- {c['name']}：{c_outfit}")
    return "\n".join(lines)


def _adult_flags(session: dict) -> tuple[bool, bool]:
    r18 = session.get("content_rating", "all") == "18" and bool(session.get("r18_enabled"))
    forced = bool(session.get("allow_forced")) and r18
    return r18, forced


def _stat_display_name(session: dict, key: str) -> str:
    """会话状态显示名（含玩家自定义），未知键回退返回键名。"""
    for c in session_stat_config(session):
        if c["key"] == key:
            return c["name"]
    return key


def build_story_system(session: dict) -> str:
    from . import vn as vn_mod      # 逐角色数值（两种模式共用），延迟导入避免循环依赖
    prot = session["protagonist"]
    _stages = tsf.stages_for(session)
    stage_idx = tsf.stage_of(prot["stats"]["progress"], _stages)
    if not prot.get("identity"):
        prot["identity"] = _stages[stage_idx]["identity"]
    enabled = [d["text"] for d in session.get("directives", []) if d.get("enabled")]
    directive_block = ("\n".join(f"- {t}" for t in enabled)
                       if enabled else "（暂无，按默认风格创作）")
    r18, forced = _adult_flags(session)
    stat_cfg = session_stat_config(session)
    stat_keys = [c["key"] for c in stat_cfg]
    stats_schema = ", ".join(f'"{k}": 0' for k in stat_keys)
    bias_hint = "/".join(stat_keys[:5]) or "（自定义状态）"
    # 1.7.34 二周目：压缩后的前情提要注入世界观（沿用 VN 的 summary 字段）
    world = str(session.get("world", "") or "")
    if session.get("summary"):
        world = (f"{world}\n\n【前情提要（二周目继承·已压缩，仅供背景参考，"
                 f"仍从当前开局状态写起）】\n{session['summary'][:800]}")
    system = prompts.TSF_STORY_SYSTEM.format(
        world=world,
        outline=(session.get("outline")
                 if cfgmod.load_config()["game"].get("inject_outline", True)
                 else "（大纲已关闭，剧情自由发挥）"),
        pname=prot["name"],
        anchor=prot.get("anchor", ""),
        stats=tsf.stats_block(prot["stats"], prot["identity"], stage_idx,
                              stat_cfg, _stages),
        stats_schema=stats_schema,
        char_states=vn_mod.char_states_block(session),
        char_states_schema=vn_mod.char_schema(session),
        bias_hint=bias_hint,
        outfit_state=_outfit_state_block(session),
        lore=prompts.format_lore(
            session.get("lorebook", []),
            prompts.recent_dialogue_text(session["log"]),
        ),
        directives=directive_block,
        craft_rules=story_rules.craft_rules_block(
            r18, session.get("adult_intensity", "成熟")),
        content_policy=content_policy_text(
            session.get("content_rating", "all"),
            bool(session.get("r18_enabled")),
            session.get("adult_intensity", "成熟"),
            bool(session.get("allow_forced"))),
        outfit_policy=(prompts.outfit_policy_adult() if r18
                       else prompts.outfit_policy_safe()),
        cg_policy=(prompts.cg_policy_adult() if r18
                   else prompts.cg_policy_safe()),
        characters=prompts.format_characters(session["characters"]),
        emotions="、".join(session["emotions"]),
    )
    # 1.9.20：玩家导演指令（持续生效；放在模板之后，确保不会被 format 的花括号误伤）
    _dir = str(session.get("ai_directive") or "").strip()
    if _dir:
        system += ("\n\n【玩家导演指令（本局持续生效，优先级高于默认风格；"
                   "但不得违反内容分级政策）】\n" + _dir[:500])
    # API 前置注入：启用中的注入文本拼到 system prompt 最前面（全局生效）
    from . import inject as inject_mod
    return inject_mod.inject_block(system)


def build_messages(session: dict) -> list[dict]:
    """把会话历史构造成 chat messages，末尾附上待回答的用户消息。"""
    messages = [{"role": "system", "content": build_story_system(session)}]
    log_entries = session["log"]
    for i, entry in enumerate(log_entries):
        if i == 0:
            if session.get("summary"):
                user = "【前情提要已并入设定】请直接继续推进剧情，不要重新开场。"
            else:
                user = ("请生成游戏的开场剧情段落（第一段 dialogue 之前可以有一两句话交代"
                        "时间地点与主角的初始状态）。")
        else:
            prev_choice = log_entries[i - 1]["choice"] or {"text": "继续"}
            effect = prev_choice.get("effect", "")
            bias = prev_choice.get("bias", "")
            user = f"玩家选择了：{prev_choice['text']}"
            if effect:
                user += f"（走向：{effect}）"
            if bias:
                user += (f"（此选项倾向影响「{_stat_display_name(session, bias)}」，"
                         "请优先体现）")
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant",
                         "content": json.dumps(entry["turn"], ensure_ascii=False)})
    last_choice = log_entries[-1]["choice"]
    if last_choice:
        effect = last_choice.get("effect", "")
        bias = last_choice.get("bias", "")
        final_user = f"玩家选择了：{last_choice['text']}"
        if effect:
            final_user += f"（走向：{effect}）"
        if bias:
            final_user += (f"（此选项倾向影响「{_stat_display_name(session, bias)}」，"
                           "请优先体现）")
        messages.append({"role": "user", "content": final_user})
    return messages


async def _maybe_compress(session: dict) -> None:
    """历史过长时把较早轮次摘要压缩，控制上下文长度。"""
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
            {"role": "system", "content": prompts.TSF_SUMMARY_SYSTEM},
            {"role": "user", "content": summary_text},
        ], temperature=0.3)
        session["summary"] = result.strip()
    except LLMError as e:
        log.warning("summary failed: %s", e)
        session["summary"] = summary_text[:600]
    session["log"] = keep


# ---------- 立绘 / 背景生成 ----------

def _reserve_file_name(session: dict, base: str) -> int:
    """预占规范序号：文件名形如 `{base}——{序号:03d}_{内容hash8}.{ext}`。
    内容哈希在写盘时由实际图片字节计算（同内容同哈希，不可替代、天然去重）。"""
    seq = session.setdefault("file_seq", {})
    i = seq.get(base, 0) + 1
    seq[base] = i
    return i


def _finish_file_name(base: str, seq: int, data: bytes, ext: str = "png") -> str:
    """写盘后生成最终文件名：序号 + 内容哈希（规整且有唯一性）。"""
    import hashlib as _hl
    return f"{base}——{seq:03d}_{_hl.sha1(data).hexdigest()[:8]}.{ext}"


def _skey(session: dict, *parts: str) -> str:
    """会话隔离的资产键：同一设定在不同对局生成不同图片，防止跨局错乱。"""
    return asset_key(str(session["sid"]), *parts)


def _ensure_session_info(session: dict, dir_name: str) -> None:
    """在对局目录写入 info.json（说明这个文件夹属于哪一局）。"""
    ip = cfgmod.CACHE_DIR / dir_name / "info.json"
    if not ip.is_file():
        try:
            cfgmod.atomic_write_json(ip, {
                "title": session.get("title", ""),
                "protagonist": session["protagonist"]["name"],
                "prot_handle": session.get("prot_handle", ""),
                "created": session.get("created", 0),
                "note": "本目录为该对局的立绘/背景/原图/预览，按角色规范命名；旧版立绘在 versions/。",
            }, indent=1)
        except OSError:
            pass


def _archive_file_to_versions(session: dict, file: str) -> str:
    """把归档文件复制到 {dir}/versions/（目录说明清晰），返回新引用或原引用。"""
    if not file or "/" not in file:
        return file
    parts = file.split("/")
    if len(parts) < 2:
        return file
    dir_name = parts[0]
    name = parts[-1]
    src = cfgmod.CACHE_DIR / file
    if not src.is_file():
        return file
    try:
        vdir = cfgmod.CACHE_DIR / dir_name / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        import shutil as _sh
        dst = vdir / name
        if not dst.exists():
            _sh.copy2(src, dst)
        return f"{dir_name}/versions/{name}"
    except OSError:
        return file


def _archive_same_label(session: dict, group: str, label: str) -> None:
    """生成新图前，把同标签的旧 ready 项归档（保留文件），确保新图独占显示。"""
    hist = session.setdefault("portrait_history", [])
    for k in list(session["assets"].get(group, {}).keys()):
        it = session["assets"][group][k]
        if it.get("label") == label and it.get("status") == "ready":
            hist.append({
                "archive_ts": time.time(),
                "label": it.get("label", ""),
                "file": _archive_file_to_versions(session, it.get("file", "")),
                "raw_file": it.get("raw_file", ""),
                "progress": ((session["protagonist"].get("stats") or {})
                             .get("progress", 0)
                             if it.get("label", "").startswith(
                                 session["protagonist"]["name"] + "·") else 0),
            })
            session["assets"][group].pop(k)
    _prune_history(session, label)


def _history_limit(session: dict) -> int:
    """同一标签最多保留 N 个旧版（versions/ 防无限膨胀），至少 3。"""
    try:
        return max(3, int(cfgmod.load_config()["game"].get(
            "history_max_per_label", 10)))
    except Exception:
        return 10


def _prune_history(session: dict, label: str) -> None:
    """按精确标签裁剪立绘历程：同一标签（如某阶段/某表情/某换装）超出限制的
    最旧条目连同 versions/ 文件一起删除（只删 versions/ 子目录内的文件，
    绝不碰 portraits/raw/根级）。平滑过渡的每 10% 条带各自成组、互不挤压。"""
    hist = session.get("portrait_history") or []
    keep = _history_limit(session)
    same = [i for i, e in enumerate(hist) if e.get("label") == label]
    if len(same) <= keep:
        return
    ids = set(same[:len(same) - keep])
    for i in ids:
        e = hist[i]
        f = e.get("file", "")
        if "/versions/" in f:
            try:
                (cfgmod.CACHE_DIR / f).unlink(missing_ok=True)
            except OSError:
                pass
    hist[:] = [e for i, e in enumerate(hist) if i not in ids]


def _restore_latest_for(session: dict, group: str, label: str) -> bool:
    """生成失败兜底：把该标签最近一版「有效」归档立绘恢复为当前显示（不空屏）。

    跳过空白/全透明版本（曾把空白图当上一版回退，立绘仍是白板）。
    """
    hist = session.get("portrait_history", [])
    for it in reversed(hist):
        if it.get("label") != label or not it.get("file"):
            continue
        src = cfgmod.CACHE_DIR / it["file"]
        if not src.is_file():
            continue
        try:
            from PIL import Image as _Img
            with _Img.open(src) as im:
                rgb = im.convert("RGB")
                rgb.thumbnail((64, 64))
                px = list(rgb.getdata())
                lums = [(r + g + b) // 3 for r, g, b in px]
                if max(lums) - min(lums) < 8:
                    continue   # 近纯色/空白版（如整张白色）跳过
                if im.mode in ("RGBA", "LA", "P"):
                    a = im.convert("RGBA").getchannel("A")
                    if a.getbbox() is None:
                        continue   # 全透明空白版跳过
        except OSError:
            continue
        for item in session["assets"].get(group, {}).values():
            if item.get("label") == label and item.get("status") != "ready":
                item["status"] = "ready"
                item["file"] = it["file"]
                item["warn"] = "本次生成失败，已回退展示上一版立绘"
                return True
    return False


def _queue_asset(session: dict, group: str, key: str, label: str,
                 coro_factory, base: str, seq: int, ext: str = "png",
                 persist_fn=None) -> None:
    """登记一个图片资产并启动后台生成任务。

    每个会话有独立可读子目录（标题_主角_随机码）/portraits|backgrounds|raw/，
    成品文件名 = {base}——{序号:03d}_{内容hash8}.{ext}；raw 原图同目录、哈希名。

    `persist_fn`（t55，C×T3）：可选的自有持久化回调，**默认 None = 原行为**
    （`persist(session)` → 写 `<SAVES_DIR>/<sid>.json`）。给 CoC×TSF 镜像会话用时
    传 `game.coc_tsf_persist`，把镜像落到 `<DATA_DIR>/coc_tsf/<sid>.json`，
    避免「CoC 镜像被写成主模式存档 → `load_all()` 把 CoC 局当主模式对局载入」（t53 §4.7/§8.2）。
    """
    assets = session["assets"]
    _persist = persist_fn or persist          # ← 唯一的持久化出口（三条路径共用）
    old = assets[group].get(key)
    if old is not None:
        # 1.9.8：失败项不能永久占着同一个缓存键。旧逻辑只要 key 存在就直接
        # return——背景自动重试换到 bg3 时仍然复用同一个 key，于是「重试」根本
        # 没有新任务，看起来就是旧档场景永远生成不出来。error 项允许重新排队。
        if old.get("status") in ("pending", "ready"):
            return  # 已在生成或已就绪
        log.info("asset retry after error: %s (%s)", label,
                 str(old.get("error", ""))[:80])
        assets[group].pop(key, None)
    assets[group][key] = {"status": "pending", "file": "", "label": label,
                          "error": "", "queued_at": time.time()}
    # 新图独占显示：同标签旧版先归档保留；生成失败时回退展示旧版
    _archive_same_label(session, group, label)
    sub = {"portraits": "portraits", "backgrounds": "backgrounds"}.get(group, group)
    dir_name = _session_dir_name(session)
    sid_dir = cfgmod.CACHE_DIR / dir_name / sub
    sid_dir.mkdir(parents=True, exist_ok=True)
    # raw 原图目录：立绘工厂先写 raw 再抠图，目录缺失会整张失败（背景无 raw 所以正常）
    (cfgmod.CACHE_DIR / dir_name / "raw").mkdir(parents=True, exist_ok=True)
    _ensure_session_info(session, dir_name)

    async def run():
        item = assets[group][key]
        log.info("asset task start: %s", label)
        _t_start = time.time()
        _is_preview = "·演变" in str(label)
        try:
            data = await coro_factory()
            if isinstance(data, tuple):
                data, raw_name = data
                item["raw_file"] = raw_name
            # P10-A 像素画风（默认关）：**立绘写盘前的唯一闸门**。
            # 位置选在这里而不是 image.process_portrait()，因为后者被 4 条真实
            # 生成路径绕过（阶段渐变链式 2109-2127、换装限定重绘 2035-2048 与
            # 2311-2324、初始参考重建 2010-2016），只挂钩子在那里会出现
            # 「主角立绘是像素的、换装重绘那张不是」的功能不一致。
            # 开关关闭时 apply_async 直接原样返回 data（零改动）；开启时是
            # CPU 密集的整图解码+逐像素循环 → 内部走 asyncio.to_thread（B-05）。
            if group == "portraits":
                data = await pixel_mod.apply_async(
                    data, (cfgmod.load_config().get("image") or {}))
            # 1.7.39 全局成品复检（写 ready 前的最后一道门——所有生成路径
            # 统一拦截：双人同框/绿色噪点绝不入库，合格才写文件；检出即
            # 抛异常 → 走失败回退/自动重画（含种子换簇）
            # B-05/M-02：成品复检是 CPU/解码密集的同步函数，丢线程池
            # （原来直接在事件循环上跑：一次复检要整图解码 + 像素循环）。
            # 同时把「两次调用」合成一次（`_raw_quality_issue` 内部一次解码）。
            # 演示模式（没有真实生图服务）的占位图是彩色噪声，不能走真实
            # 成品质检——否则会出现「主角有图、NPC 全失败」的假故障。
            _mock_image = bool((session.get("mock") or {}).get("image"))
            if not _mock_image:
                _bad = await asyncio.to_thread(
                    _raw_quality_issue, data,
                    allow_multi=(group == "backgrounds"))
                if _bad:
                    raise ImageGenError(f"成品复检未通过（{_bad}）")
            filename = _finish_file_name(base, seq, data, ext)
            rel = f"{dir_name}/{sub}/{filename}"
            path = sid_dir / filename
            if path.is_file() and path.stat().st_size > 1024:
                # 同会话缓存命中（同键重试时直接复用成品）
                item["status"] = "ready"
                item["file"] = rel
                _save_cg_if_bridge(session, key, rel)
                _persist(session)
                return
            path.write_bytes(data)
            item["status"] = "ready"
            item["file"] = rel
            _save_cg_if_bridge(session, key, rel)
            _p = _perf(session)
            _p["images"] += 1
            _p["image_seconds"] += max(0.0, time.time() - _t_start)
            if _is_preview:
                _p["previews"] += 1
            _persist(session)
            log.info("asset task ready: %s", label)
        except Exception as e:
            # 兜底：任何异常都标记 error（否则任务逃逸会让资产永远 pending）
            log.warning("asset generation failed %s: %s", label, e)
            _perf(session)["failed"] += 1
            item["status"] = "error"
            item["error"] = str(e)[:200]
            # 生成失败：展示上一版旧立绘，不让玩家空屏
            if not _restore_latest_for(session, group, label):
                pass
            _persist(session)
            # 1.7.32 失败自动重画（用户规则：失败不要卡着，自动重画收盘）：
            # 每个资产 key 失败后延迟 12 秒自动重试一次（防「失败=永久
            # error」——引擎/判定器/构图齐备前提下重试通常即成功）；
            # 第二次失败停止自动重试（防死循环），保留回退旧版。
            try:
                retries = session.setdefault("_asset_retry", {})
                if retries.get(key, 0) < 1:
                    retries[key] = retries.get(key, 0) + 1
                    # 1.7.39 失败自动重画时前进会话级种子大簇（+1000003）——
                    # 确定性劣化簇（某提示词×种子跨实例固定出废图）永久避开
                    session["seed_offset"] = int(session.get("seed_offset", 0) or 0) + 1
                    await asyncio.sleep(12)
                    log.info("asset auto-retry: %s (n=%d, seed_offset=%d)",
                             label, retries[key],
                             int(session.get("seed_offset", 0)))
                    assets[group].pop(key, None)
                    _queue_asset(session, group, key, label,
                                 coro_factory, base, seq, ext,
                                 persist_fn=persist_fn)
            except Exception as re:
                log.warning("asset auto-retry failed %s: %s", label, re)

    if not _spawn_task(run()):
        # B-02：没有任何事件循环可投递时，绝不能留下「永不被推进的 pending」
        #（旧行为：create_task 抛 RuntimeError 被上层吞掉 → 条目永久 pending，
        # 前端 hasPending() 据此每 2 秒无限轮询，bg_count 也白加一次）。
        assets[group].pop(key, None)
        log.warning("asset task not scheduled (no event loop) sid=%s group=%s "
                    "key=%s label=%s -> pending entry rolled back",
                    session.get("sid"), group, key, label)
        raise GameError("后台生成任务未能排入事件循环（服务未就绪），请稍后重试")


# =============================================================================
# CoC×TSF 立绘桥（t55 / C×T3）—— 让 CoC 会话复用**同一个**出图闸门与 tsf 配方
#
# 设计依据 `D:\dsh创造\coc-mode\COC-TSF-REUSE.md` §8.2 / §9 / §12：
#   · CoC 原生会话**保持 CoC 语义不变**，不动 `app/coc/**`（他人 inScope）；
#   · 另建**镜像会话 mirror**（形状对齐 `new_session` 的最小集）承载资产；
#   · mirror **不落 `saves/`** —— 走 `coc_tsf_persist` 写 `<DATA_DIR>/coc_tsf/<sid>.json`，
#     否则 `load_all()` 会把 CoC 局当主模式对局载入（t53 实测）；
#   · 提示词**只 import `app/tsf.py`**（配方红线：不改它），阶段由 `tsf.stage_of`
#     按进度算；像素画风由 `_queue_asset` 内的既有钩子（`pixel_mod.apply_async`）自动生效。
# =============================================================================

#: 镜像会话的存档目录名（相对 `cfgmod.DATA_DIR`）；与 `saves/` 完全不同层
COC_TSF_DIR = "coc_tsf"


def coc_tsf_persist(mirror: dict, cfg: dict | None = None) -> None:
    """CoC×TSF 镜像会话的**自有**持久化：`<DATA_DIR>/coc_tsf/<sid>.json`。

    为什么不用 `persist()`：`persist` 写 `saves/`，而 `load_all()` 会把「含 `protagonist`
    的存档」当主模式会话载入 ⇒ CoC 局会出现在主模式对局列表里（t53 §4.7 实测）。
    这里只写镜像的最小集（assets/progress/stages），不含主模式的会话语义。
    """
    try:
        sid = str(mirror.get("sid") or "")
        if not sid:
            return
        d = cfgmod.DATA_DIR / COC_TSF_DIR
        d.mkdir(parents=True, exist_ok=True)
        cfgmod.atomic_write_json(d / f"{sid}.json", {
            "sid": sid,
            "title": mirror.get("title", ""),
            "protagonist": mirror.get("protagonist", {}),
            "assets": mirror.get("assets", {}),
            "portrait_history": mirror.get("portrait_history", []),
            "seed_offset": mirror.get("seed_offset", 0),
            "stage_idx": mirror.get("_last_stage_idx"),
            "saved_at": time.time(),
        }, indent=1)
    except Exception as e:                     # 持久化失败不得影响出图
        log.warning("coc_tsf persist failed: %s", e)


def coc_tsf_load(sid: str) -> dict | None:
    """读回 CoC×TSF 镜像（存档联动：读档后立绘与阶段要一致）。"""
    try:
        p = cfgmod.DATA_DIR / COC_TSF_DIR / f"{sid}.json"
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("coc_tsf load failed %s: %s", sid, e)
        return None


def make_coc_tsf_mirror(coc_session: dict, *, progress: int, anchor: str = "",
                        character: str = "", stages: list[dict] | None = None,
                        title: str = "") -> dict:
    """按 t53 §8.2 的镜像形状建一个 CoC×TSF 会话（**只读** CoC 会话，不改它）。

    `progress` 由 CoC 侧算好（t53 §9 的投影：`tsf_progress_of(run.state)`）；
    未显式给 `stages` 时用 `tsf.STAGES_V6`（六段，与「一章一段」的 Initiate 对齐）。
    """
    sid = str(coc_session.get("sid") or "")
    name = character or ((coc_session.get("run") or {}).get("state", {})
                         .get("investigator", {}) or {}).get("name", "") or "调查员"
    stages_tbl = stages or tsf.build_stages(6)
    return {
        "sid": sid,
        "title": title or f"CoC·{name}",
        "prot_handle": f"{name}_{sid[:4]}",
        "created": coc_session.get("created", time.time()),
        "content_rating": coc_session.get("content_rating", "18"),
        "r18_enabled": coc_session.get("r18_enabled", True),
        "allow_forced": coc_session.get("allow_forced", True),
        "protagonist": {
            "name": name,
            "anchor": anchor,
            "stats": {"progress": int(progress)},
        },
        "stages": stages_tbl,
        "stat_config": [],
        "assets": {"portraits": {}, "backgrounds": {}},
        "bg_map": {},
        "portrait_history": [],
        "seed_offset": 0,
        "_coc_tsf": True,            # 标记：这个会话不是主模式会话
    }


def coc_tsf_prompts(mirror: dict, progress: int | None = None,
                    target_en: str = "") -> dict:
    """CoC×TSF 立绘的提示词（**只 import tsf.py，不改它**）——纯函数，便于对照验证。

    返回 `{stage_idx, positive, negative, denoise}`：段索引用 `tsf.stage_of`，
    正/负面词用同一套 `tsf` 函数，保证与主模式**同一配方**。
    """
    tbl = tsf.stages_for(mirror) if mirror.get("stages") else tsf.build_stages(6)
    prot = mirror.get("protagonist") or {}
    prog = int(progress if progress is not None
               else (prot.get("stats") or {}).get("progress", 0) or 0)
    idx = tsf.stage_of(prog, tbl)
    anchor = prot.get("anchor", "") or ""
    positive = (tsf.stage_portrait_prompt(anchor, prog, target_en, tbl)
                + "，" + TRANSFORM_IMPROVE_EN)
    negative = (tsf.stage_negative_relaxed_stage(idx, tbl)
                + "，" + TRANSFORM_IMPROVE_NEG + "，" + PROT_AGE_NEG
                + "，no camera, no phone, no lens")
    return {"stage_idx": idx, "progress": prog, "positive": positive,
            "negative": negative,
            "denoise": tsf.stage_chain_denoise(idx, tbl, cross_stage=False)}


def start_coc_tsf_portrait(mirror: dict, *, progress: int | None = None,
                           target_en: str = "", supply_bytes: bytes | None = None,
                           cfg: dict | None = None) -> str:
    """发起一次 CoC×TSF 立绘生成（走**既有** `_queue_asset` 闸门，不复制它的逻辑）。

    `supply_bytes` 仅用于**无真实生图时的管线验证**（喂一张真实缓存立绘走完整闸门：
    像素钩子 → 成品复检 → 落盘）。为 None 时走与主模式相同的 `ImageClient.generate_portrait`。
    返回资产 key（`mirror["assets"]["portraits"][key]` 里跟踪状态/文件）。
    """
    cfg = cfg or cfgmod.load_config()
    tbl = tsf.stages_for(mirror) if mirror.get("stages") else tsf.build_stages(6)
    prot = mirror["protagonist"]
    p = int(progress if progress is not None
            else (prot.get("stats") or {}).get("progress", 0) or 0)
    prot["stats"]["progress"] = p                     # 保持一致（stage_of 也读这里）
    idx = tsf.stage_of(p, tbl)
    mirror["_last_stage_idx"] = idx
    name, anchor = prot["name"], prot.get("anchor", "")
    prompt = coc_tsf_prompts(mirror, p, target_en)
    client = ImageClient(cfg)
    mode_tag = "mock" if client.mock else "real"
    key = _skey(mirror, "coc-tsf-portrait", mode_tag, PROMPT_VERSION, _params_tag(cfg),
                *pixel_mod.key_parts(cfg.get("image")),   # 像素开关进 key：开/关各自成图
                *style_key_part(cfg.get("image")),        # t61：画风进键（默认零影响）
                name, anchor, idx, int(p // 10))
    label = f"{name}·阶段{idx + 1}"
    handle = mirror.get("prot_handle") or name
    seq = _reserve_file_name(mirror, handle)
    style = _img_style(mirror)
    neg = (prompt["negative"]
           + ("" if _adult_flags(mirror)[0]
              else "，nude, naked, underwear only, lingerie, topless"))

    nude_ok = _nude_hint_text("", "")     # CoC 立绘无换装/重画需求 ⇒ 与主模式"阶段立绘"同参

    async def factory():
        if supply_bytes is not None:                  # 管线验证路径（真实缓存图）
            data = supply_bytes
        else:
            data = await client.generate_portrait(
                name, prompt["positive"], f"阶段{idx + 1}", f"{name}的阶段立绘",
                style, extra_negative=neg, seed=_seed_for(mirror, f"coc|{name}|{idx}"),
                strict=True)
        # ★ t58：补齐与主模式（`_start_protagonist_portrait` L2333-2350）**同一套**三步后处理。
        # `generate_portrait` 不含抠图，`_queue_asset` 也不负责抠图 ⇒ 不补这三步，
        # CoC×TSF 立绘就是"带背景的不透明方图"（t55 实测 alpha 级=1、透明 0%）。
        raw_name = f"{_session_dir_name(mirror)}/raw/{key}_raw.png"
        (cfgmod.CACHE_DIR / raw_name).write_bytes(data)          # 1) 未抠图原图（同名同目录约定）
        out = await process_portrait_async(data, _cutout_mode())  # 2) 抠图（rembg，CPU）
        issue = await asyncio.to_thread(
            _final_check_issue, out, nude_ok=nude_ok)             # 3) 成品完整性复检
        if issue:
            # 语义与主模式**阶段立绘**（`not outfit_s`）一致：CoC×TSF 立绘没有"换装差分"形态，
            # 不合格就该拒绝入库 → 由 `_queue_asset` 的失败路径回退上一版（不空屏），
            # 而不是把一张有缺陷的图写进 ready（主模式对换装差分才只软告警）。
            raise ImageGenError(f"成品完整性复检未通过（{issue}）")
        return out, raw_name

    _queue_asset(mirror, "portraits", key, label, factory, handle, seq,
                 persist_fn=coc_tsf_persist)
    return key


# 1.7.35 场景→制服自动映射（用户：学园背景角色应统一校服；神社/女仆店/
# 医院等特定场所也是制服而非衣着各异；玩家显式「本局制服设定」优先）
SCENE_UNIFORMS = [
    (("学园", "校园", "学校", "高中", "学院", "大学", "教室", "走廊",
      "部室", "社团", "班级", "体育馆", "操场", "教室", "食堂"),
     "school uniform, classic student uniform, white blouse, "
     "dark blazer, ribbon tie, pleated skirt, knee-high socks",
     "casual clothes, streetwear, jeans, t-shirt, hoodie"),
    (("神社", "巫女", "参道", "祭典", "庙会"),
     "miko outfit, white kimono, red hakama, shrine maiden costume",
     "casual clothes, modern clothes, jeans"),
    (("女仆", "咖啡厅", "咖啡店", "甜品店"),
     "maid outfit, black dress, white apron, frilled headdress",
     "casual clothes, streetwear, jeans"),
    (("医院", "病房", "诊室", "诊所", "护理"),
     "nurse uniform, white coat, medical uniform",
     "casual clothes, streetwear, jeans"),
    (("泳池", "海滩", "沙滩", "海水浴", "泳装"),
     "school swimsuit, swimwear",
     "casual clothes, streetwear, jeans, dress"),
]


def scene_uniform_for(scene_name: str) -> tuple[str, str] | None:
    """按当前场景名返回 (uniform_en, uniform_neg)；无匹配返回 None。"""
    name = str(scene_name or "")
    if not name:
        return None
    for keys, en, neg in SCENE_UNIFORMS:
        if any(k in name for k in keys):
            return en, neg
    return None


def _uniform_for(session: dict) -> tuple[str | None, str | None]:
    """本局生效制服（1.7.35）：玩家显式设定 > 当前场景自动映射 > 无。"""
    en = str(session.get("uniform_en") or "").strip()
    neg = str(session.get("uniform_neg") or "").strip()
    if en or neg:
        return (en or None), (neg or None)
    hit = scene_uniform_for(session.get("current_scene", ""))
    if hit:
        return hit
    return None, None


# 1.9.0：换装时只有「本来就是制服类」的服装才继续套本局制服锁。
# 旧逻辑无条件把 uniform_en 追加到换装 prompt——玩家让角色换「白色连衣裙」
# 时，prompt 里同时有 dress 和 school uniform，两者互撕，经常被拉回校服
# 或画成四不像。非制服类换装 = 显式清空制服锁（空串，而不是 None——None
# 会回退全局 config，仍然套锁）。
_UNIFORM_OUTFIT_HINTS = (
    "制服", "校服", "水手服", "西装校服", "学生服", "巫女", "女仆",
    "护士", "白大褂", "军装", "警服", "和服", "浴衣", "骑士", "圣女",
    "偶像", "打歌服", "啦啦队", "运动服", "体操服", "飞行服", "驾驶服",
    "uniform", "sailor", "blazer", "miko", "maid", "nurse", "lab coat",
)


def _outfit_uniform_like(outfit: str) -> bool:
    text = str(outfit or "").strip().lower()
    return bool(text) and any(k.lower() in text for k in _UNIFORM_OUTFIT_HINTS)


def _uniform_for_portrait(session: dict, outfit_s: str = "") -> tuple[str | None, str | None]:
    """立绘生效制服：普通立绘按会话/场景；换装非制服类时显式摘锁。"""
    if str(outfit_s or "").strip() and not _outfit_uniform_like(outfit_s):
        return "", ""
    return _uniform_for(session)


def _uniform_text_for(session: dict, outfit_s: str = "") -> str:
    en, _neg = _uniform_for_portrait(session, outfit_s)
    return str(en or "").strip()


async def _context_summary(session: dict) -> str:
    """压缩对话上下文的摘要（1.7.36「压缩上下文·重置 AI」专用）：
    与 _prior_summary 不同——不只压缩历史，还要**保留当前剧情方向**：
    人物关系进展/主角当前形态/感情张力/未了悬念/（玩家允许的）尺度氛围
    ——重置后 AI 仍沿着正确方向写，且不再被长上下文稀释（长局 AI 变保守
    的常见元凶：早期尺度语境被几百条对话淹没）。"""
    prot = session.get("protagonist") or {}
    raw_log = session.get("log") or []
    hist = "\n".join(
        f"- {t.get('character', '')}：{str(t.get('text', ''))[:70]}"
        for t in raw_log[-40:]) or "（无剧情记录）"
    chars = "、".join(
        f"{c.get('name', '')}（{str(c.get('personality', ''))[:20]}）"
        for c in (session.get("characters") or [])) or "（无）"
    prompt = (
        "你是视觉小说上下文压缩器。请把以下对局压缩成 250 字以内的中文"
        "【上下文提要】：人物关系进展、主角当前形态与状态、当前感情/张力、"
        "未了悬念、以及情节氛围方位（若成人向允许的尺度氛围请如实保留）；"
        "只输出正文：\n"
        f"主角：{str(prot.get('name', ''))}（{str(prot.get('anchor', ''))[:50]}，"
        f"身份：{str(prot.get('identity', ''))[:30]}）\n其他角色：{chars}\n"
        f"最近剧情（按时间序）：\n{hist}")
    try:
        from .llm import LLMClient
        cfg = cfgmod.load_config()
        out = (await LLMClient(cfg).chat(
            [{"role": "user", "content": prompt}], temperature=0.3) or "").strip()
        if out:
            return out[:500]
    except Exception as e:
        log.warning("context summary LLM failed (%s), local fallback", e)
    return (f"（自动摘要）{hist}"[:600] or "（无剧情记录）")


async def context_reset(sid: str, keep_turns: int = 6) -> dict:
    """压缩上下文·重置 AI（1.7.36）：把长对话压缩为摘要（保留方向与氛围），
    只留最近 keep_turns 幕——system 的【内容分级政策】恒定注入不受影响，
    重置后 AI 回到接近开局的开放状态（解决「AI 后期不做成人向」的上下文
    稀释问题）。"""
    session = get_session(sid)
    raw_log = session.get("log") or []
    if len(raw_log) < keep_turns + 3:
        raise GameError(
            f"剧情还短（{len(raw_log)} 幕），无需压缩；多推进几幕再试")
    summary = await _context_summary(session)
    kept = raw_log[-int(keep_turns):]
    session["log"] = kept
    session["summary"] = summary
    session["context_compacted"] = time.time()
    persist(session)
    log.info("context reset: %s (summary %d 字, kept %d 幕)",
             sid, len(summary or ""), len(kept))
    return {"ok": True, "summary_chars": len(summary or ""),
            "kept_turns": len(kept)}


async def _prior_summary(session: dict, use_llm: bool = True) -> str:
    """二周目前情压缩（1.7.34）：把上一局剧情压缩成 ≤250 字中文梗概——
    LLM 总结；失败（离线/mock/超时）或 use_llm=False（隔离测试）
    回退为「最近剧情截断」版，**保证上下文不爆表**（不继承全量 log，
    仅注入压缩提要）。"""
    prot = session.get("protagonist") or {}
    if not isinstance(prot, dict):  # 1.7.34 防脏快照（protagonist 反序列化为 str）
        prot = {"name": str(prot)[:12], "anchor": ""}
    raw_log = session.get("log") or []
    chars = "、".join(
        f"{c.get('name', '')}（{str(c.get('personality', ''))[:24]}）"
        for c in (session.get("characters") or [])) or "（无）"
    events = "\n".join(
        f"- {t.get('character', '')}：{str(t.get('text', ''))[:70]}"
        for t in raw_log[-24:]) or "（无剧情记录）"
    prompt = (
        f"请把下面视觉小说对局『{session.get('title', '')}』的剧情压缩成 "
        "250 字以内的中文前情提要：概述人物关系、关键事件与未了悬念，"
        "只输出正文，不要任何格式：\n"
        f"主角：{prot.get('name', '')}（{str(prot.get('anchor', ''))[:60]}）\n"
        f"其他角色：{chars}\n最近剧情：\n{events}")
    try:
        if use_llm:
            from .llm import LLMClient
            cfg = cfgmod.load_config()
            out = (await LLMClient(cfg).chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3) or "").strip()
            if out:
                return out[:500]
    except Exception as e:
        log.warning("prior summary LLM failed (%s), local fallback", e)
    # 本地回退：最近剧情截断（保证非空、可控长度）
    return (f"（自动摘要）{events}"[:600] or "（上一局无剧情记录）")


def _inherit_scene_assets(source: dict, new: dict) -> int:
    """二周目继承场景（1.7.34）：把旧局 ready 的场景背景文件复制进新局并
    注册（scenes/scene bg_map/assets）——首次进入该场景即复用，零重绘。"""
    n = 0
    src_assets = (source.get("assets") or {}).get("backgrounds") or {}
    src_bg_map = source.get("bg_map") or {}
    scenes = source.get("scenes") or {}
    for sc_id, sc in scenes.items():
        if not isinstance(sc, dict):
            continue
        bg_id = sc.get("bg_id")
        key = (src_bg_map or {}).get(bg_id or "")
        entry = src_assets.get(key or "")
        if not entry or entry.get("status") != "ready" or not entry.get("file"):
            continue
        p = cfgmod.CACHE_DIR / entry["file"]
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
            dir_name = _session_dir_name(new)
            nd = cfgmod.CACHE_DIR / dir_name / "backgrounds"
            nd.mkdir(parents=True, exist_ok=True)
            fname = _finish_file_name(str(sc.get("name") or bg_id or "scene"),
                                      n + 1, data, "png")
            rel = f"{dir_name}/backgrounds/{fname}"
            (nd / fname).write_bytes(data)
            new.setdefault("bg_map", {})[bg_id] = key
            (new.setdefault("assets", {}).setdefault("backgrounds", {}))[key] = {
                "status": "ready", "file": rel,
                "label": str(sc.get("name", "")),
            }
            new.setdefault("scenes", {})[sc_id] = dict(sc, bg_id=bg_id)
            new["bg_count"] = int(new.get("bg_count", 0)) + 1
            n += 1
        except Exception as e:
            log.warning("inherit scene %s skipped: %s", sc_id, e)
    return n


async def new_game_plus(source: dict, title: str = "") -> dict:
    """二周目（1.7.34）：继承角色/制服/分级/转变目标/主角锚点 + 场景背景，
    **上下文压缩**（前情摘要 ≤600 字注入新局）——数值/剧情/立绘从头
    开始（fresh），唯「人物与场景」延续。
    1.7.34 响应修复：摘要先取**瞬时本地版**（不阻塞）；开局后由后台任务
    用 LLM 精炼升级（不阻塞首次响应——首屏 ≈ 普通开局耗时）。"""
    prior = await _prior_summary(source, use_llm=False)
    src_prot = source.get("protagonist") or {}
    if not isinstance(src_prot, dict):
        src_prot = {"name": str(src_prot)[:12], "anchor": ""}
    payload = {
        "mode": source.get("mode", "tsf"),
        "title": (str(title).strip()
                  or f"{str(source.get('title', '未命名'))}·二周目")[:40],
        "world": source.get("world", ""),
        "characters": charm.normalize_character_list(
            source.get("characters") or [], limit=4,
            protagonist=str(src_prot.get("name", ""))),
        "protagonist": {
            "name": src_prot.get("name", "主角"),
            "anchor": src_prot.get("anchor", "黑色短发，深色眼瞳，日常便装"),
            "personality": "安静内向的成年人",
        },
        "outline": "",
        "content_rating": source.get("content_rating", "18"),
        "r18_enabled": bool(source.get("r18_enabled", True)),
        "allow_forced": bool(source.get("allow_forced", True)),
        "adult_intensity": source.get("adult_intensity", "浓烈"),
        "stat_config": source.get("stat_config"),
        "uniform_en": source.get("uniform_en", ""),
        "uniform_neg": source.get("uniform_neg", ""),
        "uniform_name": source.get("uniform_name", ""),
        "directives": [d for d in (source.get("directives") or [])
                       if d.get("enabled")],
        "tsf_target": source.get("tsf_target", {}),
        "prior_summary": prior,
    }
    state = await start_game(payload)
    new_sid = state.get("sid") or ""
    new_session = SESSIONS.get(new_sid)
    if new_session:
        n = _inherit_scene_assets(source, new_session)
        if n:
            persist(new_session)
            try:
                state = public_state(new_session)
            except Exception:
                pass
        log.info("newgame-plus: '%s' -> '%s' (scenes %d, prior %d 字)",
                 source.get("title", ""), new_session.get("title", ""),
                 n, len(prior or ""))

        async def _enhance_prior():
            """后台 LLM 精炼前情（不阻塞首次响应；失败保留本地截断版）。"""
            try:
                en = await _prior_summary(source, use_llm=True)
                if en and new_session.get("summary") != en:
                    new_session["summary"] = en[:800]
                    persist(new_session)
                    log.info("newgame-plus prior enhanced (%d 字)", len(en))
            except Exception as e:
                log.warning("newgame-plus prior enhance failed: %s", e)

        _spawn_task(_enhance_prior())   # B-02：统一经 _spawn_task 投递
    return state


def _img_style(session: dict) -> str:
    """风格后缀：游戏配置 + 电影风格档 + 插件追加的 image_style 指令 + 画风预设。

    t61：末尾**追加** `image.style_preset` / `image.style_custom` 的画风词
    （`style_suffix()` 在默认/未设时返回空串 ⇒ 与改动前逐字节相同）。
    追加顺序刻意放在最后：既有 TSF 阶段提示词、电影档、插件指令**一个字符不动**，
    画风只在尾部叠加（契约红线：不得替换既有提示词）。
    """
    from .plugins import image_style_extras
    game_cfg = cfgmod.load_config()["game"]
    base = apply_style_mode(game_cfg.get("style_suffix", ""),
                            game_cfg.get("style_mode", "default"))
    extras = image_style_extras()
    if extras:
        base += "，" + "，".join(extras)
    img_cfg = cfgmod.load_config().get("image") or {}
    # t70：画风若绑定了专门 LoRA（如像素风 → pixel-art-xl），LoRA 词**必须**拼进提示词，
    # 否则模型永远加载不上（A1111 靠 `<lora:名:权重>` 词触发加载）。
    # 放在最末：既有阶段词/电影档/插件指令/画风词**一个字符不动**（沿用 t61 红线）。
    lo = lora_tag(img_cfg)
    return base + style_suffix(img_cfg) + (("，" + lo) if lo else "")


def _img_extra_neg(session: dict) -> str:
    """插件追加的立绘负面词（减少错误生成）+ 画风预设的负面追加词（t61）。

    画风负面**只来自预设表**：`image.style_custom`（玩家自由文本）**不进负面**，
    否则玩家输入会把负面区破掉（契约要求）。
    """
    from .plugins import image_negative_extras
    parts = ["，".join(image_negative_extras()), style_negative(
        cfgmod.load_config().get("image") or {}).lstrip("，")]
    return "，".join(p for p in parts if p)




def delete_portrait_version(sid: str, label: str, file: str) -> dict:
    """立绘历程「删除此立绘」（1.7.38/1.7.39）：从 portrait_history 与资产
    引用中移除该版本并删除文件——**被删版本不再被任何系统调用**。
    1.7.39 升级：**当前使用版也可删除**——若是当前资产引用的版本，删除后
    立即**按阶段论自动重绘**（主角=按当前同化率阶段全量重画
    regenerate_label→regenerate_portrait；NPC=重画该表情差分），
    返回 regenerating=重绘中的标签。"""
    session = get_session(sid)
    file = str(file or "").strip()
    label = str(label or "").strip()
    if not file or not label:
        raise GameError("缺少立绘信息")
    p = (cfgmod.CACHE_DIR / file).resolve()
    cache_root = str(cfgmod.CACHE_DIR.resolve())
    if not str(p).startswith(cache_root) or p == Path(cache_root):
        raise GameError("非法路径")
    hist = session.setdefault("portrait_history", [])
    removed = [e for e in hist
               if e.get("file") == file and e.get("label") == label]
    session["portrait_history"] = [
        e for e in hist
        if not (e.get("file") == file and e.get("label") == label)]
    # 当前资产引用（1.7.39：允许删除——删除后按阶段重新绘画）
    refs = [k for k, it in (session.get("assets", {}).get("portraits") or {}).items()
            if it.get("file") == file]
    # 若删除的是「固定显示」版本 → 解除固定（否则已删立绘仍被固定显示）
    pins = session.setdefault("pinned_portraits", {})
    for k in [k for k, v in pins.items() if v == label]:
        pins.pop(k, None)
    if p.is_file():
        try:
            p.unlink()
        except OSError as e:
            log.warning("portrait file unlink failed (%s)，条目已移除", e)
    persist(session)
    if not removed and not refs:
        raise GameError("未找到该立绘记录（可能已清理）")
    regenerating = ""
    if refs:
        # 删除的是当前使用版：移除资产条目 → 按阶段论自动重绘该标签
        for k in refs:
            session["assets"]["portraits"].pop(k, None)
        try:
            r = regenerate_label(sid, label)
            regenerating = r.get("regenerating", "") or label
        except GameError as e:
            log.warning("delete->regenerate failed: %s", e)
        persist(session)
    return {"ok": True, "label": label, "file": file,
            "pinned_cleared": True, "regenerating": regenerating}


def _final_check_issue(data: bytes, nude_ok: bool = False) -> str | None:
    """成品复检（抠图后）：完整性不过关的立绘不入 ready（前端自动回退
    基础立绘/表情，避免「半身/腿特写/双人同框/裸体」坏图出现在游戏里）。
    1.7.37 新增双人同框与裸露硬拦截（nude_ok=True：命令裸体档跳裸露检查）。

    B-05/M-02：整图只解码一次（`_check_bundle`），四个判据复用同一份缩图；
    判据顺序/数值与命中结果不变。调用方请用
    `await asyncio.to_thread(_final_check_issue, ...)`，不要直接同步调用。
    """
    try:
        from .image import (_check_bundle, _completeness_issue, _min_size_issue,
                            _multi_figure_issue_b, _green_noise_issue_b,
                            _skin_excess_issue_b)
        bundle = _check_bundle(data)
        issue = (_completeness_issue(bundle["full"])
                 or _min_size_issue(bundle["full"])
                 or _multi_figure_issue_b(bundle)
                 # 1.7.37 绿噪复检（raw 层漏网时 cutout 再拦——只查绿：
                 # 紫判据在抠图画布上会把蓝裙/蓝制服误判为紫斑）
                 or _green_noise_issue_b(bundle))
        if issue:
            return issue
        if not nude_ok:
            return _skin_excess_issue_b(bundle)
        return None
    except Exception:
        return None


def _raw_quality_issue(data: bytes, *, allow_multi: bool = False) -> str | None:
    """raw 级成品复检（`_queue_asset` 写 ready 前的那道门）：双人同框 + 绿噪。

    `allow_multi=True` 用于背景/CG：它们本来就允许多人同框（CG 更是必须），
    旧逻辑拿「双人同框」去毙背景，导致旧存档的庭院/走廊反复生成失败、永远
    没有场景图。背景仍保留绿噪/解码/尺寸等硬检查。
    """
    try:
        from .image import (_check_bundle, _multi_figure_issue_b,
                            _green_noise_issue_b)
        bundle = _check_bundle(data)
        if allow_multi:
            # 背景/CG：多人同框与绿色场景（庭院/森林/草地）都是正常内容，
            # 不能拿立绘的绿噪点判据去毙；解码/尺寸由 _bg_check 负责。
            return None
        return _multi_figure_issue_b(bundle) or _green_noise_issue_b(bundle)
    except Exception:
        return None


def _nude_hint_text(*parts: str) -> bool:
    """是否裸体/内衣档（复检跳过裸露检查——用户明确的裸体命令）。"""
    all_text = "，".join(str(p) for p in parts if p)
    return any(k in all_text for k in (
        "裸体", "一丝不挂", "未着寸缕", "全裸", "内衣", "内裤",
        "nude", "naked", "underwear", "lingerie"))


def _cutout_mode() -> str:
    return (cfgmod.load_config()["image"].get("cutout_mode") or "auto")


_NOTE_EN_CACHE: dict = {}


async def _await_cutout(session: dict, name: str, label: str,
                        timeout: float = 120.0) -> bytes | None:
    """等待某角色立绘底图就绪（表情差分图生图用：先等 neutral 完成）。
    任务并发执行，neutral 可能在另一任务里刚排队——轮询到超时为止。"""
    import asyncio as _aio
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = _find_prev_cutout(session, name, label)
        if b:
            return b
        await _aio.sleep(0.6)
    return None


def _chain_issue(out: bytes, prev: bytes | None = None) -> str | None:
    """链式成品（img2img 演化）组合质检——「软件内部精选种子」的判据：
    把全量质检家底 + 高频纹理异常（块方差）合并，命中即换种子重试。
    1.7.30 用户要求：游戏内变化要像人工精选种子那样精美——凭据就是这
    组检查（数分钟前的测试证明：选种后被弃的绿噪/紫斑/横纹/晶簇图全部
    在此被拦截或部分拦截）。

    B-05/M-02：整图只解码一次（`_check_bundle`），判据/阈值不变；
    调用方请用 `await asyncio.to_thread(_chain_issue, ...)`。
    """
    from . import image as _im
    try:
        bundle = _im._check_bundle(out)
        comp = (_im._completeness_issue(bundle["full"])
                or _im._min_size_issue(bundle["full"])
                or _im._nan_issue_b(bundle) or _im._purple_blob_issue_b(bundle)
                or _im._fast_face_test_b(bundle))
        if comp:
            return comp
        # 高频纹理异常（晶簇/横纹家族：块内方差高占比）
        # B-05/M-02：复用 bundle 里那张 48px RGB 缩图（与原来这里
        # 自己缩一次的参数完全一致：convert("RGB") + thumbnail((48,48), LANCZOS)），
        # 不再为这一项多解码/多缩一次。
        im2 = bundle["rgb48"]
        pw, ph = im2.size
        px = list(im2.getdata())
        B = 4
        high = total = 0
        for by in range(0, ph, B):
            for bx in range(0, pw, B):
                vals = [px[y * pw + x]
                        for y in range(by, min(by + B, ph))
                        for x in range(bx, min(bx + B, pw))]
                n = len(vals)
                mean = [sum(v[i] for v in vals) / n for i in range(3)]
                var = sum(sum((v[i] - mean[i]) ** 2 for i in range(3))
                          for v in vals) / n
                total += 1
                if var > 2600:
                    high += 1
        if total and high / total > 0.23:
            return "高频纹理异常"
        return None
    except Exception:
        return None


async def _refine_with_retry(client, prev_cut: bytes, prompt: str, seed: int,
                             denoise: float, body_dn: float,
                             hair_words: str, negative_extra: str) -> bytes:
    """链式「种子轮换+质检」包装（1.7.30）：把人工挑种子的行为内化为
    软件自身——不合格自动 seed+1000003×k 换轮（最多 3 轮），全败抛错由
    上层回退。这就是「游戏内像指定种子般精美」的实现机制。"""
    from .image import ImageGenError
    last = ""
    for k in range(3):
        s = (seed + 1000003 * k) % (2 ** 31)
        out = await client.refine_transition(
            prev_cut, prompt, s, denoise=denoise,
            body_pass=True, body_denoise=body_dn,
            hair_words=hair_words, negative_extra=negative_extra)
        # refine_transition 的内部降级会返回原图；不能把“没有发生变化”
        # 当成链式成功，否则 TSF 阶段会写入一张伪 ready 旧图。
        if out == prev_cut:
            issue = "转变链未产生变化"
        else:
            issue = await asyncio.to_thread(_chain_issue, out, prev_cut)
        if not issue:
            return out
        last = issue
        log.warning("chain refine seed rot (k=%d): %s", k, issue)
    raise ImageGenError(f"转变链重绘全部退化（{last}）")


# 网络性别转变 AI 绘画教学沉淀：
# - 柔和渐变词（softer/slightly 渐进，不用极端替换词——保持「同一人」）
# - 身份保真词（same eye shape/nose shape/facial proportions）
def _tsf_guard_front(anchor: str, keep_hair_color: bool = True) -> str:
    """转变链图生图（refine_transition / use_initial）的「前置守护词」：
    服装锁（便装给具体词）+ 瞳色锁；发色锁仅当 keep_hair_color=True（目标
    设定里取消勾选发色保持时，改由目标发色词驱动——1.7.16）。词必须在
    提示词**前段**才有效——A1111 末端词被稀释（1.7.13 实测）。"""
    casual = any(k in anchor for k in ("便装", "便服", "日常", "休闲"))
    dress = ("fully dressed, wearing clothes, wearing everyday casual outfit, "
             "shirt and pants" if casual
             else "fully dressed, wearing clothes, "
                  "proper outfit covering the body")
    color_part = (", (same hair color:1.2), (same eye color:1.2), "
                  "(keep original hair and eye colors:1.15)"
                  if keep_hair_color else ", (same eye color:1.2)")
    return f"{dress}, {color_part.lstrip(', ')}"


# 主角「转变目标设定」（游戏内可配置，仅主角生效）——中文档位 → 英文提示词
TSF_TARGET_HAIR = {
    "维持": "", "短发": "short neat hair", "及肩长发": "shoulder-length soft hair",
    "长发披肩": "long flowing hair", "及腰长发": "waist-length flowing hair",
}
TSF_TARGET_BUILD = {
    "维持": "", "纤细均称": "slender graceful figure, slim body",
    "丰盈曲线": "fuller feminine curves, hourglass figure, soft bust",
    "高挑苗条": "tall slim figure, elegant long legs",
    # 1.7.32 修正：原「petite childish build/cute round face」组合在该
    # realskin 模型上系统性崩溃（跨 5 个种子簇全噪点废图，实锤）——
    # 改写为温和的娇小形态词（小个子/纤细/精致），仍只做体型引导：
    "娇小幼态": "petite small female build, small slim stature, "
                "tiny delicate figure, slender petite frame, "
                "soft refined features, large bright eyes",
    # 1.7.33 开局面板「最终变化样式·身材」三档（温和轮廓词，不涉性化）：
    "萝莉": "petite girlish build, small stature, tiny delicate figure, "
            "slender petite frame, soft refined features, large bright eyes",
    "少女": "slender youthful figure, slim elegant frame, "
            "graceful youthful girl build",
    "熟女": "mature elegant woman figure, graceful womanly curves, "
            "strong feminine presence",
}

# 1.7.33 最终变化样式·胸部大小（保守轮廓词，全程不涉露骨/巨型词）
TSF_TARGET_CHEST = {
    "维持": "", "小巧": "small modest bust, gentle chest",
    "适中": "natural balanced bust, soft chest",
    "丰满": "fuller soft bust, feminine curves developing",
}

# 1.7.33 最终变化样式·瞳色（中文档位 → 英文）
TSF_TARGET_EYE = {
    "维持": "", "黑色": "black eyes", "蓝色": "blue eyes",
    "紫色": "purple eyes", "红色": "red eyes", "金色": "golden eyes",
    "绿色": "emerald green eyes", "银色": "silver eyes",
}

# 1.7.33 最终变化样式·发色（中文档位 → 英文；「维持原色」由 keep_hair_color 表达）
TSF_TARGET_HAIR_COLOR = {
    "维持原色": "", "黑色": "black hair", "银白色": "silver white hair",
    "金色": "golden hair", "棕色": "chestnut brown hair",
    "紫色": "purple hair", "粉色": "pink hair", "蓝色": "blue hair",
}

# 1.7.33 最终变化样式·最终衣物（保守档位；外观含便装词时自动摘制服锁）
TSF_TARGET_CLOTHES = {
    "维持": "", "白衬衫黑长裤": "white blouse, dark slacks, smart casual",
    "白色连衣裙": "white dress, knee length dress",
    "学园制服": "school uniform, white blouse, pleated skirt, ribbon tie",
    "蕾丝裙装": "lace dress, frilled skirt",
    "便装卫衣": "hoodie, casual everyday clothes",
}


def _tsf_target_en(session: dict, female_w: float | None = None) -> str:
    """按会话「转变目标设定」组装英文目标词（发/体型/胸/瞳/气质/发色/衣物）。
    1.7.33 新增：chest（胸）词仅在女性权重 >=0.5 才并入——
    「最终样式」仍按同化率渐变（target_part 注入门阀保持不变）。"""
    t = session.get("tsf_target") or {}
    parts = []
    hair = str(t.get("hair", "")).strip()
    build = str(t.get("build", "")).strip()
    chest = str(t.get("chest", "")).strip()
    eye = str(t.get("eye", "")).strip()
    clothes = str(t.get("clothes", "")).strip()
    aura = str(t.get("aura", "")).strip()
    hair_color = str(t.get("hair_color", "")).strip()
    keep_color = bool(t.get("keep_hair_color", True))
    en = TSF_TARGET_HAIR.get(hair)
    if en:
        parts.append(en)
    en = TSF_TARGET_BUILD.get(build)
    if en:
        parts.append(en)
    en = TSF_TARGET_CHEST.get(chest)
    if en and (female_w is None or female_w >= 0.5):
        parts.append(en)
    en = TSF_TARGET_EYE.get(eye)
    if en:
        parts.append(en)
    en = TSF_TARGET_CLOTHES.get(clothes)
    if en:
        parts.append(en)
    if aura:
        parts.append(aura)
    if hair_color and not keep_color:
        # 发色档位映射到完整英文短语；未命中（自由文本）补 " hair"
        parts.append(TSF_TARGET_HAIR_COLOR.get(hair_color)
                     or (hair_color + " hair"))
    return ", ".join(p for p in parts if p)


def _norm_tsf_target(raw) -> dict:
    """规范化「转变目标设定」字段：**「维持」与空串一律视为清除**——
    用户把某字段改回「维持」/留空即回退默认，不会残留在 Session。"""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k in ("hair", "build", "chest", "eye", "clothes", "aura",
              "hair_color"):
        v = str(raw.get(k, "") or "").strip()[:60]
        # 「维持/维持原色」与空串一律视为清除（该字段回默认）
        out[k] = "" if v in ("维持", "维持原色") else v
    out["keep_hair_color"] = bool(raw.get("keep_hair_color", True))
    return out


def _stage_plan_style(session: dict) -> str:
    """阶段计划样式（1.7.46 设置可选）：auto 跟随当前生图模型是否 wai 系；
    wai/generic 为显式覆盖。读取 config.image.stage_style（默认 auto）。"""
    try:
        mode = str(cfgmod.load_config().get("image", {}).get("stage_style", "auto")).lower()
    except Exception:
        mode = "auto"
    if mode in ("wai", "generic"):
        return mode
    from .image import is_wai_checkpoint
    ck = ""
    try:
        ck = str(cfgmod.load_config().get("image", {}).get("model", ""))
    except Exception:
        ck = ""
    if not ck:
        ck = str((session.get("engine") or {}).get("model", ""))
    return "wai" if is_wai_checkpoint(ck) else "generic"


async def _ensure_stage_plan_llm(session: dict) -> None:
    """1.7.48 LLM 渐变规划器：AI API 按**角色情况**（锚点/目标/剧情摘要/段数）
    编排本局专属的每段六维度变化词，合并覆盖模板计划（方向主导+保底）。

    设计要点：
    - 先跑同步 _ensure_stage_plan（模板兜底永远存在——LLM 失败/未配置/无
      API 时整局照常走固定链，**绝不断出图**）；
    - LLM 成功且 merge 出差异后替换 session["stage_plan"]，meta 追加 llm
      指纹（锚点+目标+摘要头部+样式+显性），同局重建不再重复调用；
    - 开关 config.image.tsf_plan_llm（默认 true）；LLMClient mock/异常 → 保留模板。
    """
    _ensure_stage_plan(session)  # 同步兜底：plan 与 meta 此时已存在
    meta = dict(session.get("stage_plan_meta") or {})
    try:
        enabled = bool(cfgmod.load_config().get("image", {}).get("tsf_plan_llm", True))
    except Exception:
        enabled = True
    if not enabled or meta.get("llm"):
        return
    stages_tbl = tsf.stages_for(session)
    prot = session.get("protagonist") or {}
    anchor = str(prot.get("anchor", ""))
    target = session.get("tsf_target") or {}
    keep_color = bool(target.get("keep_hair_color", True))
    hair_color = str(target.get("hair_color", "")).strip()
    hair_final_en = ("" if keep_color or not hair_color
                     else (TSF_TARGET_HAIR_COLOR.get(hair_color)
                           or (hair_color + " hair")))
    target_en = _tsf_target_en(session, female_w=1.0)
    if hair_final_en:
        target_en = ", ".join(p for p in [target_en, hair_final_en] if p)
    style = _stage_plan_style(session)
    explicit = bool(meta.get("e"))
    summary = str(session.get("summary") or "")
    if not summary:
        raw_log = session.get("log") or []
        if isinstance(raw_log, list) and raw_log:
            summary = "；".join(str(m.get("summary") or m.get("text") or "")
                                for m in raw_log[-2:])
    try:
        llm = LLMClient(cfgmod.load_config())
        if llm.mock:
            return
        out = await llm.chat_json([
            {"role": "system", "content": tsf.LLM_GRADIENT_SYSTEM},
            {"role": "user", "content": tsf.llm_gradient_user_text(
                anchor, target_en, stages_tbl,
                int((prot.get("stats") or {}).get("progress", 0)),
                summary=summary, style=style, explicit=explicit)},
        ], temperature=0.3)
    except Exception as e:
        log.warning("tsf llm gradient planner unavailable (%s), keep template",
                    repr(e)[:120])
        return
    segments = out.get("segments") if isinstance(out, dict) else None
    merged = tsf.merge_llm_plan(session["stage_plan"], segments)
    if merged is session["stage_plan"]:
        return
    session["stage_plan"] = merged
    meta["llm"] = asset_key("llm-grad", anchor, str(target_en)[:80],
                            str(summary)[:60], style, str(explicit),
                            PROMPT_VERSION)
    session["stage_plan_meta"] = meta
    log.info("tsf stage plan upgraded by LLM (%d/%d segments)", 
             sum(1 for e in merged if e.get("llm")), len(merged))


def _ensure_stage_plan(session: dict) -> None:
    """开局/目标变更后**预写整局每段立绘提示词**（1.7.43 渐变状态机计划）。

    「写出六步/十步变化」能力落点：把 sd-webui 实机渐变配方（v14/v15
    十七帧链式沉淀）的六条状态机（发长/发色溶解/脸型/表情/服装重组/
    性征）× 段语义层级，按会话段数（六段/十段）一次写好全部段的提示词，
    存 session["stage_plan"]；meta（PROMPT_VERSION+段数+anchor+目标指纹）
    任一变化自动重建。阶段跃迁全量生成优先消费该计划（plan-first），
    链式/初始重建路径维持原已验证曲线不变。"""
    stages_tbl = tsf.stages_for(session)
    target = session.get("tsf_target") or {}
    prot = session.get("protagonist") or {}
    anchor = str(prot.get("anchor", ""))
    # 1.7.47 性转变显性链：需「设置里开启（image.tsf_explicit）~且~ 该局
    # 18+（content_rating=18 + r18_enabled）」双条件——默认关闭时保持
    # 项目立绘非露骨红线（显性= v15 实机收缩链，亦不涉未成年）。
    explicit = False
    try:
        if bool(cfgmod.load_config().get("image", {}).get("tsf_explicit", False)):
            explicit = _adult_flags(session)[0]
    except Exception:
        explicit = False
    meta = {"pv": PROMPT_VERSION, "n": len(stages_tbl), "a": anchor,
            "t": asset_key(*(f"{k}={target.get(k)}" for k in
                             ("hair", "build", "chest", "eye", "clothes",
                              "aura", "hair_color", "keep_hair_color"))),
            "s": _stage_plan_style(session), "e": explicit}
    if (isinstance(session.get("stage_plan"), list) and session["stage_plan"]
            and session.get("stage_plan_meta") == meta):
        return
    keep_color = bool(target.get("keep_hair_color", True))
    hair_color = str(target.get("hair_color", "")).strip()
    hair_final_en = ("" if keep_color or not hair_color
                     else (TSF_TARGET_HAIR_COLOR.get(hair_color)
                           or (hair_color + " hair")))
    # 1.7.47 性转变显性链：需「设置里开启（image.tsf_explicit）~且~ 该局
    # 18+（content_rating=18 + r18_enabled）」双条件——默认关闭时保持
    # 项目立绘非露骨红线（显性= v15 实机收缩链，亦不涉未成年）。
    explicit = False
    try:
        if bool(cfgmod.load_config().get("image", {}).get("tsf_explicit", False)):
            explicit = _adult_flags(session)[0]
    except Exception:
        explicit = False
    plan = tsf.build_stage_plan(
        stages_tbl, anchor=anchor,
        target_en=_tsf_target_en(session, female_w=1.0),
        hair_final_en=hair_final_en, keep_hair_color=keep_color,
        cloth_states=True, explicit=explicit,
        style=_stage_plan_style(session))
    session["stage_plan"] = plan
    session["stage_plan_meta"] = meta
    log.info("tsf stage plan built (%d stages, keep_color=%s, hair=%s)",
             len(plan), keep_color, hair_final_en or "(original)")


def _strip_short_hair(anchor: str) -> str:
    """转变链专用锚点清洗：剥离「短发」类固定设定——锚点带「黑色短发」会
    在每档转变提示词里与 tier 发长词打架（img2img 底图+文字双重短压，
    长发永远长不出来，1.7.15 实测发长卡死波波头）。发长改由
    _hair_words/tier 词按同化率驱动；发色/瞳色词保留不受影响。"""
    import re as _re
    return _re.sub(r"利落短发|短发|头发很短|短碎发|短发碎发|短头发", "",
                   anchor or "").strip()


def _band_anchor(progress: int) -> str:
    """档位**正面锚词**（1.7.27，链式 step1 用）：负面封杀只是不让模型跑偏，
    正面高权锚词才是「属于这一档」的确定指引——30% 一步全女化的实证修复
    （仅负面时模型按默认少女画；正面锚定「仍属男性/中性」后档位站得住）。
    用 A1111 双层括号权重（(word:1.25) 形式），置于提示词前段。"""
    p = max(0, min(100, int(progress)))
    if p < 35:
        return ("(soft boyish male face:1.25), (slim boyish build:1.2), "
                "(young man look:1.15)")
    if p < 55:
        return ("(androgynous face:1.3), (ambiguous gender:1.25), "
                "(petite slim neutral figure:1.2)")
    if p < 75:
        # 1.7.27 中段增强（实证：55-75 档用 ambiguous 词 → 模型仍画男孩，
        # 半程点 60% 该「女性初现」而非「模糊男」——60→90 才不突兀）
        return ("(soft feminine face:1.3), (feminine features emerging:1.25), "
                "(girl-like slim figure:1.25), (androgynous soft look:1.1)")
    return ("(young woman:1.25), (feminine figure:1.2), "
            "(elegant female look:1.15)")


def _hair_words(progress: int) -> str:
    """发长渐进词（按同化率档）——发区掩膜 pass 专用：发长必须由发区独立
    重绘驱动（整幅低噪不会长头发、身体掩膜不覆盖头部，1.7.15 实测定稿）。"""
    p = max(0, min(100, int(progress)))
    if p < 35:
        return ("hair growing slightly longer, soft rounded strands, "
                "slightly longer bob with soft edges")
    if p < 70:
        # 1.7.27 中段发长加权（实证：无权重时 60% 仍短发——半程不长
        # 发，「中性」感就立不住）
        return ("(hair lengthening:1.3), (shoulder-length soft hair:1.3), "
                "soft hair strands growing to shoulders, smooth flowing "
                "side hair, loose silky strands")
    # 满档：发长词带权重才能压过「短发底图偏置」（实测 90% 仍画波波头）
    return ("(very long flowing hair:1.35), (hair reaching chest:1.25), "
            "(elegant long strands:1.2), long back-length flowing hair, "
            "hair strands flowing over shoulders")


SOFT_TRANSFORM_EN = (
    "jawline softening, shoulders narrowing, lips fuller, eyelashes longer, "
    "cheekbones higher, hair flowing longer (same color), subtle makeup, "
    "smooth skin, waist narrowing, hips widening, feminine curves developing"
)
KEEP_IDENTITY_EN = (
    "same eye shape, same nose shape, same facial proportions, "
    "recognizable identical person"
)

# 转变过程强化词（1.6.85）：仅注入主角（TSF）立绘管线——
# 正面=渐进转变叙事（平滑过渡/中间态和性），负面=特征冲突/突变（防双性特征打架）
TRANSFORM_IMPROVE_EN = (
    "male-to-female transformation progressing, feminization process, "
    "features changing toward feminine, feminine silhouette developing, "
    "hair lengthening, waist slimming, hips widening, "
    "softening features, androgynous phase passing, "
    "seamless body evolution, consistent single person throughout the "
    "change, full body standing straight front view, whole figure visible "
    "in frame"
)
TRANSFORM_IMPROVE_NEG = (
    "sudden transformation, abrupt gender change, mismatched features, "
    "masculine face on female body, feminine face on male body, "
    "half-transformed glitch, clashing masculine and feminine traits, "
    "double gender features, sudden hair change, instant body change"
)
# 1.7.19：loli/child 类形象词**不再封杀**——奇幻/萌系设定下主角会变成
# 萝莉类角色（用户决策）；转变目标设定新增「娇小幼态」档位即可引导。
# 保留少量「婴儿态」压制词防画出真正婴儿（形体不设防=双胞胎丑变体）。
PROT_AGE_NEG = "baby, infant, newborn, toddler body"

TSF_TRANSFORM_SYSTEM = (
    "你是立绘生图提示词工程师。主角是一名成年男性，正在经历平滑的性别转变过程，"
    "变化必须「肉眼可见」且随同化率如实递进。"
    "请生成一段英文生图提示词（逗号分隔，25~50 词）：描述【以他初始的男性立绘为底，"
    "平滑演变成当前形态的画面】——按同化率写渐进特征（体型/发型/面容/气质），"
    "同化率越高女性特征越成形："
    "低（<35%）：仍是男性身形，仅下颌线渐柔、肩线收窄、头发变长到耳下；"
    "中（35~65%）：发型至肩、腰肢收窄、胯部渐宽、胸部开始发育，"
    "五官明显柔化（softening jawline、fuller lips、longer eyelashes、smooth skin）；"
    "高（>65%）：长发披肩、胸臀曲线成形（developed bust、slim waist、rounded hips、"
    "hourglass figure），面容女性化（delicate feminine features）；"
    "头发按进度逐步变长（短→耳下→及肩→披肩→及腰，same color）；"
    "渐变特征必须用柔和渐进词（softer / slightly / gradually），不要极端切换词；"
    "必须包含：same character、same eye color、same eye shape、same nose shape、"
    "same facial proportions、recognizable identical person、发色保持不变；"
    "不要写任何露骨或性暗示内容；不要输出解释和引号，只输出提示词正文。"
)


async def _tsf_transform_prompt(anchor: str, progress: int) -> str:
    """由后台 API 生成「按当前同化率从初始立绘演化」的英文生图提示词，
    让模型描写尽量合理（渐进特征如实对应同化率）；失败/mock 返回空。"""
    cache_k = f"trans|{anchor}|{int(progress) // 10}"
    if cache_k in _NOTE_EN_CACHE:
        return _NOTE_EN_CACHE[cache_k]
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return ""
    try:
        r = await llm.chat([
            {"role": "system", "content": TSF_TRANSFORM_SYSTEM},
            {"role": "user",
             "content": f"同化率：{int(progress)}%；初始外貌锚点：{anchor}"},
        ], temperature=0.4)
        r = (r or "").strip().replace("\n", " ")
        if len(r) >= 8:
            _NOTE_EN_CACHE[cache_k] = r
            return r
    except LLMError:
        pass
    return ""


async def _regen_prompt_suffix(note: str, char_desc: str = "") -> str:
    """重画需求增强：AI 结合角色设定与玩家修改要求，自主判断并生成
    一组完整的英文生图提示词补充（含关键点、细节与风格调整）；失败时中文原样。"""
    note = (note or "").strip()
    if not note:
        return ""
    cache_k = note + "|" + (char_desc or "")
    if cache_k in _NOTE_EN_CACHE:
        return _NOTE_EN_CACHE[cache_k]
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return f"（重画要求：{note}）"
    try:
        r = await llm.chat([
            {"role": "system",
             "content": ("你是专业的立绘生图提示词工程师。集合【角色设定】与【玩家的修改要求】，"
                        "自主判断应当如何生成最终立绘：可调整外貌/表情/气质关键词，"
                        "强化或弱化某些特征，补充细节与构成词；只输出一段英文提示词"
                        "（逗号分隔，30~60 个词），不要输出任何解释。")},
            {"role": "user",
             "content": f"【角色设定】{char_desc or '（未提供）'}\n【玩家的修改要求】{note[:100]}"},
        ], temperature=0.3)
        r = (r or "").strip().replace("\n", " ")
        if len(r) >= 8:
            _NOTE_EN_CACHE[cache_k] = r
            return r
    except LLMError:
        pass
    return f"（重画要求：{note}）"


def _face_refine_enabled(cfg: dict) -> bool:
    """脸部 img2img 一致性细化开关（config.json image.face_refine，默认开）。"""
    return bool(cfg.get("image", {}).get("face_refine", True))


def _find_ref_face(session: dict, name: str, exact_label: str = "",
                   allow_any: bool = True) -> bytes | None:
    """找同角色的脸部参考原图（raw）供 img2img：
    优先同标签（同阶段/同表情——保证参考的是「同一个身份」），其次 neutral，
    再退当前 ready 原图，最后任意历史。主角传 allow_any=False：阶段跃迁时
    禁止跨阶段取脸，避免「脸被旧阶段冻结」破坏平滑转变。"""

    def read(f):
        try:
            p = cfgmod.CACHE_DIR / f
            if p.is_file():
                return p.read_bytes()
        except OSError:
            pass
        return None

    hist = session.get("portrait_history") or []
    ports = (session.get("assets", {}).get("portraits") or {})
    # 1) 同标签历史原图
    if exact_label:
        for e in reversed(hist):
            if e.get("label") == exact_label and e.get("raw_file"):
                return read(e["raw_file"])
    # 2) neutral（表情基准脸）
    for e in reversed(hist):
        if (e.get("label") or "") == f"{name}·neutral" and e.get("raw_file"):
            return read(e["raw_file"])
    # 3) 当前 ready 原图（同标签优先，其次任意）
    for it in ports.values():
        if it.get("status") == "ready" and it.get("raw_file") \
                and exact_label and (it.get("label") or "") == exact_label:
            return read(it["raw_file"])
    if allow_any:
        for it in ports.values():
            if it.get("status") == "ready" and it.get("raw_file") \
                    and (it.get("label") or "").startswith(name + "·"):
                return read(it["raw_file"])
        for e in reversed(hist):
            if (e.get("label") or "").startswith(name + "·") and e.get("raw_file"):
                return read(e["raw_file"])
    return None


_OUTFIT_TERMS_CACHE: dict = {}


async def _outfit_terms(outfit_cn: str, char_desc: str = "") -> str:
    """换装需求先交给大语言模型：结合角色设定把中文换装要求转成英文生图
    关键词（材质/款式/配色/装饰，且不破坏制服感）；失败返回空串。"""
    outfit_cn = (outfit_cn or "").strip()
    if not outfit_cn:
        return ""
    cache_k = outfit_cn + "|" + (char_desc or "")
    if cache_k in _OUTFIT_TERMS_CACHE:
        return _OUTFIT_TERMS_CACHE[cache_k]
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return ""
    try:
        r = await llm.chat([
            {"role": "system",
             "content": ("你是专业的动漫角色服装关键词工程师。把玩家的中文换装要求"
                         "转成英文生图关键词（材质/款式/配色/装饰/系带等细节），"
                         "并确保与【角色设定】的世界观与场合相符（学园角色保持校服"
                         "体系，只换款式不破坏制服感）；只输出逗号分隔的 15~35 个英文"
                         "词，不要解释。")},
            {"role": "user",
             "content": f"【角色设定】{char_desc or '（未提供）'}\n【换装要求】{outfit_cn[:60]}"},
        ], temperature=0.3)
        r = (r or "").strip().replace("\n", " ")
        if len(r) >= 6:
            _OUTFIT_TERMS_CACHE[cache_k] = r
            return r
    except LLMError:
        pass
    return ""


def _find_live_cutout(session: dict, name: str, label: str) -> bytes | None:
    """**CG 参考底图专用**（1.7.40）：只取**当前 ready 资产里该标签的最新一
    张**——主角阶段变化后的当前立绘（如阶段4 最新的 026）；无匹配再回退
    _find_prev_cutout（归档/旧版兜底）。
    不用 _find_prev_cutout 的原因：它「历史归档优先」是给换装/渐变链设计
    的——CG 参考底图据此会拿到归档里的**最早阶段图**，后来立绘的变化
    （重画/新阶段）永远不被考虑（用户实测反馈）。"""
    ports = (session.get("assets", {}).get("portraits") or {})
    best = None
    best_ts = -1.0
    for it in ports.values():
        if it.get("status") != "ready" or not it.get("file"):
            continue
        if (it.get("label") or "") != label:
            continue
        ts = it.get("finished_at", 0) or 0
        if ts >= best_ts:
            best_ts, best = ts, it
    if best:
        p = cfgmod.CACHE_DIR / best["file"]
        if p.is_file():
            return p.read_bytes()
    return _find_prev_cutout(session, name, label)


def _find_prev_cutout(session: dict, name: str, stage_label: str = "") -> bytes | None:
    """换装/渐变图生图的底图：该角色的历史立绘成品（透明底 PNG 字节）。
    重画命令会先把旧版归档（portrait_history），因此先查归档再查当前 ready；
    同阶段标签优先，其次任意同角色最新一张；无则返回 None（走全量生成）。"""
    hist = session.get("portrait_history") or []
    # 1) 历史归档（最新优先）
    for e in reversed(hist):
        lbl = e.get("label", "") or ""
        if not e.get("file"):
            continue
        if stage_label:
            ok = lbl == stage_label
        else:
            ok = lbl.startswith(name + "·")
        if ok:
            p = cfgmod.CACHE_DIR / e["file"]
            if p.is_file():
                return p.read_bytes()
    # 2) 当前 ready 资产（同标签优先，其次最新）
    ports = (session.get("assets", {}).get("portraits") or {})
    cand = None
    best_ts = -1.0
    for it in ports.values():
        if it.get("status") != "ready" or not it.get("file"):
            continue
        label = it.get("label", "") or ""
        if not label.startswith(name + "·"):
            continue
        if stage_label and label == stage_label:
            p = cfgmod.CACHE_DIR / it["file"]
            if p.is_file():
                return p.read_bytes()
        ts = it.get("finished_at", 0) or 0
        if ts >= best_ts:
            best_ts, cand = ts, it
    if cand:
        p = cfgmod.CACHE_DIR / cand["file"]
        if p.is_file():
            return p.read_bytes()
    return None


def _tsf_stage_matcher(client, session: dict, name: str, stage_idx: int,
                       prev_b: bytes | None, nude_ok: bool = False,
                       keep_hair_color: bool = True):
    """阶段跃迁全量的「转变判定器」（1.7.32 转变技能内建核心）：
    成品经 A1111 interrogate 打标 → 与段特征词集（tsf.stage_feat）比对打分
    + 段间同人色差软分——_sd_retry 多候选生成后自动挑「转变正确 + 着装
    符合（nude_ok=False 时裸体大罚）+ 同人」的最优种子。interrogate 失败/
    非 sdwebui 线路返回 None（由 _sd_retry 降级为旧行为：首张合格即收，
    绝不因判定器不出图）。"""
    from . import tsf as _tsf_mod
    from .image import (tsf_stage_score, _rough_mean_color, _color_dist,
                        _hair_mean_color, _hair_shift_penalty)
    if client.provider != "sdwebui":
        return None
    stages = _tsf_mod.stages_for(session)
    feat = _tsf_mod.stage_feat(stage_idx, stages)
    ref = _rough_mean_color(prev_b) if prev_b else None
    # 段间「发色统一」判定位：上一段成品的发区主色（1.7.32——
    # 全量独立种子的发色漂移老顽疾：漂色候选在挑优时被重罚淘汰）
    ref_hair = _hair_mean_color(prev_b) if prev_b else None

    async def matcher(raw: bytes) -> float | None:
        import base64 as _b64
        # 1.7.47 优化插件优先：wd14-tagger 打标（booru 标签集与本判定
        # 词集天然匹配，准确性高于 BLIP/CLIP 整句；未装/失败自动回退）
        cap = ""
        if await client._has_tagger():
            cap = await client._tagger_interrogate(_b64.b64encode(raw).decode())
        if not cap or "<error>" in cap:
            # 回退 A1111 interrogate（model="clip"＝shared.interrogator 完整
            # 链路：BLIP 出句 + CLIP 类别筛序；两个标注模型首次自动下载一次）
            cap = await client._interrogate(_b64.b64encode(raw).decode(),
                                            model="clip")
        # 打标失败（caption 空）→ 返回 None：_sd_retry 收到 None 即降级
        # 为「首张合格即收」（判定器不可用=旧行为，绝不断出图，
        # 也不产生额外候选/额外打标——interrogate 高频是 SD 卡死元凶）
        if not cap or "<error>" in cap:
            return None
        score = tsf_stage_score(cap, feat, nude_ok=nude_ok)
        if ref is not None:
            m = _rough_mean_color(raw)
            if m is not None:
                # 同人软分：主色差小（发色/瞳色/服装同锚）加分，
                # 差越大（换人/漂色）分越低——只做排名权重，不做硬拒
                score += max(0.0, 1.0 - _color_dist(ref, m) / 150.0) * 2.0
        if ref_hair is not None and keep_hair_color:
            hm = _hair_mean_color(raw)
            if hm is not None:
                # 发色统一硬罚（1.7.32）：同色系小差不罚，漂色重罚
                score -= _hair_shift_penalty(ref_hair, hm)
        return score

    return matcher


def _start_protagonist_portrait(session: dict, nonce: int = 0,
                                outfit: str = "", note: str = "",
                                use_initial: bool = False,
                                force_fullgen: bool = False,
                                stage_jump: bool = False,
                                stage_override: int | None = None,
                                progress_override: int | None = None,
                                label_override: str = "",
                                key_tag: str = "") -> str:  # noqa: E501
    """生成主角当前阶段（或换装）的立绘。outfit 非空 = 换装差分；
    note 非空 = 玩家的重画需求（翻译后注入提示词）。
    use_initial=True：以「初始（阶段1）立绘」为底图，按当前同化率重建当前
    阶段——提示词由后台 API 按同化率生成（勾选「参考目前同化率重新生成」）。
    force_fullgen（1.7.18）：阶段四（81-100%）首次跃迁强制走**全量生成**——
    实测链式/初始重建（img2img 0.36~0.5）都变不出完整女性（男性底图质量
    压不住），全量+放宽负面+女性词=「从男性变为女性」的最终形态（用户诉求）。

    1.8.0「六段演变工坊」扩展（全部默认 None/空 = 旧行为逐字不变）：
    `stage_override` / `progress_override` 让同一条游戏内引擎按指定段与
    同化率生成（演廊预演），`label_override` / `key_tag` 把它与真实阶段
    资产区分开（真实资产仍是「角色·阶段N」）。返回资产 key，调用方可等待。"""
    use_initial = bool(use_initial)
    cfg = cfgmod.load_config()
    game_cfg = cfg["game"]
    client = ImageClient(cfg)
    prot = session["protagonist"]
    stages_tbl = tsf.stages_for(session)
    _ensure_stage_plan(session)  # 1.7.43：预写计划缺失/过期时重建（惰性）
    if stage_override is None:
        stage_idx = tsf.stage_of(prot["stats"]["progress"], stages_tbl)
    else:
        stage_idx = max(0, min(len(stages_tbl) - 1, int(stage_override)))
    progress = (int(progress_override) if progress_override is not None
                else int(prot["stats"]["progress"]))
    progress = max(0, min(100, progress))
    pband = int(progress // 10)  # 进度分带：平滑提示词按 10% 重画
    name, anchor = prot["name"], prot.get("anchor", "")
    outfit_s = (outfit or "").strip()
    note_s = (note or "").strip()
    mode_tag = "mock" if client.mock else "real"
    key = _skey(session, "tsf-portrait", mode_tag, PROMPT_VERSION, _params_tag(cfg),
                *pixel_mod.key_parts(cfg.get("image")),
                *style_key_part(cfg.get("image")),     # t61：画风进键（默认空元组 ⇒ 键逐位不变）
                name, anchor, stage_idx, pband, outfit_s[:60], note_s[:60],
                "initref" if use_initial else "", key_tag, nonce)
    label = (label_override
             or (f"{name}·阶段{stage_idx + 1}" if not outfit_s
                 else f"{name}·{outfit_s}"))
    fname = _reserve_file_name(session, session.get("prot_handle") or name)  # 主角固定文档名(序号)
    style = _img_style(session)

    async def factory():
        # 1.7.48 LLM 渐变规划器：模板兜底先行，LLM 可用时按角色情况升级
        # 本局渐变计划（同局缓存，仅首次多一次 API 调用）
        await _ensure_stage_plan_llm(session)
        note_en = await _regen_prompt_suffix(note_s, anchor)
        req = (f"，{note_en}" if note_en else "")
        # progress 已在函数入口按 progress_override 归一（1.8.0 演廊用）
        # 1.7.16：主角「转变目标设定」—— 目标词注入 + 放宽负面（不封杀
        # 女性特征）+ 防变化词已从 TRANSFORM_IMPROVE_EN/SOFT_TRANSFORM_EN
        # 中移除；NPC 路径零影响
        fw = tsf.stage_gender_weights(stage_idx, stages_tbl)[2]
        target_en = _tsf_target_en(session, female_w=fw)
        keep_color = bool((session.get("tsf_target") or {})
                          .get("keep_hair_color", True))
        # 1.7.32 电影风格词（cinematic film still/dramatic lighting/film
        # grain/masterpiece）× 转变目标词组合会诱导该 realskin 模型**系统性
        # 崩溃**（跨 5+ 种子簇全噪点废图；无电影词同种子 100% 成功——
        # 实机 A/B/C/D 四组对照定案）。目标设定路径只用基础风格。
        style_us = style
        if target_en:
            from .image import CINEMATIC_SUFFIX as _cin
            style_us = (style
                        .replace(_cin, "")
                        .replace("，cinematic film still, cinematic lighting, "
                                 "dramatic light and shadow, film grain", ""))
        # 1.7.35 本局生效制服（玩家设定 > 当前场景自动映射 > 无）
        unif_eff = _uniform_for_portrait(session, outfit_s)
        # 1.7.43 预写计划优先（「写出六步/十步变化」能力）：开局已按六条
        # 渐变状态机（发长/发色溶解/脸型/表情/服装重组/性征）写好该段词，
        # 每段只推进 1-2 个主维度；目标词已按段内女性权重并入计划
        plan_entry = tsf.plan_stage_entry(session.get("stage_plan"),
                                          progress, stages_tbl)
        if plan_entry and not outfit_s:
            base_prompt = (plan_entry["prompt"] + "，" + TRANSFORM_IMPROVE_EN)
        else:
            # 1.7.32 阶段制提示词：按**段特征锚点**取词（不再被 0/25/50/75/100
            # 五档插值稀释——六段 60-80「绽放」旧版落在 50% 中性档 = 变化不明显）
            base_prompt = (tsf.stage_portrait_prompt(anchor, progress, target_en,
                                                     stages_tbl)
                           + "，" + TRANSFORM_IMPROVE_EN)
            # 段间发色统一（1.7.32）：keep_hair_color 时全量路径也上发色锁——
            # 全量独立种子会把黑发漂成银白/孔雀绿（实机 s4 深灰漂移实锤）；
            # 「转变目标设定」取消发色保持时由目标词驱动演化（不加锁）
            if keep_color:
                base_prompt += ("，(same hair color:1.2), (keep original hair "
                                "color:1.15), same hair color as before")
        # 1.7.32 转变判定器（仅全量、非换装、sdwebui 线路）：
        # 内部自动挑「转变正确 + 同人（含发色统一）」的种子；
        # 上一段/同段旧图作发区色差参照
        stage_matcher = None
        # 1.7.32 判定器仅用于「阶段跃迁」（stage_jump）——目标设定重画/重画
        # 需求/换装回退全量**不用**（同 SD 同种子同 prompt 下游戏任务失败、
        # 无判定器直调 100% 成功；判定器只服务该局六段验证过的跃迁路径）
        if (not outfit_s and stage_jump and not client.mock
                and client.provider == "sdwebui"
                and cfg["image"].get("tsf_stage_matcher", True)
                and not cfg["image"].get("eco_mode")):  # 1.8.0 节能：跃迁也不打标
            prev = (_find_prev_cutout(session, name,
                                      f"{name}·阶段{stage_idx + 1}")
                    or _find_prev_cutout(session, name,
                                         f"{name}·阶段{stage_idx}"))
            # 重画需求明确「裸体/内衣」时允许裸体（判定器不着装罚分）
            nude_ok = any(k in note_s for k in (
                "裸体", "一丝不挂", "未着寸缕", "全裸", "内衣", "内裤",
                "nude", "naked", "underwear", "lingerie"))
            stage_matcher = _tsf_stage_matcher(client, session, name,
                                               stage_idx, prev,
                                               nude_ok=nude_ok,
                                               keep_hair_color=keep_color)
        if client.mock:
            return await client.generate_portrait(
                name, base_prompt
                + (f"（服装变化：{outfit_s}）" if outfit_s else "")
                + req,
                f"阶段{stage_idx + 1}",
                f"{name}的{'换装' if outfit_s else '阶段'}立绘",
                style_us,
                extra_negative=(tsf.stage_negative_relaxed_stage(stage_idx,
                                                                 stages_tbl)
                                + "，" + TRANSFORM_IMPROVE_NEG
                                + "，" + PROT_AGE_NEG + "，no camera, no phone, no lens"
                                + ("" if _adult_flags(session)[0]
                                   else "，nude, naked, underwear only, "
                                        "lingerie, topless")),
                seed=_seed_for(session, f"prot|{name}|{stage_idx}"),
                strict=not bool(outfit_s),
                uniform_en=unif_eff[0],
                uniform_neg=unif_eff[1],
            )
        # 「参考目前同化率重新生成」：以主角初始（阶段1）立绘为底图整幅重绘，
        # 提示词由后台 API 按同化率描写渐进转变；重绘度按教学取 0.40→0.60
        # （保留身份的最佳区间 0.35~0.5 / 中强度转变 0.4~0.6，随同化率递增）
        if use_initial and not outfit_s and client.provider == "sdwebui":
            initial = _find_prev_cutout(session, name, f"{name}·阶段1")
            if initial:
                try:
                    trans_en = await _tsf_transform_prompt(anchor, progress)
                    keep_en = append_appearance_en(anchor)
                    soft_en = (f"，{SOFT_TRANSFORM_EN}"
                               if progress >= 20 else "")
                    base_c = (tsf.portrait_prompt(_strip_short_hair(anchor),
                                                  progress, target_en)
                              + "，" + TRANSFORM_IMPROVE_EN)
                    trans_prompt = (_tsf_guard_front(anchor, keep_color)
                                    + ", " + _band_anchor(progress)
                                    + ", "
                                    + base_c
                                    + (f"，{trans_en}" if trans_en else "")
                                    + (f"，{keep_en}" if keep_en else "")
                                    + (f"，{KEEP_IDENTITY_EN}" if keep_en else "")
                                    + soft_en
                                    + (", same hair color, same eye color, "
                                       "keep original hair and eye colors"))
                    denoise = min(0.36, 0.30 + progress / 500.0)
                    body_dn = min(0.50, 0.36 + progress / 250.0)
                    out = await _refine_with_retry(
                        client, initial, trans_prompt,
                        _seed_for(session, f"initref|{name}|{stage_idx}|{pband}"),
                        denoise, body_dn, _hair_words(progress),
                        tsf.stage_negative_relaxed(progress)
                        + "，" + TRANSFORM_IMPROVE_NEG)
                    # 脸部低噪（0.22）渐进细化：结构不乱、不出现「奇怪脸」，
                    # 女性化特征柔和演化（由 keep/soft 词引导）
                    out = await client.refine_face(
                        out, initial,
                        f"{anchor}，{KEEP_IDENTITY_EN}，"
                        "softly refined feminine features, young adult woman, "
                        "mature elegant young adult features, "
                        "same facial structure, gentle expression change",
                        _seed_for(session, f"initface|{name}|{stage_idx}") + 7,
                        denoise=0.22)
                    raw_name = f"{_session_dir_name(session)}/raw/{key}_raw.png"
                    (cfgmod.CACHE_DIR / raw_name).write_bytes(out)
                    log.info("tsf initial-reference rebuild ok (denoise %.2f, "
                             "body %.2f)", denoise, body_dn)
                    return out, raw_name
                except ImageGenError as e:
                    log.warning("tsf initial-ref rebuild failed (%s), "
                                "fallback full gen", e)
        if outfit_s:
            # 换装首选「图生图限定服装区域重绘」：底图=同阶段当前立绘，脸不变
            outfit_en = await _outfit_terms(outfit_s, anchor)
            # 非制服类换装显式摘制服锁（见 _uniform_for_portrait 注释）
            unif_en = _uniform_text_for(session, outfit_s)
            inp_prompt = (f"{base_prompt}，{style_us}，换装为：{outfit_s}"
                          + (f"，{outfit_en}" if outfit_en else "")
                          + (f"，{unif_en}" if unif_en else "")
                          + (", fully dressed, outfit covering the body, "
                             "detailed clothing texture, crisp fabric folds, "
                             "light colors, white blouse accent"))
            prev = _find_prev_cutout(session, name,
                                     f"{name}·阶段{stage_idx + 1}")
            if prev and cfg["image"].get("outfit_inpaint", True):
                try:
                    out = await client.refine_outfit(
                        prev, inp_prompt,
                        _seed_for(session, f"fit|prot|{name}|{stage_idx}|{outfit_s}"),
                        nude_mode=_outfit_nude_mode(session, outfit_s))
                    # B-05/M-02：复检丢线程池；原来是「先调一次判断、
                    # 再调一次取文案」（两次整图质检），现在算一次复用
                    _advise = await asyncio.to_thread(
                        _final_check_issue, out,
                        nude_ok=_nude_hint_text(outfit_s))
                    if _advise:
                        log.warning("outfit inpaint result advisory: %s", _advise)
                    raw_name = f"{_session_dir_name(session)}/raw/{key}_raw.png"
                    (cfgmod.CACHE_DIR / raw_name).write_bytes(out)
                    return out, raw_name
                except ImageGenError as e:
                    log.warning("prot outfit inpaint failed (%s), fallback full gen", e)
        # TSF 渐变图生图：站在「旧版自己」肩膀上平滑演化——
        # 同阶段链 0.30；跨阶段整幅降到 0.38（低噪保脸部结构，不再「整幅大
        # 改造成奇怪脸」）+ 强制身体掩膜 pass（身材缓慢渐变，与同化率挂钩）
        # + 脸部 0.22 低噪渐进细化；提示词追加「外观色锁定 + 身份保真 +
        # 柔和渐变词」标签，防止噪声下发色/瞳色漂移
        if (not outfit_s and not note_s and not client.mock
                and cfg["image"].get("outfit_inpaint", True)
                and client.provider == "sdwebui"
                # 1.7.16：设定过「转变目标」的中断链式（链式对「换形态」只
                # 微调——目标=长发/丰盈曲线/改发色必须走全量：目标词+放宽
                # 负面才能按目标重画，实测链式卡在短发）
                and not target_en and not force_fullgen):
            prev = _find_prev_cutout(session, name,
                                     f"{name}·阶段{stage_idx + 1}")
            # 1.7.13 定稿：同阶段链 0.32 / 跨阶段 0.38。0.42 实测发色漂移+
            # 服装流失（NSFW 模型高重绘度剥衣服）；0.30/0.38 时可见性由
            # 前置服装/发色守护词 + 强身体通道（0.40~0.55）驱动——
            # 实测 25→50 链式：头发明显变长、身形渐柔、衣裤保持、无伪影
            # 1.7.17 回落放缓：0.30 / 0.35 + 身体通道 0.36~0.50（用户反馈
            # 阶段间跳跃感强、变化突兀——放缓每步幅度、多步累积同样可见）
            denoise = 0.30
            if not prev:
                prev = _find_prev_cutout(session, name,
                                         f"{name}·阶段{stage_idx}")
                denoise = 0.35
            if stage_jump and prev:
                # 1.7.30 阶段跃迁链式：整幅 0.40（大变化放开——每段跨
                # 20-25% 需足够重绘才有「新形态」观感；0.45 实测触发该模型
                # 溢出家族（晶簇/横纹/绿噪全链化），0.35 以下段位感不足；
                # 0.40=变化足+崩率低；发色/服装由 guard+band 锚词+色锁保障）
                denoise = 0.40
            if prev:
                try:
                    keep_en = append_appearance_en(anchor)
                    soft_en = (f"，{SOFT_TRANSFORM_EN}"
                               if progress >= 20 else "")
                    base_c = (tsf.portrait_prompt(_strip_short_hair(anchor),
                                                  progress, target_en)
                              + "，" + TRANSFORM_IMPROVE_EN)
                    trans_prompt = (_tsf_guard_front(anchor, keep_color)
                                    + ", " + _band_anchor(progress)
                                    + ", "
                                    + base_c + req
                                    + (f"，{keep_en}" if keep_en else "")
                                    + (f"，{KEEP_IDENTITY_EN}" if keep_en else "")
                                    + soft_en
                                    + (", same hair color, same eye color, "
                                       "keep original hair and eye colors")
                                    # 1.7.30 阶段跃迁（0.45 高重绘）额外高权
                                    # 发色/瞳色锁——防 0.42 时代的发色漂移
                                    + (", (same hair color:1.3), "
                                       "(same eye color:1.25), "
                                       "(keep original hair color:1.2)"
                                       if stage_jump else ""))
                    body_dn = min(0.50, 0.36 + progress / 250.0)
                    # 1.7.30 链式「种子轮换+组合质检」：不合格自动换种子
                    # 重试（软件内部行为），全败上层回退——游戏内自动达到
                    # 「像精选种子般精美」
                    out = await _refine_with_retry(
                        client, prev, trans_prompt,
                        _seed_for(session, f"trans|{name}|{stage_idx}|{pband}"),
                        denoise, body_dn, _hair_words(progress),
                        tsf.stage_negative_relaxed(progress)
                        + "，" + TRANSFORM_IMPROVE_NEG)
                    out = await client.refine_face(
                        out, prev,
                        f"{anchor}，{KEEP_IDENTITY_EN}，"
                        "softly refined feminine features, young adult woman, "
                        "mature elegant young adult features, "
                        "same facial structure, gentle expression change",
                        _seed_for(session, f"transface|{name}|{stage_idx}") + 7,
                        denoise=0.22)
                    raw_name = f"{_session_dir_name(session)}/raw/{key}_raw.png"
                    (cfgmod.CACHE_DIR / raw_name).write_bytes(out)
                    log.info("tsf transition refine ok (denoise %.2f, body %.2f)",
                             denoise, body_dn)
                    return out, raw_name
                except ImageGenError as e:
                    log.warning("tsf transition failed (%s), fallback full gen", e)
        data = await client.generate_portrait(
            name, base_prompt
            + (f"（服装变化：{outfit_s}）" if outfit_s else "")
            + req,
            f"阶段{stage_idx + 1}",
            f"{name}的{'换装' if outfit_s else '阶段'}立绘",
            style_us,
            extra_negative=(tsf.stage_negative_relaxed_stage(stage_idx,
                                                             stages_tbl)
                            + "，" + TRANSFORM_IMPROVE_NEG
                            + "，" + PROT_AGE_NEG + "，no camera, no phone, no lens"
                            + ("" if _adult_flags(session)[0]
                               else "，nude, naked, underwear only, "
                                    "lingerie, topless")),
            seed=_seed_for(session, f"prot|{name}|{stage_idx}"),
            strict=not bool(outfit_s),
            # 1.7.35 制服：玩家本局设定 > 当前场景自动映射（学园/神社…）
            uniform_en=unif_eff[0],
            uniform_neg=unif_eff[1],
            # 1.7.32 转变判定器：多候选自动挑「转变正确 + 同人」的种子
            matcher=stage_matcher,
            # 主角 TSF 渐变：anchor 可含「男性/女性阶段发色」描述，全量生成
            # 放行发色演化；transition refine 阶段仍带锁定（变身后不再漂）
            identity_lock=False,
        )
        # 保存未抠图原始图，供「重新抠图 / 还原原图」使用
        raw_name = f"{_session_dir_name(session)}/raw/{key}_raw.png"
        (cfgmod.CACHE_DIR / raw_name).write_bytes(data)
        out = await process_portrait_async(data, _cutout_mode())
        issue = await asyncio.to_thread(
            _final_check_issue, out,
            nude_ok=_nude_hint_text(outfit_s, note_s))
        # 演示/mock 图只是占位噪声，不能拿真实成品质检去毙掉它，否则
        # 无 API 的首次体验会出现「主角有图、NPC 全是破图」的假故障。
        if issue and not outfit_s and not client.mock:
            raise ImageGenError(f"成品完整性复检未通过（{issue}）")
        # 脸部 img2img 一致性：仅同阶段重画/换装有可参考的原图时执行（禁跨阶段取脸）
        if not issue and _face_refine_enabled(cfg):
            ref = _find_ref_face(session, name,
                                 f"{name}·阶段{stage_idx + 1}", allow_any=False)
            if ref:
                out = await client.refine_face(
                    out, ref, base_prompt + req,
                    _seed_for(session, f"face|prot|{name}|{stage_idx}"))
        return out, raw_name

    _queue_asset(session, "portraits", key, label, factory,
                 session.get("prot_handle") or name, fname)
    return key


def _stage_tour_label(session: dict, idx: int) -> str:
    """六段演变预演的统一标签（与真实「阶段N」资产严格区分）。"""
    return f"{session['protagonist']['name']}·演变{int(idx) + 1}"


def _stage_tour_progress(stages: list[dict], idx: int) -> int:
    """取该段的「代表同化率」= 段区间中点（末段 100）。

    为什么不用段下界：`stage_of` 的边界是「progress <= max」——
    六段一 0-20 的 20 会被判回第一段。中点既保证落段正确，也让该段
    的女性权重/特征词处于段内代表值，预览图更像玩家真正会看到的样子。
    """
    try:
        s = stages[int(idx)]
    except (IndexError, TypeError):
        return 0
    lo = int(s.get("min", 0))
    hi = int(s.get("max", lo))
    return 100 if lo >= 100 or hi >= 100 else min(100, max(lo, (lo + hi) // 2))


def _stage_tour_entry(session: dict, idx: int):
    """会话资产里查找某段的演廊条目 → (key, item|None)。"""
    label = _stage_tour_label(session, idx)
    for k, item in (session.get("assets", {}).get("portraits") or {}).items():
        if item.get("label") == label:
            return k, item
    return "", None


def stage_tour_view(sid: str) -> dict:
    """「六段演变工坊」视图：段表 + 预演资产状态 + 任务进度（只读）。"""
    session = get_session(sid)
    _ensure_stage_plan(session)
    stages = tsf.stages_for(session)
    prot = session["protagonist"]
    progress = int(prot["stats"]["progress"])
    cur = tsf.stage_of(progress, stages)
    plan = session.get("stage_plan") or []
    out_stages = []
    for i, s in enumerate(stages):
        key, item = _stage_tour_entry(session, i)
        entry = {
            "index": i,
            "name": s.get("name", f"第{i + 1}段"),
            "identity": s.get("identity", ""),
            "desc": s.get("desc", ""),
            "min": int(s.get("min", 0)),
            "max": int(s.get("max", 0)),
            "progress": _stage_tour_progress(stages, i),
            "current": i == cur,
            "plan_prompt": (plan[i].get("prompt", "") if i < len(plan) else ""),
        }
        if item:
            entry["preview"] = {
                "key": key,
                "status": item.get("status", ""),
                "file": item.get("file", ""),
                "error": item.get("error", ""),
                "raw_file": item.get("raw_file", ""),
                "label": item.get("label", ""),
            }
        out_stages.append(entry)
    tour = dict(session.get("stage_tour") or {})
    running = sid in _STAGE_TOUR_TASKS and not _STAGE_TOUR_TASKS[sid].done()
    if tour.get("status") == "running" and not running:
        # 服务重启/任务丢失：不要永远显示「生成中」
        tour["status"] = "interrupted"
    cfg = cfgmod.load_config()
    return {
        "sid": sid,
        "version": PROMPT_VERSION,
        "protagonist": prot["name"],
        "progress": progress,
        "stage": cur,
        "stage_count": len(stages),
        "stages": out_stages,
        "tour": {
            "status": tour.get("status", "idle"),
            "current": int(tour.get("current", -1)),
            "total": int(tour.get("total", 0)),
            "indices": tour.get("indices") or [],
            "error": tour.get("error", ""),
            "started": tour.get("started", 0),
            "updated": tour.get("updated", 0),
        },
        "eco": bool(cfg["image"].get("eco_mode")),
        "gen_cache": bool(cfg["image"].get("gen_cache", True)),
    }


async def _wait_stage_asset(session: dict, key: str, timeout: float = 900.0) -> dict:
    """等待一个演廊资产落定（ready/error）；error 留 20s 给自动重画翻转。"""
    t0 = time.time()
    saw_error = 0.0
    while time.time() - t0 < timeout:
        item = (session.get("assets", {}).get("portraits") or {}).get(key)
        if item is None:
            return {"status": "missing"}
        st = item.get("status")
        if st == "ready":
            return item
        if st == "error":
            if not saw_error:
                saw_error = time.time()
            elif time.time() - saw_error > 20:
                return item
        await asyncio.sleep(0.4)
    return {"status": "timeout"}


async def run_stage_tour(sid: str, indices: list[int] | None = None,
                         force: bool = False) -> dict:
    """用游戏内同一条立绘引擎，按顺序生成「六段性转变」全程。

    - 串行逐段（SD 串行闸门天然生效），每段全量生成（跨性别只能全量）；
    - 已有且非 force 的段直接复用（缓存/重复点击零消耗）；
    - 可取消：任务取消后已完成的段保留，未开始的段不再消耗算力。
    """
    session = get_session(sid)
    if sid in _STAGE_TOUR_TASKS and not _STAGE_TOUR_TASKS[sid].done():
        return {"started": False, "running": True,
                "tour": stage_tour_view(sid)["tour"]}
    stages = tsf.stages_for(session)
    n = len(stages)
    idxs = [int(i) for i in (indices or list(range(n)))
            if 0 <= int(i) < n]
    if not idxs:
        idxs = list(range(n))
    tour = session.setdefault("stage_tour", {})
    tour.update({"status": "running", "current": -1, "total": len(idxs),
                 "indices": idxs, "error": "", "_pending": True,
                 "started": time.time(), "updated": time.time()})
    _perf(session)["tour_runs"] += 1
    persist(session)

    async def runner():
        try:
            for pos, i in enumerate(idxs):
                tour["current"] = i
                tour["updated"] = time.time()
                persist(session)
                key, item = _stage_tour_entry(session, i)
                if item and item.get("status") == "ready" and not force:
                    continue
                if item and item.get("status") == "pending":
                    await _wait_stage_asset(session, key)
                    continue
                prog = _stage_tour_progress(stages, i)
                key2 = _start_protagonist_portrait(
                    session, nonce=int(time.time()) if force else 0,
                    stage_override=i, progress_override=prog,
                    force_fullgen=True,
                    label_override=_stage_tour_label(session, i),
                    key_tag="stage-tour")
                res = await _wait_stage_asset(session, key2)
                if res.get("status") != "ready":
                    tour["status"] = "error"
                    tour["error"] = (res.get("error") or
                                     f"第 {i + 1} 段生成失败（{res.get('status')}）")
                    persist(session)
                    return
            tour["status"] = "done"
            tour["current"] = idxs[-1]
            tour["updated"] = time.time()
            tour.pop("_pending", None)
            persist(session)
            log.info("stage tour done: %s (%d stages)", sid, len(idxs))
        except asyncio.CancelledError:
            tour["status"] = "cancelled"
            tour["updated"] = time.time()
            try:
                persist(session)
            except Exception:
                pass
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("stage tour failed: %s", e)
            tour["status"] = "error"
            tour["error"] = str(e)[:200]
            tour["updated"] = time.time()
            persist(session)
        finally:
            _STAGE_TOUR_TASKS.pop(sid, None)

    task = asyncio.create_task(runner())
    _STAGE_TOUR_TASKS[sid] = task
    return {"started": True, "running": True,
            "tour": {"status": "running", "current": -1,
                     "total": len(idxs), "indices": idxs}}


def cancel_stage_tour(sid: str) -> dict:
    """取消正在进行的六段演变预演（已生成段保留）。"""
    task = _STAGE_TOUR_TASKS.get(sid)
    if task and not task.done():
        task.cancel()
        return {"cancelled": True}
    try:
        session = get_session(sid)
        tour = session.setdefault("stage_tour", {})
        if tour.get("status") == "running":
            tour["status"] = "cancelled"
            persist(session)
    except Exception:
        pass
    return {"cancelled": False}


def perf_view(sid: str) -> dict:
    """本局/本次运行的「性能与消耗」快照（只读，供 UI 展示与调参依据）。"""
    session = get_session(sid)
    p = _perf(session)
    cfg = cfgmod.load_config()
    from . import image as image_mod
    runtime = image_mod.gen_stats_snapshot(reset=False)
    return {
        "session": p,
        "runtime": runtime,
        "avg_seconds": round(p["image_seconds"] / max(1, p["images"]), 1),
        "eco": bool(cfg["image"].get("eco_mode")),
        "gen_cache": bool(cfg["image"].get("gen_cache", True)),
        "gen_cache_mb": int(cfg["image"].get("gen_cache_mb", 384) or 384),
        "sd": image_mod.sd_state(),
    }


def _start_npc_portrait(session: dict, character: dict, nonce: int = 0,
                        outfit: str = "", note: str = "", file_base: str = "",
                        emotion_only: str = "") -> None:
    """生成一个 NPC 的立绘。outfit 非空 = 换装差分；note 非空 = 重画需求；
    emotion_only 非空 = 只重画指定表情（立绘历程「重绘差分」用）。"""
    cfg = cfgmod.load_config()
    game_cfg = cfg["game"]
    client = ImageClient(cfg)
    outfit_s = (outfit or "").strip()
    note_s = (note or "").strip()
    # 三维差分（1.6.77）：角色 × 衣着 × 表情——换装后按全套表情差分生成
    # （换装 neutral 走限定区域重绘，其余表情以它为底「只变表情」），
    # 每套衣着独立调取，表情切换不再跳回默认衣的图。
    emotions = ([emotion_only] if emotion_only
                else (list(session["emotions"]) if outfit_s
                      else session["emotions"]))
    style = _img_style(session)
    extra_neg = _img_extra_neg(session)
    # 非 R18 档强制保守着装：连内衣/疑似裸体一并封杀（与内容分级关联）
    if not _adult_flags(session)[0]:
        extra_neg = extra_neg + "，nude, naked, underwear only, lingerie, " \
                              "topless, breast exposure, panties, bra"
    mode_tag = "mock" if client.mock else "real"
    for emotion in emotions:
        name, appearance = character["name"], character.get("appearance", "")
        outfit_part = f"（服装：{outfit_s}）" if outfit_s else ""
        key = _skey(session, "portrait", mode_tag, PROMPT_VERSION, _params_tag(cfg),
                    *pixel_mod.key_parts(cfg.get("image")),
                    *style_key_part(cfg.get("image")),   # t61：画风进键（默认零影响）
                    name, appearance, emotion, outfit_s[:60], note_s[:60],
                    nonce)
        label = (f"{name}·{outfit_s}·{emotion}" if outfit_s
                 else f"{name}·{emotion}")
        fname = _reserve_file_name(session, file_base or name)

        async def factory(client=client, name=name, appearance=appearance,
                          emotion=emotion, outfit_s=outfit_s,
                          extra_neg=extra_neg, style=style,
                          raw_name=f"{_session_dir_name(session)}/raw/{key}_raw.png"):
            note_en = await _regen_prompt_suffix(note_s, appearance)
            req = (f"，{note_en}" if note_en else "")
            emotion_cn = game_cfg.get("emotion_cn", {}).get(emotion, emotion)
            # 1.7.35 本局生效制服（玩家设定 > 当前场景自动映射 > 无）
            unif_eff = _uniform_for_portrait(session, outfit_s)
            if client.mock:
                data = await client.generate_portrait(
                    name, appearance + outfit_part + req, emotion,
                    emotion_cn, style, extra_negative=extra_neg,
                    seed=_seed_for(session, f"npc|{name}"),
                    strict=not bool(outfit_s),
                    uniform_en=unif_eff[0],
                    uniform_neg=unif_eff[1])
                return data
            # 表情差分「只变表情」图生图（1.6.74+）：以同衣着 neutral 立绘为底、
            # 仅脸部掩膜重绘——身材/服装/发型/姿势/尺寸零改动（底图像素保证），
            # 脸部一致性由 refine_face 的「同人锁定」+ 面部上下文兜底。
            if (not note_s and emotion != "neutral"
                    and cfg["image"].get("face_refine", True)
                    and client.provider == "sdwebui"):
                base_label = (f"{name}·{outfit_s}·neutral"
                              if outfit_s else f"{name}·neutral")
                base = await _await_cutout(session, name, base_label)
                if base:
                    try:
                        out = await client.refine_face(
                            base, base,
                            f"{appearance}，{emotion_cn}，"
                            f"{emotion_en_tags(emotion)}，"
                            "only the facial expression changes, "
                            "everything else identical, same body size, "
                            "same pose, same hairstyle, same outfit, "
                            "same body proportions",
                            _seed_for(session, f"diff|{name}|{emotion}"),
                            denoise=0.45)
                        # 精修被守卫弃用（返回原图）时不得把底图当成品——
                        # 视为失败回退全量生成（否则该表情会变成 neutral）
                        if out is base or out == base:
                            raise ImageGenError("diff refine 未生效（守卫弃用）")
                        issue = await asyncio.to_thread(
                            _final_check_issue, out,
                            nude_ok=_nude_hint_text(outfit_s, note_s))
                        if not issue:
                            raw_name = f"{_session_dir_name(session)}/raw/{key}_raw.png"
                            (cfgmod.CACHE_DIR / raw_name).write_bytes(out)
                            return out, raw_name
                        log.warning("diff refine advisory: %s", issue)
                    except ImageGenError as e:
                        log.warning("diff refine failed (%s), fallback full gen", e)
                elif outfit_s and cfg["image"].get("outfit_inpaint", True):
                    # 换装底图（`衣着·neutral`）尚未就绪：不空等全量回退——
                    # 直接以「当前立绘」为底做同一套换装（身形/姿势/脸不动，
                    # 且不会落入会被制服锁定词拽回校服的 prompt 冲突）
                    outfit_en = await _outfit_terms(outfit_s, appearance)
                    unif_en_ = _uniform_text_for(session, outfit_s)
                    nm = _outfit_nude_mode(session, outfit_s)
                    inp_prompt_ = (f"{appearance}，{emotion_cn}，{style}，"
                                   f"换装为：{outfit_s}"
                                   + (f"，{outfit_en}" if outfit_en else "")
                                   + (f"，{unif_en_}" if unif_en_ and nm == "dressed"
                                      else "")
                                   + ("" if nm != "dressed" else
                                      (", fully dressed, outfit covering the "
                                       "body, detailed clothing texture, "
                                       "crisp fabric folds, light colors")))
                    prev = _find_prev_cutout(session, name)
                    if prev:
                        try:
                            out = await client.refine_outfit(
                                prev, inp_prompt_,
                                _seed_for(session, f"fit|npc|{name}|{outfit_s}"),
                                nude_mode=nm)
                            raw_name = f"{_session_dir_name(session)}/raw/{key}_raw.png"
                            (cfgmod.CACHE_DIR / raw_name).write_bytes(out)
                            return out, raw_name
                        except ImageGenError as e:
                            log.warning("noutfit fallback failed (%s)", e)
            if outfit_s:
                # 换装首选「图生图限定服装区域重绘」：旧立绘为底，脸/姿势不变
                outfit_en = await _outfit_terms(outfit_s, appearance)
                unif_en = _uniform_text_for(session, outfit_s)
                nm = _outfit_nude_mode(session, outfit_s)
                inp_prompt = (f"{appearance}，{emotion_cn}，{style}，"
                              f"换装为：{outfit_s}"
                              + (f"，{outfit_en}" if outfit_en else "")
                              + (f"，{unif_en}" if unif_en and nm == "dressed" else "")
                              + ("" if nm != "dressed" else
                                 (", fully dressed, outfit covering the body, "
                                  "detailed clothing texture, crisp fabric folds, "
                                  "light colors, white blouse accent")))
                prev = _find_prev_cutout(session, name)
                if prev and cfg["image"].get("outfit_inpaint", True):
                    try:
                        out = await client.refine_outfit(
                            prev, inp_prompt,
                            _stable_seed(f"fit|npc|{name}|{outfit_s}"),
                            nude_mode=_outfit_nude_mode(session, outfit_s))
                        (cfgmod.CACHE_DIR / raw_name).write_bytes(out)
                        _nude_npc = _nude_hint_text(outfit_s, note_s)
                        # B-05/M-02：复检丢线程池；原来这一处连调三次
                        # （判断 + 取文案），现在算一次复用
                        _advise = await asyncio.to_thread(
                            _final_check_issue, out, nude_ok=_nude_npc)  # 软告警
                        if _advise:
                            log.warning("outfit inpaint result advisory: %s",
                                        _advise)
                        return out, raw_name
                    except ImageGenError as e:
                        log.warning("npc outfit inpaint failed (%s), fallback full gen", e)
            data = await client.generate_portrait(
                name, appearance + outfit_part + req, emotion,
                emotion_cn, style,
                extra_negative=extra_neg,
                seed=_seed_for(session, f"npc|{name}"),
                strict=not bool(outfit_s),
                # 1.7.35 本局生效制服（玩家设定 > 当前场景自动映射 > 无）
                uniform_en=unif_eff[0],
                uniform_neg=unif_eff[1],
            )
            (cfgmod.CACHE_DIR / raw_name).write_bytes(data)
            out = await process_portrait_async(data, _cutout_mode())
            issue = await asyncio.to_thread(
                _final_check_issue, out,
                nude_ok=_nude_hint_text(outfit_s, note_s))
            if issue and not outfit_s and not client.mock:
                raise ImageGenError(f"成品完整性复检未通过（{issue}）")
            # 脸部 img2img 一致性：NPC 跨表情以 neutral 或同角色原图为参考
            if not issue and _face_refine_enabled(cfg):
                ref = _find_ref_face(session, name,
                                     f"{name}·neutral", allow_any=True)
                if ref:
                    out = await client.refine_face(
                        out, ref, f"{appearance}，{emotion_cn}{req}",
                        _seed_for(session, f"face|npc|{name}"))
            return out, raw_name

        _queue_asset(session, "portraits", key, label, factory, file_base or name, fname)


def _register_imported_asset(session: dict, key: str, label: str,
                             data: bytes) -> None:
    """把角色库带进来的立绘注册为本局 ready 资产（不重新生成）。
    文件名基底与生成管线一致：主角用固定文档名，NPC 用角色名。"""
    dir_name = _session_dir_name(session)
    name0 = label.split("·", 1)[0]
    base = (session.get("prot_handle") or name0) \
        if name0 == session["protagonist"]["name"] else name0
    seq = _reserve_file_name(session, base)
    fname = _finish_file_name(base, seq, data)
    rel = f"{dir_name}/portraits/{fname}"
    path = cfgmod.CACHE_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    session["assets"]["portraits"][key] = {
        "status": "ready", "file": rel, "label": label,
        "raw_file": "", "finished_at": time.time(), "imported": True,
    }


def inject_library_assets(session: dict, imports: list, cfg: dict) -> int:
    """开局前把「角色库」已有立绘注入本局（status=ready），生成管线自动
    跳过匹配变体（_queue_asset 同键即返回），缺的（如脸红表情）仍正常生成——
    导入角色与本地生成的角色享受同一条完整线路。

    imports: [{name, role|None}]；role=="主角"或名字等于主角名 → 主角线路
    （TSF 按阶段、vn 按表情），否则按 NPC 表情线路。
    """
    if not imports:
        return 0
    from . import library
    prot = session["protagonist"]
    prot_name = prot["name"]
    is_vn = session.get("mode") == "vn"
    mode_tag = "mock" if ImageClient(cfg).mock else "real"
    params = _params_tag(cfg)
    emotions = list(session.get("emotions", []))
    injected = 0

    for imp in imports:
        if not isinstance(imp, dict):
            continue
        name = str(imp.get("name", "")).strip()[:12]
        role = str(imp.get("role", "")).strip()
        info = library.get_roster_char(name, str(imp.get("dir") or ""))
        if not info or not name:
            continue
        try:
            is_prot = (name == prot_name) or (role == "主角")
            if is_prot:
                # 主角：TSF 按阶段匹配（阶段1~4），vn 按表情匹配
                if is_vn:
                    appearance = prot.get("anchor", "")
                    for emo in emotions:
                        data = _lib_bytes(info, emo)
                        if not data:
                            continue
                        key = _skey(session, "portrait", mode_tag,
                                    PROMPT_VERSION, params, name, appearance,
                                    emo, "", "", 0)
                        _register_imported_asset(
                            session, key, f"{name}·{emo}", data)
                        injected += 1
                else:
                    stage_idx = tsf.stage_of(prot["stats"]["progress"],
                             tsf.stages_for(session))
                    v = f"阶段{stage_idx + 1}"
                    data = _lib_bytes(info, v)
                    if data:
                        key = _skey(session, "tsf-portrait", mode_tag,
                                    PROMPT_VERSION, params, name,
                                    prot.get("anchor", ""), stage_idx,
                                    "", "", 0)
                        _register_imported_asset(
                            session, key, f"{name}·{v}", data)
                        injected += 1
            else:
                char = next((c for c in session.get("characters", [])
                             if c["name"] == name), None)
                if not char:
                    continue
                appearance = char.get("appearance", "")
                for emo in emotions:
                    data = _lib_bytes(info, emo)
                    if not data:
                        continue
                    key = _skey(session, "portrait", mode_tag, PROMPT_VERSION,
                                params, name, appearance, emo, "", "", 0)
                    _register_imported_asset(
                        session, key, f"{name}·{emo}", data)
                    injected += 1
        except OSError:
            log.warning("library inject failed: %s", name)
    return injected


def _lib_bytes(info: dict, variant: str) -> bytes | None:
    """读取角色库某变体立绘字节；不存在返回 None。"""
    from . import library
    p = library.variant_path(info, variant)
    if not p:
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


def restore_portrait(sid: str, label: str, file: str, pin: bool = False) -> dict:
    """立绘历程「一键还原」：把某标签当前立绘归档，再从 versions/ 把旧版
    复制回 portraits/ 作为新的当前立绘（文件保留、可再次归档）。
    pin=True（「立刻使用此立绘」）：该角色舞台显示固定为此标签，直到重新
    生成立绘或手动取消固定（否则 TSF 还原旧阶段/旧换装会被当前取图逻辑
    跳过，看到的立绘永远不变——这就是「还原了没反应」的根因）。"""
    session = get_session(sid)
    label = (label or "").strip()[:60]
    file = (file or "").strip()
    dir_name = _session_dir_name(session)
    if not label or not file.startswith(f"{dir_name}/versions/"):
        raise GameError("只能还原本局归档的旧版立绘")
    if ".." in file or "\\" in file:
        raise GameError("非法路径")
    src = cfgmod.CACHE_DIR / file
    if not src.is_file():
        raise GameError("归档文件不存在（可能已被清理，请重新生成立绘）")
    _archive_same_label(session, "portraits", label)
    data = src.read_bytes()
    name = label.split("·", 1)[0]
    base = name
    seq = _reserve_file_name(session, base)
    fname = _finish_file_name(base, seq, data)
    rel = f"{dir_name}/portraits/{fname}"
    (cfgmod.CACHE_DIR / rel).write_bytes(data)
    key = f"restore:{int(time.time() * 1000)}:{abs(hash(label))}"
    session["assets"]["portraits"][key] = {
        "status": "ready", "file": rel, "label": label, "raw_file": "",
        "finished_at": time.time(), "restored": True,
    }
    if pin:
        session.setdefault("pinned_portraits", {})[name] = label
    persist(session)
    return {"ok": True, "label": label, "file": rel,
            "pinned": session.get("pinned_portraits", {})}


def unpin_portrait(sid: str, name: str) -> dict:
    """立绘历程「取消固定」：恢复该角色按当前阶段/表情/服装自动取图。"""
    session = get_session(sid)
    name = (name or "").strip()[:60]
    session.setdefault("pinned_portraits", {}).pop(name, None)
    persist(session)
    return {"ok": True, "pinned": session.get("pinned_portraits", {})}


def pin_portrait(sid: str, label: str) -> dict:
    """立绘历程「替换为此立绘」（1.7.24）：把该角色**指定标签**设为固定显示
    ——不动任何文件（适合标着「当前」但实际没生效/或想换回正在使用的版
    本：直接固定即可），直到重新生成立绘或取消固定。"""
    session = get_session(sid)
    label = (label or "").strip()[:60]
    if not label or "·" not in label:
        raise GameError("缺少有效的立绘标签")
    name = label.split("·", 1)[0]
    session.setdefault("pinned_portraits", {})[name] = label
    persist(session)
    return {"ok": True, "label": label,
            "pinned": session.get("pinned_portraits", {})}


def set_tsf_target(sid: str, target: dict) -> dict:
    """游戏内「转变目标设定」（仅主角，1.7.16）：保存目标（发型/体型/气质/
    发色/是否保持原发色）并**立即按新目标重画主角当前阶段**——玩家可以
    直接指定「变成什么样」，不再被锚点固定词限制。"""
    session = get_session(sid)
    if session.get("mode") == "vn":
        raise GameError("转变目标设定仅支持 TSF 模式")
    merged = dict(session.get("tsf_target") or {})
    merged.update(_norm_tsf_target(target))
    session["tsf_target"] = merged
    nonces = session.setdefault("regen_nonce", {})
    nonces["__prot__"] = nonces.get("__prot__", 0) + 1
    _start_protagonist_portrait(session, nonce=nonces["__prot__"])
    persist(session)
    return {"ok": True, "target": merged,
            "regenerating": "主角当前阶段（按新目标）"}


def migrate_cache_to_session_dirs() -> dict:
    """把「旧存档」引用的历史图片归位到本局三层目录：
    data/cache/{sid}/portraits|backgrounds|raw/（规范命名 + raw 归 raw/），
    同时把游离的旧哈希文件收进 _legacy/。仅复制，原文件保留。
    """
    moved = 0
    for s in SESSIONS.values():
        sid = str(s["sid"])
        for group, sub in (("portraits", "portraits"), ("backgrounds", "backgrounds")):
            counter = {}
            dir_name = _session_dir_name(s)
            for item in s.get("assets", {}).get(group, {}).values():
                f = item.get("file") or ""
                raw = item.get("raw_file") or ""
                if "/" not in f and "\\" not in f:
                    # 根级旧哈希 → 规范名归位
                    label = item.get("label", "") or ""
                    base = label.split("·")[0] if label and "·" in label else (
                        "背景" if group == "backgrounds" else "图片")
                    counter[base] = counter.get(base, 0) + 1
                    new_name = f"{base}——{counter[base]:03d}.png"
                    src = cfgmod.CACHE_DIR / f
                    if f and src.is_file():
                        try:
                            d = cfgmod.CACHE_DIR / dir_name / sub
                            d.mkdir(parents=True, exist_ok=True)
                            import shutil as _sh
                            _sh.copy2(src, d / new_name)
                            item["file"] = f"{dir_name}/{sub}/{new_name}"
                            moved += 1
                        except OSError:
                            continue
                elif "/" in f and f.count("/") == 1:
                    # 两层（旧目录/名称）→ 三层（可读目录/{sub}/名称）
                    parts = f.split("/", 1)
                    old = cfgmod.CACHE_DIR / f
                    if old.is_file():
                        try:
                            d = cfgmod.CACHE_DIR / dir_name / sub
                            d.mkdir(parents=True, exist_ok=True)
                            new_n = parts[1]
                            import shutil as _sh
                            _sh.copy2(old, d / new_n)
                            item["file"] = f"{dir_name}/{sub}/{new_n}"
                            moved += 1
                        except OSError:
                            continue
                if raw and "/" in raw:
                    raw_parts = raw.split("/")
                    if len(raw_parts) == 2 and raw_parts[1].endswith("_raw.png"):
                        raw_old = cfgmod.CACHE_DIR / raw
                        if raw_old.is_file():
                            try:
                                rd = cfgmod.CACHE_DIR / dir_name / "raw"
                                rd.mkdir(parents=True, exist_ok=True)
                                import shutil as _sh
                                _sh.copy2(raw_old, rd / raw_parts[1])
                                item["raw_file"] = f"{dir_name}/raw/{raw_parts[1]}"
                            except OSError:
                                pass
        if moved:
            persist(s)
    # 遗留孤儿（根级旧哈希文件）：复制一份进 _legacy/ 备份（原位保留，引用不断裂）
    legacy_n = 0
    legacy_dir = cfgmod.CACHE_DIR / "_legacy"
    for f in list(cfgmod.CACHE_DIR.glob("*.png")):
        try:
            legacy_dir.mkdir(parents=True, exist_ok=True)
            import shutil as _sh
            _sh.copy2(f, legacy_dir / f.name)
            legacy_n += 1
        except OSError:
            continue
    return {"migrated": moved, "legacy": legacy_n}


def _find_asset_file(file: str) -> str | None:
    """路径自适应：file 引用失效时回溯备选位置（三层/两层/_legacy/根级）。"""
    if not file:
        return None
    p = cfgmod.CACHE_DIR / file
    if p.is_file():
        return file
    parts = file.split("/")
    if len(parts) in (2, 3):
        cand = cfgmod.CACHE_DIR / "_legacy" / parts[-1]
        if cand.is_file():
            return f"_legacy/{parts[-1]}"
        cand2 = cfgmod.CACHE_DIR / parts[-1]
        if cand2.is_file():
            return parts[-1]
    cand3 = cfgmod.CACHE_DIR / "_legacy" / file
    if cand3.is_file():
        return file
    return None


def _recover_interrupted_assets(session: dict) -> int:
    """读档/启动时修复「上次进程中断留下的 pending 资产」。

    老存档常见问题：快照保存时某张背景/立绘还是 pending（服务正好在生成），
    下次读档后这个 pending 永远没有任务推进它——前端一直等，场景也永远不生成。
    这里把 pending 转 error；若文件其实已落盘则恢复 ready。同时清空场景补生成
    的重试预算，让旧档读入后能重新尝试生成背景。
    """
    recovered = 0
    for group, items in (session.get("assets") or {}).items():
        for item in (items or {}).values():
            if not isinstance(item, dict) or item.get("status") == "ready":
                continue
            f = item.get("file") or ""
            path = cfgmod.CACHE_DIR / f if f else None
            min_size = 60000 if group == "portraits" else 1024
            if path is not None and path.is_file() and path.stat().st_size > min_size:
                item["status"] = "ready"
                item["error"] = ""
                recovered += 1
            elif item.get("status") == "pending":
                item["status"] = "error"
                item["error"] = "存档中的生成任务在上次退出时中断，将自动重新生成"
                recovered += 1
    # 旧档可能已经耗尽了场景补生成预算（state=unavailable）；读档应给一次新机会。
    if session.get("bg_retry"):
        session["bg_retry"] = {}
    # 旧档的 bg_hint_cache 可能缓存了带角色名的旧提示词（模型据此画出人物）；
    # 清掉后让 _enhance_bg_hint 用当前规则重新提炼。
    if session.get("bg_hint_cache"):
        session["bg_hint_cache"] = {}
    try:
        entry = session["log"][-1]
        turn = entry.get("turn") if isinstance(entry, dict) else None
        if isinstance(turn, dict):
            turn.pop("bg_unavailable", None)
            turn.pop("scene_revisit", None)
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return recovered + _ensure_asset_paths(session)


def _ensure_asset_paths(session: dict) -> int:
    """修复会话全部资产引用（读档/启动后调用），使立绘始终可读取。

    立绘：路径自适应（历史路径/旧哈希名）；背景：文件彻底丢失（无替代可修）
    时标记 error——配合 _assign_scene_bg 的「不可用即重新生成」，旧存档读档后
    背景可自动补生成，不再被「死引用」卡死。
    """
    fixed = 0
    for group, items in session.get("assets", {}).items():
        for item in items.values():
            f = item.get("file") or ""
            if f and not (cfgmod.CACHE_DIR / f).is_file():
                new_f = _find_asset_file(f)
                if new_f:
                    item["file"] = new_f
                    fixed += 1
                elif group == "backgrounds" and item.get("status") == "ready":
                    item["status"] = "error"
                    item["error"] = "背景文件已丢失（随剧情推进将自动重新生成）"
            raw = item.get("raw_file") or ""
            if raw and not (cfgmod.CACHE_DIR / raw).is_file():
                new_raw = _find_asset_file(raw)
                if new_raw:
                    item["raw_file"] = new_raw
                    fixed += 1
    return fixed


def reload_assets(sid: str) -> dict:
    """手动修复按钮：重新读取/校正本局全部立绘引用（路径自适应）。"""
    session = get_session(sid)
    fixed = _ensure_asset_paths(session)
    persist(session)
    return {"ok": True, "fixed": fixed}
def _drop_old_assets(session: dict, name: str) -> int:
    """重画前把该角色的旧立绘「归档保留」：从当前显示资产表移除，
    文件留在磁盘并记入 portrait_history（可随时回看变化过程），不删除。"""
    prot_name = session["protagonist"]["name"]
    hist = session.setdefault("portrait_history", [])
    targets = [k for k, it in session["assets"]["portraits"].items()
               if it.get("label", "").startswith(f"{name}·")
               or (name == prot_name and it.get("label", "").startswith(prot_name + "·"))]
    archived_labels = set()
    archived = 0
    for k in targets:
        item = session["assets"]["portraits"].pop(k)
        archived_labels.add(item.get("label", ""))
        hist.append({
            "archive_ts": time.time(),
            "label": item.get("label", ""),
            "file": _archive_file_to_versions(session, item.get("file", "")),
            "raw_file": item.get("raw_file", ""),
            "progress": ((session["protagonist"].get("stats") or {})
                         .get("progress", 0) if name == prot_name else 0),
        })
        archived += 1
    for lbl in archived_labels:
        _prune_history(session, lbl)
    # 重画 = 用户要新图，该角色的「固定立绘」同时失效
    session.get("pinned_portraits", {}).pop(name, None)
    return archived


def portrait_history_view(sid: str) -> dict:
    """立绘历程：该局全部角色立绘的版本历史（含归档旧版与当前版本），
    按时间倒序，主角条目附带当时的同化率。"""
    session = get_session(sid)
    prot_name = session["protagonist"]["name"]
    # 1.9.13：旧实现把**所有 ready 立绘**都标成 current，于是新生成的阶段6
    # 在历程里显示「当前」，但舞台实际还在用阶段5（或 pin 的另一张）。
    # current 必须按引擎真实选图规则计算：pin > 主角当前换装 > 当前阶段
    # （阶段图缺失时回退到不高于当前阶段的最近一张）；NPC 取最近完成一张。
    ready = [it for it in session["assets"]["portraits"].values()
             if it.get("file") and it.get("status") == "ready"]
    pins = session.get("pinned_portraits") or {}
    actual = set()
    pin = pins.get(prot_name)
    if pin:
        actual.add(pin)
    else:
        outfit = session.get(f"{prot_name}|outfit") or ""
        if outfit and any(it.get("label") == f"{prot_name}·{outfit}" for it in ready):
            actual.add(f"{prot_name}·{outfit}")
        else:
            stages = tsf.stages_for(session)
            prog = int((session["protagonist"].get("stats") or {}).get("progress", 0) or 0)
            cur = tsf.stage_of(prog, stages)
            for idx in range(cur, -1, -1):
                label = f"{prot_name}·阶段{idx + 1}"
                if any(it.get("label") == label for it in ready):
                    actual.add(label)
                    break
    for name in {str((it.get("label") or "").split("·")[0]) for it in ready}:
        if name == prot_name:
            continue
        cand = [it for it in ready
                if (it.get("label") or "").startswith(name + "·")]
        if cand:
            cand.sort(key=lambda it: it.get("finished_at") or 0, reverse=True)
            actual.add(cand[0].get("label", ""))

    entries = []
    for it in session.get("portrait_history", []):
        if it.get("file"):
            entries.append(dict(it, current=False))
    for it in ready:
        label = it.get("label", "")
        entries.append({
            "archive_ts": it.get("finished_at") or session["saved_at"] or 0,
            "label": label,
            "file": it.get("file", ""),
            "raw_file": it.get("raw_file", ""),
            "progress": ((session["protagonist"].get("stats") or {})
                         .get("progress", 0)
                         if label.startswith(prot_name + "·") else 0),
            "current": label in actual,
        })
    entries.sort(key=lambda e: e.get("archive_ts", 0), reverse=True)
    return {"protagonist": prot_name, "entries": entries}


def regenerate_portrait(sid: str, target: str, note: str = "",
                        use_initial: bool = False,
                        force_fullgen: bool = False) -> dict:
    """针对性重新生成某个角色的立绘（防止畸形/不满意时只重画该角色）。
    force_fullgen=True（1.7.31）：主角重画走全量精美生成（内建 3 种子
    轮换+组合质检挑精品——像人工精选种子一样），跨性别变化只有全量做得到。

    target == "protagonist" 重画主角当前阶段；target == "all" 重画全场立绘
    （主角当前阶段 + 所有 NPC 全部表情）；否则按角色名重画该 NPC 全部表情。
    note = 玩家的重画需求（如「更女性化」「去掉眼镜」「要笑的表情」），
    会翻译为英文关键词注入立绘提示词。通过递增 nonce 生成新的缓存键。
    use_initial=True（仅主角）：以初始（阶段1）立绘为参考、按当前同化率重建。
    """
    session = get_session(sid)
    nonces = session.setdefault("regen_nonce", {})
    if target == "all":
        _drop_old_assets(session, session["protagonist"]["name"])
        for npc in session["characters"]:
            _drop_old_assets(session, npc["name"])
        nonces["__prot__"] = nonces.get("__prot__", 0) + 1
        _start_protagonist_portrait(session, nonce=nonces["__prot__"],
                                    note=note, use_initial=use_initial,
                                    force_fullgen=force_fullgen)
        n = len(session["characters"]) + 1
        for npc in session["characters"]:
            nonces[npc["name"]] = nonces.get(npc["name"], 0) + 1
            _start_npc_portrait(session, npc, nonce=nonces[npc["name"]], note=note)
        label = f"全部角色立绘（{n} 个角色）"
    elif target == "protagonist":
        _drop_old_assets(session, session["protagonist"]["name"])
        nonces["__prot__"] = nonces.get("__prot__", 0) + 1
        _start_protagonist_portrait(session, nonce=nonces["__prot__"],
                                    note=note, use_initial=use_initial,
                                    force_fullgen=force_fullgen)
        label = f"{session['protagonist']['name']}（当前阶段）"
    else:
        npc = next((c for c in session["characters"] if c["name"] == target), None)
        if not npc:
            raise GameError(f"角色不存在：{target}")
        _drop_old_assets(session, target)
        nonces[target] = nonces.get(target, 0) + 1
        _start_npc_portrait(session, npc, nonce=nonces[target], note=note)
        label = f"{target}（全部表情）"
    persist(session)
    return {"ok": True, "regenerating": label}


def regenerate_label(sid: str, label: str) -> dict:
    """立绘历程「重绘差分」：只重画指定的某一张表情差分（非 neutral 走
    「以同衣着 neutral 为底的图生图只变表情」管线），旧版归档、其余表情
    不受影响；label 支持三维「角色·衣着·表情」；主角 TSF 标签 = 重画
    主角当前阶段。TSF/VN 通用。"""
    session = get_session(sid)
    pname = session["protagonist"]["name"]
    # 1.7.5/1.7.37 精确解析：第二/中间段若是「表情词」则不是服装段——
    # 两段式「名字·表情」应还原为（无服装, 表情）；三段怪标签
    # （名字·neutral·neutral：无换装的表情被误拆为三段）同理，
    # 否则会把表情当成服装去换装，永远出怪图/无效标签
    _EMOTION_KEYS = {"neutral", "happy", "shy", "sad", "aroused",
                     "flustered", "angry", "surprised"}
    parts = (label or "").split("·")
    name = parts[0].strip()
    if len(parts) == 2:
        second = parts[1].strip()
        if second in _EMOTION_KEYS:
            outfit, emo = "", second
        else:
            outfit, emo = second, ""
    else:
        outfit, emo = "·".join(parts[1:-1]).strip(), parts[-1].strip()
    if outfit in _EMOTION_KEYS:
        outfit = ""
    if not name or (not emo and not outfit):
        raise GameError("无效的立绘标签")
    if name == pname and session.get("mode") != "vn" and emo.startswith("阶段"):
        # TSF 主角阶段图：复用「重画主角立绘」语义（当前阶段，progress 驱动）
        return regenerate_portrait(sid, "protagonist", "")
    if name == pname and session.get("mode") != "vn" and not emo:
        # TSF 主角换装差分（「主角·服装」两段标签）：直接重绘该换装单张
        nonces = session.setdefault("regen_nonce", {})
        _archive_same_label(session, "portraits", label)
        nonces["__prot__"] = nonces.get("__prot__", 0) + 1
        _start_protagonist_portrait(session, nonce=nonces["__prot__"],
                                    outfit=outfit)
        persist(session)
        return {"ok": True, "regenerating": label}
    npc = None
    if name != pname:
        npc = next((c for c in session["characters"] if c["name"] == name), None)
        if not npc:
            raise GameError(f"角色不存在：{name}")
    nonces = session.setdefault("regen_nonce", {})
    _archive_same_label(session, "portraits", label)   # 仅归档该标签旧版
    key = "__prot__" if name == pname else name
    nonces[key] = nonces.get(key, 0) + 1
    _start_npc_portrait(
        session,
        {"name": name,
         "appearance": (session["protagonist"].get("anchor", "")
                        if name == pname else npc.get("appearance", "")),
         "personality": npc.get("personality", "") if npc else ""},
        nonce=nonces[key],
        outfit=outfit or "",
        file_base=(session.get("prot_handle") or name) if name == pname else "",
        emotion_only=emo)
    persist(session)
    return {"ok": True, "regenerating": label}


BG_ENHANCE_SYSTEM = (
    "你是文生图提示词工程师。把玩家给出的中文场景描述改写为高质量生图提示词：\n"
    "1) 先写一句简短中文画面描写；\n"
    "2) 再给 8~14 个英文场景标签（室内/室外、时段、光线、氛围、色调、常见物品/建筑元素），"
    "用逗号分隔；\n"
    "3) 若提供了【本场角色】，请让场景细节暗示与他们身份/关系相关的氛围元素"
    "（例如学园角色→校徽、课桌椅、储物柜、社团部室等关联符号），但不要画人物；\n"
    "4) 不要输出任何解释或引号。只输出提示词正文。"
)


async def _enhance_bg_hint(hint: str, session: dict | None = None) -> str:
    """用 LLM 给背景描述先提炼成生图提示词（关键词化 + 角色关联），
    失败时原样返回。"""
    hint = (hint or "").strip()
    if not hint:
        return hint
    # mock 模式不调用 LLM
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return hint
    user_content = hint[:120]
    if session:
        prot = session.get("protagonist", {})
        chars = session.get("characters", [])
        cast = ", ".join(
            [str(prot.get("name", ""))]
            + [str(c.get("name", "")) for c in chars][:4])
        if cast.strip():
            user_content = (f"【场景】{hint[:80]}\n【本场角色】{cast.strip()}（学园/制服类"
                            f"世界观请在场景元素中自然呼应她们的身份）")
    try:
        result = await llm.chat([
            {"role": "system", "content": BG_ENHANCE_SYSTEM},
            {"role": "user", "content": user_content},
        ], temperature=0.4)
        result = (result or "").strip().replace("\n", " ")[:300]
        if len(result) >= 10:
            return result
    except LLMError as e:
        log.warning("背景提示词增强失败，使用原文：%s", e)
    return hint


def _file_base(raw: str) -> str:
    """资产文件名基名清洗：场景名/CG 标题里的空格等空白字符替换为下划线，
    避免带空格文件名被 /img 白名单以外的路径工具误伤（历史存量不再动）。"""
    import re as _re
    return _re.sub(r"\s+", "_", str(raw or "")).strip("_") or "背景"


def _start_background_task(session: dict, hint: str,
                           base_name: str = "背景") -> str:
    cfg = cfgmod.load_config()
    client = ImageClient(cfg)
    key = _skey(session, "bg", PROMPT_VERSION, _params_tag(cfg), hint[:120])
    bg_id = f"bg{session['bg_count']}"
    session["bg_count"] += 1
    fname = _reserve_file_name(session, f"场景·{_file_base(base_name)[:12]}")

    async def factory():
        # 先由 LLM 提炼英文关键词再画图（同 hint 只提炼一次）
        cache = session.setdefault("bg_hint_cache", {})
        if hint in cache:
            prompt = cache[hint]
        else:
            prompt = await _enhance_bg_hint(hint, session)
            cache[hint] = prompt
        return await client.generate_background(
            prompt, cfg["game"].get("background_style_suffix", ""))

    try:
        _queue_asset(session, "backgrounds", key, bg_id, factory,
                     f"场景·{base_name[:12]}", fname)
    except Exception:
        # 任务没排上：把刚占用的编号与映射还回去（否则 bg_count 单调增长）
        session["bg_count"] = max(0, int(session.get("bg_count", 0) or 0) - 1)
        (session.get("bg_map") or {}).pop(bg_id, None)
        raise
    session["bg_map"][bg_id] = key
    return bg_id


# ---------- 数值演进 ----------





# イベントCG 提炼（galgame 概念：角色演出名场面，不同于纯背景）
CG_ENHANCE_SYSTEM = (
    "你是视觉小说イベントCG提示词工程师。把玩家给出的剧情画面改写成英文生图"
    "提示词：1) 先在句首给出画面中的角色（主角与在场角色，用名字+外貌特征）；"
    "2) 再写横版电影镜头感的场景描述（构图/光线/氛围/关键道具）；"
    "3) 强调这是故事的高光瞬间（情感焦点、戏剧性时刻）；"
    "4) 画面允许包含人物，人物与背景一体；"
    "5) 只输出提示词正文，不要解释与引号。"
)


async def _enhance_cg_hint(hint: str, session: dict | None = None) -> str:
    """LLM 提炼イベントCG 提示词（含角色演出），失败原样返回（mock 同）。"""
    hint = (hint or "").strip()
    if not hint:
        return hint
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return hint
    user_content = hint[:120]
    if session:
        prot = session.get("protagonist", {})
        chars = session.get("characters", [])
        cast = ", ".join([str(prot.get("name", ""))]
                         + [str(c.get("name", "")) for c in chars][:4])
        if cast.strip():
            user_content = (f"【画面描述】{hint[:80]}\n"
                            f"【在场角色】{cast.strip()}")
    try:
        result = await llm.chat([
            {"role": "system", "content": CG_ENHANCE_SYSTEM},
            {"role": "user", "content": user_content},
        ], temperature=0.4)
        result = (result or "").strip().replace("\n", " ")
        return result if len(result) >= 8 else hint
    except LLMError:
        return hint


def _cg_cast_en(session: dict) -> str:
    """CG 角色演出词：主角 + 第一位主要女角色（外貌用英文标签增强）。"""
    parts = []
    prot = session.get("protagonist", {})
    pname = str(prot.get("name", ""))
    if pname:
        p_en = append_appearance_en(str(prot.get("anchor", "") or ""))
        parts.append(f"1boy ({pname}), {p_en}")
    chars = session.get("characters", [])
    if chars:
        c = chars[0]
        c_en = append_appearance_en(str(c.get("appearance", "") or ""))
        parts.append(f"1girl ({c.get('name', '')}), {c_en}")
    if len(chars) > 1:
        c2 = chars[1]
        c2_en = append_appearance_en(str(c2.get("appearance", "") or ""))
        parts.append(f"1girl ({c2.get('name', '')}), {c2_en}")
    if not parts:
        return ""
    return ", ".join(parts) + ", characters acting together in the event scene"


def _outfit_nude_mode(session: dict, outfit_cn: str) -> str:
    """换装目标档位（1.6.91）：dressed / underwear / nude——
    非 R18 强制保守（dressed+全裸封杀）；「裸体」「内衣」仅在 18+ 放行，
    让换装图生图按目标动态适配（不再「画裸体又禁裸体」）。"""
    if not _adult_flags(session)[0]:
        return "dressed"
    if any(k in outfit_cn for k in ("裸体", "一丝不挂", "未着寸缕", "全裸",
                                    "nude", "naked")):
        return "nude"
    if any(k in outfit_cn for k in ("内衣", "内裤", "underwear", "lingerie")):
        return "underwear"
    return "dressed"


def _start_cg_task(session: dict, cg: dict) -> str:
    """特殊桥段 CG：横版电影镜头感图片（背景类管线，允许人物同框）。"""
    cfg = cfgmod.load_config()
    client = ImageClient(cfg)
    prompt = (f"{cg['prompt']}，{cfg['game'].get('background_style_suffix', '')}，"
              "cinematic wide shot, dramatic composition, high quality illustration")
    key = _skey(session, "cg", PROMPT_VERSION, _params_tag(cfg),
                cg["prompt"][:80], session["bg_count"])
    cg_id = f"cg{session['bg_count']}"
    session["bg_count"] += 1
    fname = _reserve_file_name(session, f"CG·{_file_base(cg.get('title', '桥段'))[:10]}")

    async def factory():
        # イベントCG 专用：LLM 提炼（含角色演出名场面）→ 角色入画独立管线；
        # 1.7.8 传入「主角当前阶段 + 前两位女主 current 立绘」作 img2img 参考
        # 底图——CG 中的角色与立绘高度相似（发色/瞳色/服装/脸型同源）
        prompt = await _enhance_cg_hint(cg["prompt"], session)
        cutouts = []
        pname = session["protagonist"]["name"]
        try:
            stage_idx = tsf.stage_of(
                session["protagonist"]["stats"]["progress"],
                tsf.stages_for(session))
        except (KeyError, TypeError):
            stage_idx = 0
        labels = [f"{pname}·阶段{stage_idx + 1}"]
        for c in session.get("characters", [])[:2]:
            labels.append(f"{c['name']}·neutral")
        for lbl in labels:
            # 1.7.40 只取当前 ready 最新立绘（阶段变化被真正考虑）；
            # 无则回退 _find_prev_cutout（归档兜底）
            b = _find_live_cutout(session, lbl.split("·")[0], lbl)
            if b:
                cutouts.append(b)
            if len(cutouts) >= 3:
                break
        return await client.generate_cg(
            prompt, "", _cg_cast_en(session), cutouts or None)

    _queue_asset(session, "backgrounds", key, cg_id, factory,
                 f"CG·{_file_base(cg.get('title', '桥段'))[:10]}", fname)
    session["cg_map"] = session.setdefault("cg_map", {})
    session["cg_map"][cg_id] = {"key": key, "title": cg["title"],
                                "prompt": str(cg.get("prompt", ""))[:300],
                                "cast": _cg_cast_en(session)}
    return cg_id


def regen_cg(sid: str, cg_id: str = "") -> dict:
    """「刷新 CG / 重绘 CG」：以已记录的画面描述重新生成桥段 CG——
    旧图保留在资产与回廊，当前幕指向新图。TSF/VN 通用。"""
    session = get_session(sid)
    turn = session["log"][-1]["turn"]
    cid = cg_id or turn.get("cg_id") or ""
    meta = (session.get("cg_map") or {}).get(cid)
    if not meta:
        raise GameError("没有可重绘的 CG（当前幕无桥段 CG）")
    prompt = str(meta.get("prompt") or "").strip() or "黄昏的教室，两人的身影"
    new_id = _start_cg_task(session, {"prompt": prompt,
                                      "title": meta.get("title", "桥段")})
    turn["cg_id"] = new_id
    turn["cg_active"] = bool(meta.get("prompt"))
    persist(session)
    return {"ok": True, "cg_id": new_id, "title": meta.get("title", "桥段")}


# 背景补生成的重试预算（t43 实测：旧实现无上限——每次 GET /api/game/{sid} 只要
# 资产是 error 就 `unusable -> regenerate` 换一个新 background_id，实测 39 次轮询
# 走到 bg41，bg_map 与 bg_count 无界增长，且失败尝试在日志里没有痕迹）。
#
# 为什么是 3 次：`_sd_retry` 每张图内部已经做了 3 个种子的变体轮换，所以
# 「场景级 3 次」最坏 = 9 次 SD 调用，与一次阶段跃迁的量级相当；再多只会
# 在必然失败的路径上白烧显存与时间。
# 为什么退避是 20/40/80 秒：前端资产轮询与 `ensure_scene_bg` 的触发源都是
# `GET /api/game/{sid}`，不设间隔的话用户每刷新一次界面就消耗一次预算；
# 指数退避让「同一场景」的重试在时间维度上也可预测、可观察。
BG_RETRY_MAX = 3
BG_RETRY_BACKOFF = (20.0, 40.0, 80.0)


def _bg_retry_state(session: dict, scene_id: str) -> dict:
    """取（或建）某场景的补生成重试账本：{n, first_bg, last_ts, state}。"""
    ledger = session.setdefault("bg_retry", {})
    st = ledger.get(scene_id)
    if not isinstance(st, dict):
        st = {"n": 0, "first_bg": "", "last_ts": 0.0, "state": "retry"}
        ledger[scene_id] = st
    return st


def _bg_backoff_secs(n: int) -> float:
    """第 n 次尝试后应等待的秒数（n 从 0 起）。"""
    if n < len(BG_RETRY_BACKOFF):
        return BG_RETRY_BACKOFF[n]
    return BG_RETRY_BACKOFF[-1]


def _bg_retry_count(session: dict, scene_name) -> int:
    """该场景已消耗的补生成尝试次数（0 = 没试过）。给 public_state 用。"""
    try:
        scene_id = hashlib.sha256(str(scene_name or "").encode("utf-8")).hexdigest()[:10]
        st = (session.get("bg_retry") or {}).get(scene_id) or {}
        return int(st.get("n", 0) or 0)
    except Exception:
        return 0


def _assign_scene_bg(session: dict, turn: dict) -> None:
    """场景规划：同一场景名复用固定的背景图（首次访问生成，重访直接调用）。

    调试信息写入日志：first visit -> new bg / revisit -> reuse bg。
    """
    scene_name = str(turn.get("scene", "……") or "……")[:30]
    scenes = session.setdefault("scenes", {})
    # 1.9.0 场景连续性：LLM 常把同一地点写成「操场」「操场（午后）」
    # 「星冠学园·操场」等变体。旧逻辑按原名哈希 → 每变一次就重画一张背景。
    # 这里归一到已登记的同地点原名，复用它的固定背景（玩家看到的地名也稳定）。
    canon = scenemod.canonical_scene(
        scene_name, [str(x.get("name", "")) for x in scenes.values()
                     if isinstance(x, dict) and x.get("name")])
    if canon and canon != scene_name:
        log.info("scene canonicalize '%s' -> '%s' (reuse same location)",
                 scene_name, canon)
        turn["scene"] = scene_name = canon
    scene_id = hashlib.sha256(scene_name.encode("utf-8")).hexdigest()[:10]
    sc = scenes.get(scene_id)
    if sc and sc.get("bg_id"):
        key = session.get("bg_map", {}).get(sc["bg_id"])
        entry = (session.get("assets", {}).get("backgrounds", {}) or {}).get(key or "")
        # pending（生成中）也算可用：继续复用，前端轮询会等到 ready；
        # 只有 error（文件丢失/生成失败，且 _ensure_asset_paths 已标记）才
        # 释放旧引用重新生成——否则快速切幕会反复触发重复生成的死循环。
        usable = bool(entry) and entry.get("status") in ("ready", "pending")
        if usable:
            turn["background_id"] = sc["bg_id"]
            turn["scene_revisit"] = True
            log.info("scene revisit '%s' -> reuse fixed bg %s",
                     scene_name, sc["bg_id"])
            return
        # 旧存档背景文件丢失/曾生成失败：释放旧引用，按新背景重新生成
        # —— 但**受重试预算约束**（t43：旧实现无上限，每次 GET 都换新 id）。
        rst = _bg_retry_state(session, scene_id)
        if not rst.get("first_bg"):
            rst["first_bg"] = sc["bg_id"]
        if rst.get("state") == "unavailable":
            # 预算用尽：保留**首次**分配的 id（不再换新 id、不再入队），
            # 让前端拿到一个稳定的（不可用）引用而不是每轮刷新一个新背景号。
            turn["background_id"] = sc["bg_id"]
            turn["scene_revisit"] = True
            turn["bg_unavailable"] = True
            return
        if rst["n"] >= BG_RETRY_MAX:
            rst["state"] = "unavailable"
            log.warning("背景补生成放弃：scene='%s' 已尝试 %d 次仍不可用"
                        "（首次 %s，末次 %s）→ 标记永久不可用并停止重试（不再换新 id）",
                        scene_name, rst["n"], rst["first_bg"], sc["bg_id"])
            turn["background_id"] = sc["bg_id"]
            turn["scene_revisit"] = True
            turn["bg_unavailable"] = True
            return
        now = time.time()
        wait = _bg_backoff_secs(rst["n"])
        if now - float(rst.get("last_ts") or 0.0) < wait:
            # 退避窗口内：不重试、不换 id（同一 GET 轮询风暴不会消耗预算）
            turn["background_id"] = sc["bg_id"]
            turn["scene_revisit"] = True
            return
        rst["n"] += 1
        rst["last_ts"] = now
        log.info("scene '%s' bg %s unusable -> regenerate"
                 "（第 %d/%d 次尝试，上次间隔≥%.0fs）",
                 scene_name, sc["bg_id"], rst["n"], BG_RETRY_MAX, wait)
        sc.pop("bg_id", None)
    hint = turn.get("background_hint") or scene_name
    if turn.get("is_new_background", False) or not hint:
        hint = hint or scene_name
    # 场景名前置：确保生成的背景与当前场景描述严格一致
    bg_prompt = f"{scene_name}，{hint}" if hint != scene_name else hint
    bg_id = _start_background_task(session, bg_prompt, base_name=scene_name)
    turn["background_id"] = bg_id
    turn["scene_revisit"] = False
    if sc is not None:
        # 关键：不可用释放后重生成的 bg_id 立即回写场景条目——
        # 否则下次回到该场景又被当成「无背景」再次重新生成（死循环）
        sc["bg_id"] = bg_id
    log.info("scene first visit '%s' -> new bg %s", scene_name, bg_id)


def _bg_state_snapshot(session: dict, turn: dict) -> dict:
    """背景相关内存态快照（补生成/重绘失败时回滚，B-03 的「内存态被半改」）。"""
    return {
        "scene": turn.get("scene"),
        "background_hint": turn.get("background_hint"),
        "bg_id": turn.get("background_id"),
        "bg_count": int(session.get("bg_count", 0) or 0),
        "scenes": json.loads(json.dumps(session.get("scenes", {}),
                                       ensure_ascii=False)),
        "bg_map": dict(session.get("bg_map", {}) or {}),
        "bg_retry": json.loads(json.dumps(session.get("bg_retry", {}) or {},
                                          ensure_ascii=False)),
        "backgrounds": json.loads(json.dumps(
            (session.get("assets", {}) or {}).get("backgrounds", {}) or {},
            ensure_ascii=False)),
    }


def _bg_state_restore(session: dict, turn: dict, snap: dict) -> None:
    """回滚到快照（失败路径专用：内存态要么全新、要么和失败前一样）。"""
    turn["scene"] = snap["scene"]
    turn["background_hint"] = snap["background_hint"]
    turn["background_id"] = snap["bg_id"]
    session["bg_count"] = snap["bg_count"]
    session["scenes"] = snap["scenes"]
    session["bg_map"] = snap["bg_map"]
    session["bg_retry"] = snap.get("bg_retry", {})
    session.setdefault("assets", {})["backgrounds"] = snap["backgrounds"]


def ensure_scene_bg(session: dict) -> None:
    """读档/进入对局时调用：当前幕背景不可用（旧档文件丢失/曾生成失败）
    立即补生成并持久化——否则前端只认 ready，读进旧档时背景区域黑屏，
    要等再推进一幕才会由 _assign_scene_bg 触发（用户体感「旧档没背景」）。

    B-02：调用方（GET /api/game/{sid}、读档端点）已在事件循环上，任务由
    _spawn_task 真正排入循环；失败时带 sid 与原因落 debug.log，并回滚内存态，
    不再静默吞掉。"""
    sid = session.get("sid", "")
    try:
        turn = session["log"][-1]["turn"]
    except (KeyError, IndexError) as e:
        log.warning("ensure_scene_bg skipped sid=%s (no turn): %s: %s",
                    sid, type(e).__name__, e)
        return
    snap = None
    try:
        bg_id = turn.get("background_id")
        key = (session.get("bg_map") or {}).get(bg_id or "")
        entry = ((session.get("assets") or {}).get("backgrounds") or {}).get(key or "")
        if entry and entry.get("status") in ("ready", "pending"):
            return
        snap = _bg_state_snapshot(session, turn)
        before_bg = turn.get("background_id")
        _assign_scene_bg(session, turn)
        persist(session)
        # 只在**真的换了背景号**时记 regenerated：预算耗尽/退避窗口内是
        # 「有意不补生成」，若照旧打印会把日志变成误导（t43 的观察之一）。
        if turn.get("background_id") != before_bg:
            log.info("ensure_scene_bg regenerated sid=%s bg_id=%s",
                     sid, turn.get("background_id"))
        else:
            log.info("ensure_scene_bg skipped sid=%s bg_id=%s "
                     "（补生成受预算/退避约束，或已标记永久不可用）",
                     sid, turn.get("background_id"))
    except Exception as e:
        log.warning("ensure_scene_bg failed sid=%s: %s: %s",
                    sid, type(e).__name__, e)
        if snap is not None:
            try:
                _bg_state_restore(session, turn, snap)
            except Exception as re:
                log.warning("ensure_scene_bg rollback failed sid=%s: %s", sid, re)


def _clean_rename(turn_raw: dict) -> str:
    """LLM rename 清洗：1~6 个中文/·字符，无空白；非法返回空。"""
    import re as _re
    name = str(turn_raw.get("rename") or "").strip()
    if _re.fullmatch(r"[\u4e00-\u9fa5·]{1,6}", name):
        return name
    return ""


def _apply_rename(session: dict, new_name: str) -> bool:
    """应用主角改名：显示名更新 + 现有立绘 label 同步（文件不动、无需重画）。"""
    if not new_name or new_name == session["protagonist"]["name"]:
        return False
    old = session["protagonist"]["name"]
    session["protagonist"]["name"] = new_name
    for item in session["assets"].get("portraits", {}).values():
        label = item.get("label", "")
        if label.startswith(old + "·"):
            item["label"] = new_name + label[len(old):]
    pins = session.setdefault("pinned_portraits", {})
    new_pins = {}
    for k, lbl in pins.items():
        new_lbl = (new_name + lbl[len(old):]) if lbl.startswith(old + "·") else lbl
        new_pins[new_name if k == old else k] = new_lbl
    session["pinned_portraits"] = new_pins
    # 逐角色数值跟着主角改名迁移（否则会另起一份初始值）
    store = session.get("char_states")
    if isinstance(store, dict) and old in store and new_name not in store:
        store[new_name] = store.pop(old)
    return True


def _safe_title(t: str) -> str:
    """标题清理：保留中文/字母数字/短横，最长 12 字，空则「对局」。"""
    t = "".join(c for c in (t or "") if c.isalnum() or c in "_- ").strip()
    return (t[:12] or "对局")


def _session_dir_name(session: dict) -> str:
    """对局立绘文件夹的可读命名：{标题}_{主角文档名}（文档名内含随机码，天然唯一）。"""
    handle = session.get("prot_handle") or str(session.get("sid", ""))
    return f"{_safe_title(session.get('title', ''))}_{handle}"


def rename_session_dirs_to_readable() -> dict:
    """把旧的 12 位 sid 目录改为可读名（标题_主角_随机码），并更新全部引用。
    目录重命名（失败则逐文件复制），引用同步到会话与全局存档槽。"""
    import os as _os
    renamed = 0
    for s in SESSIONS.values():
        sid = str(s["sid"])
        old_dir = cfgmod.CACHE_DIR / sid
        if not old_dir.is_dir():
            continue
        new_name = _session_dir_name(s)
        new_dir = cfgmod.CACHE_DIR / new_name
        if new_dir == old_dir:
            continue
        if not new_dir.exists():
            try:
                _os.rename(old_dir, new_dir)
                renamed += 1
            except OSError:
                import shutil as _sh
                try:
                    _sh.move(old_dir, new_dir)
                    renamed += 1
                except OSError:
                    continue
        # 更新引用：内存会话
        for group in s.get("assets", {}).values():
            for item in group.values():
                for k in ("file", "raw_file"):
                    v = item.get(k) or ""
                    if v.startswith(f"{sid}/"):
                        item[k] = new_name + v[len(sid):]
        persist(s)
        # 更新引用：全局存档槽（快照内同名前缀）
        import json as _json
        for f in _slots_dir().glob("*.json"):
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            changed = False
            for group in d.get("assets", {}).values():
                for item in group.values():
                    for k in ("file", "raw_file"):
                        v = item.get(k) or ""
                        if v.startswith(f"{sid}/"):
                            item[k] = new_name + v[len(sid):]
                            changed = True
            if changed:
                try:
                    cfgmod.atomic_write_json(f, d, indent=1)
                except OSError:
                    pass
    return {"renamed": renamed}


#: 清理时间宽限（小时）：mtime 比这更新的文件**一律不删**（M-05／B-06）。
#: 依据：生成/重画/抠图是「先落盘、后登记引用」的多步过程，若在登记完成前
#: 判孤儿就会删掉刚出炉的图；24h 足以跨过任何一次生成流程与一次重启。
CLEANUP_GRACE_HOURS = 24.0
#: 清理报告落盘文件名（落在 DATA_DIR 下，可复核）
CLEANUP_REPORT_FILE = "cleanup-report.json"
#: 允许被删除的二级子目录白名单（其余位置只报告、不删）
_CLEANUP_DELETABLE_SUBDIRS = ("versions", "portraits", "backgrounds", "raw", "preview")
#: 报告里各列表的保留上限（避免报告无限膨胀）
_CLEANUP_LIST_CAP = 200


def _cleanup_session_dirs(d: dict) -> list[str]:
    """一份快照可能对应的对局目录名（新可读名 + 旧 sid 名）。"""
    out = []
    try:
        out.append(_session_dir_name(d))
    except Exception:
        pass
    sid = str((d or {}).get("sid") or "")
    if sid:
        out.append(sid)
    return out


def cleanup_orphan_cache(*, dry_run: bool = False,
                         grace_hours: float = CLEANUP_GRACE_HOURS) -> dict:
    """删除无任何引用的立绘/背景文件（M-05 / B-06：红线「先建引用集」的完整实现）。

    ## 引用集（构造位置就是本函数，字段见下）
      1) **会话资产**：`SESSIONS[*]["assets"][*][*]` 的 `file` / `raw_file`
      2) **立绘历程**：同一份快照的 `portrait_history[*]` 的 `file` / `raw_file`
      3) **全局存档槽**：`data/slots/*.json` 快照（同上两个字段）
      4) **存档**：`data/saves/*.json` 快照（同上两个字段）
      5) **CG 回廊引用**（此前完全缺失）：`data/cg/meta.json` 每条记录的
         `file`（历史条目里记的缓存相对路径）、`raw_file`，以及
         `sid` + (`id`|`cg_id`) → 该局 `cg_map[cg_id].key` →
         `assets.backgrounds[key].file`（CG 取图链的底图）
      6) **归档兜底**：`data/archives/*.zip` 内 `session.json` 声明的引用
      —— 引用一律规范成 `对局目录/子目录/文件名`（POSIX 分隔符）后比对。

    ## 删除白名单（**同时**满足才可能删）
      a) 位于 CACHE_DIR 之下且扩展名是图片；
      b) **不是根级文件**（坑 #14：根级旧哈希只做 `_legacy/` 备份 copy，禁删）；
      c) 二级目录 ∈ versions/portraits/backgrounds/raw/preview；
      d) 路径不在任何引用集里；
      e) `mtime` 早于 `now - grace_hours`（**宽限期内无论有无引用都不删**）；
      f) 所在对局目录**可归属**（是本轮加载到的会话／存档槽的对局目录）；
      g) 本轮引用集**完整**（没有任何读不出的 saves／slots／cg 清单）。

    ## 宁留不删（两条兜底）
      * 任一存档/清单读不出 → `aborted=True`，**本轮一个文件都不删**（报告里列出
        本来会被删的候选），因为「读不出」无法证明「无引用」；
      * 对局目录无法归属（存档被手动删除、损坏、或目录名不认识）→ 只报告不删。

    返回 `{"deleted": n, "report": 报告路径, ...计数, "aborted": bool}`；
    报告（候选／跳过原因／宽限跳过／实际删除）落盘 `DATA_DIR/cleanup-report.json`。
    """
    import json as _json
    import time as _time

    started = _time.time()
    now = started
    grace_s = max(0.0, float(grace_hours)) * 3600.0

    refs: dict[str, set[str]] = {k: set() for k in
                                 ("sessions", "history", "saves", "slots", "cg", "archives")}
    parse_failed: list[dict] = []
    known_dirs: set[str] = set()
    snap_by_sid: dict[str, dict] = {}

    def _norm(v) -> str:
        return str(v or "").replace("\\", "/").lstrip("/")

    def _take(d: dict, cat: str) -> None:
        """收集一份快照的全部引用（资产 + 立绘历程 + CG 映射）。"""
        if not isinstance(d, dict):
            return
        for group in (d.get("assets") or {}).values():
            if not isinstance(group, dict):
                continue
            for item in group.values():
                if not isinstance(item, dict):
                    continue
                for k in ("file", "raw_file"):
                    v = _norm(item.get(k))
                    if v:
                        refs[cat].add(v)
        for e in d.get("portrait_history") or []:
            if not isinstance(e, dict):
                continue
            for k in ("file", "raw_file"):
                v = _norm(e.get(k))
                if v:
                    refs["history"].add(v)
        bgs = (d.get("assets") or {}).get("backgrounds") or {}
        if not isinstance(bgs, dict):
            return
        for m in (d.get("cg_map") or {}).values():
            key = (m or {}).get("key") if isinstance(m, dict) else ""
            entry = bgs.get(key or "") or {}
            f = _norm(entry.get("file"))
            if f:
                refs["cg"].add(f)

    def _remember(d: dict) -> None:
        for name in _cleanup_session_dirs(d):
            if name:
                known_dirs.add(name)
        sid = str((d or {}).get("sid") or "")
        if sid and sid not in snap_by_sid:
            snap_by_sid[sid] = d

    # ---- 1) 内存会话 ----
    for s in list(SESSIONS.values()):
        _take(s, "sessions")
        _remember(s)
    # ---- 2/3) 存档 + 全局存档槽（读不出 → 记 parse_failed，本轮放弃删除）----
    for cat, d_ in (("saves", cfgmod.SAVES_DIR), ("slots", _slots_dir())):
        for f in sorted(Path(d_).glob("*.json")):
            try:
                snap = _json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(snap, dict):
                    raise ValueError("快照不是对象")
            except Exception as e:
                parse_failed.append({"source": cat, "file": f.name,
                                     "error": f"{type(e).__name__}: {e}"})
                continue
            _take(snap, cat)
            _remember(snap)
    # ---- 4) CG 回廊引用集（此前完全缺失）----
    cg_items: list = []
    try:
        raw_meta = cfgmod.CG_META_PATH.read_text(encoding="utf-8")
        cg_items = _json.loads(raw_meta)
        if not isinstance(cg_items, list):
            raise ValueError("CG 清单不是列表")
    except FileNotFoundError:
        cg_items = []
    except Exception as e:
        parse_failed.append({"source": "cg_meta", "file": cfgmod.CG_META_PATH.name,
                             "error": f"{type(e).__name__}: {e}"})
        cg_items = []
    for it in cg_items:
        if not isinstance(it, dict):
            continue
        f = _norm(it.get("file"))
        if f and "/" in f:
            refs["cg"].add(f)            # 历史条目：清单里直接记缓存相对路径
        raw = _norm(it.get("raw_file"))
        if raw:
            refs["cg"].add(raw)
        cid = str(it.get("id") or it.get("cg_id") or "")
        sid = str(it.get("sid") or "")
        snap = snap_by_sid.get(sid)
        if not (cid and snap):
            continue
        m = (snap.get("cg_map") or {}).get(cid) or {}
        key = (m or {}).get("key") if isinstance(m, dict) else ""
        entry = ((snap.get("assets") or {}).get("backgrounds") or {}).get(key or "") or {}
        f2 = _norm(entry.get("file"))
        if f2:
            refs["cg"].add(f2)
    # ---- 5) 归档兜底（zip 自包含；仅把包内声明的引用记入，避免误删导出源）----
    try:
        import zipfile as _zip
        for z in sorted((cfgmod.DATA_DIR / "archives").glob("*.zip")):
            try:
                with _zip.ZipFile(z) as zf:
                    info = zf.getinfo("session.json")
                    if info.file_size > 8 * 1024 * 1024:
                        continue
                    snap = _json.loads(zf.read("session.json").decode("utf-8"))
            except Exception:
                continue
            _take(snap, "archives")
    except Exception as e:
        log.warning("cleanup: archives refs skipped: %s", e)

    all_refs: set[str] = set()
    for v in refs.values():
        all_refs |= v

    aborted = bool(parse_failed)
    abort_reason = ("引用集不完整：存在读不出的存档/清单 —— 宁留不删，本轮不删除任何文件"
                    if aborted else "")

    # ---- 扫描与逐条判定（判定顺序即报告里的跳过原因）----
    deleted: list[dict] = []
    skipped: dict[str, list] = {k: [] for k in
                                ("grace", "unattributed", "root_protected", "legacy",
                                 "aborted", "other")}
    kept_referenced = 0
    scanned = 0
    bytes_freed = 0

    for f in cfgmod.CACHE_DIR.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        scanned += 1
        try:
            rel = f.relative_to(cfgmod.CACHE_DIR).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if rel in all_refs:
            kept_referenced += 1
            continue
        if len(parts) == 1:
            skipped["root_protected"].append(rel)     # 坑 #14：根级只备份不删
            continue
        if parts[0] == "_legacy":
            skipped["legacy"].append(rel)             # 旧哈希备份目录，只留不删
            continue
        if parts[-2] not in _CLEANUP_DELETABLE_SUBDIRS:
            skipped["other"].append(rel)
            continue
        try:
            st = f.stat()
            age = now - st.st_mtime
        except OSError as e:
            skipped["other"].append(f"{rel} (stat 失败: {e})")
            continue
        if age < grace_s:
            skipped["grace"].append({"rel": rel, "age_hours": round(age / 3600, 2)})
            continue
        if parts[0] not in known_dirs:
            skipped["unattributed"].append(rel)       # 无法证明无引用 → 宁留不删
            continue
        if aborted:
            skipped["aborted"].append(rel)            # 本轮引用集不完整 → 一个不删
            continue
        if dry_run:
            deleted.append({"rel": rel, "size": st.st_size,
                            "age_hours": round(age / 3600, 1), "dry_run": True})
            continue
        try:
            f.unlink()
        except OSError as e:
            skipped["other"].append(f"{rel} (unlink 失败: {e})")
            continue
        deleted.append({"rel": rel, "size": st.st_size,
                        "age_hours": round(age / 3600, 1)})
        bytes_freed += st.st_size

    report = {
        "version": 1,
        "generated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(_time.time() - started, 2),
        "cache_dir": str(cfgmod.CACHE_DIR),
        "dry_run": bool(dry_run),
        "grace_hours": float(grace_hours),
        "aborted": aborted,
        "abort_reason": abort_reason,
        "counts": {
            "scanned_images": scanned,
            "deleted": len(deleted),
            "kept_referenced": kept_referenced,
            "skipped_grace": len(skipped["grace"]),
            "skipped_unattributed": len(skipped["unattributed"]),
            "skipped_root_protected": len(skipped["root_protected"]),
            "skipped_legacy": len(skipped["legacy"]),
            "skipped_aborted": len(skipped["aborted"]),
            "skipped_other": len(skipped["other"]),
            "bytes_freed": bytes_freed,
        },
        "refs": dict({k: len(v) for k, v in refs.items()}, total=len(all_refs)),
        "known_session_dirs": sorted(known_dirs)[:80],
        "parse_failed": parse_failed,
        "deleted": deleted[:_CLEANUP_LIST_CAP],
        "deleted_truncated": max(0, len(deleted) - _CLEANUP_LIST_CAP),
        "skipped_samples": {k: v[:_CLEANUP_LIST_CAP] for k, v in skipped.items() if v},
    }
    report_path: Path | None = cfgmod.DATA_DIR / CLEANUP_REPORT_FILE
    try:
        cfgmod.DATA_DIR.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_json.dumps(report, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except OSError as e:
        log.warning("cleanup report write failed: %s", e)
        report_path = None
    log.info("cleanup: 扫描=%d 删除=%d 引用保留=%d 宽限跳过=%d 不可归属=%d "
             "根级保护=%d 本轮放弃=%s 报告=%s",
             scanned, len(deleted), kept_referenced, len(skipped["grace"]),
             len(skipped["unattributed"]), len(skipped["root_protected"]),
             "是" if aborted else "否", report_path or "(未落盘)")
    out = {"deleted": len(deleted),
           "report": str(report_path) if report_path else "",
           "aborted": aborted,
           "abort_reason": abort_reason}
    out.update(report["counts"])
    return out


def _apply_turn_stats(session: dict, turn: dict, mock: bool) -> dict:
    """应用本回合数值变化；TSF 稳定性：体质特征与性别同化率保持同步（差 ≤10）。

    同时应用 states_delta：各角色自己的情绪 / 好感 / 身体数值（每角色一套）。
    """
    from . import vn as vn_mod
    prot = session["protagonist"]
    delta = turn["stats_delta"]
    if mock and not delta:
        delta = dict(tsf.MOCK_DELTA)
        # 玩家自定义状态在演示模式下也缓慢增长，保证面板可见变化
        for c in session_stat_config(session):
            if c["key"] not in delta:
                delta[c["key"]] = 2
        turn["stats_delta"] = delta
    # 各角色数值：演示模式下每个角色都缓慢增长，保证面板可见变化
    char_delta = turn.get("states_delta") or {}
    if mock and not char_delta:
        keys = [c["key"] for c in vn_mod._char_stat_config(session)]
        char_delta = {n: {k: 1 for k in keys} for n in vn_mod.char_names(session)}
        turn["states_delta"] = char_delta
    vn_mod.apply_char_delta(session, char_delta)
    # 1.9.0 感情机制：缺席角色缓慢回归 + 本幕情感记忆/关系里程碑。
    # 必须在 states_delta 应用之后、scene 已确定时记录；开局幕不衰减。
    vn_mod.apply_absence_decay(session, turn.get("present") or [],
                               is_opening=not bool(session.get("log")))
    vn_mod.record_turn_emotions(session, turn)
    _stages = tsf.stages_for(session)
    before_stage = tsf.stage_of(prot["stats"]["progress"], _stages)
    before = dict(prot["stats"])
    prot["stats"] = tsf.apply_delta(prot["stats"], delta)
    # TSF 稳定约束：physique 向 progress 收敛（仅当用户保留了体质特征时才同步）
    pg = prot["stats"]["progress"]
    ph = prot["stats"].get("physique")
    if ph is not None and abs(ph - pg) > 10:
        prot["stats"]["physique"] = max(0, min(100, pg + (10 if ph > pg else -10)))
    after_stage = tsf.stage_of(prot["stats"]["progress"], _stages)
    if turn.get("identity"):
        prot["identity"] = turn["identity"]
    else:
        prot["identity"] = _stages[after_stage]["identity"]
    changed = {c["key"]: prot["stats"][c["key"]] - before[c["key"]]
               for c in session_stat_config(session)
               if prot["stats"][c["key"]] != before[c["key"]]}
    stage_up = after_stage != before_stage
    turn["stage_up"] = stage_up
    turn["stage"] = after_stage

    # 立绘更新频率（主角状态判定）：
    # stage   = 仅阶段跃迁时换立绘（1.7.28 起默认——「四段=四幅图」：
    #           用户要求阶段变化头发/身材都要明显放开，不做保守小步）
    # every10 = 同化率每跨 10% 换一次（小步链式=保守，只为柔过程可选）
    # every_turn = 每轮都换（最频繁）
    mode = cfgmod.load_config()["game"].get("portrait_update", "stage")
    need = stage_up
    if mode == "every10":
        need = need or (before["progress"] // 10 != prot["stats"]["progress"] // 10)
    elif mode == "every_turn":
        need = True
    if need:
        # 1.7.31 阶段跃迁=**全量生成**（generate_portrait 内建「3 种子轮换+
        # 组合质检」——软件内部自动挑精品种子；png 曲线实测：跨性别变化
        # 只能由全量完成，img2img（0.35-0.45）无法把中性底带向女性——
        # 六段实机 r70/r90/r100 仍柔男，实锤）。链式仅保留给换装/重画
        # 保守路径（用户规则）。stage_jump=stage_up（1.7.32：判定器随
        # 阶段跃迁启用；目标设定/重画路径不启）。
        _start_protagonist_portrait(session, force_fullgen=stage_up,
                                    stage_jump=stage_up)
    return {"changed": changed, "stage_up": stage_up, "stage": after_stage}


def _post_turn(session: dict, turn: dict) -> None:
    """轮次收尾：动态角色池入池 / 场景登记与复用 / 换装差分。"""
    # ---- 1) 动态角色池：AI 自主引入新角色（自动标记 + 完整性描写补全） ----
    pool = session["characters"]
    existing = {c["name"] for c in pool}
    prot_name = session["protagonist"]["name"]
    # 1.7.35 新角色硬校验（用户：无新角色时也在生成新角色=请求/幻觉问题）——
    # **只有真正出现在本幕 present 名单里的名字才入池+生成立绘**；
    # LLM 幻觉的 new_characters/new_names（不在场）一律丢弃只记日志
    present = set(turn.get("present") or [])
    candidates = []
    for c in turn.get("new_characters", []):
        if c.get("name") in present:
            candidates.append(c)
        else:
            log.info("skip phantom new_character %r (not in present)",
                     c.get("name", ""))
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
            log.warning("character pool full (10), ignoring %s", name)
            continue
        appearance = c.get("appearance", "").strip()
        if not appearance:
            # 自动补全完整性描写（保持成年人设定，避免生成失败）
            appearance = "成年角色，着装整洁，外貌待补充"
        entry = dict(c)
        entry["appearance"] = appearance
        entry["auto_added"] = True          # 标记：由 AI 自主引入
        entry["first_seen"] = turn.get("scene", "")
        pool.append(entry)
        existing.add(name)
        _start_npc_portrait(session, pool[-1])

    # ---- 2) 场景系统：场景登记/复用/角色出现统计 ----
    # 1.7.35 记录当前场景（制服自动映射依据：学园→校服/神社→巫女服…）
    session["current_scene"] = turn.get("scene", session.get("current_scene", ""))
    scenes = session.setdefault("scenes", {})
    scene_name = turn.get("scene", "……")
    scene_id = hashlib.sha256(scene_name.encode("utf-8")).hexdigest()[:10]
    if scene_id not in scenes:
        scenes[scene_id] = {
            "name": scene_name,
            "hint": turn.get("background_hint", ""),
            "bg_id": turn.get("background_id"),
            "roster": [],
            "visits": 0,
        }
    sc = scenes[scene_id]
    sc["visits"] += 1
    sc["bg_id"] = turn.get("background_id") or sc.get("bg_id")
    for who in turn.get("present", []):
        if who not in sc["roster"]:
            sc["roster"].append(who)   # 该场景出现过的人物统计
    turn["scene_id"] = scene_id
    turn["scene_visits"] = sc["visits"]
    turn["scene_roster"] = list(sc["roster"])

    # ---- 2.5) CG 桥段：关键情节生成 CG 背景 ----
    cg = turn.get("cg") or {}
    if cg.get("active"):
        turn["cg_id"] = _start_cg_task(session, cg)

    # ---- 3) 换装差分：服饰变化 → 生成新立绘（成人向模式下允许内衣/裸体） ----
    r18 = _is_r18(session)
    outfits = turn.get("outfit", {})
    for char in pool:
        key = f"{char['name']}|outfit"
        new_outfit = outfits.get(char["name"], "")
        if new_outfit and session.get(key) != new_outfit:
            if not r18 and any(k in new_outfit for k in ("裸体", "内衣", "内裤")):
                continue  # 非 18+ 保险：忽略违规换装声明
            session[key] = new_outfit
            _start_npc_portrait(session, char, outfit=new_outfit)
    prot_outfit = outfits.get(prot_name, "")
    if prot_outfit and session.get(f"{prot_name}|outfit") != prot_outfit:
        if r18 or not any(k in prot_outfit for k in ("裸体", "内衣", "内裤")):
            session[f"{prot_name}|outfit"] = prot_outfit
            _start_protagonist_portrait(session, outfit=prot_outfit)


# ---------- 对外流程 ----------



def content_policy_text(rating: str, r18: bool, intensity: str = "成熟",
                        allow_forced: bool = False) -> str:
    """内容分级政策（注入每轮剧情提示词）。默认全年龄，18+ 需显式开启 R18。

    intensity（仅 18+ 档生效）：温和 / 成熟 / 浓烈——档位越高，允许的成人向
    描写越充分；具体尺度交给剧情需要与角色状态自行调控，不做说教式规避。
    若开启「强制/支配向」（allow_forced，仅 18+）则额外放开成年角色之间
    支配、服从与强制类剧情（纯虚构设定，角色均为成年人）。
    """
    base = ("所有角色均为成年人；严禁与未成年人相关的不当内容、真实人物或违法情节。")
    if rating == "18" and r18:
        extra_forced = ("同时允许成年人角色之间支配、服从、强制与激烈身体冲突类情节"
                        "（纯虚构设定，仅限成年角色）；"
                        if allow_forced else "")
        if intensity == "温和":
            return ("本作为 18+（成人向·温和）：允许成年人之间的成熟情感与"
                    "含蓄亲密氛围（如依偎、低语、牵手与眼神交流），描写点到即止；"
                    + extra_forced) + base
        if intensity == "浓烈":
            return ("本作为 18+（成人向·浓烈）：允许成年人之间强烈的情感张力"
                    "与充分展开的身体亲密——包括性爱过程的具体、生动描写，"
                    "支配与服从关系，强力肢体接触与冲动性情节；"
                    "尺度由剧情走向与角色状态自行调控，不回避、不规避。"
                    + extra_forced) + base
        return ("本作为 18+ 内容（R18 情节已开启）：允许成年人之间成人向的"
                "亲密与情感场景——亲吻、抚摸、肌肤相触、共眠等成熟氛围的"
                "细腻描写，允许性成熟主题的生理与心理感受刻画；成年角色间的"
                "性爱描写可按剧情需要展开，具体尺度由剧情与角色状态自行调控。"
                + extra_forced) + base
    if rating == "16":
        return ("本作为 16+：允许成人向情感氛围与亲密暗示，点到即止，不做"
                "露骨展开；") + base
    return ("本作为全年龄：禁止任何性暗示、露骨与强烈成人向内容；"
            "所有角色均为成年人；描写保持含蓄克制。")


def update_policy(sid: str, rating: str, r18: bool,
                  intensity: str = "成熟", allow_forced: bool | None = None) -> dict:
    """中途切换内容分级与成人向强度（下一幕生效）。

    allow_forced=None 表示保留当前值（兼容旧调用方不提交该字段）。
    """
    session = get_session(sid)
    rating = str(rating).strip()[:8]
    if rating not in ("all", "16", "18"):
        rating = "all"
    if str(intensity).strip()[:4] not in ("温和", "成熟", "浓烈"):
        intensity = "成熟"
    session["content_rating"] = rating
    session["r18_enabled"] = bool(r18) and rating == "18"
    intensity = str(intensity).strip()[:4]
    if intensity not in ("温和", "成熟", "浓烈") or rating != "18":
        intensity = "成熟"
    session["adult_intensity"] = intensity
    # 强制/支配向仅 18+ R18 时生效；未显式提交则保留原值（旧调用不误关）
    if allow_forced is None:
        allow_forced = session.get("allow_forced", False)
    session["allow_forced"] = bool(allow_forced) and bool(session["r18_enabled"])
    persist(session)
    return {"content_rating": session["content_rating"],
            "r18_enabled": session["r18_enabled"],
            "adult_intensity": intensity,
            "allow_forced": session["allow_forced"]}



def tasks_view(session: dict) -> dict:
    """M-07（后端部分）：把这个对局「在排队 / 在跑什么」变成可观测。

    返回 pending 资产（含排队时长 elapsed）+ 资产计数 + SD 串行通道状态
    （谁在生成、谁在等待、各等多久）。**只读**：不改任何状态、不触发生成。
    """
    from . import image as _im
    now = time.time()
    queue = []
    counts = {"pending": 0, "ready": 0, "error": 0}
    for group, bucket in (session.get("assets") or {}).items():
        for key, item in (bucket or {}).items():
            st = item.get("status") or "?"
            if st in counts:
                counts[st] += 1
            if st != "pending":
                continue
            q0 = item.get("queued_at")
            queue.append({"group": group, "key": key,
                          "label": item.get("label", ""), "status": st,
                          "queued_at": q0,
                          "elapsed_s": round(now - q0, 1) if q0 else None})
    queue.sort(key=lambda x: x.get("queued_at") or 0)
    try:
        sd = _im.sd_state()
    except Exception as e:      # 观测不可用绝不能让接口挂掉
        sd = {"error": f"{type(e).__name__}: {e}"}
    return {"sid": session.get("sid", ""), "now": round(now, 3),
            "assets": counts, "queue_len": len(queue), "queue": queue[:20],
            "sd": sd}


def public_state(session: dict) -> dict:
    from . import vn as vn_mod      # 逐角色数值视图（两种模式共用）
    turn = session["log"][-1]["turn"]
    prot = session["protagonist"]
    stage_idx = tsf.stage_of(prot["stats"]["progress"],
                             tsf.stages_for(session))
    return {
        "sid": session["sid"],
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
        # 背景补生成可见性（t45）：预算耗尽后前端能直接看到"本场景背景永久不可用"
        # 与已尝试次数，而不是只看到一直转圈。字段是**新增**的，旧前端忽略即可。
        "bg_unavailable": bool(turn.get("bg_unavailable")),
        "bg_retry_attempts": _bg_retry_count(session, turn.get("scene")),
        "cg_active": bool(turn.get("cg_id")),
        "cg_id": turn.get("cg_id", ""),
        "cg_title": (turn.get("cg") or {}).get("title", ""),
        # 前端用 cg_key 判断「是否换了一张 CG」；旧代码恒为空串 → dataset.key
        # 永远与空串相等，img 从不 set src，CG 永远不显示。这里返回真实 key。
        "cg_key": ((session.get("cg_map") or {}).get(turn.get("cg_id")) or {}).get("key", ""),
        "characters_meta": [
            {"name": c["name"], "role": c.get("role", ""),
             "background": c.get("background", ""),
             "auto_added": bool(c.get("auto_added"))}
            for c in session["characters"]
        ],
        # 每个角色自己的情绪 / 好感 / 身体数值（含主角，与主角转变数值并存）
        "char_stats": vn_mod.panel_view_chars(session),
        "catchphrases": session.get("catchphrases", []),
        "outfits": {
            char["name"]: session.get(f"{char['name']}|outfit", "")
            for char in session["characters"]
        } | ({session["protagonist"]["name"]:
              session.get(f"{session['protagonist']['name']}|outfit", "")}
             if True else {}),
        "content_rating": session.get("content_rating", "all"),
        "r18_enabled": bool(session.get("r18_enabled")),
        "adult_intensity": session.get("adult_intensity", "成熟"),
        "allow_forced": bool(session.get("allow_forced")),
        "cg_saved": bool(turn.get("cg_saved")),
        "cmd_cg": bool(turn.get("cmd_cg")),
        "cmd_cg_saved": bool(turn.get("cmd_cg_saved")),
        "llm_degraded": bool(session.get("llm_degraded")),
        "protagonist": prot["name"],
        "npcs": [c["name"] for c in session["characters"]],
        "tsf": tsf.panel_view(prot["stats"], prot["identity"], stage_idx,
                              session_stat_config(session),
                              tsf.stages_for(session)),
        "stat_config": session_stat_config(session),
        "uniform_en": session.get("uniform_en", ""),
        "uniform_neg": session.get("uniform_neg", ""),
        "uniform_name": session.get("uniform_name", ""),
        # 1.9.0：本幕关系升级（前端 toast 用；无升级时为空数组）
        "bond_up": list(turn.get("bond_up") or []),
        "stage_up": bool(turn.get("stage_up")),
        "assets": session["assets"],
        "bg_map": session["bg_map"],
        "cg_url": _cg_url(session, turn),
        "turn_no": len(session["log"]),
        "mock": session["mock"],
        # 1.9.20：AI 控制台用（导演指令；控制台记录走独立端点）
        "ai_directive": str(session.get("ai_directive") or ""),
    }


def public_state_of(session: dict) -> dict:
    """按会话模式返回前端状态：普通模式用自己的状态视图（TSF 专用视图
    没有 stats，普通模式会话会被 KeyError 击穿）。"""
    if session.get("mode") == "vn":
        from . import vn as vn_mod
        return vn_mod.public_state(session)
    return public_state(session)


def _clean_edited_dialogue(session: dict, dialogue: Any) -> list[dict]:
    """校验并清洗玩家手动修改后的对话（不重跑 LLM、不改数值）。"""
    known = {"旁白", session["protagonist"]["name"]}
    known |= {c["name"] for c in session.get("characters", [])}
    emotions = set(session.get("emotions") or [])
    out: list[dict] = []
    for d in (dialogue or [])[:6]:
        if not isinstance(d, dict):
            continue
        text = str(d.get("text") or "").strip()
        if not text:
            continue
        who = str(d.get("character") or "旁白").strip() or "旁白"
        if who not in known:
            raise GameError(f"未知角色：{who}")
        emo = str(d.get("emotion") or "neutral").strip() or "neutral"
        if emotions and emo not in emotions:
            emo = "neutral"
        out.append({"character": who, "emotion": emo, "text": text[:800]})
    if not out:
        raise GameError("对话不能为空")
    return out


def edit_dialogue(sid: str, dialogue: Any, choices: Any = None) -> dict:
    """手动修改当前幕的 AI 对话（可选同时改选项文本）。

    这是**玩家直接编辑表现层**，不重新请求模型、不改任何数值/好感/状态；
    下一轮 prompt 会把修改后的 assistant 历史原样带回，因此后续剧情会据此衔接。
    """
    session = get_session(sid)
    if not session.get("log"):
        raise GameError("当前没有可编辑的对话")
    turn = session["log"][-1]["turn"]
    turn["dialogue"] = _clean_edited_dialogue(session, dialogue)
    if choices is not None:
        cleaned = []
        for c in (choices or [])[:4]:
            if not isinstance(c, dict):
                continue
            text = str(c.get("text") or "").strip()[:60]
            if not text:
                continue
            cleaned.append({
                "text": text,
                "effect": str(c.get("effect") or "").strip()[:40],
                "bias": str(c.get("bias") or "").strip()[:20],
            })
        if cleaned:
            turn["choices"] = cleaned
    turn["edited_by_user"] = True
    persist(session)
    return public_state_of(session)


def set_ai_directive(sid: str, text: str) -> str:
    session = get_session(sid)
    session["ai_directive"] = str(text or "").strip()[:500]
    persist(session)
    return session["ai_directive"]


def history_view(session: dict) -> list[dict]:
    """给前端的历史回看数据。"""
    out = []
    for e in session["log"]:
        out.append({
            "scene": e["turn"]["scene"],
            "dialogue": e["turn"]["dialogue"],
            "choice": e["choice"]["text"] if e["choice"] else None,
            "identity": None,
        })
    return out


async def random_setup() -> dict:
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return MockSetup.random()
    # 1.9.14：旧实现只发一句 user 消息、没有 system / JSON schema，模型经常
    # 回散文或带解释的 JSON → 前端拿到空 world，开局直接报「请先填写世界观」。
    # 这里复用 prompts 里本来就有、却一直没接上的 TSF_RANDOM_SETUP_SYSTEM；
    # 模型仍失败时退本地随机设定，保证「🎲 随机生成设定」永远可用。
    try:
        data = await llm.chat_json([
            {"role": "system", "content": prompts.TSF_RANDOM_SETUP_SYSTEM},
            {"role": "user", "content": "请生成一个全新的 TSF 题材开局设定。"},
        ], temperature=1.0)
        if (not isinstance(data, dict)
                or not str(data.get("world") or "").strip()
                or not isinstance(data.get("characters"), list)
                or not data.get("characters")):
            raise LLMError("随机设定缺少 world/characters")
        return data
    except LLMError as e:
        log.warning("random_setup 失败，回退本地随机设定：%s", e)
        return MockSetup.random()


async def generate_outline(world: str, pname: str, characters: list[dict]) -> dict:
    """用 AI 生成故事大纲（mock 返回内置大纲）。"""
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return {
            "title": "潮汐之形",
            "outline": (
                "◆ 平静的日常\n主角是普通的上班族，过着规律而平淡的生活，"
                "一次旧货市场的偶然相遇改变了这一切。\n"
                "◆ 转变的契机\n一件来历不明的古董怀表开始在夜晚发出微光，"
                "主角的身体出现了无法解释的细微变化，寻访卖家却线索寥寥。\n"
                "◆ 陌生的自己\n变化逐月加深，主角在否认、恐慌与隐秘的好奇之间摇摆，"
                "身边人的目光也随之改变。\n"
                "◆ 抉择之时\n解开怀表之谜的方法浮出水面：彻底完成转变，或付出代价逆转。"
                "主角必须回答——自己究竟想成为谁。\n"
                "◆ 新生\n无论选择哪条路，主角都要以自己的答案面对镜中的新面貌，"
                "以及一直守望在旁的人们。"
            ),
        }
    char_desc = "\n".join(f"- {c.get('name', '?')}：{c.get('personality', '')}"
                          for c in characters) or "（无）"
    result = await llm.chat_json([
        {"role": "system", "content": prompts.TSF_OUTLINE_SYSTEM},
        {"role": "user",
         "content": f"【世界观】{world}\n【主角】{pname}\n【其他角色】\n{char_desc}"},
    ], temperature=0.9)
    if not isinstance(result, dict) or not result.get("outline"):
        raise GameError("生成的大纲格式异常，请重试")
    return result


async def start_game(payload: dict) -> dict:
    from . import vn as vn_mod      # 逐角色数值初始化（两种模式共用）
    world = str(payload.get("world", "")).strip()
    characters = payload.get("characters") or []
    prot_in = payload.get("protagonist") or {}
    pname = str(prot_in.get("name", "")).strip()[:12] or "主角"
    anchor = str(prot_in.get("anchor", "")).strip()[:200]
    outline = str(payload.get("outline", "")).strip()[:2000]
    catchphrases = [str(x).strip()[:60] for x in (payload.get("catchphrases") or [])
                    if str(x).strip()][:5]
    content_rating = str(payload.get("content_rating", "18")).strip()[:8]
    if content_rating not in ("all", "16", "18"):
        content_rating = "18"
    r18_enabled = bool(payload.get("r18_enabled", True)) and content_rating == "18"
    adult_intensity = str(payload.get("adult_intensity", "浓烈")).strip()[:4]
    if adult_intensity not in ("温和", "成熟", "浓烈"):
        adult_intensity = "浓烈"
    allow_forced = bool(payload.get("allow_forced", True)) and r18_enabled
    lorebook = [
        {
            "keys": str(e.get("keys", ""))[:100],
            "content": str(e.get("content", ""))[:500],
            "always": bool(e.get("always")),
        }
        for e in (payload.get("lorebook") or [])
        if str(e.get("content", "")).strip()
    ]
    # 1.9.0：角色卡统一清洗（role/background 也随开局落库），去重名/主角同名。
    characters = charm.normalize_character_list(
        characters, limit=4, protagonist=pname)
    if not world:
        raise GameError("请先填写世界观/剧情设定（或点「随机生成设定」）")

    stat_cfg = tsf.merge_stat_config(payload.get("stat_config"), tsf.STAT_META,
                                     ["progress"])
    # 本局统一制服关键词（会话级：同局所有角色同款；留空回退全局 config，
    # 跨局互不干扰）
    uniform_en = str(payload.get("uniform_en", "") or "").strip()[:200]
    uniform_neg = str(payload.get("uniform_neg", "") or "").strip()[:200]
    uniform_name = str(payload.get("uniform_name", "") or "").strip()[:40]

    cfg = cfgmod.load_config()
    emotions = cfg["game"].get("emotions", ["neutral", "happy", "shy", "sad"])
    # 1.7.29：开局按 config stage_count（4/6/10，默认 6——六段制：
    # 0-20/20-40/40-60/60-80/80-99/100；四段保留可切）确定阶段表并随档持久
    try:
        _stage_n = int(payload.get("stage_count") or
                       cfg["game"].get("stage_count", 6))
    except (KeyError, TypeError, ValueError):
        _stage_n = 6
    if _stage_n not in (4, 6, 10):
        _stage_n = 6
    _stages0 = tsf.build_stages(_stage_n)
    session = {
        "sid": uuid.uuid4().hex[:12],
        "title": str(payload.get("title", "")).strip() or "未命名之物语",
        "world": world,
        "outline": outline,
        "lorebook": lorebook,
        "catchphrases": catchphrases,
        "content_rating": content_rating,
        "r18_enabled": r18_enabled,
        "adult_intensity": adult_intensity,
        "allow_forced": allow_forced,
        "directives": merge_directives(payload.get("directives") or []),
        "characters": characters,
        "protagonist": {
            "name": pname,
            "anchor": anchor or "黑色短发，深色眼瞳，日常便装",
            "stats": tsf.initial_stats(stat_cfg),
            "identity": _stages0[0]["identity"],
        },
        "stages": _stages0,   # 1.7.25：随档阶段表（4/6/10 段）
        "stat_config": stat_cfg,
        "char_states": {},    # 每角色一套情绪/好感/身体数值（含主角），建会话后填充
        "uniform_en": uniform_en,
        "uniform_neg": uniform_neg,
        "uniform_name": uniform_name,
        # 1.9.0：开局先按世界观推断一个场景，让「学园→校服 / 神社→巫女服」
        # 的制服映射在**开场立绘**就生效（第一幕返回真实 scene 后覆盖）。
        "current_scene": scenemod.guess_initial_scene(world, outline,
                                                       str(payload.get("title", ""))),
        "stage_count": _stage_n,
        "schema": 2,
        # 1.7.16：主角「转变目标设定」（开局可预置；游戏内可随时改）
        "tsf_target": _norm_tsf_target(payload.get("tsf_target") or {}),
        "prot_handle": f"{pname}_{uuid.uuid4().hex[:4]}",   # 主角固定随机文档名
        "emotions": emotions,
        "summary": "",
        # 1.9.20：AI 控制台 / 玩家导演指令（随存档持久；读档后仍可查看与修改）
        "ai_console": [],
        "ai_directive": "",
        "log": [],
        "assets": {"portraits": {}, "backgrounds": {}},
        "bg_map": {},
        # 1.7.34 二周目：压缩后的前情提要（build_story_system 注入世界观）
        "summary": str(payload.get("prior_summary", "") or "")[:800],
        "bg_count": 0,
        "mock": {"llm": LLMClient(cfg).mock, "image": ImageClient(cfg).mock},
        "created": time.time(),
    }
    SESSIONS[session["sid"]] = session
    vn_mod.ensure_char_states(session)   # 每个角色一套情绪/好感/身体数值（含主角）

    # 角色库导入：已有立绘直接复用（生成管线自动跳过匹配变体，缺的照常生成）
    inject_library_assets(session, payload.get("library_imports") or [], cfg)

    try:
        # 1) 主角阶段立绘 + NPC 表情立绘并行启动，不阻塞剧情
        _start_protagonist_portrait(session)
        for c in characters:
            _start_npc_portrait(session, c)
        # 2) 开场剧情（LLM 失败自动降级演示剧情，绝不卡在设定页）
        llm = LLMClient(cfg, session=session, label="开场")
        messages = [{"role": "system", "content": build_story_system(session)},
                    {"role": "user",
                     "content": "请生成游戏的开场剧情段落（第一段 dialogue 之前可以有一两句话交代时间地点与主角的初始状态）。"}]
        try:
            turn_raw = await llm.chat_json(messages, temperature=0.8)
        except LLMError as e:
            log.warning("LLM API unavailable (%s), degraded to demo story", e)
            session["llm_degraded"] = True
            llm = LLMClient({"llm": {"base_url": "", "api_key": ""}},
                            session=session, label="开场（演示回退）")  # 强制 mock
            turn_raw = await llm.chat_json(messages, temperature=0.8)
        if llm.mock:
            _mock_localize(turn_raw, pname)
        turn = _sanitize_turn(turn_raw, emotions, characters, pname,
                              stat_keys=stat_keys_of(session))
        _apply_turn_stats(session, turn, mock=llm.mock)
        _assign_scene_bg(session, turn)
        _post_turn(session, turn)
        session["log"].append({"turn": turn, "choice": None})
        persist(session)
        return public_state(session)
    except LLMError as e:
        SESSIONS.pop(session["sid"], None)
        raise GameError(str(e)) from e


async def advance(sid: str, choice_index: int) -> dict:
    session = get_session(sid)
    current = session["log"][-1]
    choices = current["turn"]["choices"]
    if choice_index < 0 or choice_index >= len(choices):
        raise GameError("无效的选项")
    choice = choices[choice_index]
    current["choice"] = choice

    try:
        await _maybe_compress(session)
        llm = LLMClient(cfgmod.load_config(), session=session, label="剧情推进")
        turn_raw = await llm.chat_json(build_messages(session), temperature=0.85)
    except LLMError as e:
        log.warning("LLM API unavailable (%s), this turn degraded to demo", e)
        session["llm_degraded"] = True
        llm = LLMClient({"llm": {"base_url": "", "api_key": ""}},
                        session=session, label="剧情推进（演示回退）")
        turn_raw = await llm.chat_json(build_messages(session), temperature=0.85)

    if llm.mock:
        _mock_localize(turn_raw, session["protagonist"]["name"])
    _rename = _clean_rename(turn_raw)
    turn = _sanitize_turn(turn_raw, session["emotions"], session["characters"],
                          session["protagonist"]["name"], rename_hint=_rename,
                          stat_keys=stat_keys_of(session))
    _apply_rename(session, _rename)
    _apply_turn_stats(session, turn, mock=llm.mock)
    prev_hint = session["log"][-1]["turn"].get("background_hint", "")
    _assign_scene_bg(session, turn)
    turn["background_hint"] = turn["background_hint"] or prev_hint
    _post_turn(session, turn)
    session["log"].append({"turn": turn, "choice": None})
    persist(session)
    return public_state(session)


async def speak(sid: str, text: str) -> dict:
    """玩家说一句固定话语：不进选项列表，直接作为本轮行动注入。"""
    session = get_session(sid)
    text = text.strip()[:200]
    if not text:
        raise GameError("话语内容为空")
    session["log"][-1]["choice"] = {"text": f"💬 {text}", "effect": "固定话语",
                                    "bias": ""}
    try:
        await _maybe_compress(session)
        llm = LLMClient(cfgmod.load_config(), session=session, label="固定话语")
        turn_raw = await llm.chat_json(build_messages(session), temperature=0.85)
    except LLMError as e:
        session["log"][-1]["choice"] = None
        raise GameError(str(e)) from e
    _rename = _clean_rename(turn_raw)
    turn = _sanitize_turn(turn_raw, session["emotions"], session["characters"],
                          session["protagonist"]["name"], rename_hint=_rename,
                          stat_keys=stat_keys_of(session))
    _apply_rename(session, _rename)
    _apply_turn_stats(session, turn, mock=llm.mock)
    prev_hint = session["log"][-1]["turn"].get("background_hint", "")
    _assign_scene_bg(session, turn)
    turn["background_hint"] = turn["background_hint"] or prev_hint
    _post_turn(session, turn)
    session["log"].append({"turn": turn, "choice": None})
    persist(session)
    return public_state(session)





# ---------- 命令 / 换装 / CG 收藏 ----------

# 命令面板：action -> (显示名, 给 LLM 的命令描述模板, 换装 key 或 None, stats_delta)
COMMAND_ACTIONS = {
    "outfit_casual": ("更衣 · 便服", "命令 {name} 换上休闲便服", "casual", {}),
    "outfit_underwear": ("更衣 · 内衣", "命令 {name} 脱到只剩内衣内裤", "underwear", {}),
    "outfit_nude": ("更衣 · 裸体", "命令 {name} 脱光全部衣物，全身赤裸", "nude", {}),
    "outfit_default": ("换回默认", "命令 {name} 换回设定决定的初始着装", "default", {}),
    "kiss": ("强吻", "命令 {name} 过来，然后强吻她", None, {"habit": 1}),
    "hug": ("强行拥抱", "命令 {name} 不许反抗，强行将她搂进怀里不许挣脱",
            None, {"habit": 1}),
    "sex": ("侵犯", "命令 {name} 服从，与之发生激烈而绵长的性行为", None,
            {"genital": 4, "immersion": 3, "habit": 2}),
    "blowjob": ("口交", "命令 {name} 俯身侍奉口交", None,
                {"genital": 2, "immersion": 2, "habit": 2}),
}

OUTFIT_KEYS = {"default", "casual", "underwear", "nude"}
OUTFIT_CN = {"default": "初始着装", "casual": "便服", "underwear": "内衣",
             "nude": "裸体"}


def _is_r18(session: dict) -> bool:
    return (session.get("content_rating") == "18"
            and bool(session.get("r18_enabled")))


def _apply_outfit_now(session: dict, name: str, outfit: str) -> None:
    """立即换装（更衣差分立绘异步生成），返回前只更新会话状态。"""
    session[f"{name}|outfit"] = outfit
    prot = session["protagonist"]
    if name == prot["name"]:
        _start_protagonist_portrait(session, outfit=outfit)
        return
    npc = next((c for c in session["characters"] if c["name"] == name), None)
    if npc:
        _start_npc_portrait(session, npc, outfit=outfit)


def _fallback_command_turn(pname: str, name: str, desc: str,
                           scene: str = "命令之间") -> dict:
    """LLM 不可用时的命令剧情兜底（保证命令永不卡死）。"""
    return {
        "scene": scene,
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
            {"text": "继续加深这次体验", "effect": "推进", "bias": "immersion"},
            {"text": "命令她做下一步", "effect": "继续命令", "bias": ""},
            {"text": "停下，回到平静", "effect": "克制", "bias": ""},
        ],
        "cg": {"active": True,
               "title": f"命令 · {name}",
               "prompt": (f"室内暧昧的光线中，{name}服从命令的瞬间，"
                          "电影感广角构图，氛围张力拉满")},
        "stats_delta": {},
        "identity": pname,
    }


async def apply_command(sid: str, character: str, action: str) -> dict:
    """下达命令：更衣立即生效；强制动作/更衣场面一律标记为 CG 自动收藏。"""
    from . import vn as vn_mod      # 命令对角色自身数值的影响（两种模式共用一套系数）
    session = get_session(sid)
    known = {c["name"] for c in session["characters"]} | {session["protagonist"]["name"]}
    if character not in known:
        raise GameError("命令目标角色不存在")
    if action not in COMMAND_ACTIONS:
        raise GameError("未知的命令")
    action_cn, desc_tpl, outfit, action_delta = COMMAND_ACTIONS[action]
    r18 = _is_r18(session)
    if not r18 and outfit in ("underwear", "nude"):
        raise GameError("该更衣命令仅限 18+ R18 模式使用")
    force_actions = {"kiss", "hug", "sex", "blowjob"}
    if action in force_actions and not (r18 and session.get("allow_forced")):
        raise GameError("该命令需在 18+ 模式下开启「允许强制/支配内容」")

    desc = desc_tpl.format(name=character)
    outfit_changed = outfit is not None
    if outfit_changed:
        _apply_outfit_now(session, character, OUTFIT_CN.get(outfit, outfit))

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

    try:
        llm = LLMClient(cfgmod.load_config(), session=session, label="命令")
        turn_raw = await llm.chat_json(messages, temperature=0.9)
        _rename = _clean_rename(turn_raw)
        turn = _sanitize_turn(turn_raw, session["emotions"],
                              session["characters"], session["protagonist"]["name"],
                              rename_hint=_rename, stat_keys=stat_keys_of(session))
    except (LLMError, ValueError) as e:
        log.warning("command story failed (%s), use fallback turn", e)
        _rename = ""
        turn = _sanitize_turn(
            _fallback_command_turn(session["protagonist"]["name"], character, desc),
            session["emotions"], session["characters"],
            session["protagonist"]["name"], stat_keys=stat_keys_of(session))
    _apply_rename(session, _rename)

    # 命令场面强制作为 CG 收藏；数值影响合并进本幕变化
    turn["cg"]["active"] = True
    if not turn["cg"].get("title"):
        turn["cg"]["title"] = f"命令 · {action_cn}"
    if not turn["cg"].get("prompt"):
        turn["cg"]["prompt"] = (f"室内暧昧光线中，{character}服从命令的瞬间，"
                                "电影感广角构图，氛围张力拉满")
    for k, v in action_delta.items():
        turn["stats_delta"][k] = turn["stats_delta"].get(k, 0) + v
    # 命令对【被下令角色】自己的情绪/好感/身体数值的影响（叠加在 LLM 给的变化上）
    cmd_char_delta = vn_mod.VN_COMMAND_DELTA.get(action, {})
    if cmd_char_delta:
        entry = turn["states_delta"].setdefault(character, {})
        for k, v in cmd_char_delta.items():
            entry[k] = max(-15, min(15, entry.get(k, 0) + v))

    _apply_turn_stats(session, turn, mock=False)
    prev_hint = session["log"][-1]["turn"].get("background_hint", "")
    _assign_scene_bg(session, turn)
    turn["background_hint"] = turn["background_hint"] or prev_hint
    _post_turn(session, turn)
    turn["cmd_cg"] = True
    session["log"].append({"turn": turn, "choice": None})
    persist(session)
    # 命令场面立即合成一张场景 CG 收藏（不依赖 LLM 生成的桥段图）
    # B-05/M-02：命令场面的 CG 合成是 CPU 密集（1600×900 画布 + 多张全尺寸
    # 图 resize + 文字绘制），必须丢线程池——原来在 apply_command（async）
    # 里同步跑，合成期间事件循环被独占。
    await asyncio.to_thread(save_command_cg, session, turn, f"命令·{action_cn}")
    return public_state(session)


def apply_outfit(sid: str, character: str, outfit: str) -> dict:
    """游戏内手动换装（更衣差分立绘后台生成，本幕直接生效）。"""
    session = get_session(sid)
    known = {c["name"] for c in session["characters"]} | {session["protagonist"]["name"]}
    if character not in known:
        raise GameError("角色不存在")
    outfit = (outfit or "").strip()[:40]
    if not outfit:
        raise GameError("请填写服装")
    if outfit in OUTFIT_KEYS and outfit in ("underwear", "nude") and not _is_r18(session):
        raise GameError("内衣/裸体着装仅限 18+ R18 模式使用")
    if outfit in OUTFIT_KEYS:
        outfit = OUTFIT_CN[outfit]
    _apply_outfit_now(session, character, outfit)
    persist(session)
    return public_state(session)


def save_command_cg(session: dict, turn: dict, note: str) -> None:
    """命令场面的场景 CG（背景+立绘合成，立即可用，防重复）。"""
    if turn.get("cmd_cg_saved"):
        return
    try:
        cgbook.compose_scene(session, turn, cg_type="command", note=note)
        turn["cmd_cg_saved"] = True
    except Exception as e:  # CG 收藏失败不中断游戏
        log.warning("command cg save failed: %s", e)


def save_cg(sid: str, cg_type: str = "manual", note: str = "") -> dict:
    """收藏 CG：bridge=剧情生成的高清桥段图；scene/manual=当前画面合成图。"""
    session = get_session(sid)
    turn = session["log"][-1]["turn"]
    note = (note or "").strip()[:60]
    if cg_type == "bridge":
        if not turn.get("cg_id"):
            raise GameError("当前幕没有桥段 CG")
        if turn.get("cg_saved"):
            return {"already": True}
        item = cgbook.record_bridge(session, turn, note=note)
        turn["cg_saved"] = True
        persist(session)
        return item
    if cg_type == "command":
        if turn.get("cmd_cg"):
            if turn.get("cmd_cg_saved"):
                return {"already": True}
            save_command_cg(session, turn, note or "命令场面")
            return {"ok": True, "already": False}
        raise GameError("当前幕不是命令场面")
    # 手动收藏：合成当前画面
    item = cgbook.compose_scene(session, turn, cg_type="manual", note=note)
    return item


def _save_cg_if_bridge(session: dict, key: str, rel: str) -> None:
    """资产生成完成时，若是桥段 CG 就把成品图落盘进 CG 库（永久保存）。

    桥段 CG 只登记在缓存里的话，缓存一清（或会话不在内存）图库就打不开，
    所以在生成完成这一刻就复制进 data/cg/，与缓存解耦。
    """
    cid = _cg_id_of_key(session, key)
    if not cid:
        return
    try:
        from . import cgbook as cgbook_mod
        cgbook_mod.save_bridge_image(cid, rel)
    except Exception as e:      # 收藏失败绝不能影响图片资产本身
        log.warning("cg library save skipped (%s): %s", cid, e)


def _cg_id_of_key(session: dict, key: str) -> str:
    """按资源键反查桥段 CG 编号（生成完成时据此把图落盘进 CG 库）。"""
    for cid, meta in (session.get("cg_map") or {}).items():
        if meta.get("key") == key:
            return cid
    return ""


def resolve_cg_file(sid: str, cg_id: str) -> str:
    """桥段 CG 的可用文件名（CG 库内、不带路径）；必要时把缓存成品落盘。

    顺序：CG 库里已落盘 → 在会话缓存里找到并落盘 → 空（仍在生成或已丢失）。
    直接返回缓存相对路径是不行的：/cg/<文件名> 只服务 CG 目录、且不接受
    带斜杠的路径，那样图库里会是打不开的破图。
    """
    from . import cgbook as cgbook_mod
    saved = cgbook_mod.saved_file(cg_id)
    if saved:
        return saved
    s = SESSIONS.get(sid)
    if not s:
        return ""
    meta = (s.get("cg_map") or {}).get(cg_id)
    if not meta:
        return ""
    entry = (s.get("assets", {}).get("backgrounds", {}) or {}).get(meta["key"])
    if entry and entry.get("status") == "ready" and entry.get("file"):
        return cgbook_mod.save_bridge_image(cg_id, entry["file"])
    return ""


# ---------- 全局存档库（固定位置 data/slots/，与会话无关） ----------

def _slots_dir() -> Path:
    d = cfgmod.DATA_DIR / "slots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slot_id_ok(slot_id: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", slot_id))


def list_slots(sid: str = "") -> list[dict]:
    """列出全局存档（与会话无关：重启/换局后依然可读）。"""
    out = []
    for f in sorted(_slots_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append({
                "slot_id": f.stem,
                "name": data.get("slot_name", f.stem),
                "title": data.get("title", ""),
                "turn_no": len(data.get("log", [])),
                "created": data.get("slot_created", 0),
                "sid": data.get("sid", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda x: x.get("created", 0), reverse=True)
    return out


def save_slot(sid: str, name: str) -> dict:
    """把当前会话完整快照存入全局存档库（固定位置）。"""
    session = get_session(sid)
    # 1.9.4：毫秒级 + 碰撞后缀。旧版用秒级 slot{int(time.time())}，快速连存
    # 会覆盖上一份（用户看到的「刚存的档不见了」/存档乱跳的诱因之一）。
    base = int(time.time() * 1000)
    slot_id = f"slot{base}"
    n = 1
    while (_slots_dir() / f"{slot_id}.json").exists():
        slot_id = f"slot{base}{n}"
        n += 1
    snapshot = json.loads(json.dumps(session, ensure_ascii=False))
    snapshot["slot_name"] = (name or "存档").strip()[:30]
    snapshot["slot_created"] = time.time()
    cfgmod.atomic_write_json(_slots_dir() / f"{slot_id}.json", snapshot)
    return {"slot_id": slot_id, "name": snapshot["slot_name"],
            "turn_no": len(snapshot.get("log", []))}


def load_slot(slot_id: str) -> dict:
    """从全局存档库恢复会话（无需当前会话存在）。"""
    if not _slot_id_ok(slot_id):
        raise GameError("非法存档槽")
    f = _slots_dir() / f"{slot_id}.json"
    if not f.is_file():
        raise GameError("存档不存在")
    try:
        snapshot = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise GameError(f"存档损坏：{e}")
    sid = snapshot.get("sid") or uuid.uuid4().hex[:12]
    snapshot["sid"] = sid
    # 旧存档缺新字段时补齐
    if "protagonist" in snapshot and "stats" in snapshot["protagonist"]:
        for k, meta in tsf.STAT_META.items():
            snapshot["protagonist"]["stats"].setdefault(k, meta["start"])
    snapshot.setdefault("catchphrases", [])
    snapshot.setdefault("content_rating", "all")
    snapshot.setdefault("r18_enabled", False)
    snapshot.setdefault("adult_intensity", "成熟")
    snapshot.setdefault("allow_forced", False)
    snapshot.setdefault("scenes", {})
    snapshot.setdefault("outfits", {})
    # 读档即恢复「上次中断的 pending 资产」+ 校正路径 + 重置场景补生成预算
    _recover_interrupted_assets(snapshot)
    SESSIONS[sid] = snapshot
    persist(snapshot)
    ensure_scene_bg(snapshot)   # 读档：当前幕背景不可用时立即补生成
    if snapshot.get("mode") == "vn":
        from . import vn as _vn
        return _vn.public_state(snapshot)
    return public_state(snapshot)


def load_slot_raw(slot_id: str) -> dict:
    """裸读存档槽快照（1.7.34，二周目用）：不注册 SESSIONS/不触发背景
    任务，也不做 public_state 转换——new_game_plus 需要完整 session 结构
    （world/characters/scenes/…；load_slot 的返回值是展示态，会缺字段）。"""
    if not _slot_id_ok(slot_id):
        raise GameError("非法存档槽")
    f = _slots_dir() / f"{slot_id}.json"
    if not f.is_file():
        raise GameError("存档不存在")
    try:
        snapshot = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise GameError(f"存档损坏：{e}")
    snapshot["sid"] = snapshot.get("sid") or uuid.uuid4().hex[:12]
    _recover_interrupted_assets(snapshot)
    return snapshot


def delete_slot(slot_id: str) -> bool:
    if not _slot_id_ok(slot_id):
        return False
    f = _slots_dir() / f"{slot_id}.json"
    if not f.is_file():
        return False
    try:
        f.unlink()
        return True
    except OSError:
        return False


def migrate_legacy_slots() -> int:
    """把旧版「{sid}_slots」目录迁移到全局存档库（一次性）。"""
    moved = 0
    for d in cfgmod.SAVES_DIR.glob("*_slots"):
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                new_id = f"migrated_{d.name.split('_slots')[0][:8]}_{f.stem}"
                target = _slots_dir() / f"{new_id}.json"
                if not target.is_file():
                    data.setdefault("slot_name", f"旧存档·{f.stem}")
                    cfgmod.atomic_write_json(target, data)
                    moved += 1
            except (json.JSONDecodeError, OSError):
                continue
    return moved




def debug_action(sid: str, action: str, stat: str = "",
                 value: int = 0, character: str = "", outfit: str = "") -> dict:
    """隐藏调试接口（本地调试面板用）：跳阶段/调数值/强制换装/重画全部。

    仅白名单动作；只读或重建资产，不删除任何存档。
    """
    session = get_session(sid)
    prot = session["protagonist"]
    if action == "next_stage":
        _stages = tsf.stages_for(session)
        cur = tsf.stage_of(prot["stats"]["progress"], _stages)
        prot["stats"]["progress"] = (_stages[cur + 1]["min"]
                                     if cur + 1 < len(_stages) else 100)
    elif action == "set_stat":
        if stat not in stat_keys_of(session):
            raise GameError(f"未知数值：{stat}")
        before_stage = tsf.stage_of(prot["stats"]["progress"],
                                    tsf.stages_for(session))
        prot["stats"][stat] = max(0, min(100, int(value)))
        # 1.7.31 实机跳级=走「阶段跃迁」游戏流程（全量+内部种子轮换质检；
        # 非外部挑选种子），供实机测试/调试六段变化
        if stat == "progress" and tsf.stage_of(
                prot["stats"]["progress"], tsf.stages_for(session)) != before_stage:
            _start_protagonist_portrait(session, force_fullgen=True)
    elif action == "force_outfit":
        outfit = (outfit or "").strip()[:60]
        if not outfit:
            raise GameError("请填写服饰")
        if character and character != prot["name"]:
            npc = next((c for c in session["characters"] if c["name"] == character), None)
            if not npc:
                raise GameError(f"角色不存在：{character}")
            session[f"{character}|outfit"] = outfit
            _start_npc_portrait(session, npc, outfit=outfit)
        else:
            session[f"{prot['name']}|outfit"] = outfit
            _start_protagonist_portrait(session, outfit=outfit)
    elif action == "regen_all":
        _start_protagonist_portrait(session, nonce=1)
        for c in session["characters"]:
            _start_npc_portrait(session, c, nonce=1)
    else:
        raise GameError("未知调试动作")
    _apply_turn_stats(session, {"stats_delta": {}, "identity": ""}, mock=False)
    persist(session)
    return public_state(session)



CUT_MODES = ("ai", "precise", "standard", "fast")
CUT_MODE_CN = {"ai": "AI 智能", "precise": "严格", "standard": "保守", "fast": "快速白底"}


def _portrait_items_of(session: dict, character: str = ""):
    """按角色名（空=全部）筛可重抠的立绘条目。"""
    for item in session["assets"]["portraits"].values():
        if item.get("status") != "ready" or not item.get("raw_file"):
            continue
        if character and not item.get("label", "").startswith(character + "·"):
            continue
        yield item


def _apply_cut_to_item(item: dict, mode: str) -> None:
    """把指定抠图模式的结果写到该立绘成品文件。"""
    import io as _io
    from PIL import Image as _Img
    from .image import cut_out, frame_for_display, edge_cleanup
    raw_path = cfgmod.CACHE_DIR / item["raw_file"]
    img = _Img.open(raw_path)
    img = cut_out(img, mode)
    img = frame_for_display(img)
    img = edge_cleanup(img, shrink=1, feather=0.8)
    img.save(cfgmod.CACHE_DIR / item["file"], format="PNG")


def recut_portraits(sid: str, character: str = "") -> dict:
    """用当前抠图模式对全部（或指定角色）立绘重新抠图（覆盖成品）。"""
    session = get_session(sid)
    mode = _cutout_mode()
    done = 0
    for item in _portrait_items_of(session, character):
        try:
            _apply_cut_to_item(item, mode)
            done += 1
        except OSError as e:
            log.warning("重抠 %s 失败：%s", item.get("label"), e)
    persist(session)
    return public_state_of(session)


def cut_previews(sid: str, character: str) -> dict:
    """为指定角色生成 4 种抠图方案的预览图（取该角色第一张立绘的原图）。"""
    from PIL import Image as _Img
    from .image import cut_out, frame_for_display, edge_cleanup, asset_key
    session = get_session(sid)
    prot_name = session["protagonist"]["name"]
    items = list(_portrait_items_of(session, character or None))
    if not items:
        raise GameError("该角色没有可重抠的立绘（需要 raw 原图）")
    raw_path = cfgmod.CACHE_DIR / items[0]["raw_file"]
    if not raw_path.is_file():
        raise GameError("原始图丢失，请先重新生成立绘")
    img = _Img.open(raw_path)
    out = []
    dir_name = _session_dir_name(session)
    sid_dir = cfgmod.CACHE_DIR / dir_name / "preview"
    sid_dir.mkdir(parents=True, exist_ok=True)
    for mode in CUT_MODES:
        import io as _io
        base_p = f"抠图预览·{mode}"
        seq = _reserve_file_name(session, base_p)
        try:
            cut = cut_out(img, mode)
            cut = frame_for_display(cut)
            cut = edge_cleanup(cut, shrink=1, feather=0.8)
            canvas = _Img.new("RGB", cut.size, (80, 80, 96))
            canvas.paste(cut, (0, 0), cut)
            buf = _io.BytesIO()
            canvas.save(buf, "JPEG", quality=88)
            data = buf.getvalue()
            fname = _finish_file_name(base_p, seq, data, ext="jpg")
            rel = f"{dir_name}/preview/{fname}"
            (sid_dir / fname).write_bytes(data)
            out.append({"mode": mode, "name": CUT_MODE_CN.get(mode, mode),
                        "file": rel})
        except Exception as e:
            log.warning("抠图预览 %s 失败：%s", mode, e)
    if not out:
        raise GameError("全部抠图方案生成失败")
    return {"character": character or prot_name, "previews": out}


def apply_cut(sid: str, character: str, mode: str) -> dict:
    """把选定方案应用到该角色全部立绘。"""
    session = get_session(sid)
    if mode not in CUT_MODES:
        raise GameError("未知抠图方案")
    done = 0
    for item in _portrait_items_of(session, character or None):
        try:
            _apply_cut_to_item(item, mode)
            done += 1
        except OSError as e:
            log.warning("应用抠图 %s 失败：%s", item.get("label"), e)
    if not done:
        raise GameError("该角色没有可重抠的立绘")
    persist(session)
    return {"ok": True, "applied": done, "mode": mode, "character": character}


def restore_raw_portraits(sid: str) -> dict:
    """把立绘还原为「去除背景之前」的原始图（直接展示未抠图版本）。"""
    import shutil as _sh
    session = get_session(sid)
    done = 0
    for item in session["assets"]["portraits"].values():
        if item.get("status") != "ready" or not item.get("raw_file"):
            continue
        raw_path = cfgmod.CACHE_DIR / item["raw_file"]
        if not raw_path.is_file():
            continue
        _sh.copy2(raw_path, cfgmod.CACHE_DIR / item["file"])
        done += 1
    persist(session)
    return public_state_of(session)



def scene_summary(sid: str) -> list[dict]:
    """场景清单（调试用）：名称/访问次数/出场名册/背景文件。"""
    session = get_session(sid)
    out = []
    for sc_id, sc in (session.get("scenes") or {}).items():
        bg_file = ""
        bg_id = sc.get("bg_id")
        if bg_id:
            key = session.get("bg_map", {}).get(bg_id)
            entry = (session.get("assets", {}).get("backgrounds", {}) or {}).get(key or "")
            if entry:
                bg_file = entry.get("file", "") or ("（生成中）" if entry.get("status") == "pending" else "")
        out.append({
            "scene_id": sc_id,
            "name": sc.get("name", ""),
            "visits": sc.get("visits", 0),
            "roster": sc.get("roster", []),
            "bg_file": bg_file,
        })
    out.sort(key=lambda s: -s["visits"])
    return out



def _cg_url(session: dict, turn: dict) -> str:
    """CG 主图 URL（就绪后返回 /img 路径，未就绪为空由轮询补）。"""
    cg_id = turn.get("cg_id")
    if not cg_id:
        return ""
    meta = (session.get("cg_map") or {}).get(cg_id)
    if not meta:
        return ""
    entry = (session.get("assets", {}).get("backgrounds", {}) or {}).get(meta["key"])
    if entry and entry.get("status") == "ready" and entry.get("file"):
        return f"/img/{entry['file']}"
    return ""

def scene_set(sid: str, name: str, action: str = "enter") -> dict:
    """「读取场景」：进入已有/更换场景（复用或生成对应背景）或强制重生成
    当前场景背景。TSF/VN 通用；返回前端状态供立即重绘。

    B-03：调用方（POST /api/game/{sid}/scene-set）已在事件循环上，背景任务
    由 _spawn_task 真正排入循环；任何失败都回滚内存态（bg_id / scenes /
    bg_count），不再把内存改坏且只留下 500。"""
    session = get_session(sid)
    turn = session["log"][-1]["turn"]
    snap = _bg_state_snapshot(session, turn)

    def scene_entry(scn: str) -> tuple[str, dict]:
        import hashlib as _hl
        scene_id = _hl.sha256(scn.encode("utf-8")).hexdigest()[:10]
        scenes = session.setdefault("scenes", {})
        sc = scenes.setdefault(scene_id, {"name": scn, "hint": "",
                                          "bg_id": None, "roster": [],
                                          "visits": 0})
        sc["name"] = scn
        return scene_id, sc

    try:
        if action == "rerender":
            _, sc = scene_entry(turn.get("scene", "……"))
            old_key = session.get("bg_map", {}).get(sc.get("bg_id", ""))
            bgs = session.get("assets", {}).get("backgrounds", {}) or {}
            if old_key and old_key in bgs:
                bgs[old_key]["status"] = "error"
                bgs[old_key]["error"] = "手动重生成场景背景"
            sc.pop("bg_id", None)
            _assign_scene_bg(session, turn)
            _, sc = scene_entry(turn.get("scene", "……"))
            sc["bg_id"] = turn.get("background_id") or sc.get("bg_id")
            persist(session)
            return public_state_of(session)
        name = (name or "").strip()[:30]
        if not name:
            raise GameError("请输入场景名")
        turn["scene"] = name
        if not turn.get("background_hint"):
            turn["background_hint"] = name
        _assign_scene_bg(session, turn)   # 已有场景记录 → 复用其固定背景
        _, sc = scene_entry(name)
        sc["bg_id"] = turn.get("background_id") or sc.get("bg_id")
        persist(session)
        return public_state_of(session)
    except GameError:
        _bg_state_restore(session, turn, snap)   # 早退（空场景名）等价 no-op
        raise
    except Exception as e:
        _bg_state_restore(session, turn, snap)
        log.warning("scene_set failed sid=%s action=%s name=%s: %s: %s",
                    sid, action, name, type(e).__name__, e)
        raise GameError(f"场景切换失败：{e}") from e


def delete_session(sid: str) -> None:
    SESSIONS.pop(sid, None)
    try:
        _save_path(sid).unlink(missing_ok=True)
    except OSError:
        pass


def get_directives(sid: str) -> list[dict]:
    session = get_session(sid)
    if "directives" not in session:
        session["directives"] = initial_directives()
    return session["directives"]


def set_directives(sid: str, incoming: list) -> list[dict]:
    """更新指令勾选与自定义条目。预置指令只允许改勾选；自定义条目整体校验替换。"""
    session = get_session(sid)
    session["directives"] = merge_directives(incoming)
    persist(session)
    return session["directives"]

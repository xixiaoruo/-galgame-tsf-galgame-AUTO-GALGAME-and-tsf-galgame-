"""DeepSeek 一致性检查（1.9.21）。

用户诉求：「增加 DeepSeek 一致性检查」——把一局已经写出来的剧情交给本机
配置的 LLM（默认 DeepSeek `deepseek-chat`）做一次**设定一致性审计**，输出
结构化问题清单，并可一键写进「导演指令」让下一轮自行修正。

设计要点：
· 独立调用：只发「设定 + 最近若干幕」的紧凑证据包，不把整局历史塞进上下文
  （既省 token，也避免模型被长文淹没后开始编造问题）。
· 不伪造：模型不可用/返回不合法时抛 `ConsistencyError`，界面明确提示；
  绝不返回伪造的「一切正常」。
· 只读游戏状态：本模块不改数值、不改剧情，只写 `session["consistency"]`
  与（可选的）导演指令。
· 纯函数拆分（证据包 / 归一化 / 修复指令）便于离线单测。
"""
from __future__ import annotations

import json
import time

from . import config as cfgmod
from . import prompts as promptmod
from .llm import LLMClient, LLMError

CATEGORIES = (
    "人物设定", "关系与好感", "时间线", "数值与状态", "场景与背景",
    "服饰与外形", "人称与代词", "前后矛盾", "重复与套路",
)
SEVERITIES = ("low", "medium", "high")
MAX_ISSUES = 12
MAX_EVIDENCE_TURNS = 16


class ConsistencyError(Exception):
    """检查无法进行（未配置 LLM / 接口失败 / 返回不可用）。"""


def _clip(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _char_brief(c: dict) -> dict:
    """角色卡里「硬设定」字段（一致性检查的判据来源）。"""
    locks = [str(x) for x in (c.get("locked_fields") or []) if str(x)]
    if c.get("personality_locked") and "personality" not in locks:
        locks.append("personality")
    out = {
        "name": _clip(c.get("name"), 20),
        "role": _clip(c.get("role"), 40),
        "personality": _clip(c.get("personality"), 120),
        "appearance": _clip(c.get("appearance"), 200),
        "traits": [str(x)[:12] for x in (c.get("traits") or []) if str(x)][:8],
    }
    for key, limit in (("age", 3), ("height_cm", 4), ("body_type", 20),
                       ("speech_style", 60), ("likes", 60), ("dislikes", 60)):
        if c.get(key):
            out[key] = _clip(c.get(key), limit)
    if locks:
        out["locked_fields"] = locks
    return out


def _turn_brief(entry: dict, index: int) -> dict:
    turn = entry.get("turn") if isinstance(entry, dict) else None
    if not isinstance(turn, dict):
        return {"index": index, "note": "（该条不是标准剧情回合，已跳过）"}
    dialogue = []
    for d in (turn.get("dialogue") or [])[:8]:
        if not isinstance(d, dict):
            continue
        dialogue.append({
            "who": _clip(d.get("character"), 16),
            "text": _clip(d.get("text"), 150),
        })
    choice = entry.get("choice") or {}
    return {
        "index": index,
        "scene": _clip(turn.get("scene"), 40),
        "background": _clip(turn.get("background_hint"), 60),
        "outfit": {str(k)[:20]: _clip(v, 40)
                   for k, v in (turn.get("outfit") or {}).items()} if isinstance(
                       turn.get("outfit"), dict) else {},
        "dialogue": dialogue,
        "player_choice": _clip(choice.get("text") if isinstance(choice, dict) else "", 60),
        "cg": _clip((turn.get("cg") or {}).get("title"), 40),
    }


def build_evidence(session: dict, turns: int = 12) -> dict:
    """构造紧凑证据包：设定（判据）+ 最近 N 幕（被检查对象）。"""
    prot = session.get("protagonist") or {}
    log = session.get("log") or []
    turns = max(1, min(MAX_EVIDENCE_TURNS, int(turns or 12)))
    recent = log[-turns:]
    first_index = max(1, len(log) - len(recent) + 1)
    stats = prot.get("stats") or {}
    evidence = {
        "mode": _clip(session.get("mode") or session.get("kind") or "tsf", 12),
        "title": _clip(session.get("title"), 60),
        "world": _clip(session.get("world"), 700),
        "outline": _clip(session.get("outline"), 700),
        "protagonist": {
            "name": _clip(prot.get("name"), 20),
            "anchor": _clip(prot.get("anchor"), 300),
            "identity": _clip(prot.get("identity"), 120),
            "outfit": _clip(session.get(f"{prot.get('name', '')}|outfit"), 40)
            or "初始着装",
            "stats": {str(k): v for k, v in list(stats.items())[:12]},
        },
        "characters": [_char_brief(c) for c in (session.get("characters") or [])][:8],
        "director_directive": _clip(session.get("ai_directive"), 300),
        "writing_style": _clip(session.get("writing_style"), 30),
        "turn_total": len(log),
        "turns_checked": f"第 {first_index} ~ {len(log)} 幕",
        "turns": [_turn_brief(e, first_index + i) for i, e in enumerate(recent)],
    }
    # 场景清单：用于「同一地点被写成不同名字」这类一致性判断
    scenes = []
    for e in log[-40:]:
        t = e.get("turn") if isinstance(e, dict) else None
        if isinstance(t, dict) and t.get("scene"):
            name = _clip(t.get("scene"), 40)
            if name and name not in scenes:
                scenes.append(name)
    evidence["scenes_seen"] = scenes[:20]
    return evidence


def _normalize_issue(raw: dict, idx: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    problem = _clip(raw.get("problem"), 300)
    if not problem:
        return None
    sev = str(raw.get("severity") or "medium").lower()
    cat = str(raw.get("category") or "").strip()
    try:
        turn = int(raw.get("turn"))
        turn = turn if turn > 0 else None
    except (TypeError, ValueError):
        turn = None
    return {
        "id": f"I{idx + 1:02d}",
        "severity": sev if sev in SEVERITIES else "medium",
        "category": cat if cat in CATEGORIES else "前后矛盾",
        "turn": turn,
        "problem": problem,
        "evidence": _clip(raw.get("evidence"), 200),
        "fix": _clip(raw.get("fix"), 200),
    }


def normalize_report(raw, model: str = "", elapsed: float = 0.0) -> dict:
    """把模型输出归一化成稳定结构（缺字段/类型错也不崩）。"""
    if not isinstance(raw, dict):
        raise ConsistencyError("模型没有返回 JSON 对象")
    issues = []
    for i, it in enumerate(raw.get("issues") or []):
        if len(issues) >= MAX_ISSUES:
            break
        norm = _normalize_issue(it, len(issues))
        if norm:
            issues.append(norm)
    try:
        score = int(raw.get("score"))
    except (TypeError, ValueError):
        score = 0
    if score <= 0:
        # 没有分数时按严重度折算（不谎报满分）
        ded = sum({"low": 4, "medium": 9, "high": 16}[i["severity"]]
                  for i in issues)
        score = max(20, 100 - ded)
    score = max(0, min(100, score))
    return {
        "ok": True,
        "score": score,
        "summary": _clip(raw.get("summary"), 400) or (
            "未发现明显设定冲突。" if not issues else "发现以下一致性问题。"),
        "issues": issues,
        "counts": {
            "high": sum(1 for i in issues if i["severity"] == "high"),
            "medium": sum(1 for i in issues if i["severity"] == "medium"),
            "low": sum(1 for i in issues if i["severity"] == "low"),
        },
        "model": model,
        "elapsed": round(float(elapsed), 2),
    }


def _llm_ready(cfg: dict) -> bool:
    llm = cfg.get("llm") or {}
    key = str(llm.get("api_key") or "")
    return bool(llm.get("base_url")) and bool(key) and "填入" not in key


async def check(session: dict, mode: str = "recent", turns: int = 12,
                focus: str = "") -> dict:
    """跑一次一致性检查，写入 `session["consistency"]` 并返回报告。"""
    cfg = cfgmod.load_config()
    if not _llm_ready(cfg):
        raise ConsistencyError("尚未配置可用的对话模型（设置里填写 DeepSeek API Key 后再试）")
    if mode not in ("recent", "full"):
        mode = "recent"
    if mode == "full":
        turns = MAX_EVIDENCE_TURNS
    evidence = build_evidence(session, turns)
    focus = _clip(focus, 200)
    if focus:
        evidence["player_focus"] = focus
    user_txt = json.dumps(evidence, ensure_ascii=False)
    messages = [
        {"role": "system", "content": promptmod.CONSISTENCY_SYSTEM},
        {"role": "user", "content": user_txt},
    ]
    started = time.time()
    llm = LLMClient(cfg, session=session, label="一致性检查")
    if llm.mock:                      # 双保险：__init__ 的 mock 判定更严格时直接报错
        raise ConsistencyError("对话模型处于演示模式，无法做一致性检查")
    try:
        raw = await llm.chat_json(messages, temperature=0.1)
    except LLMError as e:
        raise ConsistencyError(f"一致性检查调用失败：{e}") from e
    report = normalize_report(raw, model=str(cfg["llm"].get("model") or ""),
                             elapsed=time.time() - started)
    report["mode"] = mode
    report["turns_requested"] = turns
    report["at"] = int(time.time())
    session["consistency"] = report
    return report


def auto_turns(session: dict) -> int:
    """自动检查间隔（幕）；0 = 关闭。"""
    try:
        n = int(session.get("consistency_auto") or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(50, n))


def auto_due(session: dict) -> bool:
    n = auto_turns(session)
    if n <= 0:
        return False
    log = session.get("log") or []
    return len(log) > 0 and len(log) % n == 0


def apply_fixes(session: dict, report: dict | None = None,
                limit: int = 3, max_len: int = 500) -> str:
    """把问题清单里的「修复建议」写进导演指令（去重、限长）。"""
    rep = report if isinstance(report, dict) else (session.get("consistency") or {})
    issues = [i for i in (rep.get("issues") or [])
              if isinstance(i, dict) and str(i.get("fix") or "").strip()]
    issues.sort(key=lambda i: {"high": 0, "medium": 1, "low": 2}.get(
        str(i.get("severity")), 1))
    lines = []
    for i in issues[:max(0, int(limit))]:
        tag = {"high": "高", "medium": "中", "low": "低"}.get(
            str(i.get("severity")), "中")
        lines.append(f"[一致性·{tag}] {_clip(i.get('problem'), 60)} → "
                     f"{_clip(i.get('fix'), 120)}")
    if not lines:
        return str(session.get("ai_directive") or "")
    cur = str(session.get("ai_directive") or "").strip()
    addition = "\n".join(lines)
    if addition in cur:
        return cur
    merged = (cur + "\n" + addition).strip() if cur else addition
    merged = merged[-max_len:]
    session["ai_directive"] = merged
    return merged


def view(session: dict) -> dict:
    """给前端的视图（报告 + 自动检查配置）。"""
    rep = session.get("consistency")
    return {
        "report": rep if isinstance(rep, dict) else None,
        "auto_turns": auto_turns(session),
        "categories": list(CATEGORIES),
    }

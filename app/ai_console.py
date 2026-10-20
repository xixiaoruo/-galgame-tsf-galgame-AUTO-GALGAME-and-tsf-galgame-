"""AI 控制台：把每一次主剧情 LLM 调用的请求 / 思考 / 原始输出留痕。

设计目标（用户 1.9.20 要求）：
  · 能看到 AI 这一轮**收到了什么**、**想了什么**（模型返回 reasoning_content 时）、
    **原始说了什么**、解析成了什么；
  · 能直接改当前幕的 AI 对话；
  · 能给 AI 一个持续的「导演指令」，控制下一轮往哪写。

边界：
  · 只记录**主剧情调用**（开场 / 推进 / 发言 / 命令 / CoC 叙述），不记录
    摘要、大纲、生图提示词等辅助调用；
  · 不记录 API Key；不记录图片二进制；
  · 每条消息 / 原始回复都截断，避免存档被控制台日志撑爆；
  · 全部是进程内会话数据，随 `persist(session)` 落盘，读档可继续查看。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

#: 每个会话最多保留多少条调用记录
MAX_ENTRIES = 30
#: 单条消息 / 原始回复的截断长度
MAX_TEXT = 12000

_SUFFIX = "\n…（控制台显示已截断）"


def _cut(text: Any, limit: int = MAX_TEXT) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + _SUFFIX


def _messages(messages: Any) -> list[dict]:
    out = []
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        out.append({
            "role": str(m.get("role") or ""),
            "content": _cut(m.get("content")),
        })
    return out


def _store(session: dict | None) -> list[dict]:
    if not isinstance(session, dict):
        return []
    rows = session.get("ai_console")
    if not isinstance(rows, list):
        rows = []
        session["ai_console"] = rows
    return rows


def begin(session: dict | None, messages: Any, *, model: str = "",
          temperature: float | None = None, mock: bool = False,
          label: str = "") -> dict | None:
    """记录一次请求的开始；返回 entry（无 session 时返回 None）。"""
    rows = _store(session)
    if not rows and not isinstance(session, dict):
        return None
    now = time.time()
    entry = {
        "id": uuid.uuid4().hex[:10],
        "ts": now,
        "time": time.strftime("%H:%M:%S", time.localtime(now)),
        "label": str(label or "剧情生成"),
        "model": str(model or ""),
        "temperature": temperature,
        "mock": bool(mock),
        "status": "pending",
        "duration_ms": 0,
        "messages": _messages(messages),
        "reasoning": "",
        "raw": "",
        "usage": {},
        "parse": None,
        "error": "",
    }
    rows.append(entry)
    # 只保留最近 MAX_ENTRIES 条；旧记录从头部丢弃
    if len(rows) > MAX_ENTRIES:
        del rows[:-MAX_ENTRIES]
    return entry


def finish(session: dict | None, entry: dict | None, raw: Any, *,
           reasoning: Any = "", usage: Any = None,
           duration_ms: int = 0) -> None:
    if not isinstance(entry, dict):
        return
    entry["status"] = "ok"
    entry["raw"] = _cut(raw, MAX_TEXT * 2)
    entry["reasoning"] = _cut(reasoning)
    entry["usage"] = dict(usage) if isinstance(usage, dict) else {}
    entry["duration_ms"] = int(duration_ms)
    entry["error"] = ""
    _store(session)


def fail(session: dict | None, entry: dict | None, error: Any,
         duration_ms: int = 0) -> None:
    if not isinstance(entry, dict):
        return
    entry["status"] = "error"
    entry["duration_ms"] = int(duration_ms)
    entry["error"] = _cut(error, 2000)
    _store(session)


def note_parse(session: dict | None, entry: dict | None, ok: bool,
               error: Any = "") -> None:
    if not isinstance(entry, dict):
        return
    entry["parse"] = {"ok": bool(ok), "error": _cut(error, 800) if not ok else ""}
    _store(session)


def entries(session: dict | None, limit: int = MAX_ENTRIES) -> list[dict]:
    rows = _store(session)
    # 新→旧；返回副本，避免端点调用方改动内部状态
    return [dict(x) for x in rows[::-1][:max(1, int(limit or MAX_ENTRIES))]]


def clear(session: dict | None) -> None:
    if isinstance(session, dict):
        session["ai_console"] = []


def get_directive(session: dict | None) -> str:
    if not isinstance(session, dict):
        return ""
    return str(session.get("ai_directive") or "")


def set_directive(session: dict | None, text: Any) -> str:
    s = str(text or "").strip()[:500]
    if isinstance(session, dict):
        session["ai_directive"] = s
    return s

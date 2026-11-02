"""写作风格 / 自定义提示词预设库（1.9.21「更多提示词支持」）。

玩法：玩家在「AI 控制台 → 提示词」里选一种写作风格，并可以写一段自己的
提示词；两者都会持续注入 TSF / 普通 Galgame 的系统提示词（在导演指令之后），
下一轮生成生效。设计约束：

· **默认（default）零影响**：`style_prompt("default") == ""`，不选自定义提示词
  时 `session_prompt_block()` 返回空串——与改动前逐字节一致。
· 只追加、不替换：不覆盖现有的分级政策 / 写作技法 / 大纲，避免两套规则打架。
· 纯数据 + 纯函数：不读写文件、不调用 LLM，便于单测与前端直接展示。
"""
from __future__ import annotations

import re

# 单条自定义提示词上限（与导演指令同量级，防止把系统提示词刷爆）
MAX_CUSTOM = 600
MAX_STYLE_TEXT = 400

# 写作风格预设：prompt 会作为【写作风格】注入；为空 = 默认（不加任何词）
WRITING_STYLES: list[dict] = [
    {
        "id": "default", "name": "默认（原版）", "icon": "🎭",
        "note": "不追加任何风格词，完全按默认写作技法",
        "prompt": "",
    },
    {
        "id": "literary", "name": "文学细腻", "icon": "📖",
        "note": "短句、具体意象、少形容词堆叠",
        "prompt": ("叙事偏文学化：多用具体可感的物件与动作承载情绪，少用抽象形容词；"
                   "句子长短交错，避免排比与总结句；每幕至少一个可被看见的细节"
                   "（光线、气味、触感、衣料摩擦声）。"),
    },
    {
        "id": "cozy", "name": "日常治愈", "icon": "🍵",
        "note": "生活流、轻冲突、温度感",
        "prompt": ("叙事偏日常治愈：节奏放慢，重视吃饭/走路/换衣/闲聊这类生活细节；"
                   "冲突以误会与小别扭为主，不制造生死级压力；对白自然口语化，"
                   "允许沉默与停顿，结尾落在让人安心的小动作上。"),
    },
    {
        "id": "suspense", "name": "悬疑张力", "icon": "🕯",
        "note": "信息差、伏笔、压迫感",
        "prompt": ("叙事偏悬疑：每幕有意保留一个未解释的信息（谁在看、那件东西为什么在这里）；"
                   "用环境细节制造不安（太安静、气味不对、时间对不上）；"
                   "不提前揭晓谜底，玩家选择应改变信息的获取顺序。"),
    },
    {
        "id": "comedy", "name": "轻喜剧", "icon": "😂",
        "note": "节奏快、误解、吐槽",
        "prompt": ("叙事偏轻喜剧：误会与身份错位是主要笑点来源，节奏明快；"
                   "允许角色心里吐槽与夸张反应，但不要破坏人物设定的底线；"
                   "每幕至少一个「意料之外、情理之中」的小反转。"),
    },
    {
        "id": "sensory", "name": "感官沉浸", "icon": "🫧",
        "note": "五感 + 身体变化细节",
        "prompt": ("叙事偏感官沉浸：身体变化必须落到五感（布料贴合度、皮肤触感、"
                   "声音变化、体温、重心），不要只写「变美了/更女性化」；"
                   "环境气味与光温要随场景变化，让玩家能「感到」空间。"),
    },
    {
        "id": "plain", "name": "白描克制", "icon": "🖋",
        "note": "少修辞、冷峻、留白",
        "prompt": ("叙事偏白描：几乎不用比喻与感叹，只写动作、对白与可见事实；"
                   "情绪靠留白，不替角色解释心理；句子以短句为主，克制抒情。"),
    },
    {
        "id": "dramatic", "name": "戏剧张力", "icon": "🎬",
        "note": "冲突前置、台词锋利",
        "prompt": ("叙事偏戏剧化：每幕开场就进入冲突或选择，不铺陈背景；"
                   "台词锋利、有潜台词，角色之间互相试探；"
                   "幕尾停在最紧张的一刻，把决断留给玩家。"),
    },
]

_BY_ID = {s["id"]: s for s in WRITING_STYLES}


def list_styles() -> list[dict]:
    """给前端的干净副本。"""
    return [{"id": s["id"], "name": s["name"], "icon": s.get("icon", "🎭"),
             "note": s.get("note", ""), "prompt": s.get("prompt", "")[:MAX_STYLE_TEXT]}
            for s in WRITING_STYLES]


def get_style(style_id: str) -> dict | None:
    return _BY_ID.get(str(style_id or "").strip())


def style_prompt(style_id: str) -> str:
    s = get_style(style_id)
    if not s:
        return ""
    return str(s.get("prompt") or "").strip()[:MAX_STYLE_TEXT]


def clean_custom(text: str) -> str:
    """清掉多余空行与首尾空白，限长。"""
    t = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t[:MAX_CUSTOM]


def session_prompt_block(session: dict) -> str:
    """把「写作风格 + 自定义提示词」拼成追加到系统提示词末尾的块。

    默认风格 + 无自定义 → 返回 ""（保证默认行为零变化）。
    """
    style_id = str((session or {}).get("writing_style") or "default")
    style = style_prompt(style_id)
    custom = clean_custom((session or {}).get("custom_prompt") or "")
    if not style and not custom:
        return ""
    parts = ["\n\n【写作风格（本局持续生效；不覆盖内容分级政策与数值结算）】"]
    if style:
        parts.append(style)
    if custom:
        parts.append("【玩家自定义提示词（玩家直接写的要求，优先级高于默认风格）】\n"
                     + custom)
    return "\n".join(parts)

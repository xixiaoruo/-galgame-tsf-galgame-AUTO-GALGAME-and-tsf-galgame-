"""角色卡辅助：随机角色、自动谱写角色背景（1.9.0）。

面板里的角色卡字段（name / role / appearance / personality / background）
在这里有唯一一套清洗规则，避免开局、存档、角色库、AI 新角色四条链路
各写一份长度限制而互相漂移。

「自动谱写背景」优先用已配置的 LLM；没有 API / 请求失败 / mock 模式时
退回本地模板——功能永远可用，不会因为网络问题卡住开局页。
"""
from __future__ import annotations

import asyncio
import random
import re

from . import config as cfgmod
from .llm import LLMClient, LLMError

MAX_NAME = 12
MAX_ROLE = 20
MAX_APPEARANCE = 300
MAX_PERSONALITY = 200
MAX_BACKGROUND = 400
MAX_SPEECH = 100
MAX_BODY = 30
MAX_MEASURE = 40
MAX_BIRTHDAY = 20
MAX_LIKES = 100
MAX_DISLIKES = 100
MAX_TRAIT = 16
MAX_TRAITS = 8

# 可被「固定」的字段。固定性格会连带固定 traits / speech_style。
LOCKABLE_FIELDS = (
    "role", "appearance", "personality", "traits", "age", "height_cm",
    "body_type", "measurements", "birthday", "speech_style",
    "likes", "dislikes", "background",
)
PERSONALITY_LOCKS = ("personality", "traits", "speech_style")

# 日式/中文混合的常用名字池（随机角色用；不追求现实户籍统计）
_SURNAMES = (
    "佐藤", "铃木", "高桥", "田中", "伊藤", "渡边", "山本", "中村",
    "小林", "加藤", "吉田", "山田", "佐佐木", "山口", "松本", "井上",
    "木村", "林", "清水", "山崎", "森", "池田", "桥本", "阿部",
    "石川", "前田", "藤田", "小川", "冈田", "长谷川", "村上", "近藤",
)
_GIVEN = (
    "美咲", "优子", "莉奈", "千夏", "绫濑", "澪", "结衣", "纱希",
    "真央", "花音", "栞", "凛", "葵", "遥", "诗织", "菜月",
    "千鹤", "由纪", "麻衣", "琴音", "小春", "夏帆", "瑞希", "爱莉",
    "悠真", "健太", "翔太", "大和", "莲", "飒太", "悠斗", "拓海",
)
_ROLES = (
    "同班同学", "学生会书记", "图书委员", "社团经理", "转学生",
    "隔壁班的熟人", "咖啡厅店员", "学校保健老师", "旧书店老板",
    "青梅竹马", "研究助手", "旅店老板娘", "街头歌手", "摄影部成员",
)
_HAIR = (
    "黑色及肩发", "深棕色长直发", "栗色微卷长发", "亚麻色齐肩短发",
    "银灰色低马尾", "酒红色侧编发", "深蓝色短发", "浅金色双马尾",
    "墨绿色长卷发", "黑发高马尾",
)
_EYES = (
    "深黑色眼瞳", "琥珀色眼瞳", "灰蓝色眼瞳", "翠绿色眼瞳",
    "紫罗兰色眼瞳", "浅棕色眼瞳", "银灰色眼瞳",
)
_VIBES = (
    "温和安静的举止", "干净利落的站姿", "有点困倦的松弛感",
    "明亮而克制的笑意", "礼貌但保持距离", "带着书卷气的沉静",
)
_OUTFITS = (
    "深蓝色校服外套与白衬衫", "米色针织开衫与长裙", "黑色高领毛衣与长裤",
    "浅灰色连帽卫衣与牛仔裤", "白色衬衫与格纹半裙", "藏青色风衣与短靴",
)
_PERSONALITY = (
    "说话轻声、观察力强，习惯先听完再开口，对熟人会露出意外的固执。",
    "表面礼貌周到，实际很容易心软；被戳中痛点时会用玩笑岔开。",
    "行动派，想到就做；对细节迟钝，却总能记住别人随口提过的小事。",
    "谨慎慢热，习惯把情绪藏起来；一旦信任某人，会变得非常坦率。",
    "看起来冷淡，其实很会照顾人；不擅长表达感谢，常用行动代替。",
    "好奇心旺盛，喜欢追问；遇到危险反而先笑出来，让人摸不透。",
)
_TRAITS = (
    "温柔", "慢热", "嘴硬心软", "理性", "行动派", "观察力强", "容易害羞",
    "冷静", "毒舌", "可靠", "好奇心强", "固执", "有分寸", "天然呆",
    "护短", "克制", "健谈", "怕麻烦", "重情", "不服输",
)
_BODY_TYPES = (
    "纤细高挑", "娇小柔软", "匀称标准", "偏瘦", "柔韧有力", "丰润成熟",
)
_SPEECH = (
    "语速偏慢，措辞礼貌，偶尔会停顿思考", "说话直接，短句多，情绪上来会加快语速",
    "声音轻，常用反问和试探，很少把话说满", "措辞温和，喜欢用比喻，笑起来会放软语气",
    "条理清楚，习惯先给结论再补理由", "带一点玩笑腔，紧张时会用吐槽掩饰",
)
_LIKES = ("旧书、雨天、热牛奶", "甜食、猫、傍晚的操场", "音乐、整理房间、安静的地方",
          "摄影、街角的小店、微苦的咖啡", "园艺、手工、晒太阳")
_DISLIKES = ("吵闹的人群、被催促", "苦味、突然的身体接触", "说谎、被当成小孩",
             "浪费食物、失约", "过强的光、被盯着看")

_BG_TEMPLATES = (
    "在故事开始前，{name}一直过着按部就班的生活。作为{role}，TA熟悉这片街区"
    "的每条小路，也习惯了替别人收拾残局。{personality_short}很少有人知道，"
    "TA一直在暗中保管一件与主线有关的旧物——那是TA不愿提起的过去，也可能"
    "成为主角打开局面的关键。",
    "{name}是{role}，在周围人眼里可靠又省心。只有独处时，TA才会露出疲惫的"
    "一面。{personality_short}最近，TA开始频繁梦见同一个陌生场景，并在地图上"
    "找到了与梦境吻合的地点；这次相遇并非偶然，而是TA主动迈出的第一步。",
    "认识{name}的人都说，TA是那种「不会主动麻烦别人」的类型。身为{role}，"
    "TA把大部分时间花在照顾别人上，却很少谈自己的事。{personality_short}"
    "主角的出现让TA第一次产生了「想被谁理解」的念头，也让一段被尘封的约定"
    "重新浮出水面。",
)


def _clean(text, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def _int_in(raw, lo: int, hi: int) -> int:
    try:
        n = int(round(float(raw)))
    except (TypeError, ValueError):
        return 0
    return max(lo, min(hi, n)) if n else 0


def _traits(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,，、;；/]+", raw)
    out = []
    for x in (raw if isinstance(raw, (list, tuple)) else []):
        t = _clean(x, MAX_TRAIT)
        if t and t not in out:
            out.append(t)
        if len(out) >= MAX_TRAITS:
            break
    return out


def _locked(raw, personality_locked: bool) -> list[str]:
    out = []
    if isinstance(raw, str):
        raw = re.split(r"[,，、;；]+", raw)
    for x in (raw if isinstance(raw, (list, tuple)) else []):
        k = str(x or "").strip()
        if k in LOCKABLE_FIELDS and k not in out:
            out.append(k)
    if personality_locked:
        for k in PERSONALITY_LOCKS:
            if k not in out:
                out.append(k)
    return out


def sanitize_character(raw: dict, *, default_role: str = "NPC") -> dict:
    """把任意来源的角色 dict 归一成会话/档案使用的标准形状。

    1.9.16：增加性格标签/年龄/身高/体型/三围/生日/说话风格/喜好/厌恶，
    以及「固定字段」；旧卡缺字段时全部安全回退，不破坏现有存档。
    """
    raw = raw if isinstance(raw, dict) else {}
    personality_locked = bool(raw.get("personality_locked"))
    return {
        "name": _clean(raw.get("name"), MAX_NAME),
        "role": _clean(raw.get("role"), MAX_ROLE) or default_role,
        "appearance": _clean(raw.get("appearance"), MAX_APPEARANCE),
        "personality": _clean(raw.get("personality"), MAX_PERSONALITY),
        "background": _clean(raw.get("background"), MAX_BACKGROUND),
        "traits": _traits(raw.get("traits")),
        "age": _int_in(raw.get("age"), 18, 80),
        "height_cm": _int_in(raw.get("height_cm"), 130, 220),
        "body_type": _clean(raw.get("body_type"), MAX_BODY),
        "measurements": _clean(raw.get("measurements"), MAX_MEASURE),
        "birthday": _clean(raw.get("birthday"), MAX_BIRTHDAY),
        "speech_style": _clean(raw.get("speech_style"), MAX_SPEECH),
        "likes": _clean(raw.get("likes"), MAX_LIKES),
        "dislikes": _clean(raw.get("dislikes"), MAX_DISLIKES),
        "personality_locked": personality_locked,
        "locked_fields": _locked(raw.get("locked_fields"), personality_locked),
    }


def normalize_character_list(raw_list, *, limit: int = 4,
                             protagonist: str = "") -> list[dict]:
    """清洗角色列表：去空名、去重名、避免与主角同名、限制数量。

    重名会直接破坏 char_states 的键（两套数值互相覆盖），所以这里
    宁可丢弃后出现的重复项，也不让脏数据进入会话。
    """
    out: list[dict] = []
    seen = {str(protagonist or "").strip()}
    for raw in raw_list or []:
        c = sanitize_character(raw)
        if not c["name"] or c["name"] in seen:
            continue
        seen.add(c["name"])
        out.append(c)
        if len(out) >= limit:
            break
    return out


def random_character(*, world: str = "", avoid_names=(),
                     rng: random.Random | None = None,
                     seed: int | None = None) -> dict:
    """本地随机角色卡（不依赖 LLM，离线/演示模式也可用）。"""
    r = rng or random.Random(seed)
    avoid = {str(x) for x in (avoid_names or ()) if str(x)}
    for _ in range(40):
        name = r.choice(_SURNAMES) + r.choice(_GIVEN)
        if name not in avoid:
            break
    role = r.choice(_ROLES)
    appearance = (f"{r.choice(_HAIR)}，{r.choice(_EYES)}，"
                  f"{r.choice(_OUTFITS)}，{r.choice(_VIBES)}。")
    personality = r.choice(_PERSONALITY)
    background = r.choice(_BG_TEMPLATES).format(
        name=name, role=role, personality_short=personality[:40])
    # 世界观只影响一句气息，不硬塞长文本，避免角色卡变成设定的复读机
    if world.strip():
        background += "TA与这个故事的世界观有着未被说明的联系。"
    return {
        "name": name, "role": role, "appearance": appearance,
        "personality": personality, "background": background[:MAX_BACKGROUND],
        "traits": r.sample(_TRAITS, k=r.randint(3, 5)),
        "age": r.randint(18, 29),
        "height_cm": r.randint(150, 178),
        "body_type": r.choice(_BODY_TYPES),
        "measurements": (f"B{r.randint(78, 96)} / W{r.randint(55, 70)} / "
                         f"H{r.randint(80, 100)}"),
        "birthday": f"{r.randint(1, 12)}月{r.randint(1, 28)}日",
        "speech_style": r.choice(_SPEECH),
        "likes": r.choice(_LIKES),
        "dislikes": r.choice(_DISLIKES),
        "personality_locked": False,
        "locked_fields": [],
    }


_BG_SYSTEM = (
    "你是视觉小说/TRPG 的角色设定师。请为给定角色写一段「角色背景」，"
    "要求：\n"
    "1) 120~220 字，第三人称，简体中文，只输出正文（不要标题、不要引号、"
    "不要 markdown）；\n"
    "2) 写明出身/日常身份、与主角或当前故事的因缘、在故事中的位置；\n"
    "3) 留一个可被剧情利用的秘密、弱点或未解的钩子，但不要替玩家做决定；\n"
    "4) 不要复述外貌、不要写露骨性内容、不要写未成年人不当内容；\n"
    "5) 不要出现「作为AI」「根据设定」等元话语。"
)


def _local_background(name: str, role: str, personality: str,
                      world: str = "") -> str:
    short = (personality or "").strip()[:40]
    if short and short[-1] not in "。！？!?…":
        short += "。"
    bg = (f"{name}在故事开始前以{role or '普通居民'}的身份生活。"
          f"{short}TA对主角的第一印象并不算好，却在几次偶然的接触后，"
          f"开始注意到对方身上那些不合常理的变化。TA手里握着一件与主线"
          f"有关的旧物，既是帮助主角的线索，也可能把两人一起拖进更深的"
          f"麻烦。")
    if world.strip():
        bg += "TA的过去与这个世界的规则有着一条尚未被说破的暗线。"
    return bg[:MAX_BACKGROUND]


def _clean_generated(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json|text|markdown)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip().strip('"').strip("'").strip()
    text = re.sub(r"^【[^】]{1,12}】\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_BACKGROUND]


_FINE_TUNE_SYSTEM = """\
你是视觉小说角色卡编辑。根据玩家的微调要求修改角色卡；只输出一个 JSON
对象，不要解释、不要 markdown。可修改字段：
name 不可改；role, appearance, personality, traits（字符串数组）, age（成年，
18~80）, height_cm（130~220）, body_type, measurements, birthday,
speech_style, likes, dislikes, background。
玩家明确说「固定」的字段以及 locked_fields 中的字段必须原样保留，不得改写。
未要求修改的字段也尽量保留。所有角色均为成年人。"""


def _local_fine_tune(card: dict, instruction: str) -> dict:
    """无 LLM 的确定性微调：至少让身高/年龄/性格关键词可用。"""
    out = dict(card)
    text = instruction or ""
    m = re.search(r"(\d{3})\s*(?:cm|厘米|公分)?", text)
    height = int(out.get("height_cm") or 0)
    if m:
        height = int(m.group(1))
    elif "更高" in text or "高一点" in text:
        height += 5
    elif "更矮" in text or "矮一点" in text:
        height -= 5
    if height:
        out["height_cm"] = max(130, min(220, height))
    if "更成熟" in text:
        out["age"] = min(80, int(out.get("age") or 22) + 2)
    elif "更年轻" in text:
        out["age"] = max(18, int(out.get("age") or 22) - 2)
    if any(k in text for k in ("更温柔", "温柔一点", "更软")):
        out["personality"] = (str(out.get("personality") or "")
                              + "；语气比之前更温柔，习惯先照顾对方的情绪。")[:MAX_PERSONALITY]
        out["speech_style"] = (str(out.get("speech_style") or "")
                               + "；说话更轻、更缓。")[:MAX_SPEECH]
    if any(k in text for k in ("更冷淡", "冷淡一点", "更疏离")):
        out["personality"] = (str(out.get("personality") or "")
                              + "；表面更冷淡，习惯用简短回应保持距离。")[:MAX_PERSONALITY]
    if any(k in text for k in ("更主动", "主动一点")):
        out["personality"] = (str(out.get("personality") or "")
                              + "；行动上更主动，会先迈出一步。")[:MAX_PERSONALITY]
    if "纤细" in text or "更瘦" in text:
        out["body_type"] = "纤细高挑"
    elif any(k in text for k in ("丰润", "丰满", "更有曲线")):
        out["body_type"] = "丰润成熟"
    if any(k in text for k in ("更神秘", "神秘一点")):
        out["background"] = (str(out.get("background") or "")
                             + "；TA似乎还藏着一段不愿解释的过去。")[:MAX_BACKGROUND]
    add = re.sub(r"^(请|把|让|使|角色|她|他|TA)+", "", text).strip("。；;，, ")
    if add and len(add) <= MAX_TRAIT and add not in (out.get("traits") or []):
        traits = list(out.get("traits") or [])
        if len(traits) < MAX_TRAITS:
            traits.append(add)
            out["traits"] = traits
    return out


async def fine_tune_character(card: dict, instruction: str, *,
                              world: str = "", title: str = "",
                              protagonist: str = "") -> dict:
    """AI 微调角色卡；锁定字段不会被改写。返回 {character, source, mock}。"""
    current = sanitize_character(card)
    instruction = _clean(instruction, 300)
    if not instruction:
        return {"character": current, "source": "noop", "mock": False}
    locks = set(current["locked_fields"])
    if current.get("personality_locked"):
        locks.update(PERSONALITY_LOCKS)
    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    source = "local"
    data = None
    if not llm.mock:
        try:
            data = await asyncio.wait_for(llm.chat_json([
                {"role": "system", "content": _FINE_TUNE_SYSTEM},
                {"role": "user", "content": (
                    f"【当前角色卡】{current}\n"
                    f"【锁定字段】{sorted(locks) or '无'}\n"
                    f"【作品】{title or '（未命名）'}\n"
                    f"【世界观摘要】{world or '（未提供）'}\n"
                    f"【主角】{protagonist or '（未命名）'}\n"
                    f"【微调要求】{instruction}")},
            ], temperature=0.55), timeout=45.0)
            source = "llm"
        except (LLMError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            source = f"fallback:{type(e).__name__}"
    if not isinstance(data, dict):
        data = _local_fine_tune(current, instruction)
        if source == "llm":
            source = "fallback:shape"
    merged = dict(current)
    for key in LOCKABLE_FIELDS:
        if key in locks:
            merged[key] = current.get(key)
        elif key in data:
            merged[key] = data.get(key)
    merged["name"] = current["name"]
    merged["personality_locked"] = bool(current.get("personality_locked"))
    merged["locked_fields"] = list(current.get("locked_fields") or [])
    # 用户要求「固定」时，即使 JSON 漏写也兜底
    if any(k in instruction for k in ("固定", "不要改", "保持")):
        for key in PERSONALITY_LOCKS:
            if key in instruction:
                merged[key] = current.get(key)
    return {"character": sanitize_character(merged), "source": source,
            "mock": bool(llm.mock)}


async def write_background(*, name: str, role: str = "", appearance: str = "",
                           personality: str = "", world: str = "",
                           title: str = "", protagonist: str = "",
                           existing: list[str] | None = None,
                           temperature: float = 0.85) -> dict:
    """自动谱写角色背景。返回 {background, mock, source}。"""
    name = _clean(name, MAX_NAME)
    if not name:
        raise ValueError("角色名为空")
    role = _clean(role, MAX_ROLE)
    appearance = _clean(appearance, MAX_APPEARANCE)
    personality = _clean(personality, MAX_PERSONALITY)
    world = _clean(world, 800)
    title = _clean(title, 60)
    protagonist = _clean(protagonist, MAX_NAME)
    existing = [str(x) for x in (existing or []) if str(x)][:8]

    cfg = cfgmod.load_config()
    llm = LLMClient(cfg)
    if llm.mock:
        return {"background": _local_background(name, role, personality, world),
                "mock": True, "source": "local"}

    lines = [
        f"【作品】{title or '（未命名）'}",
        f"【世界观摘要】{world or '（未提供，按现实/近未来校园都市处理）'}",
        f"【主角】{protagonist or '（未命名）'}",
        f"【已有角色】{'、'.join(existing) if existing else '（无）'}",
        f"【本次角色】{name}",
        f"【身份/关系】{role or '（未指定，请给一个自然的日常身份）'}",
        f"【外貌】{appearance or '（未提供，不要复述外貌）'}",
        f"【性格/说话风格】{personality or '（未提供，可自行补全）'}",
    ]
    try:
        text = await asyncio.wait_for(
            llm.chat([
                {"role": "system", "content": _BG_SYSTEM},
                {"role": "user", "content": "\n".join(lines)},
            ], temperature=temperature),
            timeout=45.0)
    except (LLMError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        # 自动谱写背景是锦上添花：任何失败都退回本地模板，不阻塞角色卡
        return {"background": _local_background(name, role, personality, world),
                "mock": False, "source": f"fallback:{type(e).__name__}"}
    cleaned = _clean_generated(text)
    if len(cleaned) < 30:
        return {"background": _local_background(name, role, personality, world),
                "mock": False, "source": "fallback:short"}
    return {"background": cleaned, "mock": False, "source": "llm"}


async def write_missing_backgrounds(characters: list[dict], *,
                                    world: str = "", title: str = "",
                                    protagonist: str = "",
                                    limit: int = 4) -> list[dict]:
    """给一批角色补背景（角色库旧卡适配/开局批量补全用）。

    顺序执行、失败即本地兜底；返回与输入同序的副本，不修改原对象。
    """
    out = []
    names = [str((c or {}).get("name", "")) for c in (characters or [])]
    for c in (characters or [])[:limit]:
        c = dict(c or {})
        if not c.get("background"):
            try:
                r = await write_background(
                    name=str(c.get("name", "")), role=str(c.get("role", "")),
                    appearance=str(c.get("appearance", "")),
                    personality=str(c.get("personality", "")),
                    world=world, title=title, protagonist=protagonist,
                    existing=[n for n in names if n != c.get("name")])
                c["background"] = r["background"]
            except ValueError:
                pass
        out.append(c)
    return out

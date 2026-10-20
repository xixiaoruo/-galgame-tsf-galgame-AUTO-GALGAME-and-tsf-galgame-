"""制服提示词库与随机制服（1.9.0）。

玩家在开局设定里可以手动填写制服英文词，也可以点「🎲 随机制服」从本库
随机抽取一套与当前场景气质相符的制服。游戏内「换装」同样可以抽一套
现成的制服，避免每次都要自己想英文提示词。

本模块只提供**数据与纯函数**：不读写文件、不调用 LLM、不依赖游戏会话，
方便单测与前端直接展示。所有预设都是成年角色可穿的常规/幻想制服，
不含裸体或内衣向内容；18+ 内容尺度由剧情分级政策决定，与本库无关。
"""
from __future__ import annotations

import random
import re
from typing import Iterable

# 单条提示词上限与游戏内 uniform 字段保持一致（200 字符）。
MAX_EN = 200
MAX_NEG = 200

# 场景关键词 → 制服标签。匹配时按顺序取第一个命中的标签组。
SCENE_TAGS: list[tuple[str, tuple[str, ...]]] = [
    ("school", ("学园", "学校", "校园", "教室", "走廊", "社团", "操场",
                "食堂", "图书馆", "宿舍", "学生会", "校服", "高中", "大学",
                "school", "classroom", "campus")),
    ("shrine", ("神社", "祭典", "巫女", "鸟居", "神社", "shrine", "festival")),
    ("maid", ("女仆", "咖啡厅", "咖啡店", "甜品", "红茶", "maid", "cafe")),
    ("medical", ("医院", "病房", "诊所", "护士", "医疗", "保健室",
                 "hospital", "clinic", "nurse")),
    ("beach", ("海边", "海滩", "泳池", "游泳", "夏日", "beach", "pool",
               "swimming")),
    ("work", ("公司", "职场", "办公室", "写字楼", "银行", "前台", "秘书",
              "office", "company", "business")),
    ("military", ("军队", "军营", "司令", "战舰", "军事", "军装", "military",
                  "army", "navy")),
    ("fantasy", ("奇幻", "魔法", "学院", "冒险", "精灵", "王城", "城堡",
                 "异世界", "fantasy", "magic", "castle")),
    ("traditional", ("和风", "和服", "浴衣", "温泉", "旅馆", "京都",
                     "kimono", "onsen", "ryokan")),
    ("idol", ("偶像", "舞台", "演唱会", "演艺", "直播", "idol", "stage")),
    ("home", ("家", "公寓", "卧室", "客厅", "厨房", "宿舍", "home",
              "apartment", "bedroom")),
    ("scifi", ("科幻", "太空", "飞船", "实验室", "未来", "赛博", "scifi",
               "space", "cyber")),
]

# 标签 → 预设 id 的优先池（随机时提高权重，不是硬限制）。
TAG_POOLS: dict[str, tuple[str, ...]] = {
    "school": ("sailor", "blazer", "gym", "student_council", "library"),
    "shrine": ("miko", "kimono", "yukata"),
    "maid": ("maid", "cafe_apron", "waitress"),
    "medical": ("nurse", "labcoat", "clinic"),
    "beach": ("swimsuit", "beach_cover", "gym"),
    "work": ("office", "ol", "secretary", "labcoat"),
    "military": ("military", "pilot", "police"),
    "fantasy": ("fantasy_mage", "fantasy_knight", "fantasy_priestess"),
    "traditional": ("kimono", "yukata", "miko"),
    "idol": ("idol", "stage_dress", "cheer"),
    "home": ("loungewear", "hoodie", "pajama"),
    "scifi": ("scifi_suit", "labcoat", "pilot"),
}

UNIFORMS: list[dict] = [
    {
        "id": "sailor", "name": "水手服", "icon": "⚓", "tags": ["school"],
        "note": "经典学园水手服，蓝白配色",
        "en": "navy sailor uniform, white blouse, red ribbon tie, pleated navy skirt, knee-high socks, loafers",
        "neg": "casual clothes, jeans, t-shirt, hoodie, streetwear",
    },
    {
        "id": "blazer", "name": "西装校服", "icon": "🎓", "tags": ["school"],
        "note": "深色西装外套 + 格纹裙，学生会感",
        "en": "dark blazer school uniform, white dress shirt, striped ribbon tie, plaid pleated skirt, black socks, leather loafers",
        "neg": "casual clothes, jeans, hoodie, sportswear, streetwear",
    },
    {
        "id": "gym", "name": "运动服", "icon": "🏃", "tags": ["school", "beach"],
        "note": "体育课/社团活动用运动套装",
        "en": "school gym uniform, white t-shirt with colored trim, navy bloomers, track jacket, sneakers",
        "neg": "formal wear, dress, blazer, high heels, office wear",
    },
    {
        "id": "student_council", "name": "学生会制服", "icon": "📋",
        "tags": ["school"],
        "note": "更正式的学园制服，袖章与领结",
        "en": "formal student council uniform, white blouse, black vest, red armband, pleated black skirt, ribbon tie, polished shoes",
        "neg": "casual clothes, jeans, t-shirt, sportswear",
    },
    {
        "id": "library", "name": "图书委员", "icon": "📚", "tags": ["school"],
        "note": "文静的书卷气制服",
        "en": "school librarian uniform, cream blouse, brown cardigan, long pleated skirt, ribbon tie, reading glasses, leather shoes",
        "neg": "casual clothes, jeans, sportswear, swimsuit",
    },
    {
        "id": "miko", "name": "巫女服", "icon": "⛩", "tags": ["shrine", "traditional"],
        "note": "红白巫女装，神社/祭典场景",
        "en": "miko outfit, white kimono top, red hakama, red hair ribbon, tabi socks, wooden sandals",
        "neg": "casual clothes, jeans, school uniform, office wear, modern dress",
    },
    {
        "id": "kimono", "name": "和服", "icon": "🌸", "tags": ["traditional", "shrine"],
        "note": "传统和服，适合温泉/京都/祭典",
        "en": "elegant kimono, floral pattern, obi sash, layered collar, geta sandals, hair ornament",
        "neg": "casual clothes, jeans, school uniform, office wear, modern dress",
    },
    {
        "id": "yukata", "name": "浴衣", "icon": "🎆", "tags": ["traditional", "shrine"],
        "note": "夏日祭浴衣，轻便清爽",
        "en": "summer yukata, light cotton fabric, floral print, obi sash, wooden sandals, hair pin",
        "neg": "casual clothes, jeans, school uniform, office wear, heavy winter clothes",
    },
    {
        "id": "maid", "name": "女仆装", "icon": "☕", "tags": ["maid"],
        "note": "经典黑白女仆装，咖啡厅/宅邸",
        "en": "classic maid outfit, black dress, white apron, frilled headband, white cuffs, black stockings, mary jane shoes",
        "neg": "casual clothes, jeans, school uniform, office wear, swimsuit",
    },
    {
        "id": "cafe_apron", "name": "咖啡厅围裙", "icon": "🍰", "tags": ["maid", "work"],
        "note": "店员围裙装，日常感更强",
        "en": "cafe staff uniform, white blouse, brown apron dress, small ribbon, waist apron, comfortable shoes",
        "neg": "casual clothes, jeans, school uniform, swimsuit, formal gown",
    },
    {
        "id": "waitress", "name": "餐厅制服", "icon": "🍽", "tags": ["maid", "work"],
        "note": "餐厅服务生制服",
        "en": "waitress uniform, white shirt, black vest, short apron, black skirt, ribbon tie, comfortable shoes",
        "neg": "casual clothes, jeans, school uniform, swimsuit, formal gown",
    },
    {
        "id": "nurse", "name": "护士服", "icon": "💉", "tags": ["medical"],
        "note": "医院/保健室场景",
        "en": "nurse uniform, white dress, pale blue trim, nurse cap, white stockings, white shoes",
        "neg": "casual clothes, jeans, school uniform, office wear, swimsuit",
    },
    {
        "id": "labcoat", "name": "白大褂", "icon": "🥼", "tags": ["medical", "work", "scifi"],
        "note": "研究/医疗/实验室通用",
        "en": "white lab coat, light blue shirt, dark trousers or skirt, ID badge, pen in pocket, closed-toe shoes",
        "neg": "casual clothes, jeans, swimsuit, formal gown, school uniform",
    },
    {
        "id": "clinic", "name": "诊所制服", "icon": "🩺", "tags": ["medical"],
        "note": "柔和配色的诊所工作服",
        "en": "clinic staff uniform, soft mint scrub top, white pants, ID badge, comfortable sneakers",
        "neg": "casual clothes, jeans, school uniform, swimsuit, formal gown",
    },
    {
        "id": "office", "name": "职场套装", "icon": "💼", "tags": ["work"],
        "note": "中性职场西装，干练",
        "en": "business suit, tailored blazer, white blouse, pencil skirt, necktie or ribbon, leather pumps",
        "neg": "casual clothes, jeans, school uniform, swimsuit, sportswear",
    },
    {
        "id": "ol", "name": "OL 通勤", "icon": "🏢", "tags": ["work"],
        "note": "日常通勤风，柔和配色",
        "en": "office lady outfit, beige cardigan, white blouse, pleated midi skirt, thin belt, low heels, small handbag",
        "neg": "casual clothes, jeans, school uniform, swimsuit, sportswear",
    },
    {
        "id": "secretary", "name": "秘书制服", "icon": "🖊", "tags": ["work"],
        "note": "贴身剪裁的秘书套装",
        "en": "secretary uniform, fitted black blazer, white silk blouse, pencil skirt, sheer tights, low heels, ID badge",
        "neg": "casual clothes, jeans, school uniform, swimsuit, sportswear",
    },
    {
        "id": "military", "name": "军装", "icon": "🎖", "tags": ["military", "scifi"],
        "note": "仪仗/军官风制服",
        "en": "military dress uniform, tailored jacket, epaulettes, brass buttons, necktie, trousers, polished boots",
        "neg": "casual clothes, jeans, school uniform, swimsuit, fantasy robe",
    },
    {
        "id": "pilot", "name": "飞行员服", "icon": "✈", "tags": ["military", "scifi", "work"],
        "note": "飞行夹克/驾驶服",
        "en": "pilot uniform, flight jacket, white shirt, dark tie, trousers, leather boots, aviator glasses",
        "neg": "casual clothes, jeans, school uniform, swimsuit, fantasy robe",
    },
    {
        "id": "police", "name": "警服", "icon": "🚓", "tags": ["military", "work"],
        "note": "简洁警装",
        "en": "police uniform, navy jacket, white shirt, black tie, duty belt, trousers, polished shoes",
        "neg": "casual clothes, jeans, school uniform, swimsuit, fantasy robe",
    },
    {
        "id": "swimsuit", "name": "泳装", "icon": "🌊", "tags": ["beach"],
        "note": "海边/泳池用，成年角色常规泳装",
        "en": "adult swimsuit, one-piece swimwear, beach cover-up, sandals, sun hat",
        "neg": "school uniform, office wear, formal gown, winter clothes, jeans",
    },
    {
        "id": "beach_cover", "name": "海滨度假", "icon": "🏖", "tags": ["beach"],
        "note": "度假风罩衫与短裤",
        "en": "beach resort outfit, light linen cover-up, shorts, sandals, sunglasses, sun hat",
        "neg": "school uniform, office wear, formal gown, winter clothes, jeans",
    },
    {
        "id": "fantasy_mage", "name": "魔法师袍", "icon": "🔮", "tags": ["fantasy"],
        "note": "奇幻学院/魔法师",
        "en": "fantasy mage outfit, long robe, embroidered mantle, leather belt, spellbook, boots, gem brooch",
        "neg": "casual clothes, jeans, school uniform, office wear, modern dress",
    },
    {
        "id": "fantasy_knight", "name": "骑士装", "icon": "🛡", "tags": ["fantasy"],
        "note": "轻甲骑士/护卫",
        "en": "fantasy knight outfit, light armor, tabard, leather belt, gauntlets, greaves, sword at hip",
        "neg": "casual clothes, jeans, school uniform, office wear, modern dress",
    },
    {
        "id": "fantasy_priestess", "name": "圣女装", "icon": "✨", "tags": ["fantasy", "shrine"],
        "note": "神殿/圣女风格",
        "en": "fantasy priestess outfit, white and gold robe, long cape, holy symbol, sash, sandals",
        "neg": "casual clothes, jeans, school uniform, office wear, modern dress",
    },
    {
        "id": "idol", "name": "偶像打歌服", "icon": "🎤", "tags": ["idol"],
        "note": "舞台演出服，明亮配色",
        "en": "idol stage costume, sparkling jacket, pleated skirt, ribbon, gloves, stage boots, hair accessory",
        "neg": "casual clothes, jeans, school uniform, office wear, swimsuit",
    },
    {
        "id": "stage_dress", "name": "舞台礼服", "icon": "🎭", "tags": ["idol"],
        "note": "偏正式的舞台长裙",
        "en": "stage dress, elegant evening gown, layered skirt, gloves, hair ornament, stage heels",
        "neg": "casual clothes, jeans, school uniform, office wear, swimsuit",
    },
    {
        "id": "cheer", "name": "啦啦队服", "icon": "📣", "tags": ["idol", "school"],
        "note": "运动应援风",
        "en": "cheerleader uniform, fitted top, pleated skirt, ribbon, sneakers, pom-poms",
        "neg": "formal wear, office wear, swimsuit, winter clothes, jeans",
    },
    {
        "id": "loungewear", "name": "居家服", "icon": "🏠", "tags": ["home"],
        "note": "宽松居家/休息",
        "en": "comfortable loungewear, oversized cardigan, soft shirt, relaxed shorts or skirt, slippers",
        "neg": "formal wear, school uniform, office wear, swimsuit, armor",
    },
    {
        "id": "hoodie", "name": "卫衣便装", "icon": "🧢", "tags": ["home"],
        "note": "日常休闲",
        "en": "casual hoodie, jeans, sneakers, simple t-shirt, relaxed fit",
        "neg": "formal wear, school uniform, office wear, swimsuit, armor",
    },
    {
        "id": "pajama", "name": "睡衣", "icon": "🌙", "tags": ["home"],
        "note": "休息/夜间场景",
        "en": "cozy pajamas, soft cotton shirt and shorts, slippers, relaxed bedtime outfit",
        "neg": "formal wear, school uniform, office wear, swimsuit, armor",
    },
    {
        "id": "scifi_suit", "name": "科幻驾驶服", "icon": "🚀", "tags": ["scifi"],
        "note": "飞船/未来设施",
        "en": "sci-fi pilot suit, fitted bodysuit with panels, utility belt, gloves, boots, holographic badge",
        "neg": "casual clothes, jeans, school uniform, fantasy robe, swimsuit",
    },
]

_BY_ID = {u["id"]: u for u in UNIFORMS}


def _clean(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def public_preset(p: dict) -> dict:
    """返回给前端的干净副本（不暴露内部可变对象）。"""
    return {
        "id": p["id"], "name": p["name"], "icon": p.get("icon", "👔"),
        "tags": list(p.get("tags") or []), "note": p.get("note", ""),
        "en": _clean(p["en"], MAX_EN), "neg": _clean(p["neg"], MAX_NEG),
    }


def list_presets() -> list[dict]:
    return [public_preset(p) for p in UNIFORMS]


def get_preset(preset_id: str) -> dict | None:
    p = _BY_ID.get(str(preset_id or "").strip())
    return public_preset(p) if p else None


def scene_tag(scene: str) -> str:
    """粗粒度场景标签；无匹配返回空串。"""
    text = str(scene or "").lower()
    if not text:
        return ""
    for tag, keys in SCENE_TAGS:
        if any(k.lower() in text for k in keys):
            return tag
    return ""


def scene_tags(scene: str) -> list[str]:
    """场景命中的全部标签（按 SCENE_TAGS 顺序，去重）。"""
    text = str(scene or "").lower()
    out = []
    if not text:
        return out
    for tag, keys in SCENE_TAGS:
        if any(k.lower() in text for k in keys) and tag not in out:
            out.append(tag)
    return out


def random_preset(scene: str = "", exclude: Iterable[str] = (),
                  rng: random.Random | None = None,
                  seed: int | None = None) -> dict:
    """随机抽一套制服：有场景标签时优先该标签池，避免重复出现。

    rng/seed 仅用于测试可复现；业务调用不传，走系统随机。
    """
    r = rng or random.Random(seed)
    banned = {str(x) for x in (exclude or ()) if str(x)}
    tags = scene_tags(scene)
    preferred: list[dict] = []
    seen = set()
    for tag in tags:
        for pid in TAG_POOLS.get(tag, ()):
            p = _BY_ID.get(pid)
            if p and p["id"] not in seen:
                seen.add(p["id"])
                preferred.append(p)
    candidates = [p for p in (preferred or UNIFORMS) if p["id"] not in banned]
    if not candidates:
        candidates = [p for p in UNIFORMS if p["id"] not in banned]
    if not candidates:
        candidates = list(UNIFORMS)
    chosen = r.choice(candidates)
    out = public_preset(chosen)
    out["scene_tag"] = tags[0] if tags else ""
    out["matched"] = bool(preferred)
    return out


def normalize_pair(en: str, neg: str) -> tuple[str, str]:
    """把任意输入规范成可安全落库的 (en, neg)。"""
    return _clean(en, MAX_EN), _clean(neg, MAX_NEG)

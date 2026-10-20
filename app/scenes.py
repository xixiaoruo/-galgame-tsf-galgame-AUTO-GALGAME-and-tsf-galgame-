"""场景名归一 / 初场景推断（1.9.0）。

LLM 每幕返回的 scene 经常是同一地点的不同写法：
「星冠学园 · 操场」「星冠学园·操场」「操场（午后）」「学校操场」……
旧逻辑用 scene 名的 sha256 做 key，写法一变就当成新场景、重新画一张背景，
既浪费算力，也让画面在明明是同一个地方时来回跳。

本模块提供纯函数：
  · scene_core / scenes_similar：判断两个场景名是否指向同一地点
  · canonical_scene：在已登记场景里找同地点的旧条目，复用它的固定背景
  · guess_initial_scene：开局立绘还没等到第一幕时，从世界观推断一个场景，
    让「学园→校服 / 神社→巫女服」的制服映射在开场立绘就生效
"""
from __future__ import annotations

import difflib
import re
import unicodedata

_SEP_RE = re.compile(r"[·・/／|｜—–\-]+")
_WS_RE = re.compile(r"\s+")
_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_TRAIL_TIME_RE = re.compile(
    r"(清晨|早晨|早上|上午|中午|午后|下午|黄昏|傍晚|夜晚|深夜|凌晨|白天|"
    r"雨天|雪天|晴天|阴天|雨后|雪后|时分|时刻|时间)$")
# 场景分类：与 uniforms.SCENE_TAGS 保持同源语义，但这里只用于推断与比较。
CATEGORY_KEYS: list[tuple[str, tuple[str, ...]]] = [
    ("school", ("学园", "学校", "校园", "教室", "走廊", "社团", "操场",
                "食堂", "图书馆", "宿舍", "学生会", "校服", "高中", "大学",
                "school", "classroom", "campus")),
    ("shrine", ("神社", "祭典", "巫女", "鸟居", "shrine", "festival")),
    ("maid", ("女仆", "咖啡厅", "咖啡店", "甜品", "红茶", "maid", "cafe")),
    ("medical", ("医院", "病房", "诊所", "护士", "医疗", "保健室",
                 "hospital", "clinic", "nurse")),
    ("beach", ("海边", "海滩", "泳池", "游泳", "夏日", "beach", "pool",
               "swimming")),
    ("work", ("公司", "职场", "办公室", "写字楼", "银行", "前台", "秘书",
              "office", "company", "business")),
    ("military", ("军队", "军营", "司令", "战舰", "军事", "军装", "military",
                  "army", "navy")),
    ("fantasy", ("奇幻", "魔法", "冒险", "精灵", "王城", "城堡", "异世界",
                 "fantasy", "magic", "castle")),
    ("traditional", ("和风", "和服", "浴衣", "温泉", "旅馆", "京都",
                     "kimono", "onsen", "ryokan")),
    ("idol", ("偶像", "舞台", "演唱会", "演艺", "直播", "idol", "stage")),
    ("home", ("家", "公寓", "卧室", "客厅", "厨房", "home",
              "apartment", "bedroom")),
    ("scifi", ("科幻", "太空", "飞船", "实验室", "未来", "赛博", "scifi",
               "space", "cyber")),
]

# 推断初场景时给一个代表性地名（只用于 current_scene 的制服映射，不显示）。
CATEGORY_SCENE = {
    "school": "学园教室", "shrine": "神社", "maid": "咖啡厅",
    "medical": "医院病房", "beach": "海边", "work": "办公室",
    "military": "军营", "fantasy": "奇幻王城", "traditional": "和风旅馆",
    "idol": "舞台后台", "home": "家中客厅", "scifi": "飞船舰桥",
}


def normalize(name: str) -> str:
    """全半角统一、去多余空白与重复分隔符；保留原始地名信息。"""
    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    text = _WS_RE.sub(" ", text)
    text = _SEP_RE.sub("·", text)
    text = re.sub(r"·{2,}", "·", text).strip("· ")
    return text


def scene_core(name: str) -> str:
    """去掉括号补充与时段后缀，得到用于比较的核心场景名。"""
    text = normalize(name)
    text = _BRACKET_RE.sub("", text).strip("· ")
    text = _TRAIL_TIME_RE.sub("", text).strip("· ")
    return text or normalize(name)


def _segments(name: str) -> list[str]:
    text = scene_core(name)
    return [s for s in re.split(r"[·・/／|｜\s]+", text) if s]


def category(name: str) -> str:
    text = str(name or "").lower()
    if not text:
        return ""
    for tag, keys in CATEGORY_KEYS:
        if any(k.lower() in text for k in keys):
            return tag
    return ""


def scenes_similar(a: str, b: str) -> bool:
    """两个场景名是否可以视为同一地点（保守规则，宁可不合并）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ca, cb = scene_core(na), scene_core(nb)
    if ca and ca == cb:
        return True
    # 前缀分段：星冠学园·操场 vs 星冠学园·操场·樱花道 → 同场景
    sa, sb = _segments(na), _segments(nb)
    if sa and sb:
        short, long = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
        if len(short) >= 2 and long[:len(short)] == short:
            return True
        # 仅一段地点名互相包含（学校走廊 / 走廊）——长度差不超过 3
        if len(short) == 1 and len(long) == 1:
            x, y = short[0], long[0]
            if (x in y or y in x) and abs(len(x) - len(y)) <= 3:
                return True
        # 「操场」vs「星冠学园·操场」：短名是长名最后一段（或任一段的
        # 子串且长度差 <=3）——LLM 经常省略学校前缀，仍然算同一地点。
        if len(short) == 1 and len(long) >= 2:
            x = short[0]
            for seg in long:
                if x == seg or (x in seg and abs(len(x) - len(seg)) <= 3):
                    return True
    # 同一场景大类 + 高相似度（教室 / 教室走廊 之类）
    if category(na) and category(na) == category(nb):
        if difflib.SequenceMatcher(None, ca, cb).ratio() >= 0.82:
            return True
    return False


def canonical_scene(name: str, existing: list[str]) -> str | None:
    """在已登记场景名中找与 name 同地点的那个；找不到返回 None。

    返回 existing 里的**原名**（保留玩家第一次看到的名字），用于把
    LLM 的变体写法归一到同一个背景引用。
    """
    target = normalize(name)
    if not target:
        return None
    # 完全同名优先
    for old in existing:
        if normalize(old) == target:
            return old
    for old in existing:
        if scenes_similar(old, target):
            return old
    return None


def guess_initial_scene(*texts: str) -> str:
    """从世界观/大纲/标题推断开场场景；没有命中返回空串。

    只用于 `session["current_scene"]`（制服映射依据），不会显示给玩家；
    第一幕 LLM 返回 scene 后会被真实场景覆盖。
    """
    text = " ".join(str(t or "") for t in texts).lower()
    if not text.strip():
        return ""
    for tag, keys in CATEGORY_KEYS:
        if any(k.lower() in text for k in keys):
            return CATEGORY_SCENE.get(tag, "")
    return ""

"""TSF 转变系统：数值定义、阶段划分、立绘提示词构建。

内容边界：所有角色均为成年人，转变为「成年男性 → 中性 → 成年女性」，
剧情与立绘提示词均保持非露骨描写。
"""

STAT_KEYS = ["progress", "physique", "acuity",
             "genital", "habit", "immersion"]

STAT_META = {
    "progress": {
        "name": "性别同化率", "icon": "♀", "start": 0,
        "hint": "0% 成年男性形态 → 100% 完全女性形态",
    },
    "physique": {
        "name": "体质特征", "icon": "◈", "start": 0,
        "hint": "0 = 成年男性体格，50 = 中性，100 = 成年女性体格",
    },
    "acuity": {
        "name": "感官适应度", "icon": "✦", "start": 20,
        "hint": "身体感知与协调对新形态的适应程度",
    },
    "genital": {
        "name": "性征转化度", "icon": "◉", "start": 0,
        "hint": "生理形态向女性方向转变的完整程度（成人向转变主题，描写含蓄）",
    },
    "habit": {
        "name": "洗脑程度", "icon": "⚯", "start": 0,
        "hint": "行为习惯、思维模式向新身份贴近的深度",
    },
    "immersion": {
        "name": "堕落度", "icon": "◍", "start": 0,
        "hint": "对新身份与新生活的沉溺深度",
    },
}

# 阶段表：立绘与身份随「性别同化率」演进，全部为成年人形态。
# portrait 含英文 booru 标签（SD 类模型对标签比对中文描述更敏感），
# 强化各阶段的性别特征，防止写实模型画错性别。
# 1.7.25：支持 4/6/10 段（游戏内 stage_count 可配置，默认 10 段）；
# 四段为经典制、六段/十段为精细制（名称/身份/描述与立绘曲线逐段对齐）。
STAGES_V4 = [
    {
        "min": 0, "max": 20, "name": "阶段一 · 初变",
        "identity": "窥见变化之端的成熟男子",
        "portrait": ("成年男性 adult man, 1boy, solo male, handsome masculine face, "
                     "short hair, 皮肤开始变得细腻，发质变柔，眼神与神态略显柔和，"
                     "{anchor}"),
        "desc": "体格未变，但体毛渐疏、发质变软、精力流转——镜中还是那个男人，却莫名轻盈起来。",
    },
    {
        "min": 21, "max": 50, "name": "阶段二 · 微塑",
        "identity": "身形渐收的娇小身影",
        "portrait": ("气质中性的年轻成年人 androgynous young adult, 1person, "
                     "slim androgynous face, 男女特征交杂难辨，身形清瘦、线条柔和，"
                     "中长发，{anchor}"),
        "desc": "骨架悄悄收窄、肩线隐去、轮廓温柔模糊——身体开始替自己作主，还未变成别的谁。",
    },
    {
        "min": 51, "max": 80, "name": "阶段三 · 迷离",
        "identity": "性别模糊的中性身影",
        "portrait": ("年轻成年女性 young adult woman, 1girl, feminine beautiful face, "
                     "long hair, 面容清秀，身形纤细匀称，眉宇间仍残留些许从前的影子，"
                     "{anchor}"),
        "desc": "身体线条柔化、长发及肩，性别开始模糊——旧日男子只剩下眉宇间的一点影子，镜中人一时难辨男女。",
    },
    {
        "min": 81, "max": 100, "name": "阶段四 · 新生",
        "identity": "焕然一新的成年女性",
        "portrait": ("成年女性 mature elegant woman, 1girl, beautiful feminine face, "
                     "气质温婉而坚定，眉宇间依稀可辨昔日的神采，从容自信，"
                     "{anchor}"),
        "desc": "身形与面容完成蜕变，旧的自己只剩记忆——以新的姿态完整地生活。",
    },
]

# 六段制（1.7.29，用户定边界）：0-20/20-40/40-60/60-80/80-99/100；
# 主线=普通男性→娇小男性→中性→女性/萝莉属性少女，
# 中段两个「渐变属性」= 微塑（男→娇小男过渡）、迷离（娇小男→中性过渡）。
# 阶段输出按「全量重画（变化放开）」规则：每段一张全新的画。
STAGES_V6 = [
    {"min": 0, "max": 20, "name": "六段一 · 初变",
     "identity": "窥见变化之端的男子",
     "portrait": STAGES_V4[0]["portrait"],
     "desc": "体格未变，但肤质渐细、发质变软——普通的日常里，身体第一次露出端倪。"},
    {"min": 20, "max": 40, "name": "六段二 · 微塑",
     "identity": "身形渐收的娇小男子",
     "portrait": STAGES_V4[0]["portrait"],
     "desc": "渐变①：骨架悄悄收窄、肩线隐去——仍是男子，却一点点矮下去、柔下去。"},
    {"min": 40, "max": 60, "name": "六段三 · 迷离",
     "identity": "难辨男女的中性身影",
     "portrait": STAGES_V4[1]["portrait"],
     "desc": "渐变②：长发渐起、腰线若现——站进人堆里，旁人一时分不清男女。"},
    {"min": 60, "max": 80, "name": "六段四 · 绽放",
     "identity": "女性轮廓渐成的身影",
     "portrait": STAGES_V4[2]["portrait"],
     "desc": "线条柔化、眉目渐明——镜中的脸越来越陌生，也越来越像她。"},
    {"min": 80, "max": 99, "name": "六段五 · 羽化",
     "identity": "出落成娇小少女",
     "portrait": STAGES_V4[3]["portrait"],
     "desc": "身材与面容已完全女性——娇小纤细的少女身形，旧日男子只剩眉间一点影子。"},
    {"min": 100, "max": 100, "name": "六段六 · 新生",
     "identity": "完整的全新自我",
     "portrait": STAGES_V4[3]["portrait"],
     "desc": "转变完成——以最自在的娇小少女形态拥抱世界，旧日只活在记忆里。"},
]

# 十段制（1.7.25 默认）：每 10% 一段，变化逐档可辨
STAGES_V10 = [
    {"min": 0, "max": 9, "name": "第一段 · 察觉",
     "identity": "隐约感到变化的成熟男子",
     "desc": "照镜子时多看了两眼——皮肤细腻了些，却说不清哪里变了。"},
    {"min": 10, "max": 19, "name": "第二段 · 初变",
     "identity": "踏上变化之途的成年人",
     "desc": "体毛渐疏、发质变软、精力流转——身体开始替自己拿主意。"},
    {"min": 20, "max": 29, "name": "第三段 · 微塑",
     "identity": "身形微收的男子",
     "desc": "骨架悄悄收窄、肩线变柔、颌线圆润——仍是男子，却愈发轻盈。"},
    {"min": 30, "max": 39, "name": "第四段 · 柔化",
     "identity": "轮廓渐柔的娇小身影",
     "desc": "腮边再也不长胡茬，手脚变细，走路的姿态开始带着不自觉的轻盈。"},
    {"min": 40, "max": 49, "name": "第五段 · 模糊",
     "identity": "性别模糊的中性身影",
     "desc": "嗓音抬升，长发及肩，腰线若隐若现——旁人开始猜不透他的性别。"},
    {"min": 50, "max": 59, "name": "第六段 · 交融",
     "identity": "中性与女性交融的过渡身影",
     "desc": "曲线的雏形出现，眉眼柔和下来——既不像过去，也不再是他。"},
    {"min": 60, "max": 69, "name": "第七段 · 绽放",
     "identity": "女性轮廓渐成的身影",
     "desc": "身形曲线渐显、发丝垂肩——镜中的脸越来越熟悉，却越来越不认识。"},
    {"min": 70, "max": 79, "name": "第八段 · 羽化",
     "identity": "蜕变将成的女性",
     "desc": "女性特征几乎完成，举止与声音都有了新的韵律——旧影只剩眉间一点。"},
    {"min": 80, "max": 89, "name": "第九段 · 成形",
     "identity": "焕然一新的女性",
     "desc": "身姿、面容、气质全部归位，举手投足已是全新的自己。"},
    {"min": 90, "max": 100, "name": "第十段 · 新生",
     "identity": "以新身份完整的成年女性",
     "desc": "转变完成——以新的姿态面对世界，旧日男子只活在记忆里。"},
]

# ---------------------------------------------------------------------------
# 段特征系统（1.7.32 转变技能内建——「性转换变化」知识库）
# 主线（用户定案）：普通男 → 娇小男(渐变①) → 中性(渐变②) → 女化渐成
#   → 娇小少女 → 终形；STAGE_FEAT_TIERS 按**语义层级**存六档特征。
# 词库来源：网络 M2F 转变绘画教学沉淀（发长/身形/面容/气质逐段递进）
#  + 前几轮实机出图定稿（1.7.20/1.7.21/1.7.28/1.7.29 曲线与四/六图终验）。
# - portrait：该段英文特征词整句（SD 优先 booru 标签；中文辅词辅助 OpenAI 线）
# - core/expected：转变判定器加分词（core 高权——「这一段应该长什么样」）
# - forbidden：判定器重罚词（该段不应出现的相反特征——「转变正确性」凭据）
# 判定器（image.py）用 A1111 interrogate 给成品打标后按此评分，
# 多候选挑最优——「游戏内部自动挑转变正确的种子」，不依赖外部挑种子。
# ---------------------------------------------------------------------------
STAGE_FEAT_TIERS = [
    {"tier": 0, "name": "男子",
     "portrait": ("利落短发 short neat hair，成熟男性体格、宽阔肩背 "
                  "mature adult man, broad athletic build, broad shoulders，"
                  "棱角分明的成熟男性面容 mature masculine face, "
                  "strong sharp jawline, thick masculine eyebrows，"
                  "沉稳锐利、成熟可靠的男性气质 confident mature masculine aura"),
     "core": ["man", "male", "boy"],
     "expected": ["man", "male", "boy", "short hair", "muscular"],
     "forbidden": ["girl", "woman", "female", "feminine", "long hair",
                   "twin tails", "dress"]},
    {"tier": 1, "name": "娇小男子",
     "portrait": ("略长的软发、仍是短发 slightly longer soft hair, "
                  "soft rounded strands，身形娇小化、体格收窄 "
                  "petite slimmer male build, smaller stature, "
                  "narrowing shoulders，面容仍属男性、轮廓微柔 "
                  "boyish masculine face, jawline softening slightly, "
                  "soft features，安静乖巧的男孩气温柔气质 "
                  "quiet boyish gentle aura"),
     "core": ["petite", "boy", "male"],
     "expected": ["petite", "boy", "male", "slim", "soft", "short hair"],
     "forbidden": ["girl", "woman", "female", "feminine", "long hair",
                   "muscle", "beard"]},
    {"tier": 2, "name": "中性身影",
     "portrait": ("及肩顺发、中性柔顺 shoulder-length neat soft hair，"
                  "身形娇小青瘦、难辨男女 petite slim androgynous figure, "
                  "gender ambiguous shape，柔和中性面容、难辨性别 "
                  "soft androgynous face, ambiguous gender, delicate features，"
                  "温和安静的中性气质 calm neutral androgynous aura"),
     "core": ["androgynous", "ambiguous"],
     "expected": ["androgynous", "ambiguous", "slim", "delicate",
                  "soft", "shoulder"],
     "forbidden": ["beard", "muscular", "stubble", "male"]},
    {"tier": 3, "name": "女化渐成",
     # 1.7.39 缓化（用户：60-64% 直接变完整女性太激进）：发长压回及肩、
     # 加「仍在转变」锚词——「渐成」= 中点过渡，不是完成态
     "portrait": ("及肩柔发、正在变长 hair growing to shoulders, "
                  "longer soft hair，身形纤细、女性曲线初现但仍在过渡 "
                  "slender figure, feminine curves beginning, "
                  "still transforming, in-between stage，"
                  "面容女性化渐明、眉目渐柔 feminine features emerging "
                  "gradually, soft delicate features，"
                  "温婉渐显的女性气质 gentle feminine presence emerging"),
     "core": ["feminine", "girl", "woman"],
     "expected": ["feminine", "girl", "woman", "long hair", "slender", "curves"],
     "forbidden": ["boy", "male", "beard", "muscular", "man"]},
    {"tier": 4, "name": "娇小少女",
     "portrait": ("柔亮长发、及腰渐长 smooth long flowing hair, "
                  "waist-length soft strands，身形小巧娇美、纤细轻盈 "
                  "petite delicate female figure, small slim stature, "
                  "graceful frame，精致少女面容、明亮大眼 "
                  "refined cute young feminine face, large bright eyes, "
                  "soft features，清新明媚的少女气质 "
                  "fresh youthful feminine aura"),
     "core": ["petite", "girl", "cute"],
     "expected": ["girl", "petite", "cute", "feminine", "long hair", "youth"],
     "forbidden": ["boy", "male", "beard", "muscular", "man"]},
    {"tier": 5, "name": "终端形态",
     "portrait": ("及腰长发、发质柔亮 shining waist-length flowing hair，"
                  "完整娇小女性体态、曲线柔美 complete petite feminine figure, "
                  "graceful soft curves，秀美动人的少女面容 "
                  "exquisite lovely feminine face, large bright expressive eyes，"
                  "从容自信、风姿绰约的轻盈气质 "
                  "confident graceful feminine aura"),
     "core": ["feminine", "girl", "woman"],
     "expected": ["girl", "woman", "feminine", "long hair", "petite",
                  "confident"],
     "forbidden": ["boy", "male", "beard", "muscular", "man"]},
]

# 各段数局 → 每段的语义层级（V4/V6 按用户在档身份投影；V10 十档细粒度）
_STAGE_TIER = {
    4: [0, 1, 2, 4],
    6: [0, 1, 2, 3, 4, 5],
    10: [0, 0, 1, 1, 2, 3, 3, 4, 4, 5],
}

# 段负面（按语义层级；主角专用放宽——只压相悖特征，绝不封杀该段应有特征）
STAGE_NEG_TIER = {
    0: ("1girl, female, feminine face, twin tails, long hair, dress, "
        "breasts, woman, girl"),
    # 娇小男：封完整女性标志（防止模型 25% 就画完整女孩——1.7.19 实锤）
    # + 封胡须/肌肉（娇小化）；不封 man/1boy——该段仍是男性
    1: ("1girl, girl, woman, female, feminine face, long hair, dress, "
        "skirt, mature woman, huge breasts, massive curves, milf, "
        "heavy makeup, beard, stubble, muscular, muscles"),
    # 中性：封两端极端（男性肌肉胡须 + 过熟女性），中性→女化自由发生
    2: ("masculine muscles, beard, stubble, 1boy, man, mature woman, "
        "huge breasts, massive curves, milf, heavy makeup"),
    # 女化/少女/终形：只封男性残余
    3: ("1boy, male, masculine face, beard, man, muscular body, "
        "masculine muscles"),
}


def stage_tier(stage_idx: int, stages: list[dict] | None = None) -> int:
    """段序号 → 语义层级（0 男 / 1 娇小男 / 2 中性 / 3 女化 / 4 少女 / 5 终形）；
    未知段数按连续曲线的语义线性近似。"""
    table = stages if isinstance(stages, list) and stages else STAGES
    n = len(table)
    if n in _STAGE_TIER and 0 <= stage_idx < n:
        return _STAGE_TIER[n][stage_idx]
    progress = (table[min(stage_idx, n - 1)]["min"]
                + table[min(stage_idx, n - 1)]["max"]) / 2
    return int(round(progress / 20.0))  # 0..5 近似


def stage_feat(stage_idx: int, stages: list[dict] | None = None) -> dict:
    """段的特征锚点（portrait 词 + 判定词集）。"""
    return STAGE_FEAT_TIERS[stage_tier(stage_idx, stages)]


def stage_negative_relaxed_stage(stage_idx: int,
                                 stages: list[dict] | None = None) -> str:
    """按段的语义层级取主角放宽负面（1.7.32：与段边界对齐——旧 progress
    分档（<25/<50/<75）与六段边界（20/40/60/80）错位：40-50% 段被封
    「1girl/long hair/dress/skirt」，正是「变化不正常」的隐性元凶之一）。"""
    tier = stage_tier(stage_idx, stages)
    return STAGE_NEG_TIER.get(tier, STAGE_NEG_TIER[3])


def stage_gender_weights(stage_idx: int,
                         stages: list[dict] | None = None) -> tuple[float, float, float]:
    """段投影男/中性/女三向权重（1.7.32：阶段制下每段固定权重——比连续
    曲线（45% 起现/85% 全开）更贴合「每段一张」的节奏：中性段真正中性、
    女化段女权重已占主导；V4/V6 查表，V10 用段中心进度的连续曲线）。"""
    table = stages if isinstance(stages, list) and stages else STAGES
    n = len(table)
    if n in _STAGE_GW and 0 <= stage_idx < n:
        return _STAGE_GW[n][stage_idx]
    s = table[min(stage_idx, n - 1)]
    return _gender_weights(int((s["min"] + s["max"]) / 2))


# 段投影权重表（男/中性/女）：V4 四段=1.7.28 四图终验节奏（男→娇小男→
# 中性少女→少女终形）；V6 六段=1.7.29 六图终验节奏（男→娇小男→中性→
# 女化→少女→终形——段 2 中性偏男、段 3 女化占主导、段 4 不足 1 留柔和）
_STAGE_GW = {
    4: [(1.0, 0.0, 0.0), (0.80, 0.20, 0.0), (0.05, 0.60, 0.35),
        (0.0, 0.05, 0.95)],
    6: [(1.0, 0.0, 0.0), (0.85, 0.15, 0.0), (0.15, 0.65, 0.20),
        # 1.7.39 段4（60-80 绽放）女权重 0.70→0.58：60-64% 仍是
        # 「女化渐成」过渡态（用户反馈直接变完整女性太激进）
        (0.0, 0.42, 0.58), (0.0, 0.05, 0.95), (0.0, 0.0, 1.0)],
}

# 段特征投影回阶段表（portrait 字段此后始终与主线语义一致）
STAGES_V4 = [dict(s, portrait=STAGE_FEAT_TIERS[stage_tier(i, STAGES_V4)]["portrait"])
             for i, s in enumerate(STAGES_V4)]
STAGES_V6 = [dict(s, portrait=STAGE_FEAT_TIERS[stage_tier(i, STAGES_V6)]["portrait"])
             for i, s in enumerate(STAGES_V6)]
STAGES_V10 = [dict(s, portrait=STAGE_FEAT_TIERS[stage_tier(i, STAGES_V10)]["portrait"])
              for i, s in enumerate(STAGES_V10)]

STAGES = STAGES_V6   # 1.7.29 默认六段（用户边界 0-20/20-40/40-60/60-80/80-99/100；四段保留可切）


def build_stages(count: int) -> list[dict]:
    """按段数选择阶段表（4/6/10；其他数值回归六段）。"""
    return {4: STAGES_V4, 6: STAGES_V6, 10: STAGES_V10}.get(
        int(count) if count in (4, 6, 10) else 6, STAGES_V6)


def stages_for(session: dict) -> list[dict]:
    """会话阶段表：开局写入 session["stages"]（随档持久、档内稳定），
    旧档无该字段时按全局 config 的 stage_count 生成（默认六段）。"""
    stages = session.get("stages")
    if isinstance(stages, list) and stages and isinstance(stages[0], dict):
        return stages
    try:
        from . import config as _cfg
        n = int(_cfg.load_config()["game"].get("stage_count", 6))
    except Exception:
        n = 6
    stages = build_stages(n)
    session["stages"] = stages
    return stages

# 平滑过渡五档：0/25/50/75/100% 的特征锚点（发型/身形/面容/气质）。
# 与 STAGES 的四段选择器不同：提示词按进度连续取档，越贴近档位越精确。
# 1.7.13 重写：女性特征提前显现——25% 就有明显柔化（耳下发/窄肩/柔下颌），
# 50% 即曲线渐显（收腰/胯宽/胸部发育初现），75% 女性曲线成形，不再「前半段
# 全是男性、后半段才补女性」（用户反馈变化太保守、完全看不出来）。
SMOOTH_ATTRS = [
    # 1.7.21 五档重塑（用户路线：普通男性→娇小男性→中性→女性）：
    # 25%=娇小男性（身形变小但仍是男性），50%=中性难辨，
    # 75%=女性渐显，100%=完整女性——每档与相邻档差异适中，「逐渐变化」。
    (0.00, "利落短发 short neat hair",
     "成熟男性体格、宽阔肩背 mature adult man, broad athletic build, "
     "broad shoulders, masculine body",
     "棱角分明的成熟男性面容 mature masculine face, strong sharp jawline, "
     "thick masculine eyebrows",
     "沉稳锐利、成熟可靠的男性气质 confident mature masculine aura"),
    (0.25, "略长的软发、仍是短发 slightly longer soft hair",
     "身形娇小化、体格收窄 petite slimmer male build, smaller stature, "
     "narrowing shoulders",
     "面容仍属男性、轮廓微柔 still a boyish masculine face, jawline "
     "softening slightly",
     "安静乖巧、男孩气的温柔气质 quiet boyish aura, gentle but male"),
    (0.50, "及肩顺发、中性柔顺 shoulder-length neat hair",
     "身形娇小青瘦、难辨男女 petite slim androgynous figure, "
     "gender ambiguous shape",
     "柔和中性面容、难辨性别 soft androgynous face, ambiguous gender",
     "温和安静的中性气质 calm neutral androgynous aura"),
    (0.75, "长发披肩、光泽渐显 back-length glossy hair",
     "身形纤细、女性曲线初现 slender figure, feminine curves beginning",
     "面容女性化渐明、眉目渐柔 feminine face emerging",
     "温婉渐显的女性气息 gentle feminine presence"),
    (1.00, "及腰长发、发质柔亮 waist-length shining hair",
     "完整女性体态、曲线柔美 fully feminine figure, soft curves",
     "秀美动人的女性面容 beautiful feminine face",
     "从容自信、风姿绰约的女性气质 confident feminine aura"),
]

# 正面标签的男/中性/女三向权重（随进度连续变化，防止阶段跳变）。
# 1.7.21（用户路线「普通男性→娇小男性→中性→女性」的权重版）：
# 男性 58% 前保持存在（娇小男性段仍 1boy/男性词在场）、女性 45% 起现
# 85% 全开、中性 50% 附近峰值——50% 真正「难辨男女性」而非半女。
def _gender_weights(progress: int) -> tuple[float, float, float]:
    t = max(0, min(100, int(progress))) / 100
    male_w = max(0.0, min(1.0, 1 - t / 0.58))        # t=0 → 1.0；t≥58% → 0
    female_w = max(0.0, min(1.0, (t - 0.45) / 0.4))  # t=45% 起现；t=85% → 1.0
    neutral_w = max(0.0, 1 - male_w - female_w)
    return male_w, neutral_w, female_w


def portrait_prompt(anchor: str, progress: int, target_en: str = "") -> str:
    """按 0~100 同化率平滑生成角色立绘提示词（替代旧版按阶段硬切）。

    - 五档渐进属性：发型/身形/面容/气质随进度取最近档，先整体后局部；
    - 三向加权标签：1boy(男性权重) / androgynous(中性权重) / 1girl(女性
      权重) 连续渐变，可让 SD 类模型做出「同一个人在路上走到一半」的效果；
    - 中文描述带转变程度百分比，供 OpenAI 兼容模型理解阶段。
    - target_en（1.7.16）：主角「转变目标设定」（游戏内可配置的体型/头发/
      气质目标）——置于提示词前部，进度越高权重自然越高（正词由调用方
      按同化率加权）。
    """
    t = max(0, min(100, int(progress))) / 100
    male_w, neutral_w, female_w = _gender_weights(progress)
    idx = min(len(SMOOTH_ATTRS) - 1,
              int(round(t * (len(SMOOTH_ATTRS) - 1))))
    _, hair, build, face, aura = SMOOTH_ATTRS[idx]

    def tag(name: str, w: float) -> str:
        if w <= 0.05:
            return ""
        return f"({name}:{w:.2f})"

    tags = [t for t in (
        tag("1boy", male_w),
        tag("androgynous", neutral_w),
        tag("1girl", female_w),
        tag("masculine looking", male_w * 0.8),
        tag("soft feminine features", female_w * 0.9),
        # 1.7.13：身材/发型加权词——女性权重一旦 >0 就让 SD 看到明确的
        # 发型与身形轮廓变化方向（旧版只有中性描述，模型倾向「保持原样」）。
        # 只用「轮廓级」词：NSFW 向 realskin 模型对 hourglass/hip/chest 类
        # 强词会触发裸体/局部特写漂移，曲线细节交给受掩膜保护的 body-pass。
        # 权重注意：<1.0 是降权（曾写 0.8 导致发长永远不长——等于压制）
        tag("slender figure", min(1.0, female_w)),
        tag("long hair", min(1.2, female_w * 1.2)),
    ) if t]
    tag_line = ", ".join(tags) if tags else ""

    if t < 0.5:
        stage_hint = (f"转变程度 {progress}%：正从成年男性向中性过渡，"
                      "男性特征逐渐淡去、柔和轮廓显现")
    else:
        stage_hint = (f"转变程度 {progress}%：中性轮廓正在向女性定型，"
                      "女性特征渐明、旧日影子渐远")
    target_part = ""
    if target_en:
        # 目标词按性别权重爬升：低进度时弱（1girl 权重低自然收敛），
        # 高进度全量生效——模型一路上都能看见"终点形态"
        target_part = (f"，({target_en})" if female_w >= 0.75
                       else f"，{target_en}" if female_w >= 0.25 else "")
    return (f"{stage_hint}，{anchor}，{hair}，{build}，{face}，{aura}"
            + target_part
            + (f"，{tag_line}" if tag_line else ""))


def stage_portrait_prompt(anchor: str, progress: int, target_en: str = "",
                          stages: list[dict] | None = None) -> str:
    """阶段制主角立绘提示词（1.7.32，阶段跃迁=全量生成路径专用）。

    与 portrait_prompt（连续五档插值，every10/链式用）的区别：
    - **按段取特征锚点**：本次所处的「段」写死该段的头发/身形/面容/气质
      特征词（端口 = STAGE_FEAT_TIERS 语义层级），不再被 0/25/50/75/100
      五档插值稀释——六段 60-80「绽放」不再落在 50% 中性档（旧版错位：
      段边界 20/40/60/80/100 与曲线档位不对齐，正是「变化不正常」元凶）。
    - **段投影性别权重**：每段固定男/中性/女三向权重（不再 45% 才起现）。
    - 位置效应（1.7.13）：特征词紧跟在 anchor 之后处于提示词前段。
    """
    table = stages if isinstance(stages, list) and stages else STAGES
    stage_idx = stage_of(progress, table)
    feat = stage_feat(stage_idx, table)
    male_w, neutral_w, female_w = stage_gender_weights(stage_idx, table)

    def tag(name: str, w: float) -> str:
        if w <= 0.05:
            return ""
        return f"({name}:{w:.2f})"

    tags = [t for t in (
        tag("1boy", male_w),
        tag("androgynous", neutral_w),
        tag("1girl", female_w),
        tag("masculine looking", male_w * 0.8),
        tag("soft feminine features", female_w * 0.9),
        tag("slender figure", min(1.0, female_w)),
        tag("long hair", min(1.2, female_w * 1.2)),
    ) if t]
    tag_line = ", ".join(tags) if tags else ""
    t = max(0, min(100, int(progress))) / 100
    if t < 0.5:
        stage_hint = (f"转变程度 {progress}%：正从成年男性向中性过渡，"
                      "男性特征逐渐淡去、柔和轮廓显现")
    else:
        stage_hint = (f"转变程度 {progress}%：中性轮廓正在向女性定型，"
                      "女性特征渐明、旧日影子渐远")
    target_part = ""
    if target_en:
        # 目标词按段投影的女性权重爬升（同 portrait_prompt 语义）
        target_part = (f"，({target_en})" if female_w >= 0.75
                       else f"，{target_en}" if female_w >= 0.25 else "")
    return (f"{stage_hint}，{anchor}，{feat['portrait']}"
            + target_part
            + (f"，{tag_line}" if tag_line else ""))


def stage_negative(progress: int) -> str:
    """按进度选择立绘负面词（分档压制相反性别特征，中性窗口放宽）。"""
    if progress < 25:
        return STAGE_NEG_EN[0]
    if progress < 50:
        return STAGE_NEG_EN[1]
    if progress < 75:
        return STAGE_NEG_EN[2]
    return STAGE_NEG_EN[3]


# 主角专用放宽负面（1.7.16）：老曲线 25~50% 仍封杀「女性面容/长裙/长发」类
# ——这正是「转变看不出来」的隐性元凶（NEGATIVE 比正面更强）。
# 主角路径只留：低进度保住男性（阶段1 正确）、中高进度压制男性特征；
# 「女性向特征」词完全不放入负面，让转变自由发生；NPC 仍走原曲线。
STAGE_NEG_RELAXED = {
    0: "1girl, female, feminine face, twin tails, long hair, dress, "
       "breasts, woman, girl",
    # 1.7.17 曾只封「过熟」——1.7.19 实测（萝莉化曲线）证明模型在 25% 就
    # 直接画完整女孩（放宽无女性标志词 = 模型取默认少女）。档位1必须封
    # 「完整女性」标志（1girl/girl/woman/feminine face/long hair/dress/skirt）
    # 把起点钉在男性→柔化过渡；55% 后放开让萝莉化自由发生。
    # 1.7.28：**twin tails 已删除**——模型把「双马尾(twin)」联想到「双头」
    # （30% 连续两张双头成品，实锤）
    1: "1girl, girl, woman, female, feminine face, long hair, "
       "dress, skirt, mature woman, huge breasts, massive curves, milf, "
       "heavy makeup, 1boy, man, masculine muscles, beard",
    2: "masculine muscles, beard, stubble, 1boy, man",
    3: "1boy, male, masculine face, beard, man",
}


def stage_negative_relaxed(progress: int) -> str:
    """主角转变专用负面（放宽）：只压制相悖的男性特征，绝不封杀女性特征。"""
    if progress < 25:
        return STAGE_NEG_RELAXED[0]
    if progress < 50:
        return STAGE_NEG_RELAXED[1]
    if progress < 75:
        return STAGE_NEG_RELAXED[2]
    return STAGE_NEG_RELAXED[3]

# 各阶段立绘专用负面词：压制与当前阶段相悖的性别特征
STAGE_NEG_EN = {
    0: "1girl, female, feminine face, twin tails, long hair, dress, breasts, "
       "woman, girl",
    1: "1girl, female, feminine face, twin tails, dress, woman",
    2: "masculine muscles, beard, stubble, 1boy, man",
    3: "1boy, male, masculine face, beard, man",
}

# ---------------------------------------------------------------------------
# 渐变状态机（1.7.43：sd-webui 实机 TSF 渐变序列——v14/v15 十七帧链式配方
# 沉淀移植）。六条独立状态机按语义层级（stage_tier 0..5）逐段推进，
# **每段只推动 1-2 个主维度**——「逐渐变化」的词级实现；全部为 danbooru
# 英文标签（可带 (词:权重)），由 build_stage_plan 在开局预写进整局计划。
# ---------------------------------------------------------------------------
HAIR_STATE = {  # 发长链（发色由 HAIR_COLOR/HAIR_KEEP 链负责）
    0: "short neat hair",
    1: "slightly longer soft hair, hair tips softening",
    2: "(ear-length hair lengthening:1.15), strands reaching the collar",
    3: "neck-length hair, (hair lengthening:1.2), strands past the collar",
    4: "shoulder-length hair, (strands gathering at sides:1.15)",
    5: "long flowing settled hairstyle, smooth strands",
}
HAIR_COLOR_STATE = {  # 发色溶解链（{c}=裸色词，{color}=完整色词；换色局用）
    0: "",
    1: "",
    2: "hair tips turning {c}",
    3: "(half {c} hair:1.15), two-tone dissolving hair color",
    4: "(mostly {c} hair:1.2)",
    5: "{color} fully settled",
}
HAIR_KEEP_STATE = {  # 发色锁链（维持原色局：逐段轻锁防漂移）
    0: "(same hair color:1.2), (keep original hair color:1.15)",
    1: "(same hair color:1.2), (keep original hair color:1.15)",
    2: "(same hair color:1.15)",
    3: "(same hair color:1.15), hair color unchanged",
    4: "(same hair color:1.1)",
    5: "(same hair color:1.1)",
}
FACE_STATE = {  # 脸型链（成熟男 → 微柔 → 正太/中性 → 渐女 → 少女）
    0: "mature masculine face, strong jawline",
    1: "(jawline softening:1.1), boyish masculine face",
    2: "(boyish round face:1.2), (androgynous soft face:1.1)",
    3: "(soft feminine features emerging:1.2), face between boyish and girlish",
    4: "(cute girlish face:1.2), (large bright eyes:1.15)",
    5: "(delicate refined girlish face:1.25), large bright expressive eyes",
}
EXPR_STATE = {  # 表情链（惊讶 → 挣扎 → 失神 → 空茫 → 无表情天然呆）
    0: "surprised expression, wide eyes",
    1: "(confused frown:1.1), dizzy expression, light sweat",
    2: "(dazed vacant stare:1.2), half-lidded eyes",
    3: "(blank stare:1.15), expression fading",
    4: "(expressionless:1.2), (natural airhead:1.15), dreamy empty look",
    5: ("(expressionless:1.25), (natural airhead:1.2), blank stare, "
        "(glossy eyes:1.1), soft eye highlights"),
}
CLOTH_STATE = {  # 服装重组链（完整 → 破损 → 碎裂重组 → 新装成形 → 完成）
    0: "plain everyday male clothing",
    1: "(slightly torn shirt:1.1), fraying sleeve cuffs",
    2: "(torn shirt:1.2), torn trouser knees",
    3: "(torn clothes reassembling:1.2), ragged skirt forming at the waist",
    4: "(new outfit mostly formed:1.15), remnants of old clothes",
    5: "complete new outfit, cleanly fitted",
}
GENITAL_STATE = {  # 性征链（含蓄版：立绘默认，全程非露骨）
    0: "",
    1: "(slimmer boyish build:1.05)",
    2: "waist narrowing, figure slimming",
    3: "(girlish figure forming:1.15), chest developing slightly",
    4: "(petite girlish figure:1.2), (flat chest:1.15)",
    5: "(complete petite feminine figure:1.2)",
}
# 18+ 显性档（1.7.47 升级为 v14/v15 实机十七帧原词，四段收缩链：
# 射精后半软短缩 → 残端+女器成形 → 完全女器 → 完成体；tier0/1 保持空）。
# 仅 tsf_explicit 开关 + 会话 r18 同时成立时入词（默认关闭=非露骨红线不变）。
GENITAL_EXPLICIT_STATE = {
    0: "",
    1: "",
    2: ("(post-ejaculation:1.2), (semi-erect genitals:1.25), "
        "(genital anatomy slightly shortened:1.2), (shrinking starting:1.15)"),
    3: ("(genital transformation:1.25), (small remnant:1.3), "
        "(half-formed feminine mound:1.3), (feminine anatomy developing:1.3)"),
    4: ("(genital transformation:1.2), (male remnant gone:1.35), "
        "(fully formed feminine anatomy:1.35)"),
    5: "fully transformed female body, complete feminine anatomy",
}
# wai 显性叠层（v15 S7「高潮后洗脑完成」节拍原词）：explicit+wai 时叠入 tier2-3
WAI_EXPLICIT_EXTRA = {
    0: "",
    1: "",
    2: ("(mind control complete:1.2), (hypno spiral in eyes:1.3), "
        "(blank face:1.15)"),
    3: "(spiral fading in eyes:1.1), (blank stare:1.15), expression numbing",
    4: "",
    5: "",
}

# wai-illustrious 系特配阶段词叠层（1.7.46：v14/v15 实机 17 帧全链配方——
# wai 模型对 hypno/螺旋瞳/失神表情的渲染极稳，把「洗脑→无表情天然呆」节拍
# 前置一档：tier2 就出螺旋洗脑光、tier3 眼神转空、tier4 定稿纯天然呆高光）。
# 只在 build_stage_plan(style="wai") 时叠加；词均为英文标签（可带权重）。
WAI_EXTRA = {
    0: "",
    1: "",
    2: ("(hypno spiral eyes:1.2), (glowing pink-bright eyes:1.15), "
        "(spiral light above head:1.1), (dazed:1.1)"),
    3: "(glowing eyes fading:1.1), (spiral fading:1.05), (blank stare:1.15)",
    4: ("(expressionless:1.25), (natural airhead:1.2), blank stare, "
        "(glossy eyes:1.15), (soft eye highlights:1.05), "
        "(low twintails forming:1.1)"),
    5: ("(expressionless:1.25), (natural airhead:1.2), blank stare, "
        "(glossy eyes:1.15), soft eye highlights"),
}
# wai 模式发长链（比通用链更贴近实机 v15 节拍：短发滞留两段 → 渐长 →
# 双马尾成形——早期「发长缓慢」、后期「发型落定」）
WAI_HAIRSTEP_EXTRA = {
    0: "",
    1: "",
    2: "(hair lengthening slightly:1.1), strands growing at the nape",
    3: "(hair at shoulder length:1.15), longer soft strands",
    4: "(hair strands gathering at sides:1.15), (twin buns forming:1.1)",
    5: "(settled low twintails:1.2), (hair accessory:1.05)",
}

# ---------------------------------------------------------------------------
# LLM 渐变规划器（1.7.48：AI API 按角色情况编排「这一局的变化过程」——
# 不是固定模板：LLM 按角色锚点/转变目标/剧情摘要/段数出**每段六维度词**，
# 与状态机模板合并（LLM 词追加在后=方向主导，模板词兜底保底）。
# 失败/未配置/缺字段 → 模板计划原样，绝不断功能。
# ---------------------------------------------------------------------------
GRADIENT_KEYS = ("hair", "face", "expression", "clothes", "body")

LLM_GRADIENT_SYSTEM = (
    "你是 TSF 性转渐变立绘规划器。根据角色外貌、转变目标、剧情语境与段位安排，"
    "给**每一段**写出「本段应呈现的样子」的英文生图标签（danbooru 风格 tag，"
    "逗号分隔，可用 (词:权重) 加权，用户输入为中文时先翻译）。\n"
    "只输出一个 JSON，不要任何解释：\n"
    "{\n"
    '  "segments": [\n'
    '    {"stage": 0, "hair": "...", "face": "...", "expression": "...",\n'
    '     "clothes": "...", "body": "..."},\n'
    "    ... 每段一个对象\n"
    "  ]\n"
    "}\n"
    "规则：\n"
    "- stage 从 0 到 段数-1，必须全部覆盖；每段必须是**同一个人**的延续；\n"
    "- 六维度（发长/发色、脸型、表情、服装、身形/性征）中每段只推进 1-2 个，"
    "其余写空串（空串=沿用上段，管线自动补保底词）；变化要小步、连贯、逐段可辨；\n"
    "- 发色/瞳色/发型锚点与角色设定一致，终形与转变目标一致；不要换画风；\n"
    "- 不写露骨词（性征只用轮廓级词，如 petite/slim/curves）；显性模式输入会注明；\n"
    "- 每段末尾可加 (facing camera:1.1)，也可省略（管线自动补正面词）。"
)


def merge_llm_plan(base_plan: list[dict], segments) -> list[dict]:
    """LLM 每段六维度词 → 合并进模板计划（追加在后=方向主导）。
    segments 非法/缺段 → 该段保持模板原样；整体非法 → 返回原计划。"""
    if not isinstance(segments, list) or not segments:
        return base_plan
    by_stage: dict[int, dict] = {}
    for s in segments:
        if isinstance(s, dict) and "stage" in s:
            try:
                by_stage[int(s["stage"])] = s
            except (TypeError, ValueError):
                continue
    out: list[dict] = []
    for e in base_plan:
        s = by_stage.get(int(e.get("stage", -1)))
        if not s:
            out.append(e)
            continue
        extra = ", ".join(
            str(s.get(k) or "").strip() for k in GRADIENT_KEYS
            if str(s.get(k) or "").strip())
        if not extra:
            out.append(e)
            continue
        merged = dict(e)
        merged["prompt"] = e["prompt"] + "，" + extra
        merged["llm"] = True
        out.append(merged)
    return out


def llm_gradient_user_text(anchor: str, target_en: str, stages: list[dict],
                           progress: int, summary: str = "", style: str = "generic",
                           explicit: bool = False) -> str:
    """LLM 渐变规划的用户侧上下文（角色情况）。"""
    seg_lines = []
    for i, s in enumerate(stages):
        seg_lines.append(f"  {i}: {s.get('name', '')}（{s.get('min')}-{s.get('max')}%）")
    cur = max(0, min(100, int(progress)))
    return (
        f"角色外貌锚点：{anchor or '无（普通青年）'}\n"
        f"转变目标（终形）：{target_en or '无（自然女性化）'}\n"
        f"段数：{len(stages)}；各段：\n" + "\n".join(seg_lines) + "\n"
        f"当前同化率：{cur}%\n"
        f"剧情摘要：{str(summary or '无')[:120]}\n"
        f"模型风格：{style} ｜ 显性模式：{'开' if explicit else '关'}\n"
        "请按规则输出 JSON。"
    )


# 链式渐变重绘度曲线（实机配方：同一人渐变 = img2img 小步；低段保身份
# 0.30-0.38、女化段加大到 0.42-0.50，跨段再 +0.08（封顶 0.62）。
# 终形跃迁仍走全量生成路径（img2img 无法跨性别的实机定论不变）。
STAGE_DENOISE_CURVE = {0: 0.30, 1: 0.34, 2: 0.38, 3: 0.42, 4: 0.46, 5: 0.50}


def stage_chain_denoise(stage_idx: int, stages: list[dict] | None = None,
                        cross_stage: bool = False) -> float:
    """段语义层级 → 链式渐变 img2img 的自主重绘度（1.7.43 实机曲线）。"""
    tier = stage_tier(stage_idx, stages)
    base = STAGE_DENOISE_CURVE.get(tier, 0.38)
    return min(0.62, base + (0.08 if cross_stage else 0.0))


def _gender_tag_line(male_w: float, neutral_w: float,
                     female_w: float) -> str:
    """男/中性/女三向权重 → 加权标签行（与 stage_portrait_prompt 同式）。"""

    def tag(name: str, w: float) -> str:
        if w <= 0.05:
            return ""
        return f"({name}:{w:.2f})"

    tags = [t for t in (
        tag("1boy", male_w),
        tag("androgynous", neutral_w),
        tag("1girl", female_w),
        tag("masculine looking", male_w * 0.8),
        tag("soft feminine features", female_w * 0.9),
        tag("slender figure", min(1.0, female_w)),
        tag("long hair", min(1.2, female_w * 1.2)),
    ) if t]
    return ", ".join(tags)


def _gradient_state_words(tier: int, *, hair_color_en: str = "",
                          keep_hair_color: bool = True, cloth: bool = True,
                          explicit: bool = False) -> str:
    """语义层级 → 六状态机当档词串（发长+发色+脸+表情+服装+性征）。"""
    parts = [HAIR_STATE.get(tier, ""), FACE_STATE.get(tier, ""),
             EXPR_STATE.get(tier, "")]
    if hair_color_en and not keep_hair_color:
        w = HAIR_COLOR_STATE.get(tier, "")
        if w:
            bare = (hair_color_en[:-5] if hair_color_en.endswith(" hair")
                    else hair_color_en)
            parts.append(w.format(c=bare, color=hair_color_en))
    elif keep_hair_color:
        w = HAIR_KEEP_STATE.get(tier, "")
        if w:
            parts.append(w)
    if cloth:
        parts.append(CLOTH_STATE.get(tier, ""))
    parts.append(GENITAL_EXPLICIT_STATE.get(tier, "") if explicit
                 else GENITAL_STATE.get(tier, ""))
    return ", ".join(p for p in parts if p)


def build_stage_plan(stages: list[dict], anchor: str = "",
                     target_en: str = "", hair_final_en: str = "",
                     keep_hair_color: bool = True, cloth_states: bool = True,
                     explicit: bool = False, style: str = "generic") -> list[dict]:
    """开局（或转变目标变更）按当前段数**预写整局每段立绘提示词**。

    「写出六步/十步变化」的能力核心（1.7.43）：对每一段把
    STAGE_FEAT_TIERS 段特征锚词与六条渐变状态机（发长/发色溶解/脸型/
    表情/服装重组/性征）合成——逐段只推进 1-2 个主维度，十段制下同一
    语义层级第二次出现时自动加「较前段更进一步」词，保证十步内每步
    仍有可辨变化。返回 [{stage,tier,name,prompt,negative}]（存
    session["stage_plan"]，由 game 层在开局/目标变更时重建）。
    style="wai"（1.7.46）：叠加 wai-illustrious 特配词（洗脑螺旋/天然呆
    前置、双马尾成形、发长节拍对齐实机 v15），其余样式保持通用链。
    """
    wai = str(style or "").lower() == "wai"
    plan: list[dict] = []
    seen: dict[int, int] = {}
    for i, s in enumerate(stages):
        tier = stage_tier(i, stages)
        occ = seen.get(tier, 0)
        seen[tier] = occ + 1
        male_w, neutral_w, female_w = stage_gender_weights(i, stages)
        tag_line = _gender_tag_line(male_w, neutral_w, female_w)
        grad = _gradient_state_words(tier, hair_color_en=hair_final_en,
                                     keep_hair_color=keep_hair_color,
                                     cloth=cloth_states, explicit=explicit)
        if wai:
            ext = [WAI_EXTRA.get(tier, ""), WAI_HAIRSTEP_EXTRA.get(tier, "")]
            if explicit:
                ext.append(WAI_EXPLICIT_EXTRA.get(tier, ""))
            grad = ", ".join(p for p in [grad] + ext if p)
        step_word = ""
        if occ > 0:
            step_word = ("(further along the transformation than the "
                         "previous stage:1.15), features more defined, ")
        target_part = ""
        if target_en:
            target_part = (f"，({target_en})" if female_w >= 0.75
                           else f"，{target_en}" if female_w >= 0.25 else "")
        prompt = (f"{anchor}，{step_word}{grad}，"
                  f"{STAGE_FEAT_TIERS[tier]['portrait']}"
                  + target_part
                  + (f"，{tag_line}" if tag_line else "")
                  # 1.7.46：预写计划词自包含「正面看向镜头/玩家」——
                  # generate_portrait 的 PORTRAIT_EN_SUFFIX 已带同系词，
                  # 计划词保留轻权版本供 API/Comfy 等独立消费路径使用
                  + "，(facing camera:1.2), (looking at viewer:1.2), "
                    "(front view:1.15), direct eye contact")
        plan.append({
            "stage": i,
            "tier": tier,
            "name": s.get("name", ""),
            "prompt": prompt,
            "negative": STAGE_NEG_TIER.get(tier, STAGE_NEG_TIER[3]),
        })
    return plan


def plan_stage_entry(plan, progress: int,
                     stages: list[dict] | None = None) -> dict | None:
    """预写计划 → 当前进度所在段条目（无计划/结构不符返回 None）。"""
    if not isinstance(plan, list) or not plan:
        return None
    idx = stage_of(progress, stages)
    for e in plan:
        if isinstance(e, dict) and int(e.get("stage", -1)) == idx:
            return e
    return None


MOCK_DELTA = {"progress": 9, "physique": 9, "acuity": 4,
              "genital": 4, "habit": 5, "immersion": 5}


def default_stat_config(meta: dict | None = None) -> list[dict]:
    """内置状态默认配置（面板顺序）：[{key,name,icon,start,hint}]。"""
    meta = meta or STAT_META
    return [{"key": k, "name": m["name"], "icon": m["icon"],
             "start": int(m["start"]), "hint": m["hint"]}
            for k, m in meta.items()]


def merge_stat_config(raw, meta: dict, lock_keys: list[str] | None = None,
                      limit: int = 16) -> list[dict]:
    """规范化玩家自定义状态表（开局前编辑器提交）。

    raw: [{key,name,start}]。内置 key（progress 等）保留 key、允许改名/改初始值
    与调序；新项 key 为空 → 自动分配 custom_1..N（图标 ✦）。lock_keys
    （TSF 的 progress 是阶段引擎核心）缺失时自动补回最前部；未提交任何编辑
    （raw 为空）时返回内置默认。
    """
    defaults = {d["key"]: d for d in default_stat_config(meta)}
    entries = raw if isinstance(raw, list) else []
    if not entries:
        return default_stat_config(meta)
    out: list[dict] = []
    used_custom = 0
    for e in entries[:limit]:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()[:10]
        if not name:
            continue
        try:
            start = max(0, min(100, int(round(float(e.get("start", 0))))))
        except (TypeError, ValueError):
            start = 0
        key = str(e.get("key", "")).strip()
        if key in defaults:
            d = defaults[key]
            out.append({"key": key, "name": name, "icon": d["icon"],
                        "start": start, "hint": d["hint"]})
        else:
            used_custom += 1
            out.append({"key": f"custom_{used_custom}", "name": name,
                        "icon": "✦", "start": start,
                        "hint": "玩家自定义状态，由剧情驱动"})
    for k in (lock_keys or []):
        if k not in {x["key"] for x in out}:
            d = defaults.get(k)
            if d:
                out.insert(0, {"key": k, "name": d["name"], "icon": d["icon"],
                               "start": d["start"], "hint": d["hint"]})
    return out[:limit]


def initial_stats(config: list[dict] | None = None) -> dict:
    cfg = config if isinstance(config, list) and config else default_stat_config()
    return {c["key"]: int(c["start"]) for c in cfg}


def stage_of(progress: int, stages: list[dict] | None = None) -> int:
    """按进度取阶段序号；stages 可传会话阶段表（默认 STAGES=十段制）。"""
    table = stages if stages else STAGES
    progress = max(0, min(100, int(progress)))
    for i, s in enumerate(table):
        if progress <= s["max"]:
            return i
    return len(table) - 1


def sanitize_delta(delta, keys: list[str] | None = None) -> dict:
    """清洗 LLM 返回的数值变化：只认已知项（含玩家自定义），单项幅度限制在 ±15。"""
    known = list(keys) if keys is not None else STAT_KEYS
    out = {}
    if isinstance(delta, dict):
        for k, v in delta.items():
            if k in known:
                try:
                    out[k] = max(-15, min(15, int(round(float(v)))))
                except (TypeError, ValueError):
                    continue
    return out


def apply_delta(stats: dict, delta: dict) -> dict:
    """应用变化并截断到 0~100，返回应用后的数值。"""
    for k, v in delta.items():
        if k in stats:
            stats[k] = max(0, min(100, stats[k] + v))
    return stats


def stats_block(stats: dict, identity: str, stage_idx: int,
                config: list[dict] | None = None,
                stages: list[dict] | None = None) -> str:
    """构造注入剧情 prompt 的主角状态块（按会话状态配置，含自定义状态）。
    stages=会话阶段表（1.7.25）：段落数跟随对局设置（4/6/10）。"""
    table = stages if stages else STAGES
    cfg = config if isinstance(config, list) and config else default_stat_config()
    lines = [f"目前身份：{identity}（{table[stage_idx]['name']}）"]
    for c in cfg:
        val = int(stats.get(c["key"], c["start"]))
        lines.append(f"- {c['name']}：{val}/100（{c['hint']}）")
    lines.append(f"本阶段身体特征参考：{table[stage_idx]['desc']}")
    return "\n".join(lines)


def panel_view(stats: dict, identity: str, stage_idx: int,
               config: list[dict] | None = None,
               stages: list[dict] | None = None) -> dict:
    """给前端状态面板的数据（按会话状态配置，含自定义状态）。"""
    table = stages if stages else STAGES
    cfg = config if isinstance(config, list) and config else default_stat_config()
    return {
        "stats": [
            {"key": c["key"], "name": c["name"], "icon": c["icon"],
             "value": int(stats.get(c["key"], c["start"])), "hint": c["hint"]}
            for c in cfg
        ],
        "identity": identity,
        "stage": stage_idx,
        "stage_name": table[stage_idx]["name"],
        "stage_desc": table[stage_idx]["desc"],
    }

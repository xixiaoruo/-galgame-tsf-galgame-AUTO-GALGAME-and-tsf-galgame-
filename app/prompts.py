"""LLM 提示词模板（TSF 版本）。所有角色均为成年人；尺度由内容分级政策调控。"""

TSF_STORY_SYSTEM = """\
你是一个专业的 TSF（性别转换）题材视觉小说（galgame）剧作家，负责续写一段关于\
「主角从成年男性逐渐转变为成年女性」的互动故事。所有角色均为成年人，\
叙事尺度严格遵循下方的【内容分级政策】——政策允许的尺度可以充分展开，\
政策未允许的尺度一律不写。

【世界观设定】
{world}

【故事大纲（剧情骨架，按此脉络推进，可在细节上自由发挥）】
{outline}

【主角档案】
- 名字：{pname}
- 外貌锚点（各阶段保持一致的外貌特征）：{anchor}

【主角当前状态（剧情必须与这些数值严格相符）】
{stats}

【当前服装状态（角色现在穿什么，剧情描写必须与之一致）】
{outfit_state}

【故事书（世界设定与规则，剧情必须遵守）】
{lore}

【内容分级政策（必须严格遵守）】
{content_policy}

【创作指令（玩家指定的创作要求，写作时必须遵循）】
{directives}

【写作技法（必须应用）】
{craft_rules}

【其他角色】
{characters}

【各角色数值状态（每个出场角色一套独立数值，含主角；描写必须与各自数值相符）】
{char_states}

【可用表情列表（NPC 对话的 emotion 字段只能从中选择）】
{emotions}

【输出格式 —— 必须只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块标记】
{{
  "scene": "当前场景的简短名称",
  "present": ["在场角色名（此场景中真正出现在主角面前的其他角色；主角不必列入；不在场的角色必须从列表移除）"],
  "new_characters": [
    {{"name": "新登场的其他角色名（2~5字中文名；必须同时列在 present 中——只有本幕真正登场的新角色才写，没有新角色登场时返回空列表 []）", "role": "身份/与主角的关系（如：同班同学、社团学姐、咖啡店店员）", "appearance": "完整外貌描写（发色发型、瞳色、服装、气质，40~80字，供立绘生成，必须具体）", "personality": "性格与说话风格", "traits": ["性格标签 2~5 个，如：慢热、嘴硬心软、观察力强"], "age": 22, "height_cm": 164, "body_type": "纤细高挑/娇小柔软/匀称标准等", "measurements": "B86 / W58 / H88（可留空）", "birthday": "5月12日（可留空）", "speech_style": "说话语气、口头禅、句长", "likes": "喜欢的事物", "dislikes": "讨厌的事物", "background": "角色背景（80~150字：出身/日常、与主角的因缘、一个可被剧情利用的钩子）"}}
  ],
  "outfit": {{"角色名": "该角色当前穿着（未换装不写该键）"}},
  "cg": {{"active": false, "title": "CG 标题（4~12字）", "prompt": "CG 画面描述（40~80字，横版电影镜头感，多人同框允许；仅在 active=true 时填写）"}},
  "background_hint": "场景背景的画面描述（15~30字，必须与 scene 的视觉内容严格一致，用于生成背景图；场景没变就重复上一段描述）",
  "is_new_background": false,
  "dialogue": [
    {{"character": "角色名（其他角色用设定中的名字；主角用「{pname}」；旁白用 \"旁白\"）", "emotion": "表情（主角固定 neutral；旁白固定 neutral；NPC 从可用表情列表选）", "text": "台词或旁白（旁白描写环境与身体/心理变化，台词口语化，不加引号）"}}
  ],
  "choices": [
    {{"text": "选项文字（第一人称主角的行动或回应，10~20字）", "effect": "选择后的剧情走向简述（20字内）", "bias": "该选项倾向增长的数值名（如 {bias_hint}，仅一项）"}}
  ],
  "stats_delta": {{ {stats_schema} }},
  "states_delta": {{"角色名": {{ {char_states_schema} }}}},
  "identity": "一句话描述主角当前的形态与状态（呼应当前阶段）",
  "rename": null
}}

【写作与数值要求】
1. 每段 dialogue 2~5 条，旁白与台词穿插；身体与心理变化是叙事核心，要写出「具体的日常细节」。
2. 数值演变渐进合理：stats_delta 每项通常 0~±6，重大转折最多 ±12；多数回合只变化 1~3 项；\
转变不应无故倒退（progress 基本不下降）。
2a. states_delta 是「各角色自己的情绪与身体状态」（心情 / 好感 / 身体敏感度 / 高潮度），
    与主角的 stats_delta（转变进度）相互独立，两者都要输出。键必须是【各角色数值状态】
    里出现过的角色名（主角用「{pname}」），只写本幕确实有变化的角色，没变化的不写；
    单项通常 0~±6，重大转折最多 ±12。
3. 选项 3~4 个，风格有差异（顺从 / 抗拒 / 试探 / 回避），bias 指明该选项倾向影响哪项数值。
4. 剧情符合当前阶段与数值描述：低同化率时不能出现后期才有的身体特征，反之亦然。
5. 所有角色均为成年人；叙事尺度遵循【内容分级政策】。
6. 只输出 JSON，不要输出任何解释。
6a. rename：仅当本幕剧情中主角发生了改名（被家人/朋友/组织授予新名字、因身份转变主动改名等），
    才输出新名字（2~6字，不含空白）；否则输出 null。改名不可频繁（至少隔 6 幕）且应剧情自洽；
    从改名的当幕起，dialogue 与后续剧情统一使用新名字。
7. present 数组必须准确反映场景内实际出现的角色：谁出场写谁，没出场的不写；
   场景内无其他角色时写 []。立绘系统会根据它决定是否显示角色。
8. 内容分级政策优先于一切写作倾向：政策禁止的尺度一律不写，哪怕剧情自然走向
   也不得越界；所有角色均为成年人，严禁任何未成年人相关的不当内容。
9. 新角色引入规则：剧情需要新人物时，用 new_characters 数组一次给出其完整外貌、
   身份/关系、性格与背景（外貌必须具体，供立绘生成；background 写过去与因缘，
   不是外貌复述），角色池会自动登记并生成其立绘；已经出过场的角色不要再写入
   new_characters。无需新人物时写 []。
10. 换装检测：{outfit_policy}
11. TSF 数值稳定：体质特征（physique）必须与性别同化率（progress）保持同步，
   差值不超过 10；progress 不下降。
12. CG 规则：{cg_policy}
13. 关系阶段必须影响写法：【各角色数值状态】里的「关系 陌生/认识/熟悉/亲近/心动/依恋/恋人」
    是当前关系的硬约束——称呼、身体距离、主动性、吃醋/回避反应都要与阶段相符；
    升级要写出铺垫与契机，不能一幕之内从陌生跳到恋人；最近情感记忆可自然回扣，
    但不要机械复述数值。
14. scene 用稳定的地点名（2~10 字，如「学校教室」「商店街」），不要每幕加时间/
    天气/人物/临时动作；同一地点请沿用同一个名字，避免背景反复重画。
15. 已有角色卡中的性格标签、年龄、身高、体型、三围、说话风格是硬设定：
    剧情只能让人物在压力下表现出层次，不能改写这些固定属性；只有 new_characters
    才填写这些字段。角色卡 locked_fields / personality_locked 标记的字段绝对不可改。"""

TSF_SUMMARY_SYSTEM = """\
你是视觉小说的剧情记录员。请把下面已发生的 TSF 故事浓缩为简洁摘要（300字内），\
保留关键事件、主角身体与心理的变化节点、人物关系。只输出摘要正文。"""

TSF_OUTLINE_SYSTEM = """\
你是 TSF（性别转换）题材的故事企划师。所有角色均为成年人。请根据世界观与主角信息，\
创作一份互动故事大纲，作为后续剧情的骨架。

【输出格式 —— 只输出一个 JSON 对象，不要任何其他文字】
{{
  "title": "作品标题",
  "outline": "故事大纲（300~500字）：起因、转变的契机与代价、主要冲突与转折点、大致的结局走向。\\n分3~5个小节，每节以「◆」开头标注章节名。大纲要给数值变化留出渐进的空间。"
}}

要求：转变过程循序渐进（对应四个阶段），有悬念与代价；叙事尺度交由后续的\
内容分级政策调控。只输出 JSON。"""

TSF_RANDOM_SETUP_SYSTEM = """\
你是视觉小说企划师。请生成一个 TSF（性别转换）题材的全新开局设定。所有角色均为成年人。

【输出格式 —— 只输出一个 JSON 对象，不要任何其他文字】
{{
  "title": "作品标题",
  "world": "世界观与故事背景设定（100~200字，包含时代、地点、转变的缘由或规则、基调）",
  "outline": "故事大纲（5 段，每段以 ◆ 开头的一句标题+一两句剧情，覆盖初遇/转变契机/关系变化/抉择/结局）",
  "characters": [
    {{"name": "角色名（2~4字）", "appearance": "外貌描述（发色发型、瞳色、服装、气质，40~80字，供立绘生成用）", "personality": "性格与说话风格（40字内）", "traits": ["性格标签 2~4 个"], "age": 22, "height_cm": 164, "body_type": "体型", "measurements": "B86 / W58 / H88（可留空）", "speech_style": "说话语气", "likes": "喜欢的事物", "dislikes": "讨厌的事物"}}
  ]
}}

要求：characters 为 1~3 个成年人角色（不含主角），他们会见证/推动主角的转变。\
题材自由发挥（现代都市 / 奇幻诅咒 / 科幻实验 / 都市传说等）；叙事尺度交由后续的\
内容分级政策调控。只输出 JSON。"""


# ---- 分级政策驱动的提示词片段 ----

_OUTFIT_RULE_HEAD = "只有角色真正换了衣服时，才在 outfit 对象里写该角色的新穿着；未换装的角色不要写键。"


def outfit_policy_safe() -> str:
    """全年龄/16+：服饰仅限常规服装。"""
    return (_OUTFIT_RULE_HEAD
            + "服饰仅限正常日常/正式服装（礼服、便装、制服、运动服、家居服、睡衣等），"
            "严禁出现裸体、内衣、内裤类服饰状态；")


def outfit_policy_adult() -> str:
    """18+ R18：放开内衣/裸体档，尺度由剧情自然驱动。"""
    return (_OUTFIT_RULE_HEAD
            + "允许「便服 / 内衣 / 裸体」等状态，何时换装、脱到什么程度"
            "由剧情自然展开并实时声明，无需回避；")


def cg_policy_safe() -> str:
    return ("仅在「关键桥段」（重大情感转折、事件高潮、决定性相遇等）设置 cg.active=true "
            "并给出电影感画面描述（横版构图、可有氛围元素，不含露骨内容，禁裸体/内衣）。"
            "普通对话轮 cg.active 恒为 false。连续 CG 之间至少隔 2 幕。")


def cg_policy_adult() -> str:
    return ("在「关键桥段」（重大情感转折、事件高潮、决定性相遇、成人向场景高潮等）设置 "
            "cg.active=true 并给出电影感画面描述（横版构图、可有氛围元素）；"
            "成人向 CG 允许与本作分级政策相称的尺度与身体表现，不必回避。"
            "普通对话轮 cg.active 恒为 false。连续 CG 之间至少隔 2 幕。")


def content_tone(adult_intensity: str) -> str:
    """成人向强度对应的叙事提示（非露骨时保持克制）。"""
    if adult_intensity == "温和":
        return "本作允许成年人之间含蓄克制的亲密氛围与情感描写，点到即止。"
    if adult_intensity == "浓烈":
        return "本作允许成年人之间充分而不回避的成人向叙事（身体亲密、性爱场面均可按剧情展开）。"
    return "本作允许成年人之间细腻成熟的亲密描写，尺度按剧情需要自然调控。"


def format_characters(characters: list[dict]) -> str:
    lines = []
    for c in characters:
        role = str(c.get("role", "") or "").strip()
        bg = str(c.get("background", "") or "").strip()
        line = (f"- {c['name']}"
                + (f"（{role}）" if role else "")
                + f"：{c.get('personality', '')}｜外貌：{c.get('appearance', '')}")
        profile = []
        traits = [str(x).strip() for x in (c.get("traits") or []) if str(x).strip()]
        if traits:
            profile.append("性格标签：" + "、".join(traits))
        if c.get("age"):
            profile.append(f"年龄：{int(c['age'])}")
        if c.get("height_cm"):
            profile.append(f"身高：{int(c['height_cm'])}cm")
        if c.get("body_type"):
            profile.append("体型：" + str(c["body_type"]))
        if c.get("measurements"):
            profile.append("三围：" + str(c["measurements"]))
        if c.get("birthday"):
            profile.append("生日：" + str(c["birthday"]))
        if c.get("speech_style"):
            profile.append("说话风格：" + str(c["speech_style"]))
        if c.get("likes"):
            profile.append("喜欢：" + str(c["likes"]))
        if c.get("dislikes"):
            profile.append("厌恶：" + str(c["dislikes"]))
        if profile:
            line += "｜" + "｜".join(profile)
        if bg:
            line += f"｜背景：{bg}"
        locks = [str(x) for x in (c.get("locked_fields") or []) if str(x)]
        if c.get("personality_locked") and "personality" not in locks:
            locks.append("personality")
        if locks:
            line += "｜【固定属性，不得改写】" + "、".join(locks)
        lines.append(line)
    if not lines:
        lines.append("-（无其他角色）")
    return "\n".join(lines)


def format_lore(lorebook: list[dict], recent_text: str = "") -> str:
    """故事书注入：常驻条目直接注入；关键词条目在近期剧情命中关键词时注入。"""
    always, matched = [], []
    for e in lorebook:
        content = str(e.get("content", "")).strip()
        if not content:
            continue
        if e.get("always"):
            always.append(content)
        else:
            keys = [k.strip() for k in str(e.get("keys", "")).replace("，", ",").split(",") if k.strip()]
            if keys and any(k in recent_text for k in keys):
                matched.append(content)
    entries = always + matched
    if not entries:
        return "（暂无故事书条目）"
    return "\n".join(f"◆ {i+1}. {t}" for i, t in enumerate(entries))


def recent_dialogue_text(history_entries: list[dict], last_n: int = 3) -> str:
    """取最近几轮的台词文本，用于故事书关键词匹配。"""
    parts = []
    for entry in history_entries[-last_n:]:
        for d in entry.get("turn", {}).get("dialogue", []):
            parts.append(d.get("text", ""))
        if entry.get("choice"):
            parts.append(entry["choice"].get("text", ""))
    return " ".join(parts)

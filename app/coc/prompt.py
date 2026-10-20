"""CoC 模式的 prompt 组装与叙述层解析。

对应设计：`COC-DESIGN.md` §8.1（system prompt 结构）、§8.3（表现层手法）、§8.4（输出协议）。

## 三层职责（与 `COC-DESIGN.md` §3.1 一致）

```
确定性层（已由 t48 完成）        : 抽卡 / 掷骰 / 结算 / 钳制 / 不变式 / 结局
生成层（本文件 + LLM）           : 只写文本；本文件解析并**再清洗** LLM 的提案
内容层（data/coc/*.json）        : 卡池 / tuning / 结局表
```

本文件**不改任何数值**：它只
  ① 组装 prompt（`coc_system`）；
  ② 把 LLM 输出解析成 `{scene, dialogue, choices, coc_delta}`；
  ③ 把 `coc_delta` 交给 `state.sanitize_delta`（= `sanitize.sanitize_llm_delta`）清洗后返回。

真正的写入仍只在 `sanitize.apply_delta`（唯一写入点，t48 已用静态扫描证明）。

## 本文件为什么引入了 `llm.py`

任务书边界写的是「在 A1–A8 跑通前不得写任何 prompt 代码」，而 A1–A8 已在 t48 跑通。
本任务的目标就是"让 CoC 真正可玩"，因此需要真实的 LLM 调用点。
为守住"引擎不被污染"的性质，**引擎模块（dice/sanitize/state/loop/provenance/content）
保持零 LLM 导入**，LLM 只出现在本文件——t48 的探针断言 `E1-NO-LLM` 仍然成立，
本文件改名到 `llm_bridge.py` 也不影响它。

本文件**不导入** app.game / app.vn / app.tsf。
"""
from __future__ import annotations

import json

from . import rules_horror_erotic as rhe
from . import state as _state
from .loop import Resolution, Run
from .provenance import t

#: 与 `vn.py` 一致的文本约束（便于前端复用渲染）
MAX_DIALOGUE = 6
MAX_CHOICES = 4
MAX_TEXT = 800

#: 输出协议里的固定键（供一致性检查与文档）
OUTPUT_KEYS = ("scene", "dialogue", "choices", "coc_delta", "narrator")


def content_policy_text(rating: str, r18: bool, intensity: str = "成熟",
                        allow_forced: bool = False) -> str:
    """内容分级政策（**公式与 `app/game.py::content_policy_text` 同源**）。

    为什么在这里重写而不 import：`app/game.py` 不在本任务 inScope，
    且它 import 了 TSF 状态机（`from . import tsf`），会把无关依赖拖进来。
    这里只复制**那一小段纯文本公式**，并在测试里对两条实现做一致性断言，
    避免两份政策悄悄漂移。
    """
    base = "所有角色均为成年人；严禁与未成年人相关的不当内容、真实人物或违法情节。"
    if rating == "18" and r18:
        extra_forced = ("同时允许成年人角色之间支配、服从、强制与激烈身体冲突类情节"
                        "（纯虚构设定，仅限成年角色）；" if allow_forced else "")
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


#: 基调约束（采纳 `MECHANICS.md` §11.2 五条 + §9.4 四条规避）
TONE_RULES = """【基调约束（必须遵守）】
1. 玩家的目标**不是拯救世界，而是推迟末日**；通关收束文必须承认「它还会再来」。
2. 恐怖来自**低尺度的、私人的、日常中的异常**，不是宇宙奇观。
3. **不用 jump scare**：用扭曲的细节与持续的不适感，不用突发惊吓。
4. 每次失败都要有**具体的、身体的、私人的**代价描写，不要抽象地写"受到了伤害"。
5. 诅咒与伤势都要有**一句专属台词**，由本幕叙述复述一次，而不是纯数值通报。
6. 无力感由**资源系统**承载，**不得由旁白宣告**——不要反复写"你感到无力"。
7. 不要把本幕的检定属性、敌人弱点、标签名提前告诉玩家；只叙述已经发生的事。
8. 不要复述系统已插入的卡面正文；你要写的是**结果发生后**的场面。"""

#: 系统权威声明（对应 `COC-DESIGN.md` §3.2 E7）
AUTHORITY = """【系统权威声明】
DOOM、体力（STA）、意志（REA）、SAN（理智值）、属性、状态标签、物品、结局**全部由系统决定**，
你没有修改它们的权限，也不要在正文里断言它们的数值变化。
你唯一可以提议的数值调整是 `coc_delta` 里列出的那几项（经验/资金/物品/属性微调），
系统会校验并可能丢弃。数值以系统给出的当前值为准：正文与数值冲突时以数值为准。"""

#: 输出协议
PROTOCOL = """【输出协议（严格 JSON，不要加代码块标记）】
{
  "scene": "（8–20 字，本幕场景名）",
  "dialogue": [{"character": "旁白|角色名", "emotion": "neutral", "text": "……"}],
  "choices": [{"text": "选项文本（禁止写检定属性名或数值）", "effect": "走向短语"}],
  "narrator": {"unreliable": false, "inserted_glitch": ""},
  "coc_delta": {"exp": 0, "funds": 0}
}
约束：dialogue 最多 6 条；choices 1–4 条；不要输出上面的注释。"""


def coc_system(run: Run, *, resolution: Resolution | None = None,
               cfg: dict | None = None,
               erotic_tier: str | None = None,
               gate: rhe.GateResult | None = None,
               content_rating: str = "all",
               r18_enabled: bool = False,
               adult_intensity: str = "成熟",
               allow_forced: bool = False,
               participants=None,
               players=None) -> str:
    """组装 CoC 模式的最终 system prompt。

    **顺序**（`COC-DESIGN.md` §5.5）：基调 → 权威声明 → 分级政策 → 数值块 →
    本幕卡面 → 【恐怖×情色写作技法】（仅闸门通过时存在）→ delta schema → 输出协议。

    **空串契约（整个接线任务最关键的一条）**：
    当 `horror_erotic_block()` 返回 `""` 时，拼接结果必须与**完全不调用它**时
    **逐字节相等**。因此本函数里对那一段的处理是：先算 `block`，
    `if block: parts.append(block)` —— **不是** `parts.append(block)`
    （后者会在列表里留一个空元素，join 出多余的空行，破坏字节相等）。
    集成验收见 `测试工具/coc_wiring_probe.py`。
    """
    if cfg is None:
        from .provenance import load_tuning
        cfg = load_tuning()

    st = run.state
    card = None
    if resolution is not None and resolution.card_id:
        card = next((c for c in run.card_pool if c["id"] == resolution.card_id), None)

    parts: list[str] = []
    parts.append(TONE_RULES)
    parts.append(AUTHORITY)
    parts.append("【内容分级政策】\n" + content_policy_text(
        content_rating, bool(r18_enabled), adult_intensity, bool(allow_forced)))

    # t73：**自定义剧本**（世界观 / 大纲 / 故事书 / 强制指令）。
    # 放在数值块之前：这是"这一局在讲什么故事"的世界级前提，应该在具体数值之前先立住。
    # 空剧本 ⇒ `prompt_block` 返回空串 ⇒ 提示词**逐字节不变**（既有闸门不受影响）。
    try:
        from . import scenario as _scn
        _blk = _scn.prompt_block(getattr(run, "scenario", None))
        if _blk:
            parts.append(_blk)
    except Exception:
        pass          # 剧本数据异常 ⇒ 不注入该段，绝不让生成失败

    # 1.9.20：玩家导演指令（持续生效；放在数值块前，作为叙事基调）
    _dir = str((getattr(run, "state", {}) or {}).get("ai_directive") or "").strip()
    if _dir:
        parts.append("【玩家导演指令（本局持续生效，优先级高于默认风格；"
                     "但不得违反内容分级政策，也不得改写系统数值）】\n" + _dir[:500])

    # 数值块（t48 已有；此处只调用，不重复实现）
    parts.append("【当前数值】\n" + _state.states_block(st))

    # t71（TSF×CoC 融合）：**把背包与融合模式下的物品可用性告诉 LLM**。
    # 此前 LLM 完全看不到玩家带着什么 —— 于是会写出"你摸出绷带包扎"而玩家根本没有绷带，
    # 或写出"你挥起消防斧"而此时斧头因转变已握不住。这里只给事实与可用性，不给指令。
    try:
        from . import item_effects as _ie
        bag = _ie.inventory_effects(st, which="backpack")
        eq = _state.equipped(st)
        mech = _ie.mech_view(st)
        if bag or any(eq.values()):
            lines = []
            for it in bag:
                name = str(it.get("name") or it.get("id"))
                tag = ""
                if it.get("block_reason") == "tsf_hand_unfit":
                    tag = "（身形变化后已无法有效使用）"
                elif it.get("block_reason") == "tsf_slot_unfit":
                    tag = "（已不合身，无法继续穿戴）"
                pen = int(it.get("use_penalty") or 0)
                if pen > 0 and not tag:
                    tag = f"（使用需额外耗时 +{pen}）"
                worn = "〔已装备〕" if it.get("equipped") else ""
                lines.append(f"  · {name}{worn}{tag}")
            eq_txt = "、".join(
                f"{sl}:{( _state.item_meta(str(i)).get('name') or i)}"
                for sl, i in eq.items() if i)
            wd = mech.get("wardrobe") or {}
            block = ("\n【物品】（系统事实，不要凭空发明玩家没有的东西；标注意味的不可按原样使用）\n"
                     + ("\n".join(lines) if lines else "  · （空）"))
            if eq_txt:
                block += f"\n【已装备】{eq_txt}"
            if wd.get("label"):
                block += f"\n【衣着状态】{wd.get('label')}"
            parts.append(block)
    except Exception:
        pass          # 物品数据异常 ⇒ 不带该段，绝不因此让整局生成失败

    # 本幕卡面（代码插入，LLM 不复述）
    if card is not None:
        choices_txt = "\n".join(
            f"{i + 1}. {c.get('text', '')}" for i, c in enumerate(card.get("choices") or [])
        )
        parts.append("【本幕事件卡（系统已插入，不要复述正文）】\n"
                     + str(card.get("body", "")) + "\n可选：\n" + choices_txt)

    # 已定稿的结算（E1：LLM 收到的是结果，不是问题）
    if resolution is not None:
        parts.append("【本幕结算（已由系统定稿，不得改写）】\n"
                     + json.dumps(resolution.to_json(), ensure_ascii=False,
                                  sort_keys=True, indent=1))

    # ★ 恐怖×情色层：闸门未通过时**整段不存在**
    if gate is None:
        participants = participants or ([card.get("_erotic", {}).get("participants")]
                                       if card else None)
        gate = rhe.resolve_gate(content_rating, r18_enabled, adult_intensity,
                                participants, cfg,
                                horror_hint=rhe.horror_tier_for(st, cfg))
    tier = erotic_tier if erotic_tier is not None else gate.erotic_tier
    block = rhe.horror_erotic_block(tier, gate, {"state": st}, cfg)
    if block:
        parts.append(block)

    # 允许提议的 delta（白名单镜像自 tuning；LLM 只能提这些）
    parts.append("【允许提议的 delta（其余一律无效，且数值由系统重算）】\n"
                 + "、".join(str(k) for k in t(cfg, "llm_delta_whitelist")))
    parts.append(PROTOCOL)

    system = "\n\n".join(parts)

    # API 前置注入（与既有模式同源机制；缺失时静默跳过，不引入硬依赖）
    try:
        from .. import inject as inject_mod
        system = inject_mod.inject_block(system)
    except Exception:
        pass
    return system


def build_messages(run: Run, *, resolution: Resolution | None = None,
                   history_turns: int = 6, **system_kw) -> list[dict]:
    """构造对话消息：system + 最近若干轮的 (choice, scene) 摘要。

    历史只回灌 `scene` + `choice.text`（不回灌完整 dialogue、不回灌旧 delta）——
    避免 prompt 膨胀，也避免模型把旧数值当成现状。
    """
    messages = [{"role": "system", "content": coc_system(run, resolution=resolution,
                                                          **system_kw)}]
    recent = [e for e in run.events if e.get("kind") in
              ("investigation", "mystery_complete")][-history_turns:]
    if recent:
        lines = []
        for e in recent:
            if e["kind"] == "investigation":
                lines.append(f"- 在 {e.get('loc')} 调查（第 {e.get('investigations_done')} 次），"
                             f"DOOM {e.get('doom')}%")
            else:
                lines.append(f"- 完成谜团，拿到第 {e.get('keys')} 把钥匙，DOOM 降至 {e.get('doom')}%")
        messages.append({"role": "user",
                         "content": "【已发生的经过（只作背景，不要复述）】\n" + "\n".join(lines)})
    messages.append({"role": "user",
                     "content": "请叙述上面【本幕结算】发生之后的场面，并按输出协议返回 JSON。"})
    return messages


# ------------------------------------------------------------ 输出解析

def _clip(x, n: int = MAX_TEXT) -> str:
    return str(x or "").strip()[:n]


def narr_turn(raw, cfg: dict | None = None) -> dict:
    """解析并**清洗** LLM 的输出（只碰文本与提案，不写状态）。

    返回：
      {"scene", "dialogue", "choices", "narrator", "coc_delta", "dropped"}
    其中 `coc_delta` 已过 `state.sanitize_delta`（= 白名单 + 钳制）；
    越权键进 `dropped`，供上层记账。
    """
    if cfg is None:
        from .provenance import load_tuning
        cfg = load_tuning()
    if not isinstance(raw, dict):
        raw = {}

    dialogue = []
    for d in (raw.get("dialogue") or [])[:MAX_DIALOGUE]:
        if not isinstance(d, dict):
            continue
        text = _clip(d.get("text"))
        if not text:
            continue
        dialogue.append({
            "character": _clip(d.get("character", "旁白"), 20) or "旁白",
            "emotion": _clip(d.get("emotion", "neutral"), 12) or "neutral",
            "text": text,
        })
    if not dialogue:
        dialogue = [{"character": "旁白", "emotion": "neutral",
                     "text": "（一阵短暂的沉默。）"}]

    choices = []
    for c in (raw.get("choices") or [])[:MAX_CHOICES]:
        if isinstance(c, dict) and _clip(c.get("text")):
            choices.append({"text": _clip(c.get("text"), 60),
                            "effect": _clip(c.get("effect"), 40)})
    if not choices:
        choices = [{"text": "继续", "effect": ""}]

    narr = raw.get("narrator") or {}
    if not isinstance(narr, dict):
        narr = {}

    # 清洗提案：这里要拿到「丢了什么」的审计信息，所以直接调 sanitize 的二元组接口，
    # 而不是 state.sanitize_delta（后者是对齐 vn.py 的单返回值形态）。
    from . import sanitize as _san
    cleaned, dropped = _san.sanitize_llm_delta(raw.get("coc_delta"), cfg)
    return {
        "scene": _clip(raw.get("scene"), 40) or "……",
        "dialogue": dialogue,
        "choices": choices,
        "narrator": {
            "unreliable": bool(narr.get("unreliable")),
            "inserted_glitch": _clip(narr.get("inserted_glitch"), 60),
        },
        "coc_delta": cleaned,
        "dropped": list(dropped),
    }

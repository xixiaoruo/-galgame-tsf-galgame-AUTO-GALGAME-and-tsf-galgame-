"""恐怖 × 情色 专用写作规则层（**只设计结构，不含成人向文本**）。

对应设计：`COC-DESIGN.md` §5 全章。上游要求（任务书原文）：
> 新增「恐怖×情色」专用写作规则层（不是只沿用现有分级注入）。

## 双轴三档

| 轴 | 档位 | 取自 |
|---|---|---|
| 情色 `erotic_tier` | `subtle` / `intense` / `explicit` | `tuning.erotic.erotic_tiers` |
| 恐怖 `horror_tier` | `dread` / `visceral` / `cosmic` | `tuning.erotic.horror_tiers` |

**为什么必须双轴**：单轴表达不出「低理智 × 清醒亲密」这类组合——
而那种组合正是本模式与普通 galgame 分级注入的区别所在（COC-DESIGN §5.2）。

## 三硬闸门（本文件的核心安全边界）

| 闸门 | 条件 | 失败时行为 |
|---|---|---|
| **G1 成年闸门** | 场景**全部**参与者命中 `cast.json` 的 `age_flag=="adult"` | 整段不注入（返回空串） |
| **G2 评级闸门** | `rating == "18"` 且 `r18_enabled` 为真 | 整段不注入 |
| **G3 保守档闸门** | `adult_intensity == "温和"` → 情色档**强制钳到 `subtle`** | 钳制（只能降不能升） |

**空串语义**：任一闸门未通过 ⇒ `horror_erotic_block()` 返回 `""`，
**连段落标题都不写**。理由：留一个空标题会让模型看到「【恐怖×情色写作技法】」
却没有任何内容，从而**自行脑补**——那等于闸门没生效。

配套验收：`app/coc/tests/test_gate_empty.py`（单元级）+ `测试工具/coc_wiring_probe.py`
的集成级断言（**最终 system prompt 与「不注入该层」的基线逐字节相等**）。

本模块**不导入** LLM / 网络 / app.game / app.vn / app.tsf。
"""
from __future__ import annotations

from . import content
from .provenance import t

#: 段落标题。**只在通过全部闸门后**才会被写进返回串。
BLOCK_TITLE = "【恐怖×情色写作技法】"

#: 双轴档位（真值以 tuning.json 为准，这里只做常量便于静态检查与文档）
EROTIC_TIERS: tuple[str, ...] = ("subtle", "intense", "explicit")
HORROR_TIERS: tuple[str, ...] = ("dread", "visceral", "cosmic")

#: 情色档到项目既有 `adult_intensity`（温和/成熟/浓烈）的映射 —— 复用项目既有尺度词汇
INTENSITY_TO_TIER = {"温和": "subtle", "成熟": "intense", "浓烈": "explicit"}
TIER_ORDER = {"subtle": 0, "intense": 1, "explicit": 2}

#: 五组条目骨架（COC-DESIGN §5.6）。**只锁结构与约束，不写成人向正文**。
#: 每组 (标题, 条目列表)；条目是"规则要求"而非可朗读的成品段落。
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("H1 边界", (
        "所有出场角色均为成年人；不得出现未成年或年龄不明角色。",
        "情节必须合法：不描写真实人物、不描写违法伤害行为。",
        "分级政策已放开的部分**不做自我审查**：不加防御性措辞、不以「过于露骨」为由收敛。",
    )),
    ("H2 身体恐怖 × 情色", (
        "伤口、变化、异物感与快感并置，而不是分段落各写各的。",
        "身体的不适可以是亲密的来源，但要写出角色的**判断**而不只是生理反应。",
        "诅咒/伤势在身时，其专属台词必须在该场景里被复述一次。",
        "禁止把身体恐怖当作一次性调味；它必须与本幕的理智/体力数值相符。",
    )),
    ("H3 语言同源", (
        "恐惧词与快感词共用同一感官通道（呼吸、体温、潮湿、失控感）。",
        "禁止「恐怖段」与「情欲段」各自成套；同一段里两种感受交替推进。",
        "低理智时快感与恐惧**不可分离**——叙述者自己也分不清是哪一个。",
    )),
    ("H4 节奏与留白", (
        "节奏按「张力 → 靠近 → 接触 → 升级 → 释放 → 余韵」推进，每阶段至少两种感官。",
        "恐怖节奏压过情色节奏时，允许中断；中断本身是设计，不是失败。",
        "每幕结尾留一个未解的不适感（与恐怖基调一致），不要以「圆满」收束。",
    )),
    ("H5 禁则", (
        "禁用 jump scare 式突发惊吓（上游明确：本作恐怖来自扭曲细节与不安的种子）。",
        "禁止把亲密场景当作 DOOM 下降或回血的手段（DOOM 下降途径有穷举表）。",
        "禁止把情色场面写成「奖励结算」或数值兑换。",
        "参与者不得越出成年角色白名单。",
    )),
)


class GateResult:
    """闸门判定结果（可打印、可断言，便于报告里给出"为什么是空的"）。"""

    __slots__ = ("passed", "erotic_tier", "horror_tier", "reasons", "gate")

    def __init__(self, passed: bool, erotic_tier: str | None, horror_tier: str | None,
                 reasons: list[str], gate: dict):
        self.passed = passed
        self.erotic_tier = erotic_tier
        self.horror_tier = horror_tier
        self.reasons = reasons
        self.gate = gate

    def to_json(self) -> dict:
        return {
            "passed": self.passed,
            "erotic_tier": self.erotic_tier,
            "horror_tier": self.horror_tier,
            "reasons": self.reasons,
        }

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (f"GateResult(passed={self.passed}, erotic={self.erotic_tier}, "
                f"horror={self.horror_tier}, reasons={self.reasons})")


def adult_ok(participants, cfg: dict = None) -> tuple[bool, list[str]]:
    """G1：参与者是否**全部**命中成年白名单。

    白名单在 `data/coc/cast.json`（`age_flag == "adult"`）。
    **缺失一律视为未登记 → 拒绝**，绝不默认放行。
    """
    names = [str(p).strip() for p in (participants or []) if str(p).strip()]
    if not names:
        return False, ["G1：未声明参与者 ⇒ 无法证明全部成年"]
    try:
        table = content.cast()
    except Exception as exc:  # 内容层缺失也必须表现为"拒绝"
        return False, [f"G1：白名单不可读（{exc}）⇒ 拒绝"]
    bad = [n for n in names if (table.get(n) or {}).get("age_flag") != "adult"]
    if bad:
        return False, [f"G1：不在成年白名单内 {bad}"]
    return True, []


def resolve_gate(rating: str, r18_enabled: bool, adult_intensity: str,
                 participants, cfg: dict, *, horror_hint: str | None = None) -> GateResult:
    """三硬闸门判定（同时算两个轴的最终档位）。

    **G2 最先判**：评级不过就直接返回未通过，连档位都不算——
    这样调用方无法"绕过闸门拿到 unrestricted 的 tier"。
    """
    rating = str(rating or "").strip()
    require = str(t(cfg, "erotic.gate_rating_required"))       # "18"
    reasons: list[str] = []
    if rating != require or not r18_enabled:
        return GateResult(False, None, None,
                          [f"G2：rating={rating!r}（需 {require!r}）r18_enabled={bool(r18_enabled)}"],
                          {"rating": rating, "r18_enabled": bool(r18_enabled)})

    ok1, why1 = adult_ok(participants, cfg)
    if not ok1:
        return GateResult(False, None, None, why1,
                          {"rating": rating, "r18_enabled": True})

    # G3：保守档只降不升
    want = INTENSITY_TO_TIER.get(str(adult_intensity or ""), "subtle")
    if want not in EROTIC_TIERS:
        want = "subtle"
    if str(adult_intensity or "") == str(t(cfg, "erotic.conservative_intensity")):
        want = "subtle"
        reasons.append("G3：保守档（温和）⇒ 情色档强制钳到 subtle")

    horror = str(horror_hint or "dread")
    if horror not in HORROR_TIERS:
        horror = "dread"
    return GateResult(True, want, horror, reasons,
                      {"rating": rating, "r18_enabled": True,
                       "adult_intensity": str(adult_intensity or "")})


def horror_tier_for(state: dict, cfg: dict) -> str:
    """按状态算恐怖档（`tuning.erotic.horror_tier_thresholds`）。

    阈值对齐 `MECHANICS.md` §3.2（REA ≤9 立绘变阴郁）与 §2.1（DOOM 仪表）。
    """
    th = t(cfg, "erotic.horror_tier_thresholds")
    doom = int(state.get("doom", 0))
    sta = int(state.get("sta", 0))
    rea = int(state.get("rea", 0))
    cosmic = th.get("cosmic", {})
    if doom >= int(cosmic.get("doom_min", 101)) or rea <= int(cosmic.get("rea_max", -1)):
        return "cosmic"
    vis = th.get("visceral", {})
    if doom >= int(vis.get("doom_min", 101)) or sta <= int(vis.get("sta_max", -1)):
        return "visceral"
    return "dread"


def horror_erotic_block(erotic_tier: str | None, gate: GateResult, ctx: dict | None = None,
                        cfg: dict | None = None) -> str:
    """把双轴档位渲染成**一段可注入的规则文本**；任一闸门未通过则返回 `""`。

    **空串契约（本函数的核心）**：
      - 返回 `""` 时**不写标题**、不写任何分隔符、不写任何条目；
      - 因此"注入了空段"与"根本没调用本函数"在最终 prompt 上**不可区分**。

    这是"分支存在 ≠ 走了那条分支"的正面解法：不用断言"分支存在"，
    而是断言**最终产物的字节相等**。
    """
    if not isinstance(gate, GateResult) or not gate.passed:
        return ""
    if erotic_tier not in EROTIC_TIERS:
        return ""
    if cfg is None:
        return ""
    # 预算：intense/explicit ≤ 900 字；subtle ≤ 400 字（COC-DESIGN §5.5）
    budget = 400 if erotic_tier == "subtle" else 900
    horror = gate.horror_tier or "dread"

    lines: list[str] = [BLOCK_TITLE]
    lines.append(f"本幕档位：情色 {erotic_tier} × 恐怖 {horror}（两轴独立，不要合并成一个尺度）")
    # subtle 档：H2/H3/H4 各只留 1 条。
    # 先前只缩到「各留 2 条」，实测 411 字 > 400 预算 → 被测试抓到并修正。
    # 预算不是形式：它决定保守档下这一层的"存在感"，超预算就等于悄悄把尺度推上去了。
    _SUBTLE_KEEP = {"H2 身体恐怖 × 情色": 1, "H3 语言同源": 1, "H4 节奏与留白": 1}
    for title, items in _GROUPS:
        if erotic_tier == "subtle" and title in _SUBTLE_KEEP:
            items = items[:_SUBTLE_KEEP[title]]
        lines.append(f"{title}：")
        lines.extend(f"- {x}" for x in items)
    if gate.reasons:
        lines.append("闸门调整：" + "；".join(gate.reasons))

    text = "\n".join(lines)
    if len(text) > budget:
        # ⚠️ 截断必须**真的落在预算内**：先前写成 `text[:budget] + 提示语`，
        # 结果 = budget + len(提示语) > budget（实测 subtle 得到 411 > 400）。
        # 「输出不得超预算」是这条护栏的唯一意义，所以这里把提示语的长度也算进去。
        note = "\n（本条已截断至预算）"
        keep = max(0, budget - len(note))
        text = text[:keep].rstrip() + note
    # 不变量：无论如何都不得超过预算（超了就是护栏失效，直接暴露而不是悄悄放行）
    assert len(text) <= budget, f"block 超预算：{len(text)} > {budget}"
    return text


def gate_summary(cfg: dict) -> dict:
    """给报告/调试用：把闸门的**配置事实**一次性导出（不含任何成人文本）。"""
    return {
        "erotic_tiers": list(t(cfg, "erotic.erotic_tiers")),
        "horror_tiers": list(t(cfg, "erotic.horror_tiers")),
        "gate_rating_required": t(cfg, "erotic.gate_rating_required"),
        "conservative_intensity": t(cfg, "erotic.conservative_intensity"),
        "horror_tier_thresholds": t(cfg, "erotic.horror_tier_thresholds"),
        "block_title": BLOCK_TITLE,
        "groups": [g[0] for g in _GROUPS],
    }

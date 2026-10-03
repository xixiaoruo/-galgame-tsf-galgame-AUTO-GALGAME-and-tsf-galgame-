"""CoC 模式内的 TSF 子系统：**探索驱动的转变进度** 与它的**机制后果**。

## 这个模块是什么（以及刻意不是什么）

用户选了「**机制型**：TSF 进数值与检定」，不是只换文本。所以本模块的产出必须有
**可测量的机械后果**：同一属性、同一威胁、同一 dc 的一次检定，在不同转变进度下
**成功率必须不同**。这条由 `app/coc/tests/test_coc_tsf.py` 用大样本实测（§可观察性）。

刻意**不是**什么：
- **不重复造阶段表** —— 阶段语义一律从 `app.tsf` 取（`build_stages` / `stage_of` / `stage_tier`）。
- **不新增"进度"字段**（见下）。
- **不碰立绘/生图** —— 那是 t55+ 的事，且 `_queue_asset` 的 persist 问题不在本任务范围。

## 为什么不新增 `tsf_progress` 字段（采纳 t53 §9 的结论）

`tsf_progress_of(state)` 是**纯投影**：只读 `mystery_idx` / `investigations_done` /
`mystery_len` / `mystery_count` 四个**已有**账本字段。

理由（t53 已实测，captain 已采纳）：
1. **存档往返天然一致** —— 没有要存的字段，就不可能被读档写坏／不一致；
2. **单调性天然成立** —— 两个自变量都只增不减（`mystery_idx` 只 +1、`investigations_done`
   只 +1 且换章归零），投影是它们的单调函数；
3. **与 rng 解耦** —— 自变量是计数，不是掷骰结果，所以同 seed 同操必得同进度。

⇒ 代价：本模块**没有** `advance()`。任务书原文要求 `advance(state, ...)`，
但按 t53 的投影方案，"推进"不是一个需要调用的动作 —— 它由**账本本身**推进
（完成调查 / 完成章节）。本模块提供的等价物是：
`progress()` / `tsf_progress_of()`（读）、`progress_delta(old, new)`（比较快照）。
落 `app/coc/tsf.py` 满足任务书"新文件在 CoC 包内"的形式要求；
**语义上的修正见报告 `COC-TSF-ENGINE.md` §2**。

## 唯一新增的状态字段（并说明为什么不可推导）

`tsf_nudge_total`（int，默认 0）：**模型被允许提议的、有界的一点点转变推动**。
它是账本推导不出来的（账本只记录"调查了多少次"，不记录"模型建议把转变加速一点"），
所以必须落盘。它有独立的有界钳制（单轮 ±1，见 `sanitize.py`），
并且**只做加法**（≥0），因此不破坏单调性。

## 与既有 DOOM/STA/REA 的命名空间隔离（t53 §10 第 1 条：最危险的一条）

| 名字 | CoC 侧的归属 | 本模块的处置 |
|---|---|---|
| `stats` | `state["stats"]` = 调查员**技能**（STR/DEX/PER/KNW/CHA，1~12） | **只读**。TSF 的修正以 `shift` 形式**叠加进检定**，不改写 `stats` |
| `progress` / `stage` | **不写入 state** | 由 `tsf_progress_of` 投影；`stage` 每次由 `app.tsf.stage_of` 重算 |
| `doom`/`sta`/`rea` | 既有资源 | **本模块从不写这三个键**（有断言守着） |
| `tags`/`flags` | 既有标签与旗标 | 本模块不写；症状走独立的 `state["tsf_symptoms"]`（阶段序号列表） |

**名词约定**：TSF 侧一律拒绝使用裸 `stats` / `progress` 去指代"转变"，凡出现都在
前缀 `tsf_` 之下（`tsf_progress` 是**派生量**的名字，不是字段名）。
"""
from __future__ import annotations

from typing import Any

from .provenance import t

# ---------------------------------------------------------------- 常量

#: 机制开关点（0–100）。**刻意等于 V6 的段边界**（0/20/40/60/80/100）
#: 而不是 `tsf.py` 的语义层级 —— 见 `mechanical_effects` 的说明。
#: 这样"机制变化点"与"立绘换图点"重合，玩家能同时看到两件事一起发生。
MECH_THRESHOLDS: tuple[int, ...] = (0, 20, 40, 60, 80, 100)

#: 五档修正表的**唯一真源**：`table[档位] = {属性: 修正}`。
#: 档位由 `progress` 落在 `MECH_THRESHOLDS` 的第几段决定（0..5）。
#:
#: ## 数值理由（逐项）
#:
#: 掷骰是 `stat + threat_mod + dc_shift vs roll + 1`（`dice.py` §4.2），
#: 属性域 1~12、随机数域 1~12、`checks.dc_min/max = ±6`。所以：
#: **±1 ≈ 12 个随机数格里的 1 格 ≈ 成功率 8.3 个百分点**；
#: 本表的峰值 −2 占 dc 域的 1/3、且小于 dc_max（6），属于"可感知但不压倒"的量级
#: —— 这正是"机制型但不当成第二个 DOOM"的取舍。
#:
#: 各属性为何是这个数：
#:   · **STR / DEX −1 起、峰值 −2**：身体重构 → 原有的力量与协调基线失效。
#:     峰值 −2（不是 −3）是因为 CoC 的物理检定本就少，压太狠会让物理路线直接不可玩。
#:   · **PER −1 起、峰值 −2**：自己的形体成为"噪声源"（一直在变），
#:     对**外部**细节的注意力被自己抢走。选 PER 而不是 KNW，
#:     是因为 KNW 代表"知识"——知识不会因为身体变了而变少，拿它开刀缺乏理由。
#:   · **KNW +1（仅最高档 +1，共 1 档）**：转变被**客体化**成了一种研究对象
#:     （"我在经历什么"推动了对现象的理解）。这是**正向**修正，
#:     保证机制不是"纯惩罚"——否则玩家会本能地抗拒进度，机制就变成了反激励。
#:     只在 80+ 给，因为 80 之前"还没看明白"。
#:   · **CHA −1 起、峰值 −2**：外在形象与"本地人"的预期不符，
#:     交涉时更难被当作"可信的自己人"。这是**社会性**代价，
#:     与生理无关，正是 TSF 题材的核心张力（`MECHANICS.md` §2.3 的
#:     "转变带来社会性后果"）。
#:   · **下限 −2 的统一理由**：`threat_mod` 已经在 DOOMED 给 −2；
#:     若 TSF 也动 −3，两者叠加会到 −5，与 `dc_min`（−6）同量级 ⇒
#:     检定近乎必败，等于把 DOOM 的惩罚偷偷复制了一份。**避免与 DOOM 叠加失控**
#:     是本表封顶在 −2 的核心理由。
_MECH_TABLE: dict[str, tuple[int, ...]] = {
    "STR": (0, -1, -1, -2, -2, -2),
    "DEX": (0, -1, -1, -2, -2, -2),
    "PER": (0, -1, -1, -2, -2, -2),
    "CHA": (0, -1, -1, -2, -2, -2),
    "KNW": (0, 0, 0, 0, 0, 1),
}

#: 可见症状：按档位给出**叙述级**描述（不含露骨内容；内容边界与 tsf.py 一致）。
#: 这些是"玩家能看见的转变"，与机制修正同步换档。
_SYMPTOMS: tuple[str, ...] = (
    "皮肤比昨天细腻，指节上的旧茧变软了。",
    "肩线似乎收了一点，袖子松了——走路的步幅也不自觉地变小。",
    "声音的落点变了，自己听着都有点陌生；镜子里一时分不出男女。",
    "腰线出来了，衣服开始不合身；旁人看你的眼神比昨天更久。",
    "轮廓已经是女性的样子，旧日的影子只剩眉间一点。",
    "转变完成。你以新的形态站在这座镇上，旧的自己活在记忆里。",
)

#: 症状对应的 `state` 键：记录**已叙述过的档位序号**（去重，只增不减）。
#: 这是第二个新增字段，理由同 `tsf_nudge_total`：它记录"玩家已经看过哪些症状"，
#: 不可从账本推导（同一个 progress 下，看过与没看过是两种不同的历史）。
SYMPTOMS_KEY = "tsf_symptoms"

#: 机制档位的**叙述标签**（给 prompt 用的"感觉"措辞，不含任何数值）。
MECH_LABELS: tuple[str, ...] = (
    "几乎没有变化，只是最近总觉得哪里不太一样",
    "变化刚开始，力气与协调感都在悄悄改写",
    "明显的中间态：身体在两种形态之间，自己也说不准",
    "女化渐成，动作与声音都开始不像从前",
    "新的形体已经占主导，只有心里的旧影子还在",
    "转变完成，你已经能熟练地使用这副身体",
)

#: 模型可提议的有界推动的累计值键。
NUDGE_KEY = "tsf_nudge_total"

#: 单轮允许的模型推动上限（与 sanitize 的预算一致；±1）
NUDGE_PER_TURN_MAX = 1


# ---------------------------------------------------------------- 读：进度

def _as_int(v: Any, default: int = 0) -> int:
    """读账本计数：旧档缺字段 / 类型不对 ⇒ 返回 default（**不抛**）。"""
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def tsf_progress_of(state: dict) -> int:
    """CoC 探索进度 → TSF 同化率 0–100（**纯函数、只读、单调不减**）。

    公式（t53 §9.2 的建议映射）：

        frac  = min(1, investigations_done / mystery_len)
        ratio = min(1, (mystery_idx + frac) / mystery_count)
        base  = round(ratio * 100)
        total = clamp(base + tsf_nudge_total, 0, 100)

    **为什么换章处连续**：`complete_mystery()` 里 `mystery_idx += 1` 与
    `investigations_done = 0` **同时发生**，所以 `mystery_idx + frac` 在那一刻
    从 `k + 1` 平滑过渡到 `k + 1 + 0` —— 没有跳变也没有回退。
    `min(1.0, ...)` 兜住上限（`mystery_idx` 涨到 `mystery_count` 之后不再增长）。

    **Initiate 的对齐**（t53 §9.5）：`mystery_count=5` + `mystery_len=6`
    ⇒ 每完成一章 ≈ +20 ⇒ **一章一段**，与 V6 六段边界（0/20/40/60/80/100）重合。
    其他难度（`Sceptic` count=4；`Cultist`/`TrueBeliever`/`Harbinger` count=5）
    不额外调参：`Sceptic` 每章 +25，5 段的观感是"略快"，仍单调且不跳级。
    """
    count = max(1, _as_int(state.get("mystery_count"), 1))
    m_idx = max(0, _as_int(state.get("mystery_idx"), 0))
    length = max(1, _as_int(state.get("mystery_len"), 1))
    done = max(0, _as_int(state.get("investigations_done"), 0))
    nudge = max(0, _as_int(state.get(NUDGE_KEY), 0))

    frac = min(1.0, done / length)
    ratio = min(1.0, (m_idx + frac) / count)
    base = int(round(ratio * 100))
    return max(0, min(100, base + nudge))


#: 任务书用的名字（`progress(state)`）—— 与 `tsf_progress_of` 同一实现，
#: 只是名字不同。保留两个名字是为了让调用点读起来像英文句子：
#: `tsf.progress(state)` 读作"这个状态的转变进度"。
progress = tsf_progress_of


def progress_delta(before: int, after: int) -> int:
    """两次快照之间的进度变化（≥0 由调用方保证单调；这里只做差）。"""
    return _as_int(after) - _as_int(before)


def tier_of(prog: int) -> int:
    """进度 → 机制档位（0..5）。**只取决于 `MECH_THRESHOLDS`**，与段数无关。"""
    p = max(0, min(100, _as_int(prog)))
    tier = 0
    for i, hi in enumerate(MECH_THRESHOLDS):
        if p >= hi:
            tier = i
    return min(tier, len(MECH_THRESHOLDS) - 1)


# ---------------------------------------------------------------- 读：阶段（复用 app.tsf）

def stages(count: int = 6) -> list[dict]:
    """取阶段表 —— **复用 `app.tsf.build_stages`，不自己写表**。

    ⚠️ t53 §1.4 的坑：`build_stages` 的 `count in (4,6,10)` 是**转换前**判断，
    传 `"4"` 会得到 **6 段**。所以这里**先强制 int** 再传，杜绝字符串入参。

    延迟 import：`app/coc/state.py` 声明"本包不导入 app/tsf.py"（COC-DESIGN §7.1 B1），
    本模块是**唯一**的例外 —— 因为任务明确要求"不要自己写阶段表"。
    延迟到调用时 import，使 CoC 引擎的 import 期不依赖 app.tsf
    （`app/tsf.py` 仅在真正需要段信息时才被拉起）。
    """
    from app import tsf as _tsf          # 延迟：见上
    try:
        n = int(count)                    # 先转会 int（防字符串入参的坑）
    except (TypeError, ValueError):
        n = 6
    return _tsf.build_stages(n)


def default_stages() -> list[dict]:
    """CoC 用的默认阶段表（6 段 / V6，与 t53 §8.3 的建议一致）。

    **刻意不读主模式 `game.stage_count`**：CoC 档的阶段语义不应随
    用户给主模式设的段数变化（t53 §1.5 的结论）。
    """
    return stages(6)


def stage_index(prog: int, table: list[dict] | None = None) -> int:
    """进度 → 段序号（复用 `app.tsf.stage_of`）。

    ⚠️ t53 §1 的坑：`stage_of` 用 `<=` ⇒ `progress=20` 落**第 1 段**（idx 0），
    不是第 2 段；V6 末段是 `[100,100]` ⇒ **只有正好 100** 才是第六段。
    """
    from app import tsf as _tsf
    tbl = table if isinstance(table, list) and table else default_stages()
    return int(_tsf.stage_of(max(0, min(100, _as_int(prog))), tbl))


#: 任务书用的名字（`stage_of(progress)`）
stage_of = stage_index


def stage_view(prog: int, table: list[dict] | None = None) -> dict:
    """给面板/报告的阶段视图（字段名与主模式 `tsf.panel_view` 对齐，便于前端复用形状）。"""
    from app import tsf as _tsf
    tbl = table if isinstance(table, list) and table else default_stages()
    p = max(0, min(100, _as_int(prog)))
    idx = stage_index(p, tbl)
    return {
        "progress": p,
        "stage": idx,
        "stage_name": str(tbl[idx]["name"]),
        "stage_desc": str(tbl[idx]["desc"]),
        "identity": str(tbl[idx]["identity"]),
        "tier": int(_tsf.stage_tier(idx, tbl)),
        "mech_tier": tier_of(p),
    }


# ---------------------------------------------------------------- 机制后果（核心）

def shift_for(stat_name: str, prog: int) -> int:
    """单项属性在给定进度下的检定修正（负数=更难）。未知属性返回 0。"""
    row = _MECH_TABLE.get(str(stat_name))
    if row is None:
        return 0
    return int(row[tier_of(prog)])


def mechanical_effects(state: dict) -> dict:
    """**机制型的核心**：TSF 进度对数值与检定的实际影响。

    返回（键名与用途）：

    | 键 | 形状 | 用途 |
    |---|---|---|
    | `progress` | `int 0–100` | 当前进度（投影所得） |
    | `stage` / `stage_name` / `identity` | `int` / `str` | 段信息（来自 `app.tsf`） |
    | `tier` | `int 0–5` | `app.tsf` 的语义层级（给立绘用） |
    | `mech_tier` | `int 0–5` | **机制档位**（给检定用；由 `MECH_THRESHOLDS` 决定） |
    | `shift` | `{stat: int}` | 逐属性检定修正 → **接进 `dice.check(..., tsf_shift=)`** |
    | `symptom` | `str` | 当前档位的可见症状（叙述用） |
    | `symptom_new` | `bool` | 该症状是否**本次首次**可见（供"只叙述一次"） |

    **注意 `tier` 与 `mech_tier` 是两个不同的量**：前者是 `tsf.py` 的语义层级
    （给生图/特征用），后者是本模块的机制档位（给检定用）。它们刚好数值相同
    （因为 `MECH_THRESHOLDS` 选得与 V6 边界一致），但**语义与归属不同**，
    未来若段数改成 4/10，`tier` 会变而 `mech_tier` 不变 —— 这是**有意**的：
    机制不应随"用户选了几段"而改变强度。
    """
    prog = tsf_progress_of(state)
    view = stage_view(prog)
    mt = tier_of(prog)
    symptoms = state.get(SYMPTOMS_KEY)
    seen = symptoms if isinstance(symptoms, list) else []
    return {
        "progress": prog,
        "stage": view["stage"],
        "stage_name": view["stage_name"],
        "identity": view["identity"],
        "tier": view["tier"],
        "mech_tier": mt,
        "shift": {k: int(row[mt]) for k, row in _MECH_TABLE.items()},
        "symptom": _SYMPTOMS[mt],
        "symptom_new": mt not in seen,
    }


def check_shift(state: dict, stat_name: str) -> int:
    """给 `dice.check` 用的便捷入口：直接给某项属性的 TSF 修正。"""
    return shift_for(stat_name, tsf_progress_of(state))


# ---------------------------------------------------------------- 写：症状与推动

def note_symptoms(state: dict) -> list[str]:
    """把"本次首次可见"的症状记入 `state[SYMPTOMS_KEY]`，返回**新叙述**的症状文本。

    **只增不减**（去重集合语义），因此不会回退。返回空列表表示本次没有新症状 ——
    调用方据此决定是否把症状写进叙述（"只叙述一次"）。
    """
    prog = tsf_progress_of(state)
    mt = tier_of(prog)
    seen = state.setdefault(SYMPTOMS_KEY, [])
    if not isinstance(seen, list):           # 旧档/脏数据兜底
        seen = []
        state[SYMPTOMS_KEY] = seen
    if mt in seen:
        return []
    seen.append(mt)
    seen.sort()
    return [_SYMPTOMS[mt]]


def apply_nudge(state: dict, amount: int, *, reason: str) -> int:
    """施加一次**有界**的转变推动（模型被允许的那一点），返回施加后的进度。

    约束（三重）：
      1. `amount` 被钳到 `[-NUDGE_PER_TURN_MAX, +NUDGE_PER_TURN_MAX]`（单轮 ±1）；
      2. 累计值**只增不减**（负数推动被夹成 0），保证 `tsf_progress_of` 单调；
      3. 累计值本身钳在 `0..100`，不会把进度顶出域。

    **为什么允许模型提议**：转变的"加速度"有叙事成分（玩家在剧情里主动接受了什么），
    完全不给模型任何话语权会让机制显得与叙述脱节。但**幅度被限死在 ±1**，
    且落在独立字段上，因此**不可能**让模型直接写 `doom`/`sta`/`rea`/标签/结局。
    """
    cur = max(0, _as_int(state.get(NUDGE_KEY), 0))
    amt = _as_int(amount)
    amt = max(-NUDGE_PER_TURN_MAX, min(NUDGE_PER_TURN_MAX, amt))
    new = max(0, min(100, cur + max(0, amt)))     # 负数不产生减量（单调硬约束）
    state[NUDGE_KEY] = new
    log = state.setdefault("log", [])
    if isinstance(log, list):
        log.append({"kind": "tsf_nudge", "reason": str(reason),
                    "amount": amt, "total": new,
                    "progress": tsf_progress_of(state)})
    return tsf_progress_of(state)


# ---------------------------------------------------------------- 旧档兼容

#: 本模块引入的字段及其默认值（读档补齐用）。
DEFAULT_FIELDS: dict[str, Any] = {
    NUDGE_KEY: 0,
    SYMPTOMS_KEY: [],
}


def ensure_fields(state: dict) -> list[str]:
    """旧档兼容：缺字段 ⇒ 补默认值。返回**被补齐的键名列表**（便于报告/测试断言）。

    **不得抛异常**（任务硬要求）。类型不对也视为缺（例如 `tsf_symptoms` 被写成字符串）。
    """
    filled: list[str] = []
    if not isinstance(state, dict):
        return filled
    for key, default in DEFAULT_FIELDS.items():
        cur = state.get(key)
        if key == SYMPTOMS_KEY:
            ok = isinstance(cur, list)
        else:
            ok = isinstance(cur, int) and not isinstance(cur, bool)
        if not ok:
            state[key] = list(default) if isinstance(default, list) else default
            filled.append(key)
    return filled


def effects_block(state: dict) -> str:
    """注入叙述 prompt 的 TSF 状态块（信息遮蔽：不给数值，只给"感觉"）。

    与 `state.states_block` 的手法是同一个（COC-DESIGN §2.10 手法 2）：
    LLM 不该知道确切修正值，只该知道身体在怎样变化。
    """
    prog = tsf_progress_of(state)
    mt = tier_of(prog)
    return (f"【身体变化】{_SYMPTOMS[mt]}\n"
            f"（叙述线索：{MECH_LABELS[mt]}；不要提到任何数值或「修正」。）")

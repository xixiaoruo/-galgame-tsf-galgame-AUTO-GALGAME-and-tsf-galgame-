"""CoC 模式的权威状态：构造 / 序列化 / 视图 / 提示词状态块。

与 `app/vn.py` 的**范式复用**（COC-DESIGN §2.1 对应表；本包不 import vn.py）：

| vn.py | 本模块 | 差异 |
|---|---|---|
| `initial_states(config)` (vn.py:47) | `initial_states(payload, cfg)` | 本模式初始值是**难度表 × 角色 × 背景 × 古神**的叠加结果，不是一张固定状态表 |
| `sanitize_delta(delta, keys)` (vn.py:66) | `sanitize_delta(delta, cfg)` | 本模式**分两级钳制**（level-1 ±3 / level-2 ±5），且白名单里没有数值字段 |
| `apply_delta(states, delta)` (vn.py:80) | `apply_delta(state, delta, cfg)` | 本模式应用后立即跑 `invariants()`（属性≤0 → DOOM=100 等） |
| `panel_view(states)` (vn.py:87) | `panel_view(state)` | 本模式面板是**多资源**（DOOM + 双血条 + 5 属性 + 标签计数），不是单组 0–100 |
| `states_block(states)` (vn.py:105) | `states_block(state)` | 本模式**只暴露数值与"感觉"，不暴露标签名 / 敌人弱点 / 本次检定属性**（信息遮蔽，§2.10 手法 2） |

状态**独立存放**在 `session["coc"]`，不是 `session["states"]` / `session["char_stats"]` /
`prot["stats"]`（COC-DESIGN §7.1 B3/B4）。

`rng_state` 也保存在状态里：`random.Random` 的 `getstate()` 结果转成可 JSON 化的形式，
读档后经 `restore_rng()` 逐字节还原 —— 这是 V-08 A7"存档读档后状态一致"的对象。

本模块**不导入** app/tsf.py、app/vn.py、app/game.py，也不导入 LLM 客户端。
"""
from __future__ import annotations

import json
import os
import random
import uuid
from typing import Any

from . import paths
from . import sanitize as _san
from .provenance import t

#: 参与检定的五项技能属性（MECHANICS.md §6.2，可信度高）
STAT_KEYS: tuple[str, ...] = ("STR", "DEX", "PER", "KNW", "CHA")

#: 局阶段
PHASES = ("town", "rest", "shop", "combat", "lighthouse", "ending")

#: 存档格式版本（读档迁移用）
SCHEMA_VERSION = 1


class StateError(RuntimeError):
    """状态非法（缺字段 / 阶段非法 / 属性名非法）。"""


# ---------------------------------------------------------------- 初始构造

def initial_states(payload: dict | None = None, cfg: dict | None = None) -> dict:
    """构造一局的初始权威状态（**唯一**的初始值来源：难度表 + 角色 + 背景）。"""
    if cfg is None:
        raise StateError("initial_states 需要已校验的 tuning 数据（数值不得有代码内兜底）")
    p = dict(payload or {})

    difficulty = str(p.get("difficulty") or "Initiate")
    diff_all = t(cfg, "difficulty")
    aliases = diff_all.get("_aliases", {}) if isinstance(diff_all, dict) else {}
    difficulty = aliases.get(difficulty, difficulty)
    if difficulty not in diff_all or difficulty.startswith("_"):
        raise StateError(
            f"未知难度 {difficulty!r}；合法值 "
            f"{[k for k in diff_all if not k.startswith('_')]}（MECHANICS.md §2.4）"
        )
    d = diff_all[difficulty]

    death_mode = str(p.get("death_mode") or "last_stand")
    if death_mode not in ("last_stand", "sudden_death"):
        raise StateError(f"未知死亡模式 {death_mode!r}（MECHANICS.md §1.3 两种）")

    stats = {k: int((p.get("stats") or {}).get(k, 5)) for k in STAT_KEYS}
    for k, v in stats.items():
        if not 1 <= v <= 12:
            raise StateError(f"属性 {k}={v} 超出 1~12（COC-DESIGN §8.4）")

    sta = int(d["sta"])
    rea = int(d["rea"])
    state: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "mode": "coc",
        "sid": str(p.get("sid") or uuid.uuid4().hex[:12]),
        "difficulty": difficulty,
        "death_mode": death_mode,
        "old_god": str(p.get("old_god") or "Goizo"),
        "character": str(p.get("character") or "Kirie"),
        "background": str(p.get("background") or "Commoner"),

        "doom": 0,
        "sta": sta, "sta_max": sta,
        "rea": rea, "rea_max": rea,
        "funds": int(d["funds"]),
        "exp": 0,
        "level": 1,
        "stats": stats,
        "luck": int(p.get("luck", 6)),

        "mystery_count": int(d["mystery_count"]),
        "mystery_idx": 0,
        "keys": 0,
        "investigations_done": 0,
        "investigations_total": 0,
        "mystery_len": int(t(cfg, "mystery.len_default")),
        "lighthouse_events": [],
        "phase": "town",
        "ending": None,

        "tags": [],
        "inventory": {"backpack": [], "storage": []},
        "allies": [],
        "flags": {},
        "seen_cards": [],

        "log": [],
        "turn": 0,
        "rng_state": None,

        # ---- t54：CoC 内的 TSF 子系统（`app/coc/tsf.py`）新增的两个字段 ----
        # **刻意没有** `tsf_progress` / `tsf_stage`：它们是**派生量**
        # （`tsf.tsf_progress_of` 从账本投影、`tsf.stage_index` 从段表现算），
        # 落盘反而制造"存档与账本不一致"的可能（t53 §9 的结论，captain 已采纳）。
        # 下面两个字段不可推导，所以必须落盘：
        #   · tsf_nudge_total —— 模型被允许提议的、有界的推动累计（账本不记录它）
        #   · tsf_symptoms    —— 已叙述过的症状档位（同一进度下"看过/没看过"是两种历史）
        "tsf_nudge_total": 0,
        "tsf_symptoms": [],
    }
    if p.get("seed") is not None:
        state["rng_state"] = rng_to_json(random.Random(int(p["seed"])))
    return state


def new_rng(seed: int | None = None, state: dict | None = None) -> random.Random:
    """新建 rng；若 state 里已有 `rng_state` 则精确还原（读档一致性）。"""
    if state is not None and state.get("rng_state") is not None:
        return restore_rng(state["rng_state"])
    return random.Random(seed)


# ------------------------------------------------------- rng 的可 JSON 化

def rng_to_json(rng: random.Random) -> list:
    """`random.Random.getstate()` → 可 JSON 化的嵌套 list。

    `getstate()` 返回 `(version, tuple_of_625_ints, gauss_next_or_None)`，
    其中 ints 是 32 位无符号；直接 json.dumps 会因 tuple 变 list 而**能**工作，
    但 `gauss_next` 可能是 `None` 或 float —— 这里显式规整，保证往返稳定。
    """
    version, internal, gauss = rng.getstate()
    return [int(version), [int(x) for x in internal],
            None if gauss is None else float(gauss)]


def restore_rng(payload: list) -> random.Random:
    """`rng_to_json` 的逆操作（逐字段还原，保证后续随机序列完全一致）。"""
    if not isinstance(payload, (list, tuple)) or len(payload) != 3:
        raise StateError("rng_state 结构非法，无法还原")
    version, internal, gauss = payload
    rng = random.Random()
    rng.setstate((int(version), tuple(int(x) for x in internal),
                  None if gauss is None else float(gauss)))
    return rng


# ---------------------------------------------------------------- 序列化

def to_json_obj(state: dict) -> dict:
    """导出为可 JSON 落盘的纯数据（键序稳定，便于逐字节比较）。"""
    return json.loads(json.dumps(state, ensure_ascii=False, sort_keys=True))


def dumps(state: dict) -> str:
    return json.dumps(to_json_obj(state), ensure_ascii=False, sort_keys=True, indent=1)


def loads(text: str) -> dict:
    obj = json.loads(text)
    if not isinstance(obj, dict) or obj.get("mode") != "coc":
        raise StateError("这不是一个 CoC 存档")
    from_json_obj(obj)
    return obj


def from_json_obj(obj: dict) -> dict:
    """读档后的字段补齐（老存档迁移位）。"""
    obj.setdefault("schema", SCHEMA_VERSION)
    obj.setdefault("tags", [])
    obj.setdefault("flags", {})
    obj.setdefault("allies", [])
    obj.setdefault("seen_cards", [])
    obj.setdefault("log", [])
    obj.setdefault("inventory", {"backpack": [], "storage": []})
    obj.setdefault("stats", {k: 5 for k in STAT_KEYS})
    return obj


def save(state: dict) -> str:
    """落盘到 `<DATA_DIR>/saves/<sid>.json`，返回路径。

    路径**跟随 `app.config.DATA_DIR`**（含 `TSF_DATA_DIR` 逃逸舱），不再由包内 `__file__` 推 ——
    后者会写进安装目录，更新替换目录时连带丢档（详见 `paths.py` 顶部的两类路径说明）。
    这里调 `paths.saves_dir()` 取**当下值**而不是读顶层常量，避免进程内切换数据根后拿到旧路径。
    """
    target = paths.saves_dir()
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, f"{state['sid']}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps(state))
    return path


def load(sid: str) -> dict:
    path = os.path.join(paths.saves_dir(), f"{sid}.json")
    with open(path, "r", encoding="utf-8") as fh:
        return loads(fh.read())


def state_equal(a: dict, b: dict) -> tuple[bool, str]:
    """逐字段深比较（V-08 A7 用）。返回 `(相等?, 不同点说明)`。"""
    ja, jb = dumps(a), dumps(b)
    if ja == jb:
        return True, ""
    la, lb = ja.splitlines(), jb.splitlines()
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<缺行>"
        y = lb[i] if i < len(lb) else "<缺行>"
        if x != y:
            return False, f"第 {i + 1} 行不同：\n  A: {x}\n  B: {y}"
    return False, "长度不同但无逐行差异（不应发生）"


# ------------------------------------------------- 数值清洗（vn.py 范式）

def sanitize_delta(delta: Any, cfg: dict) -> dict:
    """vn.py:66 `sanitize_delta` 的 CoC 版：**只要 LLM 提案**（服务端结算走 apply_delta）。

    注意语义差异：vn.py 返回清洗后的 dict；本模式同时需要"丢了什么"的审计信息，
    故审计走 `sanitize_llm_delta`（返回二元组），此处保持 vn.py 的单返回值形态。
    """
    cleaned, _dropped = _san.sanitize_llm_delta(delta, cfg)
    return cleaned


def apply_delta(state: dict, delta: Any, cfg: dict, *, trust: str = "llm") -> dict:
    """vn.py:80 `apply_delta` 的 CoC 版：委托到唯一写入点。

    `trust` 默认 `"llm"`（**最不信任**的那一层）—— 与 `sanitize.apply_delta` 一致。
    引擎结算请用 `sanitize.apply_delta_engine()`（第二轮复核后新增的显式信任层）。
    """
    return _san.apply_delta(state, delta, cfg, trust=trust)


def panel_view(state: dict) -> dict:
    """前端面板视图（vn.py:87 的 CoC 版：多资源而非单组 0–100）。

    `severity` 是 sta/rea 阈值的**机读形态**（MECHANICS.md §3.2：≤9 变样子、
    ≤0 被黑污覆盖）——本模式不做立绘，故转成字段交给前端。
    """
    sta, rea = int(state.get("sta", 0)), int(state.get("rea", 0))
    severity = "normal"
    if sta <= 0 or rea <= 0:
        severity = "collapsed"
    elif sta <= 9 or rea <= 9:
        severity = "hurt" if sta <= 9 else "haunted"
    tags = state.get("tags") or []
    return {
        "mode": "coc",
        "sid": state.get("sid"),
        "doom": int(state.get("doom", 0)),
        "doom_line": doom_line(state),
        "sta": sta, "sta_max": int(state.get("sta_max", sta)),
        "rea": rea, "rea_max": int(state.get("rea_max", rea)),
        "funds": int(state.get("funds", 0)),
        "exp": int(state.get("exp", 0)),
        "level": int(state.get("level", 1)),
        "stats": {k: int(v) for k, v in (state.get("stats") or {}).items()},
        "keys": int(state.get("keys", 0)),
        "mystery_idx": int(state.get("mystery_idx", 0)),
        "mystery_len": int(state.get("mystery_len", 0)),
        "investigations_done": int(state.get("investigations_done", 0)),
        "phase": state.get("phase"),
        "severity": severity,
        # 标签**只给计数与类型**，不给 id/name —— 信息遮蔽（§2.10 手法 2）
        "tag_counts": {
            "injury": sum(1 for x in tags if x.get("kind") == "injury"),
            "curse": sum(1 for x in tags if x.get("kind") == "curse"),
            "town": sum(1 for x in tags if x.get("kind") == "town"),
        },
        "ending": state.get("ending"),
        "turn": int(state.get("turn", 0)),
    }


def states_block(state: dict) -> str:
    """注入 LLM 的**数值块**（vn.py:105 的 CoC 版）。

    与 `vn.py.char_states_block` 的**故意差异**：这里**不输出标签名**、
    **不输出敌人弱点**、**不输出本次检定属性** —— 信息遮蔽是恐怖感来源
    （MECHANICS.md §9.4 手法表、COC-DESIGN §8.2 禁忌）。
    """
    return "\n".join([
        doom_line(state),
        f"体力 {int(state.get('sta', 0))}/{int(state.get('sta_max', 0))}"
        f"　理智 {int(state.get('rea', 0))}/{int(state.get('rea_max', 0))}",
        "属性 " + " ".join(f"{k}{int(v)}" for k, v in (state.get("stats") or {}).items()),
        f"身体状态：{severity_phrase(state)}",
        f"异常感：{unease_phrase(state)}",
    ])


def doom_line(state: dict) -> str:
    """固定输出行（MECHANICS.md §8.3 设计建议 4 / §10.2 #3 / COC-DESIGN §6.1 第 11 项）。

    DOOM 到 100 时按上游 §2.1 显示 `END`（"数字改为显示 END"）。
    """
    d = int(state.get("doom", 0))
    return "[DOOM END]" if d >= 100 else f"[DOOM {d}%]"


def severity_phrase(state: dict) -> str:
    """把 sta/rea 阈值转成**自然语言**（替代立绘变化；§10.2 #12 的转化方案）。"""
    sta, rea = int(state.get("sta", 0)), int(state.get("rea", 0))
    parts: list[str] = []
    if sta <= 0:
        parts.append("身体已经撑不住了")
    elif sta <= 9:
        parts.append("身上有伤，呼吸不稳")
    if rea <= 0:
        parts.append("精神已经崩了")
    elif rea <= 9:
        parts.append("神经绷得太紧，注意力开始断线")
    return "；".join(parts) if parts else "暂时没有明显异样"


def unease_phrase(state: dict) -> str:
    """把 doom/rea 转成"不安感"措辞（**不含城镇状态名**，只需玩家能感到不对劲）。"""
    d = int(state.get("doom", 0))
    rea = int(state.get("rea", 0))
    if d >= 75 or rea <= 9:
        return "空气里有种说不上来的错位，像有什么正在被翻开"
    if d >= 40:
        return "街上比平时安静，安静得不太对"
    return "暂时还算正常"


def extra_dimensions(state: dict) -> list[dict]:
    """额外面板维度（周目 / 次数 / 等级）。

    `COC-DESIGN.md` §9.1 的验收要求"存档读档后**周目**一致"，
    这里把周目相关的计数量显式暴露出来，好让面板与报告都能看到它们。
    """
    return [
        {"key": "mystery_idx", "name": "已过谜团", "value": int(state.get("mystery_idx", 0))},
        {"key": "keys", "name": "钥匙", "value": int(state.get("keys", 0))},
        {"key": "investigations_total", "name": "累计调查",
         "value": int(state.get("investigations_total", 0))},
        {"key": "level", "name": "等级", "value": int(state.get("level", 1))},
    ]


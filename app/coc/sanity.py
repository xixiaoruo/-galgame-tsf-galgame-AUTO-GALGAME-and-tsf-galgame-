"""CoC 式 SAN（理智值）子系统。

## 与既有 REA 的分工

本项目原有 `REA` 是“意志 / 精神耐久”，归零会进 `END_REA_ZERO`（精神崩溃）。
1.9.19 在不改动 REA 公式的前提下新增独立的 **SAN 0–99**：

| 维度 | REA | SAN |
|---|---|---|
| 量纲 | 难度表给的 13~21 点 | 0–99，初始按 KNW 调整 |
| 归零 | `END_REA_ZERO` | `END_SAN_ZERO`（新增） |
| 检定 | d12 属性检定（既有） | d100 理智检定（本模块） |
| 回复 | 休息 / 战斗动作 | 镇静剂、嗅盐、威士忌、笔记等物品 |

两者**同时存在**：REA 继续承担“被消耗的精神耐久”，SAN 承担经典 CoC 的
“每次目击恐怖后的理智损失 / 临时疯狂”。LLM 无权改 SAN（不在白名单），
只有引擎的 SAN 检定与物品使用能写它。

本模块不导入 LLM、不写状态；真正的写入统一交给 `Run.apply` → `sanitize.apply_delta`。
"""
from __future__ import annotations

import re
from typing import Any

from .provenance import t

_DICE_RE = re.compile(r"^\s*(\d{1,2})?d(\d{1,4})(?:\s*([+-])\s*(\d{1,3}))?\s*$", re.I)


class SanityError(ValueError):
    """SAN 检定参数非法。"""


def _num(cfg: dict, key: str) -> int:
    return int(t(cfg, f"sanity.{key}"))


def initial_san(stats: dict, cfg: dict) -> int:
    """初始 SAN = base + (KNW − 6) × step，钳到 1..max。

    用 KNW 而不是 POW：本模式没有 POW；KNW 是“知道多少”的属性，和 SAN 的
    “知道得越多越危险”正好同向。
    """
    knw = int((stats or {}).get("KNW", 6) or 6)
    base = _num(cfg, "base")
    step = _num(cfg, "knw_step")
    cap = _num(cfg, "max")
    return max(1, min(cap, base + (knw - 6) * step))


def san_max(cfg: dict | None = None) -> int:
    if cfg is None:
        from .provenance import load_tuning
        cfg = load_tuning()
    return _num(cfg, "max")


def roll_expr(rng, expr: str | int) -> int:
    """掷一个简单骰式：`0` / `1d6` / `2d4+1`。非法 ⇒ `SanityError`。"""
    if isinstance(expr, int):
        return max(0, expr)
    s = str(expr or "").strip()
    if s in ("", "0"):
        return 0
    if s.isdigit():                       # 允许 "1" / "3" 这类固定值
        return max(0, int(s))
    m = _DICE_RE.match(s)
    if not m:
        raise SanityError(f"SAN 损失/回复骰式非法：{expr!r}（支持 0 / 1d6 / 2d4+1）")
    count = int(m.group(1) or 1)
    sides = int(m.group(2))
    if not (1 <= count <= 10 and 2 <= sides <= 100):
        raise SanityError(f"SAN 骰式越界：{expr!r}（1~10 颗、2~100 面）")
    mod = 0
    if m.group(3):
        mod = int(m.group(4)) * (1 if m.group(3) == "+" else -1)
    return max(0, sum(rng.randint(1, sides) for _ in range(count)) + mod)


def parse_loss(spec: Any) -> tuple[str, str]:
    """`"0/1d6"` → `("0","1d6")`；只给一个值时成功/失败同值。"""
    if isinstance(spec, int):
        s = str(max(0, spec))
        return s, s
    s = str(spec or "").strip()
    if not s:
        return "0", "0"
    if "/" not in s:
        return s, s
    a, b = s.split("/", 1)
    a, b = a.strip() or "0", b.strip() or "0"
    # 提前校验，避免失败分支掷到一半才抛
    for x in (a, b):
        if x in ("0", ""):
            continue
        if x.isdigit():
            continue
        if not _DICE_RE.match(x):
            raise SanityError(f"SAN 损失格式非法：{spec!r}（示例 0/1d6、1/1d10）")
    return a, b


def target_for(san: int, difficulty: str, cfg: dict) -> int:
    d = str(difficulty or "regular").lower()
    table = t(cfg, "sanity.difficulty") or {}
    if d not in table:
        raise SanityError(f"未知 SAN 检定难度 {difficulty!r}；可选 {sorted(table)}")
    node = table[d]
    ratio = float(node.get("value") if isinstance(node, dict) and "value" in node else node)
    return max(1, min(99, int(max(1, san) * ratio)))


def san_state(state: dict) -> dict:
    """面板/叙述用的 SAN 状态：`{state,label,bout}`。"""
    san = int(state.get("san", 0) or 0)
    cap = int(state.get("san_max", 99) or 99)
    bout = bool(state.get("temp_insanity")) and int(state.get("turn", 0)) < int(
        state.get("bout_until_turn", 0) or 0)
    if san <= 0:
        st, label = "zero", "SAN 归零：你已经无法把看到的东西放回原处"
    elif bout:
        st, label = "bout", "临时疯狂：你的感官正在把周围的声音拼成别的东西"
    elif san <= max(1, cap * 2 // 10):
        st, label = "fragile", "SAN 濒危：任何一次目击都可能成为最后一根稻草"
    elif san <= max(1, cap * 4 // 10):
        st, label = "shaken", "SAN 受损：你还能思考，但思考本身开始变得危险"
    else:
        st, label = "stable", "SAN 稳定"
    return {"state": st, "label": label, "bout": bout}


def check(run, *, loss: str = "0/1d6", difficulty: str = "regular",
          reason: str = "") -> dict:
    """执行一次 d100 SAN 检定并落账（**引擎写入**）。

    返回：
      `{roll, target, success, san_before, san_after, loss, bout, state, text}`
    """
    st = run.state
    cfg = run.cfg
    st.setdefault("san", initial_san(st.get("stats") or {}, cfg))
    st.setdefault("san_max", san_max(cfg))
    san_before = int(st.get("san", 0))
    if san_before <= 0:
        return {"roll": 0, "target": 0, "success": False, "san_before": 0,
                "san_after": 0, "loss": 0, "bout": False,
                "state": san_state(st), "text": "SAN 已经归零，无法再检定。"}

    target = target_for(san_before, difficulty, cfg)
    roll = int(run.rng.randint(1, 100))
    success = roll <= target
    succ_expr, fail_expr = parse_loss(loss)
    expr = succ_expr if success else fail_expr
    amount = roll_expr(run.rng, expr)
    # SAN 损失是引擎结算，不受 ±3 单轮预算限制（经典 CoC 单次可能损失 10+）
    applied = run.apply({"san": -amount}, reason=f"san_check:{reason or difficulty}",
                        clamp=False)
    san_after = int(st.get("san", 0))
    threshold = _num(cfg, "bout_threshold")
    bout = amount >= threshold and san_after > 0
    if bout:
        st["temp_insanity"] = True
        st["bout_until_turn"] = int(st.get("turn", 0)) + _num(cfg, "bout_turns")
    st["san_checks"] = int(st.get("san_checks", 0)) + 1
    st["san_loss_total"] = int(st.get("san_loss_total", 0)) + amount
    if bout:
        run.log("san_bout", {"amount": amount, "san": san_after,
                             "until_turn": st.get("bout_until_turn")})
    run.log("san_check", {"roll": roll, "target": target, "success": success,
                          "loss": amount, "san": san_after,
                          "difficulty": difficulty, "reason": reason,
                          "applied": applied.get("applied")})
    # SAN 归零 → 结局判定（新结局 END_SAN_ZERO；与 REA 分开）
    run.settle_ending()
    return {
        "roll": roll, "target": target, "success": bool(success),
        "san_before": san_before, "san_after": san_after,
        "loss": int(amount), "bout": bool(bout),
        "state": san_state(st),
        "text": ("理智检定成功" if success else "理智检定失败")
                + f"：1D100={roll} / 目标 {target}，"
                + (f"损失 {amount} 点 SAN。" if amount else "没有损失 SAN。")
                + (" 你陷入了临时疯狂。" if bout else ""),
    }


def recover(run, expr: str | int, *, reason: str = "item") -> int:
    """回复 SAN（物品/治疗）。返回实际回复量。"""
    st = run.state
    st.setdefault("san", initial_san(st.get("stats") or {}, run.cfg))
    before = int(st.get("san", 0))
    amount = roll_expr(run.rng, expr)
    if amount <= 0:
        return 0
    run.apply({"san": amount}, reason=f"san_recover:{reason}", clamp=False)
    return max(0, int(st.get("san", 0)) - before)

"""随机数 / 骰子系统（1.9.18）。

设计参考 GitHub 上的轻量 dice-notation 项目
（如 dokzlo13/dice_roller 的「NdM±K + 逐颗明细」交互），但本模块不引入第三方
依赖：使用 Python 标准库 `secrets.SystemRandom` 取数，避免随机数被时间种子预测。

支持：
    1d100 / d20 / 2d6+3 / d% / 3d8-1
    count 1~20，sides 2~1000，modifier -999~999

CoC 检定走现有 `app.coc.dice` 的规则（d12 + 威胁修正），由本模块只负责取随机数，
不把掷骰逻辑复制第二份。
"""
from __future__ import annotations

import re
import secrets
import time
import uuid

MAX_COUNT = 20
MAX_SIDES = 1000
MAX_MOD = 999
_RNG = secrets.SystemRandom()
_NOTATION = re.compile(
    r"^\s*(?P<count>\d{0,2})\s*[dD]\s*(?P<sides>%|\d{1,4})\s*"
    r"(?P<mod>[+-]\s*\d{1,3})?\s*$"
)


class RngError(ValueError):
    pass


def roll(notation: str = "1d100", label: str = "") -> dict:
    """掷一次骰子并返回逐颗明细；非法 notation 抛 RngError。"""
    text = str(notation or "").strip()
    m = _NOTATION.match(text)
    if not m:
        raise RngError("骰子表达式格式：NdM±K，如 1d100、2d6+3、d%")
    count = int(m.group("count") or 1)
    sides_raw = m.group("sides")
    sides = 100 if sides_raw == "%" else int(sides_raw)
    mod_raw = m.group("mod") or "0"
    modifier = int(mod_raw.replace(" ", ""))
    if not (1 <= count <= MAX_COUNT):
        raise RngError(f"骰子数量需在 1~{MAX_COUNT}")
    if not (2 <= sides <= MAX_SIDES):
        raise RngError(f"骰面需在 2~{MAX_SIDES}")
    if abs(modifier) > MAX_MOD:
        raise RngError(f"修正值需在 -{MAX_MOD}~{MAX_MOD}")
    rolls = [int(_RNG.randint(1, sides)) for _ in range(count)]
    total = sum(rolls) + modifier
    return {
        "id": uuid.uuid4().hex[:10],
        "notation": f"{count}d{sides}" + (f"{modifier:+d}" if modifier else ""),
        "label": str(label or "").strip()[:40],
        "count": count,
        "sides": sides,
        "modifier": modifier,
        "rolls": rolls,
        "total": total,
        "ts": time.time(),
        "source": "secrets.SystemRandom",
    }


def coc_check(stat: str, stat_value: int, threat: str = "LOW",
              dc: int = 0) -> dict:
    """CoC 风格检定：复用 app.coc.dice 的规则，随机数由本模块取。

    只做一次只读检定，不写会话状态；返回与 CoC 引擎同形的 CheckResult 字段。
    """
    from .coc import dice as coc_dice
    from .coc import provenance

    stat = str(stat or "KNW").upper()
    if stat not in ("STR", "DEX", "PER", "KNW", "CHA"):
        raise RngError("CoC 属性需为 STR / DEX / PER / KNW / CHA")
    threat = str(threat or "LOW").upper()
    if threat not in coc_dice.THREATS:
        threat = "LOW"
    try:
        value = max(0, min(99, int(stat_value)))
    except (TypeError, ValueError):
        raise RngError("属性值无效")
    cfg = provenance.load_tuning()
    result = coc_dice.check(stat, value, threat, _RNG, cfg,
                            dc=max(-3, min(3, int(dc or 0))))
    return {
        "notation": "1d12",
        "label": f"CoC 检定 · {stat}",
        "roll": result.roll,
        "stat": result.stat,
        "stat_value": result.stat_value,
        "threat": result.threat,
        "threat_mod": result.threat_mod,
        "dc": result.dc,
        "success": result.success,
        "reason": result.reason,
        "total": result.roll,
        "ts": time.time(),
        "source": "app.coc.dice",
    }

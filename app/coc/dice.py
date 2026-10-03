"""CoC 模式的属性检定（唯一的判定公式实现）。

上游依据（`MECHANICS.md` §4.2，可信度**高**，公式本身两页一致）：

    { 被检定的属性值 } vs { 1~12 的随机数 } + { 威胁等级修正 }
    数值大的一方胜；**平手判玩家胜**。

符号归一化（`MECHANICS.md` §12 #8）：`Math_Guide` 把 +1/+2 加给**掷骰方**，
`Threat_Level` 把 -1/-2 减在**玩家方**——两页符号相反、方向一致。
本模块只实现"方向"，把修正减在玩家侧：`stat + threat_mod vs roll + 1`。
两种表述在任何输入下**数值等价**（+1 给骰子 == -1 给玩家），因此不需要判定谁对。

为什么"平手判玩家胜"是本文件的唯一存在理由：
上游 §4.2 给了这条规则，而它只在**恰好打平**那一格生效——只测"赢"和"输"两侧
永远碰不到它。所以 `tie_boundary_samples()` 专门生成 `stat + mod == roll + 1` 的样本，
`test_dice.py` 对它逐条断言"判玩家胜"。

本模块**不掷骰之外的任何东西**：不写状态、不调 LLM、不知道卡与角色。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .provenance import t

#: 四档地点威胁等级（`MECHANICS.md` §2.4、§10.2 #6）
THREATS: tuple[str, ...] = ("SAFE", "LOW", "DANGEROUS", "DOOMED")


class UnknownThreatError(ValueError):
    """威胁等级不在 SAFE/LOW/DANGEROUS/DOOMED 内。"""


@dataclass(frozen=True)
class CheckResult:
    """一次检定的**已定稿结果**（确定性层产出，LLM 只能收到它，不能改它）。

    `dc` 是**对玩家侧的总修正**（卡内难度 + 转变修正），保持"只有一个修正载体"
    的形式，因此公式与既有穷举测试（`stat + threat_mod + dc >= roll + 1`）
    **逐字不变**。
    """

    stat: str
    stat_value: int
    threat: str
    threat_mod: int
    dc: int
    roll: int
    success: bool
    reason: str = field(default="")
    #: 卡内难度（`dc` 里扣掉转变修正后的部分）—— 用于叙述与诊断。
    dc_card: int = field(default=0)
    #: 本次由 **TSF 转变**带来的修正（`app/coc/tsf.py` 提供）。0 = 无转变影响。
    tsf_shift: int = field(default=0)

    @property
    def effective_mod(self) -> int:
        """本次检定对玩家侧的总修正 = 威胁修正 + 卡内难度 + TSF 转变修正。"""
        return self.threat_mod + self.dc

    @property
    def target(self) -> int:
        """玩家需要达到的最小值 = 掷骰结果 + 1（"数值大的一方胜"的等价形式）。"""
        return self.roll + 1

    @property
    def margin(self) -> int:
        """`stat_value + effective_mod - (roll + 1)`。

        `>= 0` 即胜；`== 0` 就是**平手**。
        """
        return self.stat_value + self.effective_mod - self.target

    @property
    def is_tie(self) -> bool:
        """是否恰好打平（平手判玩家胜，所以 `is_tie` 必然 `success`）。"""
        return self.margin == 0


def threat_mod(threat: str, cfg: dict) -> int:
    """取威胁修正。数值**只能**来自 tuning.json，代码里没有任何兜底常量。"""
    mods = t(cfg, "threat_mod")
    if threat not in mods:
        raise UnknownThreatError(
            f"未知威胁等级 {threat!r}；合法值 {sorted(mods)}（MECHANICS.md §2.4 四档）"
        )
    return int(mods[threat])


def clamp_dc(dc: Any, cfg: dict) -> int:
    """把卡内的 `dc` 钳进 tuning 的取值域（缺省 0）。"""
    lo = int(t(cfg, "checks.dc_min"))
    hi = int(t(cfg, "checks.dc_max"))
    try:
        v = int(dc)
    except (TypeError, ValueError):
        return 0
    return max(lo, min(hi, v))


def _clamp_tsf_shift(shift: Any, cfg: dict) -> int:
    """把 TSF 转变修正钳进同一 `checks.dc_min..dc_max` 域（缺省 0）。

    **为什么用同一个域**：`dc` 是"对玩家侧的总修正"的唯一载体，
    转变修正与卡内难度在公式里是同一种东西。用同一个域可以保证
    「卡内 dc 与转变修正的合计」不会突破 `dc_min..dc_max` 的设计边界
    （t53 §10 的"避免与 DOOM 叠加失控"考虑）。
    非数值/非法输入一律当 0（**不抛**）：这是从 LLM 与旧档都可能来的数据。
    """
    lo = int(t(cfg, "checks.dc_min"))
    hi = int(t(cfg, "checks.dc_max"))
    if isinstance(shift, bool):
        return 0
    try:
        v = int(shift)
    except (TypeError, ValueError):
        return 0
    return max(lo, min(hi, v))


def check(
    stat_name: str,
    stat_value: int,
    threat: str,
    rng,
    cfg: dict,
    *,
    dc: int = 0,
    tsf_shift: int = 0,
) -> CheckResult:
    """执行一次检定并返回**已定稿结果**。

    `rng` 由会话持有（`random.Random`，种子落盘）——LLM 从不接触它。
    `cfg` 是已校验来源的 tuning 数据（见 provenance.load_tuning）。

    `tsf_shift`（t54 新增）：`app/coc/tsf.py` 给出的**转变修正**，直接相加进 `dc`。
    默认 0 ⇒ 未接入时行为**逐字不变**。它不是随机量、也不增加掷骰次数 ——
    同一 `(stat, threat, dc, tsf_shift)` 下结果只由 `rng` 当次取值决定，
    因此**可复现性不受影响**。
    """
    lo = int(t(cfg, "checks.roll_min"))
    hi = int(t(cfg, "checks.roll_max"))
    roll = rng.randint(lo, hi)
    smod = threat_mod(threat, cfg)
    d = clamp_dc(dc, cfg)
    shift = _clamp_tsf_shift(tsf_shift, cfg)
    eff = smod + d + shift
    # 「数值大的一方胜；平手判玩家胜」→ 玩家胜 ⟺ stat + eff >= roll + 1
    success = (stat_value + eff) >= (roll + 1)
    reason = (
        f"{stat_name} {stat_value} (mod {eff:+d}) vs roll {roll}+1 = {roll + 1} "
        f"→ {'胜' if success else '负'}"
        + ("（平手，判玩家胜）" if stat_value + eff == roll + 1 else "")
    )
    return CheckResult(
        stat=stat_name,
        stat_value=int(stat_value),
        threat=threat,
        threat_mod=smod,
        dc=d + shift,
        roll=roll,
        success=success,
        reason=reason,
        dc_card=d,
        tsf_shift=shift,
    )


def resolve_deterministic(
    stat_name: str,
    stat_value: int,
    threat: str,
    roll: int,
    cfg: dict,
    *,
    dc: int = 0,
    tsf_shift: int = 0,
) -> CheckResult:
    """用**给定**的掷骰结果结算（测试与探针用；不消耗 rng）。

    `tsf_shift` 语义同 `check()`。默认 0 ⇒ 既有调用与测试**逐字不变**。
    """
    smod = threat_mod(threat, cfg)
    d = clamp_dc(dc, cfg)
    shift = _clamp_tsf_shift(tsf_shift, cfg)
    eff = smod + d + shift
    success = (stat_value + eff) >= (roll + 1)
    return CheckResult(
        stat=stat_name,
        stat_value=int(stat_value),
        threat=threat,
        threat_mod=smod,
        dc=d + shift,
        roll=int(roll),
        success=success,
        reason=(
            f"{stat_name} {stat_value} (mod {eff:+d}) vs roll {roll}+1 = {roll + 1} "
            f"→ {'胜' if success else '负'}"
            + ("（平手，判玩家胜）" if stat_value + eff == roll + 1 else "")
        ),
        dc_card=d,
        tsf_shift=shift,
    )


def tie_boundary_samples(cfg: dict, *, stat_min: int | None = None,
                         stat_max: int | None = None) -> list[dict]:
    """生成**全部**平手样本：`stat + threat_mod + dc == roll + 1`。

    这是"边界样本才是被测语义的载体"的落地：只测赢/输两侧永远覆盖不到平手。
    返回 [{stat, stat_value, threat, dc, roll}]，供测试逐条断言"判玩家胜"。
    """
    lo = int(t(cfg, "checks.roll_min"))
    hi = int(t(cfg, "checks.roll_max"))
    smin = int(stat_min if stat_min is not None else t(cfg, "sanitize.stat_value_min"))
    smax = int(stat_max if stat_max is not None else t(cfg, "sanitize.stat_value_max"))
    out: list[dict] = []
    for threat in THREATS:
        mod = threat_mod(threat, cfg)
        for dc in (int(t(cfg, "checks.dc_min")), 0, int(t(cfg, "checks.dc_max"))):
            for roll in range(lo, hi + 1):
                # 平手条件：stat + mod + dc == roll + 1
                stat_value = roll + 1 - mod - dc
                if smin <= stat_value <= smax:
                    out.append({
                        "stat": "KNW", "stat_value": stat_value, "threat": threat,
                        "dc": dc, "roll": roll,
                    })
    return out


def exhaustive_cases(cfg: dict) -> Iterable[dict]:
    """穷举 1~12 × 4 威胁 × dc 三点的全部组合（测试用，共 12×4×3=144 组）。"""
    lo = int(t(cfg, "checks.roll_min"))
    hi = int(t(cfg, "checks.roll_max"))
    smin = int(t(cfg, "sanitize.stat_value_min"))
    smax = int(t(cfg, "sanitize.stat_value_max"))
    for threat in THREATS:
        for dc in (int(t(cfg, "checks.dc_min")), 0, int(t(cfg, "checks.dc_max"))):
            for stat_value in range(smin, smax + 1):
                for roll in range(lo, hi + 1):
                    yield {"stat": "PER", "stat_value": stat_value, "threat": threat,
                           "dc": dc, "roll": roll}

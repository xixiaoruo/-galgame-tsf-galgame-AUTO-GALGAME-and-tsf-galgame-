"""CoC 模式的主循环：地点 → 抽卡 → 结算 → 章节推进 → 结局判定。

设计依据：`COC-DESIGN.md` §2.4「DOOM 时序与阶段推进」、§2.5「事件卡」、§2.7「结局与跨局」。
上游依据（`MECHANICS.md`）：§4.1 抽象层级、§4.4 每次调查 +1% DOOM、§1.1 钥匙与灯塔、
§1.4 每章追加一个不可移除的城镇状态、§7.5 设计建议「分支不要做成树，是结局判定函数」。

**本模块是确定性层的核心**（COC-DESIGN §3.2 E1/E5/E6）：
  - E1 结算先于 LLM：本模块产出 `Resolution`（已定稿数值），全程**没有 LLM 调用**；
  - E5 掷骰在代码：rng 由 `Run` 持有并随存档落盘；
  - E6 落盘即真值：`Run.log` 只由本模块写入。

本模块**不导入**任何 LLM / 网络 / app.game / app.vn / app.tsf。
"""
from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import content
from . import dice
from . import sanitize as _san
from . import state as _state
from . import tsf as _tsf
from .provenance import t


class LoopError(RuntimeError):
    """循环被非法调用（阶段不符 / 卡不存在 / 选项越界）。"""


def _enemy_index() -> dict:
    """敌人表 → `{id: enemy}`（内容文件里是 list，运行期按 id 查）。"""
    idx: dict = {}
    for e in content.enemies():
        if isinstance(e, dict) and e.get("id"):
            idx[str(e["id"])] = e
    return idx


# ------------------------------------------------------------------ 结局判定

def decide_endings(state: dict, cfg: dict) -> tuple[str | None, list[str]]:
    """**结局判定函数**（不是分支树：`MECHANICS.md` §7.5 / §10.2 #11）。

    优先级（COC-DESIGN §2.7）：
      1. 硬终止：DOOM ≥100 → 该古神的专属末日结局（§1.2）；
      2. STA 判定（`last_stand` 有 -5 缓冲带，§1.3）；
      3. REA 判定；
      4. 规则表命中的特殊结局（读 flag）；
      5. 通关（终盘已过）。

    返回 `(ending_id|None, 判定说明列表)`。说明列表让"为什么是这个结局"可审计 ——
    结局判定是最容易写出"看起来生效的分支"的地方。
    """
    notes: list[str] = []
    table = content.endings()
    by_id = {e["id"]: e for e in table}

    # 1) DOOM 100 → 古神专属末日结局
    if int(state.get("doom", 0)) >= 100:
        gid = state.get("old_god")
        hit = next((e for e in table if e.get("kind") == "doom" and e.get("old_god") == gid), None)
        if hit is None:
            hit = next((e for e in table if e.get("kind") == "doom"), None)
        if hit:
            notes.append(f"doom>=100 → {hit['id']}（古神 {gid} 专属末日结局）")
            return hit["id"], notes
        notes.append("doom>=100 但结局表里没有对应末日结局（内容缺失）")

    # 2) STA —— 死亡模式决定阈值（§1.3）
    sta = int(state.get("sta", 0))
    mode = state.get("death_mode")
    if mode == "last_stand":
        floor = int(t(cfg, "sanitize.last_stand_floor"))
        if sta <= floor:
            notes.append(f"sta<=floor({floor}) → END_STA_ZERO（LAST STAND 缓冲带用尽，§1.3）")
            return "END_STA_ZERO", notes
        if sta <= 0:
            notes.append(f"sta={sta}<=0 但仍在 LAST STAND 缓冲带内（floor={floor}），本回合不判死")
    else:
        if sta <= 0:
            notes.append("sta<=0 → END_STA_ZERO（SUDDEN DEATH，§1.3）")
            return "END_STA_ZERO", notes

    # 3) REA —— 精神崩溃进精神病院（§3.2，可信度高）
    rea = int(state.get("rea", 0))
    if mode == "last_stand":
        floor = int(t(cfg, "sanitize.last_stand_floor"))
        if rea <= floor:
            notes.append(f"rea<=floor({floor}) → END_REA_ZERO")
            return "END_REA_ZERO", notes
        if rea <= 0:
            notes.append(f"rea={rea}<=0 但仍在缓冲带内（floor={floor}）")
    else:
        if rea <= 0:
            notes.append("rea<=0 → END_REA_ZERO（SUDDEN DEATH）")
            return "END_REA_ZERO", notes

    # 4) 规则表命中的特殊结局（读 flag）
    flags = state.get("flags") or {}
    for e in sorted(table, key=lambda x: int(x.get("priority", 999))):
        rule = e.get("flag_rule")
        if not rule:
            continue
        if flags.get(rule.get("flag")) == rule.get("equals"):
            notes.append(f"flag {rule.get('flag')}={rule.get('equals')!r} → {e['id']}")
            return e["id"], notes

    # 5) 通关
    if flags.get("lighthouse_cleared") is True:
        notes.append("lighthouse_cleared → END_CLEARED（§1.2 通关）")
        return "END_CLEARED", notes

    return None, notes


# ------------------------------------------------------------------ Run

@dataclass
class Resolution:
    """一次调查的**已定稿结果**（LLM 只能收到它，改不了它 —— E1）。"""

    card_id: str | None
    choice_index: int
    choice_text: str
    check: dice.CheckResult | None
    branch: str | None                 # "success" / "failure" / None
    outcome: dict                      # 卡面声明的数值/标签/flag 变化
    combat: dict | None                # 若有战斗：轮数 / 结果 / 敌人
    delta_applied: dict
    dropped: list[str]
    notes: list[str]
    text: str = ""                     # 结果文本（MVP 直接取卡面 outcome.text）

    def to_json(self) -> dict:
        d = {
            "card_id": self.card_id,
            "choice_index": self.choice_index,
            "choice_text": self.choice_text,
            "branch": self.branch,
            "outcome": self.outcome,
            "combat": self.combat,
            "delta_applied": self.delta_applied,
            "dropped": self.dropped,
            "notes": self.notes,
            "text": self.text,
        }
        if self.check is not None:
            d["check"] = {
                "stat": self.check.stat,
                "stat_value": self.check.stat_value,
                "threat": self.check.threat,
                "threat_mod": self.check.threat_mod,
                "dc": self.check.dc,
                "roll": self.check.roll,
                "target": self.check.target,
                "margin": self.check.margin,
                "success": self.check.success,
                "is_tie": self.check.is_tie,
                "reason": self.check.reason,
            }
        return d


@dataclass
class Run:
    """一局的运行态：权威状态 + rng + 内容快照 + 事件日志。"""

    state: dict
    cfg: dict
    rng: random.Random
    loc_table: list[dict] = field(default_factory=list)
    mystery_table: list[dict] = field(default_factory=list)
    card_pool: list[dict] = field(default_factory=list)
    enemy_table: dict = field(default_factory=dict)
    town_table: list[dict] = field(default_factory=list)

    #: 事件日志（只由本模块写入 —— E6）
    events: list[dict] = field(default_factory=list)
    #: 已抽过城镇状态的 id
    town_picked: list[str] = field(default_factory=list)

    # -------------------------------------------------------------- 构造

    @classmethod
    def start(cls, payload: dict | None = None, cfg: dict | None = None, *,
              extra_cards: list[dict] | None = None) -> "Run":
        """开一局。`extra_cards` 供测试注入夹具卡（不改内容文件）。"""
        if cfg is None:
            from .provenance import load_tuning
            cfg = load_tuning()
        p = dict(payload or {})
        st = _state.initial_states(p, cfg)
        rng = random.Random(int(p["seed"])) if p.get("seed") is not None else random.Random()
        pool = content.cards()
        if extra_cards:
            pool = list(pool) + list(extra_cards)
        run = cls(
            state=st, cfg=cfg, rng=rng,
            loc_table=content.locations(),
            mystery_table=content.mysteries(),
            card_pool=pool,
            enemy_table=_enemy_index(),
            town_table=content.town_status(),
        )
        run.state["rng_state"] = _state.rng_to_json(run.rng)
        run.log("run_start", {
            "difficulty": st["difficulty"], "old_god": st["old_god"],
            "seed": p.get("seed"), "cards": len(pool),
        })
        return run

    @classmethod
    def resume(cls, saved: dict, cfg: dict | None = None, *,
               extra_cards: list[dict] | None = None) -> "Run":
        """从存档恢复（rng 精确还原、**事件账本也还原** —— E6 / V-08 A7）。

        事件账本必须一起还原：它是 V-08 A2「DOOM 为何变化」的唯一依据，
        丢账本会让读档后的判定与存档前不一致（A7 就是靠这个差异变红抓到的）。
        """
        if cfg is None:
            from .provenance import load_tuning
            cfg = load_tuning()
        st = _state.from_json_obj(json.loads(json.dumps(saved, ensure_ascii=False)))
        pool = content.cards()
        if extra_cards:
            pool = list(pool) + list(extra_cards)
        log = st.get("log") or []
        run = cls(
            state=st, cfg=cfg, rng=_state.restore_rng(st["rng_state"]),
            loc_table=content.locations(),
            mystery_table=content.mysteries(),
            card_pool=pool,
            enemy_table=_enemy_index(),
            town_table=content.town_status(),
        )
        run.events = [dict(x) for x in log]
        return run

    # -------------------------------------------------------------- 日志

    def log(self, kind: str, data: dict) -> None:
        self.events.append({"i": len(self.events), "turn": int(self.state.get("turn", 0)),
                            "kind": kind, **data})
        self.state["log"] = self.events

    # -------------------------------------------------------------- 环境

    def location(self, loc_id: str) -> dict:
        for loc in self.loc_table:
            if loc["id"] == loc_id:
                return loc
        raise LoopError(f"未知地点 {loc_id!r}")

    def current_mystery(self) -> dict | None:
        idx = int(self.state.get("mystery_idx", 0))
        if idx < len(self.mystery_table):
            return self.mystery_table[idx]
        return None

    def available_locations(self) -> list[str]:
        m = self.current_mystery()
        if m:
            return list(m.get("locations") or [])
        return [l["id"] for l in self.loc_table]

    def threat_of(self, loc_id: str) -> str:
        return str(self.location(loc_id).get("threat", "SAFE"))

    # ------------------------------------------------------ 程序化修改（唯一写点）

    def apply(self, delta: Any, *, reason: str, clamp: bool = True,
              trust: str = "engine") -> dict:
        """所有数值/标签/flag 变化的**唯一入口**（委托到 `sanitize.apply_delta_*`）。

        每次调用都记一条 `delta` 事件 —— V-08 A2 的"DOOM 为何变化"就是靠这份账本。

        ## 信任层（第二轮复核后新增）

        默认 `trust="engine"`：本类是**引擎**，它调用 `apply` 的每一处都是确定性结算
        （抽卡结果 / 章完成 / 战斗伤害 / 探针刻度）。传 `trust="llm"` 会走**被限制**的
        那一层，只能写 `exp`/`funds`/物品/属性 —— 用于"这里拿到的可能是模型原值"的场合。

        为什么默认 engine 而不是 llm：这是**引擎自己的方法**，它的语义就是"我结算完了，落账"。
        真正的防线在 `sanitize.apply_delta` 的**默认**参数上（默认 llm），
        所以"外部/新路径直接调 sanitize"仍然受限；而"新路径调 Run.apply 且传模型原值"
        这种误用，靠 `reason=` 参数与代码审查可见（每处调用都带 reason）。

        ⚠️ 注意：`prompt.narr_turn` 已经把模型提案过了 `sanitize_llm_delta`（白名单+钳制），
        所以 `session.py:206` 的 `run.apply(turn["coc_delta"])` 落进来的是**已清洗**的值。
        """
        before = {k: int(self.state.get(k, 0)) for k in ("doom", "sta", "rea", "exp", "funds")}
        res = _san.apply_delta(self.state, delta, self.cfg, clamp=clamp, trust=trust)
        after = {k: int(self.state.get(k, 0)) for k in ("doom", "sta", "rea", "exp", "funds")}
        changes = {k: after[k] - before[k] for k in before if after[k] != before[k]}
        self.log("delta", {"reason": reason, "changes": changes,
                           "applied": res["applied"], "dropped": res["dropped"],
                           "invariant_notes": res["notes"],
                           "doom": after["doom"], "sta": after["sta"], "rea": after["rea"]})
        return {"changes": changes, **res}

    # ------------------------------------------------------ 抽卡

    def _card_eligible(self, card: dict, loc_id: str) -> bool:
        if loc_id not in (card.get("locations") or []):
            return False
        m = self.current_mystery()
        mid = m["id"] if m else None
        if card.get("mystery_only") and card["mystery_only"] != mid:
            return False
        req = card.get("requirements") or {}
        flags = self.state.get("flags") or {}
        bag = (self.state.get("inventory") or {}).get("backpack") or []
        if any(f not in flags or not flags[f] for f in req.get("flags_all", [])):
            return False
        if any(flags.get(f) for f in req.get("flags_none", [])):
            return False
        if any(i not in bag for i in req.get("items_all", [])):
            return False
        if "items_any" in req and not any(i in bag for i in req["items_any"]):
            return False
        d = int(self.state.get("doom", 0))
        if "doom_min" in req and d < int(req["doom_min"]):
            return False
        if "doom_max" in req and d > int(req["doom_max"]):
            return False
        for k, v in (req.get("stat_min") or {}).items():
            if int((self.state.get("stats") or {}).get(k, 0)) < int(v):
                return False
        return True

    def draw_card(self, loc_id: str, *, allow_seen: bool = False) -> dict | None:
        """抽一张卡。

        可见性策略（`MECHANICS.md` §9.4 坑 1「重复劳作 / 背板」的规避）：
        优先抽**没见过**的；都被抽过时，`allow_seen` 才放开重抽
        （设计里的 `S-03`：MVP 降级为"抽到已见卡则重抽一次"，此处更保守）。
        """
        pool = [c for c in self.card_pool if self._card_eligible(c, loc_id)]
        if not pool:
            return None
        seen = set(self.state.get("seen_cards") or [])
        fresh = [c for c in pool if c["id"] not in seen]
        use = fresh if fresh else (pool if allow_seen else [])
        if not use:
            return None
        weights = [max(1, int(c.get("weight", 10))) for c in use]
        return self.rng.choices(use, weights=weights, k=1)[0]

    # ------------------------------------------------------ 战斗（MVP 最简）

    def resolve_combat(self, enemy_id: str, *, is_boss: bool = False,
                       extra_dmg: int = 0) -> dict:
        """极简确定性战斗：双方每回合互打，直到一方倒下或到回合上限。

        **刻意做成极简**：本任务（t48）只保证"A1–A8 能在不花 LLM 调用的前提下跑通"，
        完整动作表/时间槽（`MECHANICS.md` §5.1–§5.3）属 v2。
        但两条上游结论已经体现在数据上：
          - §9.4 坑 2「战斗拖沓」→ `round_cap` 兜底，非 Boss 必须 ≤2 回合可解；
          - §5.4 幽灵免疫普通攻击 → `incorporeal` 的敌人打不动，探针会如实记账。
        """
        en = self.enemy_table.get(enemy_id)
        if en is None:
            raise LoopError(f"敌人表里没有 {enemy_id!r}")
        cap = int(t(self.cfg, "combat.round_cap"))
        atk = int(t(self.cfg, "probe.probe_attack_damage"))
        dmg = int(en.get("damage", 1)) + int(self._difficulty()["enemy_dmg"]) + int(extra_dmg)
        dmg = max(0, dmg)
        hp = int(en.get("hp", 1))
        incorporeal = "incorporeal" in (en.get("traits") or [])
        rounds = 0
        player_dmg_dealt = 0
        taken = 0
        while hp > 0 and rounds < cap:
            rounds += 1
            if not incorporeal:
                hp -= atk
                player_dmg_dealt += atk
            else:
                self.log("combat_note", {
                    "enemy": enemy_id, "note": "普通攻击穿过了它的身体（MECHANICS.md §5.4 幽灵免疫）"})
            if hp <= 0:
                break
            taken += dmg
            self.apply({"sta": -dmg}, reason=f"combat:{enemy_id}:enemy_hit", clamp=False)
            if int(self.state.get("sta", 0)) <= 0:
                break
        won = hp <= 0
        res = {"enemy": enemy_id, "is_boss": bool(is_boss), "rounds": rounds,
               "player_damage": player_dmg_dealt, "damage_taken": taken,
               "incorporeal": incorporeal, "won": won,
               "hit_cap": rounds >= cap and not won}
        self.log("combat", res)
        return res

    def _difficulty(self) -> dict:
        diff = t(self.cfg, "difficulty")
        return diff[self.state["difficulty"]]

    # ------------------------------------------------------ 结算（E1）

    def resolve_card(self, card: dict, choice_index: int) -> Resolution:
        """结算一次事件卡：检定 → 选分支 → 数值/标签/flag 变化（**LLM 未参与**）。"""
        choices = card.get("choices") or []
        if not (0 <= choice_index < len(choices)):
            raise LoopError(f"{card['id']} 选项越界：{choice_index}（共 {len(choices)} 个）")
        ch = choices[choice_index]
        spec = ch.get("check")
        res_check: dice.CheckResult | None = None
        branch: str | None = None
        outcome: dict = {}
        if spec:
            locs = card.get("locations") or []
            threat = str(spec.get("threat") or (self.threat_of(locs[0]) if locs else "SAFE"))
            stat_name = str(spec.get("stat"))
            stat_value = int((self.state.get("stats") or {}).get(stat_name, 0))
            # ★ t54 接线点：TSF 转变修正接进检定公式（**唯一**的检定点）
            #   · 修正来自 `tsf.check_shift`，是**纯函数**（只读账本投影 + 固定表）
            #   · 因此同 seed 同输入 ⇒ 同 shift ⇒ 同结果（可复现性见 test_coc_tsf）
            #   · shift=0 时公式与接入前**逐字相同**（dice.check 的默认参数）
            tsf_shift = _tsf.check_shift(self.state, stat_name)
            res_check = dice.check(stat_name, stat_value, threat, self.rng, self.cfg,
                                   dc=spec.get("dc", 0), tsf_shift=tsf_shift)
            self.log("check", {
                "card": card["id"], "choice": choice_index, "stat": res_check.stat,
                "roll": res_check.roll, "threat": res_check.threat,
                "threat_mod": res_check.threat_mod, "dc": res_check.dc,
                "target": res_check.target, "margin": res_check.margin,
                "success": res_check.success, "is_tie": res_check.is_tie,
                # t54：把转变修正记入账本（V-08 A2 的"数值为何变化"同样适用于它）
                "tsf_shift": res_check.tsf_shift,
                "tsf_progress": _tsf.progress(self.state),
            })
            branch = "success" if res_check.success else "failure"
            outcome = dict(ch.get(branch) or {})
        else:
            outcome = dict(ch.get("success") or ch.get("outcome") or {})

        # 战斗先打（若该分支触发战斗）
        combat_info = None
        cspec = outcome.pop("combat", None)
        if isinstance(cspec, dict) and cspec.get("enemy"):
            combat_info = self.resolve_combat(
                str(cspec["enemy"]),
                is_boss=bool(cspec.get("is_boss")),
                extra_dmg=int(self._extra_enemy_dmg()),
            )

        # 数值/标签/flag 变化（唯一写点）
        delta: dict[str, Any] = {}
        for k in ("doom", "sta", "rea", "exp", "funds"):
            if k in outcome:
                delta[k] = int(outcome[k])
        if isinstance(outcome.get("stat"), dict):
            delta["stat"] = dict(outcome["stat"])
        for k in ("item_grant", "item_take"):
            if outcome.get(k):
                delta[k] = list(outcome[k])
        if outcome.get("tags_grant"):
            delta["tags_grant"] = [
                self._make_tag(tid) for tid in outcome["tags_grant"] if self._make_tag(tid)
            ]
        if outcome.get("tags_remove"):
            delta["tags_remove"] = list(outcome["tags_remove"])
        if isinstance(outcome.get("flag_set"), dict):
            delta["flag_set"] = dict(outcome["flag_set"])
        if outcome.get("ending"):
            delta["ending"] = outcome["ending"]
        applied = self.apply(delta, reason=f"card:{card['id']}:{branch or 'plain'}"
                             if delta else f"card:{card['id']}:noop")

        if card["id"] not in (self.state.get("seen_cards") or []):
            self.state.setdefault("seen_cards", []).append(card["id"])

        return Resolution(
            card_id=card["id"], choice_index=choice_index,
            choice_text=str(ch.get("text", "")), check=res_check, branch=branch,
            outcome=outcome, combat=combat_info,
            delta_applied=applied.get("applied", {}), dropped=applied.get("dropped", []),
            notes=applied.get("notes", []),
            text=str(outcome.get("text", "")),
        )

    def _extra_enemy_dmg(self) -> int:
        over = (t(self.cfg, "probe") or {}).get("_probe") or {}
        v = over.get("extra_enemy_dmg")
        return int(v["value"]) if isinstance(v, dict) else 0

    def _extra_doom_per_turn(self) -> int:
        over = (t(self.cfg, "probe") or {}).get("_probe") or {}
        v = over.get("extra_doom_per_turn")
        return int(v["value"]) if isinstance(v, dict) else 0

    def _always_combat(self) -> bool:
        over = (t(self.cfg, "probe") or {}).get("_probe") or {}
        v = over.get("always_combat")
        return bool(v["value"]) if isinstance(v, dict) else False

    def _make_tag(self, tid: str) -> dict | None:
        """从内容表里按 id 取标签定义（伤势 / 诅咒 / 城镇状态统一为 tag）。"""
        for group in (content.injuries(), content.curses(), content.town_status(),
                      content.treatments()):
            for node in group:
                if node["id"] == tid:
                    return {k: v for k, v in node.items() if not k.startswith("_")}
        return None

    # ------------------------------------------------------ 章节推进

    def _town_tag_delta(self) -> dict:
        """章末追加 1 个**永久不可移除**的城镇状态（`MECHANICS.md` §1.4、§2.5 设计建议）。"""
        left = [x for x in self.town_table if x["id"] not in self.town_picked]
        if not left:
            return {}
        pick = self.rng.choice(left)
        self.town_picked.append(pick["id"])
        tag = {k: v for k, v in pick.items() if not k.startswith("_")}
        tag["duration"] = "run"
        tag["kind"] = "town"
        return {"tags_grant": [tag]}

    def complete_mystery(self) -> dict:
        """章完成序列（COC-DESIGN §2.4 第 4 步）。

        `clamp=False` 是**有意**的：章节减免是**引擎的结构化规则**
        （`MECHANICS.md` §2.1「完成一个谜团 −5%/−4%/−3%/−2%/0」随难度），
        不是 LLM 提案 —— 而 `clamp_turn_delta` 的 ±3 预算（COC-DESIGN §3.2 E3）
        是给"单轮数值变化"设的护栏。两者混用会把 Initiate 的 −4 压成 −3，
        导致实际减免与难度表不符（这个 bug 是 V-08 A2 抓到的）。
        """
        d = self._difficulty()
        relief = int(d["doom_relief_per_mystery"])
        before_keys = int(self.state.get("keys", 0))
        delta: dict[str, Any] = {"flag_set": {"mystery_done_%d" % int(self.state.get("mystery_idx", 0)): True}}
        if relief:
            delta["doom"] = -relief
        delta.update(self._town_tag_delta())
        self.apply(delta, reason="mystery_complete", clamp=False)
        self.state["keys"] = before_keys + 1
        self.state["mystery_idx"] = int(self.state.get("mystery_idx", 0)) + 1
        self.state["investigations_done"] = 0
        m = self.current_mystery()
        self.state["mystery_len"] = int(t(self.cfg, "mystery.len_default"))
        note = {
            "keys": int(self.state["keys"]),
            "doom_relief": relief,
            "town_status_added": [x["id"] for x in (self.state.get("tags") or [])
                                 if x.get("kind") == "town"][-1:],
            "next_mystery": m["id"] if m else None,
        }
        self.log("mystery_complete", note)
        # 钥匙齐 → 终盘阶段（灯塔），不可退出（§1.1、§1.6 设计建议）
        if int(self.state["keys"]) >= int(self.state.get("mystery_count", 5)):
            self.state["phase"] = "lighthouse"
            cnt = int(t(self.cfg, "mystery.lighthouse_event_count"))
            self.state["lighthouse_events"] = [f"LE-{i + 1}" for i in range(cnt)]
            self.log("lighthouse_enter", {"events": self.state["lighthouse_events"]})
        return note

    # ------------------------------------------------------ 主推进

    def advance_town(self, loc_id: str, *, choice_index: int = 0,
                     policy: str = "probe") -> Resolution:
        """城镇阶段推进一步：选地点 → +DOOM → 抽卡 → 结算。

        `choice_index` 固定为整数索引（探针用确定性策略选它）；
        `policy="probe"` 时由 `pick_choice_index` 决定，保证**不依赖模型随机性**。
        """
        if self.state.get("phase") == "ending":
            raise LoopError("本局已结束（phase=ending），不能再推进")
        if self.state.get("ending"):
            raise LoopError("本局已判定结局，不能再推进")
        avail = self.available_locations()
        if loc_id not in avail:
            raise LoopError(f"地点 {loc_id!r} 不在当前可选范围 {avail}")
        self.state["phase"] = "town"
        self.state["turn"] = int(self.state.get("turn", 0)) + 1

        # ① 每次调查任意地点 +1% DOOM（MECHANICS.md §4.1，无论是否主线）
        self.apply({"doom": int(t(self.cfg, "doom_up.investigate"))},
                   reason="investigate", clamp=False)
        # DOOMED 地点额外 +1%（§2.4）
        if self.threat_of(loc_id) == "DOOMED":
            self.apply({"doom": int(t(self.cfg, "doom_up.investigate_doomed"))},
                       reason="investigate_doomed", clamp=False)

        self.state["investigations_done"] = int(self.state.get("investigations_done", 0)) + 1
        self.state["investigations_total"] = int(self.state.get("investigations_total", 0)) + 1

        # ② 抽卡（或按探针旋钮强进战斗）
        card = None if self._always_combat() else self.draw_card(loc_id)
        if card is None:
            forced = self._force_combat(loc_id)
            resolved = Resolution(
                card_id=None, choice_index=0, choice_text="（遭遇）", check=None,
                branch=None, outcome={}, combat=forced,
                delta_applied={}, dropped=[], notes=[],
                text="有东西从暗处迎上来。")
        else:
            if policy == "probe":
                choice_index = self.pick_choice_index(card)
            resolved = self.resolve_card(card, choice_index)

        # ③ 探针加速刻度（**仅**用于 V-08 A5 构造终局；默认值来自 tuning，正式路径为 0 时无效）
        extra = self._extra_doom_per_turn()
        if extra:
            self.apply({"doom": extra}, reason="probe_extra_doom", clamp=False)

        self.log("investigation", {
            "loc": loc_id, "threat": self.threat_of(loc_id),
            "card": resolved.card_id, "investigations_done": self.state["investigations_done"],
            "mystery_len": self.state["mystery_len"],
            "doom": int(self.state["doom"]),
        })

        # ④ 章完成？
        if int(self.state["investigations_done"]) >= int(self.state["mystery_len"]):
            if int(self.state["keys"]) < int(self.state.get("mystery_count", 5)):
                self.complete_mystery()

        return resolved

    def _force_combat(self, loc_id: str) -> dict:
        m = self.current_mystery()
        boss = m.get("boss") if m else None
        if boss and boss in self.enemy_table:
            return self.resolve_combat(str(boss), is_boss=True,
                                       extra_dmg=self._extra_enemy_dmg())
        any_enemy = sorted(self.enemy_table)[0]
        return self.resolve_combat(any_enemy, is_boss=False,
                                   extra_dmg=self._extra_enemy_dmg())

    @staticmethod
    def pick_choice_index(card: dict) -> int:
        """探针用的**确定性**选项策略：优先无检定、无战斗的选项，否则第 0 个。

        它存在的意义是：让 V-08 的 A1–A8 在**不依赖模型随机性**的前提下可重复。
        真实玩家路径会把 `choice_index` 换成玩家输入（接线任务）。
        """
        for i, ch in enumerate(card.get("choices") or []):
            if "check" in ch:
                continue
            ok = ch.get("success") or {}
            if "combat" in ok:
                continue
            return i
        return 0

    # ------------------------------------------------------ 终局收尾

    def settle_ending(self) -> tuple[str | None, list[str]]:
        """判定并落定结局（幂等）。"""
        eid, notes = decide_endings(self.state, self.cfg)
        if eid:
            self.state["ending"] = eid
            self.state["phase"] = "ending"
            self.log("ending", {"ending": eid, "notes": notes,
                                "doom": int(self.state["doom"]),
                                "sta": int(self.state["sta"]),
                                "rea": int(self.state["rea"])})
        return eid, notes

    def ending_title(self, eid: str) -> str:
        for e in content.endings():
            if e["id"] == eid:
                return str(e.get("title", eid))
        return eid

    # ------------------------------------------------------ 存档

    def snapshot(self) -> dict:
        """落盘用的完整状态（含 rng 状态 —— V-08 A7 的一致性对象）。"""
        self.state["rng_state"] = _state.rng_to_json(self.rng)
        self.state["log"] = self.events
        return _state.to_json_obj(self.state)

    def save(self) -> str:
        self.state["rng_state"] = _state.rng_to_json(self.rng)
        self.state["log"] = self.events
        return _state.save(self.state)

    # ------------------------------------------------------ 视图

    def panel(self) -> dict:
        return _state.panel_view(self.state)

    def doom_line(self) -> str:
        return _state.doom_line(self.state)

    def public_view(self, turn: Resolution | None = None) -> dict:
        """玩家可见视图。

        **A8 的检查对象**：这里的任何字段都不得出现 `STR/DEX/PER/KNW/CHA` 或"检定"
        字样（结算前不得预告考哪个属性 —— `MECHANICS.md` §4.2 信息遮蔽）。
        因此本函数**只**输出卡面正文与选项文本，**不**输出 `Resolution.check`。
        """
        card = None
        if turn is not None and turn.card_id:
            card = next((c for c in self.card_pool if c["id"] == turn.card_id), None)
        view: dict[str, Any] = {
            "mode": "coc",
            "sid": self.state.get("sid"),
            "panel": self.panel(),
            "doom_line": self.doom_line(),
            "phase": self.state.get("phase"),
            "locations": self.available_locations(),
            "scene": (card or {}).get("body", "（街上没有人。）") if card else "（街上没有人。）",
            "choices": [str(ch.get("text", "")) for ch in ((card or {}).get("choices") or [])],
        }
        return view

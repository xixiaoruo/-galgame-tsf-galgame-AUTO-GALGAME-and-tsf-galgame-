"""CoC 模式离线验收探针（**不联网、不调用任何 LLM、不加任何依赖**）。

跑 V-08 的 A1–A8（`COC-DESIGN.md` §9.2），并打印每条断言的判决与证据。

为什么必须离线：
  > 本任务的价值正在于**不花 LLM 调用、不受模型随机性影响**即可验证引擎正确性。

所以本文件里没有 `import requests`、没有 `openai`、没有 `app.llm`，
所有随机性都来自固定种子的 `random.Random`（随存档落盘）。

用法：
    python 测试工具/coc_cli_probe.py                 # 全部场景
    python 测试工具/coc_cli_probe.py --scenario a1    # 只跑某个场景
    python 测试工具/coc_cli_probe.py --json           # 机读输出（给 CI / 报告用）
    python 测试工具/coc_cli_probe.py --dump-fixture   # 导出反例夹具 JSON

退出码：全部 PASS → 0；有 FAIL → 1。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import random
import sys
from typing import Any

# --- sys.path：让本探针既能在项目根运行，也能被绝对路径调用 -------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.coc import dice as _dice          # noqa: E402
from app.coc import loop as _loop          # noqa: E402
from app.coc import provenance as _prov    # noqa: E402
from app.coc import sanitize as _san       # noqa: E402
from app.coc import state as _state        # noqa: E402

#: A8 的禁词：结算前不得把"要考哪个属性"告诉玩家（MECHANICS.md §4.2 信息遮蔽）
FORBIDDEN_TOKENS = ("STR", "DEX", "PER", "KNW", "CHA", "检定")

#: 非法玩家可见字段（这些字段本身就不该出现在 view 里）
INTERNAL_FIELDS = ("check", "roll", "threat_mod", "dc", "target", "margin", "success")


# --------------------------------------------------------------- 断言框架

class Report:
    """收集断言结果；每条都附「反例输入」说明（全队规则）。"""

    def __init__(self, name: str):
        self.name = name
        self.items: list[dict] = []

    def check(self, aid: str, desc: str, ok: bool, evidence: str,
              counterexample: str) -> bool:
        self.items.append({
            "id": aid, "desc": desc, "ok": bool(ok),
            "evidence": evidence, "counterexample": counterexample,
        })
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {aid} {desc}")
        print(f"         证据：{evidence}")
        print(f"         反例输入：{counterexample}")
        return ok

    @property
    def failed(self) -> list[dict]:
        return [x for x in self.items if not x["ok"]]


# --------------------------------------------------------------- 工具

def _signed(v: int) -> str:
    return f"{v:+d}" if v else "0"


def _doom_deltas(events: list[dict]) -> list[dict]:
    """从事件账本里抽出所有改变 doom 的记录（A2 的判定依据）。"""
    out = []
    for e in events:
        if e.get("kind") != "delta":
            continue
        ch = e.get("changes") or {}
        if "doom" in ch:
            out.append({"reason": e.get("reason"), "delta": int(ch["doom"]),
                        "after": int(e.get("doom", 0))})
    return out


def _visible_text_for_card(card: dict | None) -> str:
    """玩家在**结算前**能看到的文本：卡面正文 + 各选项文本。"""
    if not card:
        return ""
    parts = [str(card.get("body", ""))]
    for ch in card.get("choices") or []:
        parts.append(str(ch.get("text", "")))
    return "\n".join(parts)


def _card_by_id(run: _loop.Run, cid: str | None) -> dict | None:
    if not cid:
        return None
    return next((c for c in run.card_pool if c["id"] == cid), None)


#: 夹具卡：带**检定**的事件卡（A3 必须真的掷到骰子，否则"0 次检定"会掩盖公式错误）。
#: 它只注入到 `Run.card_pool`（测试夹具），**不写进 data/coc/cards/**。
FIXTURE_CHECK_CARD = {
    "id": "FIX-CHECK-TIE",
    "title": "",
    "pack": "base",
    "locations": ["Apartments", "School", "Hospital", "Downtown"],
    "mystery_only": None,
    "weight": 1,
    "requirements": {},
    "body": "夹具卡：墙上有一行字，你要不要看清它。",
    "choices": [
        # 第一个选项**带检定** → 探针的 pick_choice_index 会跳过它，故这里再加一个
        # 同样带检定的首选项并不起作用；因此夹具卡**只放带检定的选项**，
        # 让 pick_choice_index 的兜底分支（返回 0）落到检定上。
        {"text": "凑近看清那行字",
         "check": {"stat": "KNW", "threat": "SAFE", "dc": 0},
         "success": {"exp": 3, "text": "你看清了。"},
         "failure": {"rea": -1, "text": "那行字像是活的。"}},
    ],
    "erotic_hook": None,
    "_source": "本项目设计：仅为 V-08 A3/A4 提供“真的会掷骰”的路径，不参与正式内容",
    "_unverified": True,
}


def _make_fixture_pool(run: _loop.Run) -> None:
    """给 run 注入夹具卡（**只在内存里**，内容文件零改动）。"""
    run.card_pool = list(run.card_pool) + [dict(FIXTURE_CHECK_CARD)]


def _drive(run: _loop.Run, *, max_turns: int = 60,
           stop_after_first_mystery: bool = True,
           force_card: str | None = None) -> list[dict]:
    """推进一局直到结局 / 回合上限 / 第一个谜团完成。

    参数说明了**停止条件是被测语义的一部分**（不要让它变成"跑到世界末日"）：
      - `stop_after_first_mystery`：A6 的断言是"**章完成时**恰好追加 1 个 town 标签"。
        如果一路跑到 5 个章完成，就会有 5 个标签——那不是失败，只是断言的前提被跑过头了。
        所以第一个 `mystery_complete` 出现即停。
      - `force_card`：指定抽到的卡 id（A3/A4 用它保证"真的走到掷骰那条路"）。

    **观测在调用之后**：`advance_town` 内部已做 `invariants()` 纠正，
    所以外面读到的是最终值；"结算前玩家看到了什么"从卡面取。
    """
    obs: list[dict] = []
    avail = run.available_locations()
    i = 0
    while i < max_turns and not run.state.get("ending"):
        # **每轮重取**：章完成后当前谜团换了，可用地点集也会换
        # ——不刷新会撞上 LoopError("地点不在当前可选范围")，
        #   这正是第一版场景 A 只在城镇阶段空转、永远走不到 DOOM=100 的原因。
        avail = run.available_locations()
        loc = avail[i % len(avail)]
        before_doom = int(run.state.get("doom", 0))
        if force_card:
            card = next((c for c in run.card_pool if c["id"] == force_card), None)
            if card is None:
                raise RuntimeError(f"夹具卡 {force_card} 不在卡池里")
            res = run.resolve_card(card, 0)
            # 手工补上"每次调查 +1% DOOM"，与 advance_town 的正式路径保持一致
            run.apply({"doom": int(_prov.t(run.cfg, "doom_up.investigate"))},
                      reason="investigate", clamp=False)
            run.state["investigations_done"] = int(run.state.get("investigations_done", 0)) + 1
            run.state["investigations_total"] = int(run.state.get("investigations_total", 0)) + 1
        else:
            res = run.advance_town(loc)
        card = _card_by_id(run, res.card_id)
        if card:
            obs.append({"kind": "pre_resolution_view", "card": card["id"],
                        "text": _visible_text_for_card(card)})
        eid, notes = run.settle_ending()
        obs.append({
            "i": i, "loc": loc, "card": res.card_id, "check": res.to_json().get("check"),
            "doom_before": before_doom, "doom_after": int(run.state.get("doom", 0)),
            "sta": int(run.state.get("sta", 0)), "rea": int(run.state.get("rea", 0)),
            "keys": int(run.state.get("keys", 0)),
            "investigations_total": int(run.state.get("investigations_total", 0)),
            "ending": eid, "ending_notes": notes,
        })
        if stop_after_first_mystery and any(
                e.get("kind") == "mystery_complete" for e in run.events):
            break
        i += 1
    return obs


def _apply_probe_overrides(run: _loop.Run, cfg: dict, overrides: dict) -> None:
    """把 `_probe` 旋钮按场景打开（**只在探针内改内存副本，不写回 tuning.json**）。"""
    for key, value in (overrides or {}).items():
        node = cfg["probe"]["_probe"][key]
        node["value"] = value


# --------------------------------------------------------------- 场景

def scenario_a1_a2_a3_a4_a6_a7_a8() -> tuple[Report, dict]:
    """主场景：开局 → 完成第一个谜团（≥6 次调查）→ A2/A3/A4/A6/A7/A8。"""
    cfg = _prov.load_tuning()
    rep = Report("main")
    run = _loop.Run.start({"difficulty": "Initiate", "seed": 42,
                           "stats": {"STR": 5, "DEX": 5, "PER": 5, "KNW": 9, "CHA": 5}}, cfg)
    _make_fixture_pool(run)
    obs = _drive(run)

    # ---------------- A1：能开局并完成 ≥6 次调查（含完成第一个谜团并拿到钥匙）
    need = int(_prov.t(cfg, "probe.investigations_required"))
    inv_total = int(run.state.get("investigations_total", 0))
    keys = int(run.state.get("keys", 0))
    mystery_completed = any(e.get("kind") == "mystery_complete" for e in run.events)
    rep.check(
        "A1", f"开局并完成 ≥{need} 次调查，且完成第一个谜团（keys≥1）",
        inv_total >= need and keys >= 1 and mystery_completed,
        f"investigations_total={inv_total}（需 ≥{need}），keys={keys}，"
        f"mystery_complete 事件={mystery_completed}",
        f"反例：把 tuning.mystery.len_default 调到 {inv_total + 1}（永远完不成一章）→ "
        f"keys 停在 0、mystery_completed=False → 变红",
    )

    # ---------------- A2：DOOM 只在章完成时下降；每次调查至少 +1（上升途径之一生效）
    dd = _doom_deltas(run.events)
    downs = [d for d in dd if d["delta"] < 0]
    downs_ok = all(d["reason"] == "mystery_complete" for d in downs)
    inv_ups = [d for d in dd if d["reason"] == "investigate"]
    base = int(_prov.t(cfg, "doom_up.investigate"))
    ups_ok = bool(inv_ups) and all(d["delta"] >= base for d in inv_ups)
    relief = int(_prov.t(cfg, "difficulty.Initiate.doom_relief_per_mystery"))
    # 下降幅度必须**恰好等于**难度表的减免值（取负）
    relief_ok = all(d["delta"] == -relief for d in downs)
    rep.check(
        "A2", "DOOM 每次调查按规则上升；且**只有**章完成会使它下降，下降幅度等于难度表",
        downs_ok and ups_ok and relief_ok,
        f"doom 变化 {len(dd)} 笔：上升 {len([d for d in dd if d['delta'] > 0])} 笔、"
        f"下降 {len(downs)} 笔（全部 reason=mystery_complete；幅度 {[d['delta'] for d in downs]}，"
        f"难度表 Initiate=-{relief}）；investigate 上升幅度={[d['delta'] for d in inv_ups]}",
        "反例：把 doom_up.investigate 改成 0 → 上升笔数变 0，ups_ok=False 变红；"
        "或让某张卡写 doom:-5 → 会出现 reason!=mystery_complete 的下降，downs_ok=False 变红",
    )

    # ---------------- A3/A4：**强制**走到掷骰那条路（否则"0 次检定"会让公式错误蒙过去）
    run_c = _loop.Run.start({"difficulty": "Initiate", "seed": 2026,
                             "stats": {"STR": 5, "DEX": 5, "PER": 5, "KNW": 5, "CHA": 5}}, cfg)
    _make_fixture_pool(run_c)
    obs_c = _drive(run_c, max_turns=12, stop_after_first_mystery=False,
                   force_card=FIXTURE_CHECK_CARD["id"])
    checks = [o["check"] for o in obs_c if o.get("check")]
    lo, hi = int(_prov.t(cfg, "checks.roll_min")), int(_prov.t(cfg, "checks.roll_max"))
    bad = []
    for c in checks:
        want = (c["stat_value"] + c["threat_mod"] + c["dc"]) >= (c["roll"] + 1)
        if c["success"] != want or not (lo <= c["roll"] <= hi):
            bad.append(c)
    # 反向自检：故意用一个**错误公式**重算，确认差异真的能被检出
    detected = sum(
        1 for c in checks
        if c["success"] != (c["stat_value"] + c["threat_mod"] + c["dc"] > c["roll"] + 1)
    )
    rep.check(
        "A3", f"检定按公式结算（stat+threat_mod+dc ≥ roll+1，平手偏袒玩家），roll ∈ [{lo},{hi}]",
        bool(checks) and not bad and detected > 0,
        f"强制夹具卡路径发生 {len(checks)} 次检定，全部满足公式；roll 取值 "
        f"{sorted({c['roll'] for c in checks})}；"
        f"反向自检（改用无平手偏袒的 `>` 公式重算）：有 {detected} 笔判决不同 → 说明这些"
        f"样本确实落在公式的分野外（若为 0 则说明本断言测不到差异）",
        "反例：把判定改成 stat + mod > roll + 1（去掉平手偏袒）→ 命中平手的那些笔 "
        "success 与 want 不符 → bad 非空 → 变红",
    )

    # ---------------- A4：专喂平手样本 —— 穷举 1~12 × 4 威胁的**边界**样本
    ties = _dice.tie_boundary_samples(cfg)
    tie_ok = all(
        (lambda r: r.is_tie and r.success)(
            _dice.resolve_deterministic("KNW", s["stat_value"], s["threat"], s["roll"],
                                        cfg, dc=s["dc"]))
        for s in ties
    )
    # 固定平手样本（值取自 tuning，且**反查自洽性**）
    fs = int(_prov.t(cfg, "checks.tie_sample_stat"))
    fr = int(_prov.t(cfg, "checks.tie_sample_roll"))
    fx = _dice.resolve_deterministic("KNW", fs, "SAFE", fr, cfg)
    fixed_ok = fx.is_tie and fx.success
    # 边界两侧（**方向不要写反**，这里踩过一次）：
    #   roll = fr - 1 → 需要 fr，实际有 fs = fr + 1 → fs > fr → **胜**
    #   roll = fr + 1 → 需要 fr + 2，实际有 fs = fr + 1 → fs < fr + 2 → **负**
    # 即固定样本正好卡在"刚好够"的那一格，两侧一个是富余、一个是差一寸。
    fminus = _dice.resolve_deterministic("KNW", fs, "SAFE", fr - 1, cfg)
    fplus = _dice.resolve_deterministic("KNW", fs, "SAFE", fr + 1, cfg)
    discrimination_ok = fminus.success and (not fplus.success)
    rep.check(
        "A4", "平手判玩家胜：穷举边界样本全部判胜；且边界两侧确实可区分",
        tie_ok and fixed_ok and discrimination_ok and len(ties) > 0,
        f"平手样本 {len(ties)} 条（覆盖 4 档威胁 × dc∈{{-6,0,6}} × roll∈[1,12] 中所有可取平手的组合），"
        f"全部 is_tie=True 且 success=True；固定样本 stat={fs} roll={fr} → margin={fx.margin}（平手，判胜）；"
        f"roll={fr - 1} → 只需 {fr} 而有 {fs}（富余）→ success={fminus.success}；"
        f"roll={fr + 1} → 需 {fr + 2} 而只有 {fs}（差一寸）→ success={fplus.success}",
        "反例：把平手改成判玩家负（success = stat + mod > roll + 1）→ "
        f"固定样本 margin={fx.margin} 且 success=False → 变红。"
        "**只测赢/输两侧是抓不到这个的**，所以必须专喂平手样本。",
    )

    # ---------------- A6：章完成时 keys+1、DOOM 下降难度表值、恰好追加 1 个 town 标签
    mc = [e for e in run.events if e.get("kind") == "mystery_complete"]
    town_tags = [x for x in (run.state.get("tags") or []) if x.get("kind") == "town"]
    a6_ok = bool(mc) and len(mc) == 1 and len(town_tags) == 1 and \
        all(x.get("duration") == "run" for x in town_tags) and \
        int(mc[0]["keys"]) == 1 and int(mc[0]["doom_relief"]) == relief
    rep.check(
        "A6", "章完成：keys+1、DOOM 按难度表下降、恰好追加 1 个 duration=run 的 town 标签",
        a6_ok,
        f"mystery_complete 事件 {len(mc)} 次（keys={mc[0]['keys'] if mc else 'n/a'}，"
        f"doom_relief={mc[0]['doom_relief'] if mc else 'n/a'}）；"
        f"town 标签 {[(x['id'], x['duration']) for x in town_tags]}",
        "反例：把 _town_tag_delta 改成追加 2 条（或把 duration 写成 'mystery'）→ "
        "town 标签数变 2 / duration 不符 → 变红",
    )

    # ---------------- A7：存 → 读 → 再存，状态（含 rng）逐字节一致
    snap1 = run.snapshot()
    path = run.save()
    restored = _state.load(run.state["sid"])
    run2 = _loop.Run.resume(restored, cfg)
    snap2 = run2.snapshot()
    eq1, d1 = _state.state_equal(snap1, snap2)
    # 再落盘一次 → 再读一次（"三次"）
    run2.save()
    restored2 = _state.load(run.state["sid"])
    eq2, d2 = _state.state_equal(snap2, _state.to_json_obj(restored2))
    # rng 序列在还原后必须完全一致
    seq_a = [run2.rng.randint(1, 100) for _ in range(5)] if False else None
    r1 = _state.restore_rng(snap1["rng_state"])
    r2 = _state.restore_rng(snap2["rng_state"])
    rng_ok = [r1.randint(1, 1000) for _ in range(8)] == [r2.randint(1, 1000) for _ in range(8)]
    rep.check(
        "A7", "存档→读档→再存 状态逐字节一致（含 rng_state / tags / flags / inventory）",
        eq1 and eq2 and rng_ok,
        f"第一轮一致={eq1}{'' if eq1 else '（' + d1 + '）'}；第二轮一致={eq2}"
        f"{'' if eq2 else '（' + d2 + '）'}；存档 {os.path.basename(path)}；"
        f"rng 还原后 8 连抽一致={rng_ok}",
        "反例：save() 里不写 rng_state（或写 random.random() 的种子而非 getstate）→ "
        "rng 序列不一致 / 状态有差异 → 变红",
    )

    # ---------------- A8：全程没有任何"结算前可见文本"泄漏检定属性
    leaks: list[str] = []
    for o in obs:
        if o.get("kind") != "pre_resolution_view":
            continue
        txt = o["text"]
        hits = [tok for tok in FORBIDDEN_TOKENS if tok in txt]
        if hits:
            leaks.append(f"{o['card']}:{hits}")
    # 反向自检：把禁词注入一段文本，确认这个检查**真的会红**
    probe_leak = [tok for tok in FORBIDDEN_TOKENS if tok in "这一点需要 KNW 检定才能看出来"]
    rep.check(
        "A8", "结算前玩家可见文本不含检定属性名与「检定」字样（信息遮蔽）",
        not leaks and bool(probe_leak),
        f"扫描 {len([o for o in obs if o.get('kind') == 'pre_resolution_view'])} 段结算前文本，"
        f"泄漏 {len(leaks)} 处；反向自检：把禁词注入测试串能被检出（命中 {probe_leak}）",
        "反例：把选项文本改写成「用 KNW 检定」→ leaks 非空 → 变红。"
        "（反向自检那一半是防止本断言写成永远为真的形式）",
    )

    return rep, {"observations": obs, "events": run.events, "panel": run.panel()}


def scenario_a5() -> Report:
    """A5：至少 2 种结局可达，且两种结局的文本 id 不同。"""
    cfg = _prov.load_tuning()
    rep = Report("endings")

    # 结局 1：DOOM=100 → 古神专属末日结局
    cfg1 = _prov.deep_copy(cfg)
    _apply_probe_overrides(None, cfg1, {"extra_doom_per_turn": 13})
    run1 = _loop.Run.start({"difficulty": "Initiate", "seed": 42, "old_god": "Goizo"}, cfg1)
    _drive(run1, max_turns=30, stop_after_first_mystery=False)
    e1, n1 = run1.settle_ending()

    # 结局 2：STA=0（LAST STAND 缓冲带用尽）→ 肉体死亡
    cfg2 = _prov.deep_copy(cfg)
    _apply_probe_overrides(None, cfg2, {"extra_enemy_dmg": 4, "always_combat": True})
    run2 = _loop.Run.start({"difficulty": "Initiate", "seed": 7}, cfg2)
    _drive(run2, max_turns=30, stop_after_first_mystery=False)
    e2, n2 = run2.settle_ending()

    t1 = run1.ending_title(e1) if e1 else "(无)"
    t2 = run2.ending_title(e2) if e2 else "(无)"
    rep.check(
        "A5", "至少 2 种结局可达，且两者是不同的结局 id / 不同的收束文本",
        bool(e1) and bool(e2) and e1 != e2,
        f"场景 A（探针加速 DOOM）：{e1}「{t1}」（doom={run1.state['doom']}，判定说明={n1}）；"
        f"场景 B（探针放大敌人伤害并强制战斗）：{e2}「{t2}」"
        f"（sta={run2.state['sta']}，判定说明={n2}）",
        "反例：把 decide_endings 的 DOOM 分支删掉 → 场景 A 拿不到 doom 结局，"
        "e1 会是 None 或与 e2 相同 → 变红",
    )
    # 补一条：结局判定必须是**函数**而非分支树残留（幂等）
    again, _ = run1.settle_ending()
    rep.check(
        "A5b", "结局判定幂等（重复调用不改变已落定结局）",
        again == e1 and run1.state.get("ending") == e1,
        f"再次调用返回 {again}，state.ending={run1.state.get('ending')}",
        "反例：把 decide_endings 写成按顺序消耗状态的副作用函数 → 第二次调用会返回不同值 → 变红",
    )
    return rep


def scenario_sanitize() -> Report:
    """E2/E3/E4 的机读判据（白名单里没有数值字段 + 钳制 + 丢弃）。"""
    cfg = _prov.load_tuning()
    rep = Report("sanitize")

    # 白名单里**根本没有**这些键 —— LLM 连提议权都没有
    banned = ("doom", "sta", "rea", "tags_grant", "tags_remove", "flag_set", "ending")
    wl = list(_prov.t(cfg, "llm_delta_whitelist"))
    absent = [k for k in banned if k not in wl]
    rep.check(
        "E2-WL", "coc_delta 白名单里根本没有 doom/sta/rea/tags/flag/ending",
        len(absent) == len(banned),
        f"白名单 = {wl}；被排除的 {len(absent)}/{len(banned)} 个键：{absent}",
        "反例：把 'doom' 加进 tuning.llm_delta_whitelist → sanitize.assert_whitelist_matches_tuning "
        "会抛 SchemaViolation，且本断言变红（两道，互相兜底）",
    )

    # 恶意 delta：所有越权键必须被丢弃
    evil = {"doom": -100, "sta": 999, "rea": -999, "tags_grant": [{"id": "x"}],
            "flag_set": {"lighthouse_cleared": True}, "ending": "END_CLEARED",
            "exp": 3, "funds": 2, "item_grant": ["Bandage"]}
    cleaned, dropped = _san.sanitize_llm_delta(evil, cfg)
    allowed = set(wl)
    leaked = [k for k in cleaned if k not in allowed]
    rep.check(
        "E2-DROP", "恶意 delta 的越权键全部被丢弃（不留一条越权旁路）",
        not leaked and cleaned.get("exp") == 3 and cleaned.get("funds") == 2,
        f"输入 {sorted(evil)} → 保留 {sorted(cleaned)}；丢弃 {sorted(set(dropped))}",
        "反例：把 sanitize_llm_delta 的 `if key not in LLM_DELTA_WHITELIST: continue` 删掉 → "
        "cleaned 里会出现 doom/ending → leaked 非空 → 变红",
    )

    # 越界钳制：level-1 ±3、level-2 ±5
    # ⚠️ 这里必须走**引擎信任层**：doom/sta 是确定性层独占键，`apply_delta` 默认
    # `trust="llm"` 会（正确地）抛 TrustViolation。本探针扮演的是引擎结算，
    # 所以用 `apply_delta_engine()` —— 与 `loop.Run.apply` 的默认信任层一致。
    st = _state.initial_states({"difficulty": "Initiate", "seed": 1}, cfg)
    st_before = dict(st)
    res = _san.apply_delta_engine(st, {"doom": 999, "sta": -999, "exp": -999}, cfg)
    b1 = int(_prov.t(cfg, "sanitize.turn_budget_level1"))
    b2 = int(_prov.t(cfg, "sanitize.turn_budget_level2"))
    clamp_ok = (res["applied"].get("doom") == b1 and res["applied"].get("sta") == -b1
                and res["applied"].get("exp") == -b2)
    rep.check(
        "E3", f"单轮钳制：level-1 ±{b1}、level-2 ±{b2}",
        clamp_ok,
        f"apply_delta_engine({{doom: 999, sta: -999, exp: -999}}) → applied="
        f"{res['applied']}（doom={res['applied'].get('doom')}, sta={res['applied'].get('sta')}, "
        f"exp={res['applied'].get('exp')}）",
        "反例：去掉 clamp_turn_delta → doom 会直接 +100 并被 invariants 截成 100 → applied.doom=100 ≠ ±3 → 变红",
    )

    # E4 不变式：属性 ≤0 → DOOM 立即 100（MECHANICS.md §2.1 隐性绝境）
    st2 = _state.initial_states({"difficulty": "Initiate", "seed": 2}, cfg)
    st2["stats"]["KNW"] = 1
    res2 = _san.apply_delta(st2, {"stat": {"KNW": -5}}, cfg)
    inv_ok = st2["doom"] == 100 and any("属性≤0" in n for n in res2["notes"])
    rep.check(
        "E4", "属性 ≤0 → DOOM 立即 100（且记下不变式修正说明）",
        inv_ok,
        f"KNW 1 → -5 后 doom={st2['doom']}，invariants 说明={res2['notes']}",
        "反例：删掉 invariants 的 ① 分支 → doom 保持 0 → 变红",
    )

    # 唯一写入点：静态扫描，除 sanitize.py 外无人直接给 state["doom"] 赋值
    import ast
    offenders = []
    coc_dir = os.path.join(_ROOT, "app", "coc")
    for name in sorted(os.listdir(coc_dir)):
        if not name.endswith(".py") or name in ("sanitize.py",):
            continue
        p = os.path.join(coc_dir, name)
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=p)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id in ("state", "st")
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value in _san.STATE_NUMERIC_FIELDS):
                        offenders.append(f"{name}:{tgt.slice.value}")
    rep.check(
        "E-ONE-WRITE", "权威数值只有一个写入点（sanitize.apply_delta）",
        not offenders,
        f"静态扫描 app/coc/*.py（排除 sanitize.py）：直接赋值 state['doom'|'sta'|'rea'|'exp'|'funds'] "
        f"的地方 {len(offenders)} 处 {offenders}",
        "反例：在 loop.py 里写 state['doom'] = 100 → offenders 非空 → 变红",
    )

    # E1：结算先于「叙事」—— 判据按**层**拆开（t51 引入叙事层后重写，见报告 §判据口径）
    #
    # 为什么拆：t48 写这条时的口径是「app/coc/*.py 里不许出现任何 LLM 标记」，
    # 当时 coc 只有确定性引擎。t51 把**叙事层**（session.py/prompt.py/rules_*）
    # 放进同一个包 —— 它必须能调 LLM，于是原来的粗口径必然变红。
    # 拆开后反而更强：**确定性层零 LLM**（原保证）+ **叙事层只允许函数内惰性导入**
    # （新保证）+ **模型提案进状态必须显式 trust="llm"**（新保证，AST 检查）。
    LLM_MARKERS = ("import openai", "import requests", "httpx", "urllib.request",
                   "from ..llm", "from app.llm", "chat_json")
    #: 确定性结算层：必须零 LLM/网络标记
    DET_CORE = ("dice.py", "provenance.py", "content.py", "sanitize.py",
                "state.py", "loop.py")
    #: 叙事层：允许引用 LLM，但只允许**函数内**惰性导入/调用
    NARRATIVE = ("session.py", "prompt.py", "rules_horror_erotic.py")

    def _marker_lines(name: str) -> list[tuple[int, str]]:
        p = os.path.join(coc_dir, name)
        if not os.path.isfile(p):
            return []
        out = []
        with open(p, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if any(k in line.lower() for k in LLM_MARKERS):
                    out.append((i, line))
        return out

    core_hits = [f"{n}:{i}" for n in DET_CORE for i, _ in _marker_lines(n)]
    rep.check(
        "E1-DET-CORE", "确定性结算层零 LLM/网络标记（结算先于叙事的结构前提）",
        not core_hits,
        f"扫描 {list(DET_CORE)}：LLM/网络标记命中 {len(core_hits)} 处 {core_hits}",
        "反例：在 loop.py 里 from ..llm import LLMClient → 命中非空 → 变红",
    )

    top_level_hits = [f"{n}:{i}" for n in NARRATIVE for i, ln in _marker_lines(n)
                      if ln[:1] not in ("", " ", "\t")]
    rep.check(
        "E1-NARR-LAZY", "叙事层的 LLM 引用只允许出现在函数体内（模块顶层导入会让引擎启动即依赖 LLM）",
        not top_level_hits,
        f"扫描 {list(NARRATIVE)} 的**顶格** LLM 标记：命中 {len(top_level_hits)} 处 {top_level_hits}",
        "反例：在 session.py 顶格写 from ..llm import LLMClient → 命中非空 → 变红",
    )

    # AST：叙事层把模型提案交给引擎时必须**显式** trust="llm"（否则会继承 Run.apply 的 engine 默认）
    trust_hits = []
    sess = os.path.join(coc_dir, "session.py")
    if os.path.isfile(sess):
        with open(sess, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=sess)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "apply"):
                kw = {k.arg: k for k in node.keywords}
                t_node = kw.get("trust")
                got = (t_node.value.value if isinstance(t_node, ast.keyword)
                       and isinstance(t_node.value, ast.Constant) else None)
                trust_hits.append(got)
    rep.check(
        "E1-LLM-TRUST", "叙事层把 LLM 提案交给 run.apply 时显式 trust=\"llm\"（不得继承 engine 默认）",
        bool(trust_hits) and all(x == "llm" for x in trust_hits),
        f"session.py 里 .apply() 调用的 trust 实参：{trust_hits}（全部应为 'llm'）",
        "反例：写成 run.apply(delta, reason=...)（不传 trust）→ 继承 engine 默认 → 变红",
    )

    return rep


# --------------------------------------------------------------- 主入口

SCENARIOS = {
    "main": scenario_a1_a2_a3_a4_a6_a7_a8,
    "a5": scenario_a5,
    "sanitize": scenario_sanitize,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CoC 模式离线验收探针（无 LLM、无网络）")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default=None,
                    help="只跑指定场景（默认全部）")
    ap.add_argument("--json", action="store_true", help="输出机读 JSON")
    args = ap.parse_args(argv)

    # 证明离线：本进程被禁网也不会失败；这里显式打印环境事实
    net_env = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY") if k in os.environ}
    started = _dt.datetime.now().isoformat(timespec="seconds")
    print("=" * 78)
    print("CoC 模式离线验收探针（coc_cli_probe）")
    print(f"  时间        : {started}")
    print(f"  Python      : {sys.version.split()[0]}")
    print(f"  项目根      : {_ROOT}")
    print("  LLM 调用    : 0（本文件不导入任何 LLM/网络客户端）")
    print(f"  代理环境变量: {net_env or '（无）'}")
    print("=" * 78)

    names = [args.scenario] if args.scenario else sorted(SCENARIOS)
    reports: list[Report] = []
    extra: dict[str, Any] = {}
    for name in names:
        print(f"\n--- 场景 {name} " + "-" * (60 - len(name)))
        fn = SCENARIOS[name]
        out = fn()
        if isinstance(out, tuple):
            rep, blob = out
            extra[name] = blob
        else:
            rep = out
        reports.append(rep)

    all_items = [x for r in reports for x in r.items]
    failed = [x for x in all_items if not x["ok"]]
    print("\n" + "=" * 78)
    print(f"合计断言 {len(all_items)} 条：PASS {len(all_items) - len(failed)}，FAIL {len(failed)}")
    for x in failed:
        print(f"  FAIL {x['id']} {x['desc']}")
    print("=" * 78)

    if args.json:
        print(json.dumps({
            "started": started,
            "assertions": all_items,
            "failed": [x["id"] for x in failed],
            "ok": not failed,
        }, ensure_ascii=False, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

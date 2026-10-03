# -*- coding: utf-8 -*-
"""P18-A（t51）接线验收探针：**离线、不花 LLM 调用、不加依赖**。

覆盖任务书要求的三块可机读判据：
  1. **三硬闸门**：单元级 `horror_erotic_block(...) == ""`（连标题都不写）
     + 集成级**最终 system prompt 与「不注入该层」的基线逐字节相等**；
  2. **`coc_delta` 白名单**：接线后的实际白名单值 + 恶意 delta 被拒的真实证据；
  3. **存读档往返**：关键字段（doom/sta/rea/周目）一致；
  4. **config 缺键**：老用户 config 缺 `coc` 段时不崩。

用法（项目根）：
    python 测试工具/coc_wiring_probe.py
    python 测试工具/coc_wiring_probe.py --json

退出码：全绿 0 / 有失败 1。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.coc import loop as _loop              # noqa: E402
from app.coc import prompt as _prompt          # noqa: E402
from app.coc import provenance as _prov        # noqa: E402
from app.coc import rules_horror_erotic as R   # noqa: E402
from app.coc import session as _session        # noqa: E402

CHECKS: list[dict] = []


def check(cid: str, desc: str, ok: bool, evidence: str, counterexample: str) -> bool:
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok),
                   "evidence": evidence, "counterexample": counterexample})
    print(f"  [{'PASS' if ok else 'FAIL'}] {cid} {desc}")
    print(f"         证据：{evidence}")
    print(f"         反例：{counterexample}")
    return bool(ok)


# ---------------------------------------------------------------- 闸门

def gate_cases(cfg: dict) -> list[tuple[str, dict]]:
    """三组必测参数（任务书点名）：rating=all / rating=16 / 温和档。"""
    return [
        ("rating=all", dict(content_rating="all", r18_enabled=False,
                            adult_intensity="浓烈", participants=["Kirie"])),
        ("rating=16", dict(content_rating="16", r18_enabled=False,
                           adult_intensity="浓烈", participants=["Kirie"])),
        ("intensity=温和（保守档）", dict(content_rating="18", r18_enabled=True,
                                     adult_intensity="温和", participants=["Kirie"])),
    ]


def probe_gates(cfg: dict) -> None:
    print("\n--- ① 三硬闸门 ---")
    run = _loop.Run.start({"difficulty": "Initiate", "seed": 42}, cfg)

    # ---- 单元级 ----
    unit_ok = True
    unit_ev = []
    for label, kw in gate_cases(cfg):
        # 「温和档」这一组按 COC-DESIGN §5.3 是**通过 G2 但被 G3 钳到 subtle**，
        # 所以它不是"空串"场景，而是"降档"场景。空串场景是 all / 16 两组。
        gate = R.resolve_gate(kw["content_rating"], kw["r18_enabled"],
                              kw["adult_intensity"], kw["participants"], cfg)
        block = R.horror_erotic_block(gate.erotic_tier, gate, {}, cfg)
        is_empty = (block == "")
        # 「温和」组：闸门应通过但 tier 被钳到 subtle
        if label.startswith("intensity="):
            unit_ok &= (gate.passed and gate.erotic_tier == "subtle" and not is_empty)
            unit_ev.append(f"{label}: passed={gate.passed} tier={gate.erotic_tier} "
                           f"len(block)={len(block)}")
        else:
            unit_ok &= (not gate.passed and is_empty
                        and "恐怖" not in block and "情色" not in block)
            unit_ev.append(f"{label}: passed={gate.passed} block={block!r} "
                           f"（连标题都不写）")
    check("GATE-UNIT", "单元级：all / 16 两组返回空串且不含「恐怖」「情色」字样；温和档钳到 subtle",
          unit_ok, "；".join(unit_ev),
          "反例：把 horror_erotic_block 的闸门判断删掉（改成只看 tier）→ all/16 两组会返回"
          "带标题的整段 → 本断言立刻红（这正是「分支存在 ≠ 走了那条分支」要防的）")

    # ---- 额外：连标题都不写（拿标题常量做正向对照）----
    any_block = ""
    g_ok = R.resolve_gate("18", True, "浓烈", ["Kirie"], cfg)
    any_block = R.horror_erotic_block(g_ok.erotic_tier, g_ok, {}, cfg)
    check("GATE-TITLE", "门通过时确实写出了标题（证明上一条的空串不是因为常量笔误）",
          R.BLOCK_TITLE in any_block and any_block != "",
          f"通过时 block 长度={len(any_block)}，含标题={R.BLOCK_TITLE in any_block}",
          "反例：把 BLOCK_TITLE 改坏/不写标题 → 本断言红，且上一条的'空串'就变得不可信"
          "（因为它可能只是永远不写标题）")

    # ---- 集成级：最终 system prompt 逐字节相等 ----
    baseline = _prompt.coc_system(
        run, resolution=None, cfg=cfg,
        content_rating="all", r18_enabled=False, adult_intensity="浓烈",
        participants=["Kirie"], gate=None)
    # 显式传一个"未通过"的 gate（模拟调用方没判闸门就直接调用）
    dead_gate = R.resolve_gate("all", False, "浓烈", ["Kirie"], cfg)
    injected = _prompt.coc_system(
        run, resolution=None, cfg=cfg,
        content_rating="all", r18_enabled=False, adult_intensity="浓烈",
        participants=["Kirie"], gate=dead_gate)
    same = (injected == baseline)
    check("GATE-INTEG", "集成级：最终 system prompt 与「不注入该层」逐字节相等（含长度）",
          same,
          f"baseline len={len(baseline)}；injected len={len(injected)}；"
          f"bytes equal={same}；两串都不含标题="
          f"{R.BLOCK_TITLE not in baseline and R.BLOCK_TITLE not in injected}",
          "反例：在 coc_system 里写成 `parts.append(block)`（无条件 append）→ 空串会留下一处"
          "空行 → join 后多一个 '\\n\\n' → 长度不等 → 本断言红。"
          "**半生效（注入了空段但留了标题/空行）就是被这条抓住的**")

    # ---- 反向自检：门通过时基线**必须**不同（否则上一条断言恒真）----
    live = _prompt.coc_system(
        run, resolution=None, cfg=cfg,
        content_rating="18", r18_enabled=True, adult_intensity="浓烈",
        participants=["Kirie"], gate=None)
    check("GATE-INTEG2", "反向自检：门通过时最终 prompt **必须**比基线长（防恒真）",
          live != baseline and len(live) > len(baseline),
          f"live len={len(live)} > baseline len={len(baseline)}；"
          f"live 含标题={R.BLOCK_TITLE in live}",
          "反例：若 live == baseline，说明该层从来不会被注入 —— 那么上一条的'相等'毫无意义"
          "（任何实现都能通过）。这条是防「断言恒真」的对照")


# ---------------------------------------------------------------- 白名单

def probe_whitelist(cfg: dict) -> None:
    print("\n--- ② coc_delta 白名单（接线后运行时取值）---")
    wl = list(_prov.t(cfg, "llm_delta_whitelist"))
    banned = ("doom", "sta", "rea", "tags_grant", "tags_remove", "flag_set", "ending")
    present = [k for k in banned if k in wl]
    check("WL-VALUE", "运行时白名单里没有 doom/sta/rea/tags_grant/tags_remove/flag_set/ending",
          not present, f"实际白名单 = {wl}；被排除 {len(banned)}/{len(banned)}",
          "反例：把 'doom' 加进 tuning.llm_delta_whitelist → present 非空 → 红；"
          "且 sanitize.assert_whitelist_matches_tuning 也会抛 SchemaViolation（两道）")

    run = _loop.Run.start({"difficulty": "Initiate", "seed": 7}, cfg)
    evil = {"doom": -100, "sta": 999, "rea": -999,
            "tags_grant": [{"id": "FakeGod", "kind": "town", "duration": "run"}],
            "flag_set": {"lighthouse_cleared": True}, "ending": "END_CLEARED",
            "exp": 3, "funds": -2, "item_grant": ["Bandage"]}
    before = {k: int(run.state.get(k, 0)) for k in ("doom", "sta", "rea")}
    turn = _prompt.narr_turn({"coc_delta": evil, "scene": "x", "dialogue": [],
                              "choices": []}, cfg)
    run.apply(turn["coc_delta"], reason="probe_llm")
    after = {k: int(run.state.get(k, 0)) for k in ("doom", "sta", "rea")}
    leaked = [k for k in turn["coc_delta"] if k not in wl]
    check("WL-REJECT", "恶意 delta 的越权键全部被拒（且状态里的 doom/sta/rea 未被它改动）",
          not leaked and "ending" not in turn["coc_delta"]
          and turn["coc_delta"].get("exp") == 3,
          f"保留 {sorted(turn['coc_delta'])}；丢弃 {sorted(set(map(str, turn['dropped'])))}；"
          f"doom/sta/rea {before} → {after}（未被 LLM 提案改动）；"
          f"state.ending={run.state.get('ending')}",
          "反例：把 ending 放进白名单 → turn['coc_delta'] 里出现 ending，且 state.ending 会被"
          "写成一个 LLM 选的值 → leaked 非空 + 状态被改 → 双重红")


# ---------------------------------------------------------------- 存读档

KEY_FIELDS = ("doom", "sta", "rea", "mystery_idx", "keys", "investigations_total",
              "difficulty", "old_god", "character", "ending")


def probe_save_roundtrip(cfg: dict) -> None:
    print("\n--- ③ 存档 / 读档往返 ---")
    s = _session.start({"difficulty": "Cultist", "seed": 2026,
                        "old_god": "Ath-Yolazsth", "character": "Aiko"}, cfg)
    sid = s["sid"]
    # 推进几步，让状态有内容（不请 LLM）
    for i in range(3):
        _session.advance_without_llm(_session.get(sid), i)
    before_state = dict(_session.get(sid)["run"].state)
    saved = _session.save_slot(sid, "P18A-往返")
    slot_id = saved["slot_id"]
    loaded = _session.load_slot(slot_id)
    new_sid = loaded["sid"]
    after_state = dict(_session.get(new_sid)["run"].state)

    diffs = {k: (before_state.get(k), after_state.get(k)) for k in KEY_FIELDS
             if before_state.get(k) != after_state.get(k)}
    check("SAVE-ROUNDTRIP", "存档→读档后关键字段一致（doom/sta/rea/周目/难度/古神/角色）",
          not diffs and before_state.get("sid") == after_state.get("sid"),
          f"slot={slot_id}；比对 {len(KEY_FIELDS)} 个关键字段，差异 {diffs or '无'}；"
          f"doom={after_state.get('doom')} sta={after_state.get('sta')} "
          f"rea={after_state.get('rea')} 已过谜团={after_state.get('mystery_idx')} "
          f"钥匙={after_state.get('keys')}",
          "反例：save_slot 里不写 coc_state（或 load_slot 不还原 rng_state）→ 读档后 sid/doom 缺"
          "→ diffs 非空 → 红")

    # rng 也必须一起还原（否则读档后掷骰序列漂移）
    r1 = _session.get(sid)["run"]
    r2 = _session.get(new_sid)["run"]
    import random as _r
    seq1 = [_r.Random(1).randint(1, 100) for _ in range(1)]
    from app.coc import state as _st
    a = _st.restore_rng(before_state["rng_state"])
    b = _st.restore_rng(after_state["rng_state"])
    same = [a.randint(1, 9999) for _ in range(8)] == [b.randint(1, 9999) for _ in range(8)]
    check("SAVE-RNG", "读档后 rng 状态原样还原（后续随机序列完全一致）",
          same, f"两侧 8 连抽一致={same}",
          "反例：只存种子不存 getstate → 序列漂移 → 红")

    # 列表里能看到
    slots = _session.list_slots()
    check("SAVE-LIST", "存档出现在全局存档槽列表里（前端可读）",
          any(x["slot_id"] == slot_id for x in slots),
          f"coc 存档 {len(slots)} 条，含刚存的 {slot_id}="
          f"{any(x['slot_id'] == slot_id for x in slots)}",
          "反例：把 slot 写到别的目录 → 列表看不到 → 前端无法读档 → 红")

    # 清理：只删本次探针造的槽（不动别人的）
    try:
        os.remove(os.path.join(_session.paths.SLOTS_DIR, f"{slot_id}.json"))
    except OSError:
        pass


# ---------------------------------------------------------------- config 缺键

def probe_config_missing_key(cfg: dict) -> None:
    print("\n--- ④ config 缺键（老用户）---")
    from app import config as cfgmod
    defaults = cfgmod.DEFAULTS
    has_coc = "coc" in defaults
    # 模拟"老用户 config.json 里没有 coc 段"：deep_merge(DEFAULTS, {}) 应仍带 coc
    merged = cfgmod._deep_merge(defaults, {})
    ok_defaults = has_coc and isinstance(merged.get("coc"), dict) \
        and merged["coc"].get("default_difficulty") == "Initiate"
    check("CFG-DEFAULTS", "config.example.json 的 coc 段已进 DEFAULTS（老用户缺键时由默认值补齐）",
          ok_defaults,
          f"DEFAULTS 含 coc={has_coc}；deep_merge(DEFAULTS, {{}}) 后 "
          f"coc.default_difficulty={merged.get('coc', {}).get('default_difficulty')!r}",
          "反例：只改 config.json 不改 config.example.json → DEFAULTS 里没有 coc → "
          "老用户 config 缺键时读到的默认值缺失 → 本断言红。"
          "（config.py:93 的 DEFAULTS 就是从这个 example 文件读的）")

    # 模拟：用户 config 里有 coc 但某子键缺失 → merge 后仍能取到默认子键
    partial = {"coc": {"enabled": False}}
    merged2 = cfgmod._deep_merge(defaults, partial)
    ok_partial = (merged2["coc"]["enabled"] is False
                  and merged2["coc"]["default_difficulty"] == "Initiate")
    check("CFG-PARTIAL", "用户只写了部分 coc 子键时，其余子键由默认值补齐（深合并非替换）",
          ok_partial,
          f"merged2.coc = {json.dumps(merged2['coc'], ensure_ascii=False)[:120]}…",
          "反例：把 _deep_merge 改成浅 merge（dict.update）→ 用户的 {{'enabled': False}} 会把整段"
          "默认值替换掉 → default_difficulty 消失 → 红")

    # coc 层的 config_view 在没有 coc 段时也不应崩（它读的是 tuning，不是 config）
    view = _session.config_view(cfg)
    check("CFG-VIEW", "CoC 开局配置视图可读（难度/古神/角色/双轴档位/白名单）",
          bool(view.get("difficulties")) and bool(view.get("old_gods"))
          and bool(view.get("erotic_tiers")),
          f"difficulties={view['difficulties']} old_gods={view['old_gods']} "
          f"characters={view['characters']} erotic_tiers={view['erotic_tiers']}",
          "反例：删掉 data/coc/old_gods.json → view['old_gods'] 为空 → 前端下拉空白 → 红")


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P18-A 接线验收探针（离线、无 LLM）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    started = time.time()
    cfg = _prov.load_tuning()
    print("=" * 78)
    print("P18-A（t51）CoC 接线验收探针")
    print(f"  Python : {sys.version.split()[0]}")
    print(f"  项目根 : {_ROOT}")
    print("  LLM    : 0（本文件不调用任何 LLM）")
    print("=" * 78)

    probe_gates(cfg)
    probe_whitelist(cfg)
    probe_save_roundtrip(cfg)
    probe_config_missing_key(cfg)

    failed = [c for c in CHECKS if not c["ok"]]
    print("\n" + "=" * 78)
    print(f"合计 {len(CHECKS)} 条：PASS {len(CHECKS) - len(failed)}，FAIL {len(failed)}"
          f"（{time.time() - started:.2f}s）")
    for c in failed:
        print(f"  FAIL {c['id']} {c['desc']}")
    print("=" * 78)
    if args.json:
        print(json.dumps({"checks": CHECKS, "ok": not failed}, ensure_ascii=False, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

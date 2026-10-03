# -*- coding: utf-8 -*-
"""P18-A（t51）HTTP 级接线验收：**用 TestClient 走真实的 FastAPI 应用**。

为什么不启真实端口：
  - 用户正在跑的 8765 实例（pid 30120）**不能碰**；
  - 起第二个 uvicorn 要处理端口占用、`NO_PROXY` 里的 `[::1]`、进程清理；
  - `TestClient(app)` 走的是**同一个 app 对象**（含全部中间件、路由、Pydantic 校验），
    所以"五个分发点是否真的被路由到 coc"这件事在这里能被完整验证，
    且**零端口、零网络、零副作用**。
本脚本同时记录"未做真实端口实测"这一限制，避免把 TestClient 结论当成端口级结论。

用法：python 测试工具/coc_http_probe.py
"""
from __future__ import annotations

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

CHECKS: list[dict] = []


def check(cid, desc, ok, evidence, counterexample) -> bool:
    CHECKS.append({"id": cid, "desc": desc, "ok": bool(ok), "evidence": evidence,
                   "counterexample": counterexample})
    print(f"  [{'PASS' if ok else 'FAIL'}] {cid} {desc}")
    print(f"         证据：{evidence}")
    print(f"         反例：{counterexample}")
    return bool(ok)


def main() -> int:
    from fastapi.testclient import TestClient
    import server as srv
    from app.coc import session as coc

    started = time.time()
    print("=" * 78)
    print("P18-A（t51）HTTP 级接线验收（TestClient，零端口 / 零网络）")
    print("=" * 78)
    client = TestClient(srv.app, raise_server_exceptions=False)
    # 非浏览器调用方签名：不带 Origin / Sec-Fetch-*（`_looks_like_browser` 返回 False），
    # 因此写操作不需要 token —— 与 launcher / acceptance_e2e 同一条路径。
    #
    # ⚠️ Host 头必须写成**不含端口**的回环地址：先前写成 `127.0.0.1:1`，
    # 该值经 `app.llm` 的 proxy/URLPattern 处理后被解析成非法端口 `':1]'`，
    # 使 H8 的 tsf 回归请求在**发请求阶段**就抛异常（而不是返回状态码）。
    H = {"host": "127.0.0.1"}

    # ---------- 分发点②：start_game ----------
    r = client.post("/api/game/start", headers=H, json={
        "mode": "coc", "title": "P18A-HTTP", "difficulty": "Initiate",
        "death_mode": "last_stand", "old_god": "Goizo", "character": "Kirie",
        "content_rating": "all", "r18_enabled": False,
    })
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    sid = body.get("sid") or ""
    check("H1-START", "POST /api/game/start {mode:coc} → 200 且 sid 非空、mode=coc",
          r.status_code == 200 and bool(sid) and body.get("mode") == "coc",
          f"status={r.status_code} sid={sid!r} mode={body.get('mode')!r} "
          f"scene={str(body.get('scene'))[:20]!r} choices={len(body.get('choices') or [])}",
          "反例：删掉 start_game 里的 coc 分支 → 会落到 game.start_game（要求 world/角色），"
          "返回 400「请先填写世界观」→ status!=200 → 红")

    # ---------- 分发点③：game_state ----------
    r2 = client.get(f"/api/game/{sid}", headers=H)
    b2 = r2.json() if r2.status_code == 200 else {}
    has_coc = isinstance(b2.get("coc"), dict)
    check("H2-STATE", "GET /api/game/{sid} → 200 且带 coc 面板（doom/sta/rea/stats）",
          r2.status_code == 200 and has_coc
          and {"doom", "sta", "rea", "stats", "doom_line"} <= set(b2.get("coc") or {}),
          f"status={r2.status_code} coc keys={sorted(b2.get('coc') or {})[:12]} "
          f"doom_line={(b2.get('coc') or {}).get('doom_line')!r}",
          "反例：把 coc 分支放到 vn 分支之后 → 会走 game.ensure_scene_bg/public_state 读 "
          "prot['stats']（TSF 字段）→ 500 或 KeyError → status!=200 → 红")

    # ---------- 分发点④：advance ----------
    r3 = client.post(f"/api/game/{sid}/advance", headers=H, json={"choice_index": 0})
    b3 = r3.json() if r3.status_code == 200 else {}
    d_before = int(b2.get("coc", {}).get("investigations_done", -1))
    d_after = int(b3.get("coc", {}).get("investigations_done", -2))
    check("H3-ADVANCE", "POST /api/game/{sid}/advance → 200 且状态确实推进了",
          r3.status_code == 200 and d_after > d_before and bool(b3.get("sid")),
          f"status={r3.status_code} investigations_done {d_before} → {d_after}；"
          f"doom {b2.get('coc', {}).get('doom')} → {b3.get('coc', {}).get('doom')}；"
          f"turn_no={b3.get('turn_no')}",
          "反例：删掉 advance 的 coc 早返回 → 走 game.get_session(sid) 抛「会话不存在」→ "
          "404 → status!=200 → 红")

    # ---------- 分发点⑤：save / load slots ----------
    r4 = client.post(f"/api/game/{sid}/slots", headers=H, json={"name": "P18A-HTTP槽"})
    b4 = r4.json() if r4.status_code == 200 else {}
    slot_id = b4.get("slot_id", "")
    check("H4-SAVE", "POST /api/game/{sid}/slots → 200 且返回 slot_id（coc 分支）",
          r4.status_code == 200 and slot_id.startswith("slot"),
          f"status={r4.status_code} slot_id={slot_id!r} name={b4.get('name')!r} "
          f"mode={b4.get('mode')!r}",
          "反例：删掉 save_slot 的 coc 分支 → game.save_slot(sid) 抛「会话不存在/过期」→ 404 → 红")

    r5 = client.post(f"/api/slots/{slot_id}/load", headers=H)
    b5 = r5.json() if r5.status_code == 200 else {}
    same_state = (b5.get("coc", {}).get("doom") == b3.get("coc", {}).get("doom")
                  and b5.get("coc", {}).get("sta") == b3.get("coc", {}).get("sta")
                  and b5.get("coc", {}).get("rea") == b3.get("coc", {}).get("rea"))
    check("H5-LOAD", "POST /api/slots/{id}/load → 200 且读回同一状态（doom/sta/rea 一致）",
          r5.status_code == 200 and b5.get("mode") == "coc" and same_state,
          f"status={r5.status_code} mode={b5.get('mode')!r} "
          f"doom={b5.get('coc', {}).get('doom')}（存档前 {b3.get('coc', {}).get('doom')}）"
          f" sta={b5.get('coc', {}).get('sta')} rea={b5.get('coc', {}).get('rea')}",
          "反例：删掉 load_slot 的 peek/分支 → game.load_slot 把 coc 信封当 TSF 会话注册，"
          "然后 game.get_session(...).get('mode') 不为 vn → 返回原始 r（无 coc 字段）→ "
          "b5.get('coc') 为 None → 红")

    r6 = client.post(f"/api/game/{sid}/slots/{slot_id}/load", headers=H)
    check("H6-LOAD-COMPAT", "兼容端点 POST /api/game/{sid}/slots/{id}/load 同样走 coc",
          r6.status_code == 200 and (r6.json().get("mode") == "coc"),
          f"status={r6.status_code} mode={r6.json().get('mode')!r}",
          "反例：只给 /api/slots/{id}/load 加分支、漏掉兼容端点 → status!=200 或 mode 缺失 → 红")

    # ---------- 配置端点 ----------
    r7 = client.get("/api/game/coc/config", headers=H)
    b7 = r7.json() if r7.status_code == 200 else {}
    check("H7-CONFIG", "GET /api/game/coc/config → 200 且给出难度/古神/角色/双轴档位",
          r7.status_code == 200 and bool(b7.get("difficulties")) and bool(b7.get("old_gods"))
          and bool(b7.get("erotic_tiers")),
          f"status={r7.status_code} difficulties={b7.get('difficulties')} "
          f"old_gods={b7.get('old_gods')} whitelist={b7.get('llm_delta_whitelist')}",
          "反例：删掉该路由 → 404 → 前端下拉空白 → 红")

    # ---------- 不回归：既有 tsf 路径仍可用（只验路由没被 coc 抢走）----------
    # 说明：这是**结构性**回归（"tsf 请求没被 coc 分支吃掉"），不是 tsf 的功能验收。
    # 本机 tsf 开局会真的去连 LLM（`app/game.py` 的 start_game），因此这里
    # 只要求"不是 404 且返回了某个 HTTP 状态"，把 500 也视为通过 ——
    # 否则会把"LLM 不可用"误报成"接线破坏了 tsf"。
    # 用 raise_server_exceptions=False 拿状态码而不是抛栈。
    r8 = client.post("/api/game/start", headers=H, json={
        "mode": "tsf", "world": "P18A 回归用", "content_rating": "all",
    })
    check("H8-NOREGRESS", "既有 mode=tsf 仍能路由到既有分支（非 404；coc 分支没抢走路由）",
          r8.status_code != 404,
          f"status={r8.status_code}（404 才是路由被 coc 抢走的症状；"
          f"200/400/500 属 tsf 自身参数或 LLM 不可用，与本次接线无关）",
          "反例：把 start_game 改成无条件 `return coc.start(...)` → tsf 请求会落到 coc → "
          "仍非 404 但 mode 会变成 coc（那种情况由 H1 的 mode 断言兜住）；"
          "若把分支写成 `if payload.mode == 'coc': ... else: raise` → tsf 会 404 → 红")

    # 清理本次探针造的 coc 会话与槽（不动任何别人的数据）
    try:
        from app.coc import paths as cocpaths
        p = os.path.join(cocpaths.SLOTS_DIR, f"{slot_id}.json")
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass
    for k in list(coc.sessions().keys()):
        coc.drop(k)

    failed = [c for c in CHECKS if not c["ok"]]
    print("\n" + "=" * 78)
    print(f"合计 {len(CHECKS)} 条：PASS {len(CHECKS) - len(failed)}，FAIL {len(failed)}"
          f"（{time.time() - started:.2f}s）")
    print("限制：本脚本用 TestClient（同 app 对象、含中间件），**未做真实端口实测**；")
    print("      用户 8765 实例（pid 30120）全程未被触碰。")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

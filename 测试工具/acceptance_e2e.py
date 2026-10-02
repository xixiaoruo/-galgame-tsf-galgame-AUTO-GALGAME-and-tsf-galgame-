#!/usr/bin/env python3
"""L4 端到端交付验收脚本（审计 modernize-plan 4.5）。

一条命令验证「能开机 → 能玩 → 能换装 → 能出 CG → 能存档 → 能读档 → 能退出」：

    ① 启动（轮询 /api/version，记录冷启动耗时）
    ② 开局（TSF 模式 + 普通模式 vn 各一次；sid 非空 + /api/game/{sid} 200）
    ③ 推进剧情（≥2 幕；history 条目与 turn 增长）
    ④ 资源就绪（pending 归零；ready/error 如实记录，不允许永久 pending）
    ⑤ 换装　⑥ 重画　⑦ CG（截场景或重绘其一）
    ⑧ 存档（slot 文件存在 + JSON 合法）
    ⑨ 读档（关键字段与存档时一致 + 资源可解析）
    ⑩ 优雅关闭（退出耗时 + 无残留进程 + debug.log 里的持久化摘要）

用法::

    python 测试工具/acceptance_e2e.py                     # 默认样本 + 自动选端口
    python 测试工具/acceptance_e2e.py --sample <dir>       # 指定隔离样本根
    python 测试工具/acceptance_e2e.py --port 8791          # 指定端口（8765 被占用则自动顺延）
    python 测试工具/acceptance_e2e.py --json <file>        # 指定结果 JSON 输出路径
    （内部自起用）python 测试工具/acceptance_e2e.py --serve <sample> --port N

退出码：0=全部通过；1=有失败项；2=环境/启动失败；3=隔离哨兵触发（拒绝启动）

隔离（沿用 t10/t16/t30 的成熟做法）：
  · 覆盖 `app.config` 的六个路径常量（DATA_DIR/CACHE_DIR/SAVES_DIR/CG_DIR/CG_META_PATH/CONFIG_PATH）
  · 哨兵断言：任何一处落在 `D:\\TSF_Galgame_Data` 内 → `[FATAL]` 且退出码 3
  · LLM 走 mock（样本 config 无 api_key）；SD 走**本进程内的假 SD**（127.0.0.1 回环，不联网、不占 GPU、不碰 7860）
  · 起服务用真实 `server.py` 主流程（含启动序列 1→8）；只把监听端口从 8765 重映射到自选端口
本脚本**只新增自己**，不修改 app/、server.py、static/ 的任何文件。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
REAL_DATA_ROOT = Path("D:/TSF_Galgame_Data").resolve()
DEFAULT_SAMPLE = HERE / "e2e-sample"
PAYLOAD_VERSION = "1.0"

#: 关掉 uvicorn 的访问日志噪音（只影响本脚本起的实例）
os.environ.setdefault("PYTHONUTF8", "1")
#: 控制台可能是 GBK：中文 + 状态标记必须能安全输出，否则摘要会崩在打印上
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 隔离：覆盖六常量 + 哨兵
# --------------------------------------------------------------------------- #
def guard_sample(sample: Path) -> None:
    """哨兵：样本根本身也不许落在真实数据目录内（在建任何目录/文件**之前**判）。

    否则「样本=T 真实数据目录」会让 isolate() 先 mkdir 再报错 —— 那就已经写进去了。
    """
    rp = Path(sample).resolve()
    if rp == REAL_DATA_ROOT or REAL_DATA_ROOT in rp.parents:
        print(f"[FATAL] 隔离哨兵触发：样本根 {rp} 落在真实数据目录 "
              f"{REAL_DATA_ROOT} 内 —— 拒绝启动、未创建任何文件", flush=True)
        raise SystemExit(3)


def isolate(sample: Path) -> dict:
    """把路径常量改指样本；返回诊断信息。哨兵失败直接 SystemExit(3)。

    ⚠️ 顺序很关键：**先算路径 + 先过哨兵，再 mkdir**。反过来的话，一旦常量
    指向真实数据目录，脚本会先在真实目录里建出 cache/saves/... 再报错。
    """
    guard_sample(sample)
    sys.path.insert(0, str(REPO))
    from app import config as cfgmod

    data = sample / "data"
    cfgmod.DATA_DIR = data
    cfgmod.CACHE_DIR = data / "cache"
    cfgmod.SAVES_DIR = data / "saves"
    cfgmod.CG_DIR = data / "cg"
    cfgmod.CG_META_PATH = cfgmod.CG_DIR / "meta.json"
    cfgmod.CONFIG_PATH = sample / "config.json"

    info = {}
    for name in ("DATA_DIR", "CACHE_DIR", "SAVES_DIR", "CG_DIR",
                 "CG_META_PATH", "CONFIG_PATH"):
        p = Path(getattr(cfgmod, name)).resolve()
        info[name] = str(p)
        if p == REAL_DATA_ROOT or REAL_DATA_ROOT in p.parents:
            print(f"[FATAL] 隔离哨兵触发：{name}={p} 落在真实数据目录 "
                  f"{REAL_DATA_ROOT} 内 —— 拒绝启动、未创建任何文件", flush=True)
            raise SystemExit(3)

    for sub in ("cache", "saves", "slots", "cg"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    (sample / "logs").mkdir(parents=True, exist_ok=True)
    return info


# --------------------------------------------------------------------------- #
# 假 SD（本进程内、回环、无 GPU）：让 ④⑤⑥ 走**真实 HTTP 生图链路**
# --------------------------------------------------------------------------- #
def _draw_portrait(w: int = 768, h: int = 1152) -> bytes:
    """合成立绘：中性深灰衣物（避开紫/绿/肤色三条质检判据）+ 肤色头脸。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (w, h), (236, 236, 238))
    d = ImageDraw.Draw(im)
    cx = w // 2
    d.polygon([(cx - int(w * 0.26), int(h * 0.30)), (cx + int(w * 0.26), int(h * 0.30)),
               (cx + int(w * 0.30), int(h * 0.96)), (cx - int(w * 0.30), int(h * 0.96))],
              fill=(90, 90, 95))
    d.rectangle([cx - int(w * 0.33), int(h * 0.34),
                 cx - int(w * 0.27), int(h * 0.70)], fill=(232, 190, 170))
    d.rectangle([cx + int(w * 0.27), int(h * 0.34),
                 cx + int(w * 0.33), int(h * 0.70)], fill=(232, 190, 170))
    d.ellipse([cx - int(w * 0.20), int(h * 0.06),
               cx + int(w * 0.20), int(h * 0.32)], fill=(64, 52, 58))
    d.ellipse([cx - int(w * 0.14), int(h * 0.10),
               cx + int(w * 0.14), int(h * 0.28)], fill=(233, 191, 172))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class FakeSD:
    """极简 A1111 兼容桩：/sdapi/v1/{options,samplers,upscalers,schedulers,progress}
    + POST txt2img/img2img + interrogate。只监听 127.0.0.1。"""

    def __init__(self, delay: float = 0.4):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.png = _draw_portrait()
        self.delay = delay
        self.calls: dict = {}
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _bump(self, key):
                with outer.lock:
                    outer.calls[key] = outer.calls.get(key, 0) + 1

            def do_GET(self):
                p = self.path.split("?")[0]
                self._bump("GET " + p)
                if p.endswith("/options"):
                    self._send({"sd_model_checkpoint": "e2e-fake.safetensors",
                                "sd_vae": "auto"})
                elif p.endswith("/samplers"):
                    self._send([{"name": "DPM++ 2M"}, {"name": "Euler a"}])
                elif p.endswith("/upscalers"):
                    self._send([{"name": "R-ESRGAN 4x+"}, {"name": "None"}])
                elif p.endswith("/schedulers"):
                    self._send([{"name": "Automatic"}, {"name": "Karras"}])
                elif p.endswith("/progress"):
                    self._send({"progress": 1.0, "state": "done"})
                else:
                    self._send({})

            def do_POST(self):
                p = self.path.split("?")[0]
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                self._bump("POST " + p)
                if outer.delay:
                    time.sleep(outer.delay)
                if p.endswith("/txt2img") or p.endswith("/img2img"):
                    self._send({"images": [base64.b64encode(outer.png).decode("ascii")],
                                "info": "{}", "parameters": {}})
                elif p.endswith("/interrogate"):
                    self._send({"caption": "1girl, solo, standing, dress"})
                else:
                    self._send({})

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, base: str, timeout: float = 90.0):
        self.base = base
        self.timeout = timeout

    def _req(self, method: str, path: str, body=None, timeout=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:              # 连接层错误
            return None, f"{type(e).__name__}: {e}"

    def get(self, path, timeout=None):
        return self._req("GET", path, None, timeout)

    def post(self, path, body=None, timeout=None):
        return self._req("POST", path, body if body is not None else {}, timeout)


def jload(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 结果收集：断言而非只记录
# --------------------------------------------------------------------------- #
class Stage:
    def __init__(self, sid_no: str, name: str):
        self.id = sid_no
        self.name = name
        self.t0 = time.time()
        self.status = "fail"
        self.expect = ""
        self.actual = ""
        self.reason = ""
        self.details: dict = {}
        self.notes: list = []

    def finish(self, ok: bool, expect="", actual="", reason="", details=None,
               skipped=False):
        self.expect = expect
        self.actual = actual
        self.reason = reason
        self.status = "skip" if skipped else ("pass" if ok else "fail")
        if details:
            self.details.update(details)
        return self.as_dict()

    def note(self, text: str):
        self.notes.append(text)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "status": self.status,
                "expect": self.expect, "actual": self.actual, "reason": self.reason,
                "duration_ms": int((time.time() - self.t0) * 1000),
                "details": self.details, "notes": self.notes}


def wait_assets_idle(cli: Client, sid: str, timeout=90.0, poll=0.5) -> dict:
    """等 pending 归零；返回最后的 tasks 视图（含 assets 计数与 queue）。"""
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        st, raw = cli.get(f"/api/game/{sid}/tasks")
        obj = jload(raw) or {}
        last = obj
        if st == 200 and (obj.get("assets") or {}).get("pending", 1) == 0:
            last["_waited_s"] = round(time.time() - t0, 2)
            return last
        time.sleep(poll)
    last["_waited_s"] = round(time.time() - t0, 2)
    last["_timeout"] = True
    return last


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def pick_port(preferred: int, tries=40) -> int:
    for p in range(preferred, preferred + tries):
        if port_free(p):
            return p
    raise EnvFailure(f"{preferred}~{preferred + tries - 1} 全部被占用：无法为验收实例找到端口")


class EnvFailure(Exception):
    """环境/启动类失败（不是被测功能失败）→ 退出码 2，但仍会写出结果 JSON。"""


# --------------------------------------------------------------------------- #
# --serve：真实 server.py 主流程 + 端口重映射（不改服务器源码）
# --------------------------------------------------------------------------- #
def run_serve(sample: Path, port: int) -> None:
    info = isolate(sample)
    # 端口重映射：server.py 主流程里硬编码 8765，这里在 bind 层把它换成本次自选端口，
    # 从而「8765 被他人占用也不动别人的进程」。除端口外，主流程一字不改。
    import socket as _socket
    _orig_bind = _socket.socket.bind

    def _bind(self, addr):
        if isinstance(addr, tuple) and len(addr) >= 2 and addr[1] == 8765:
            addr = (addr[0], port)
        return _orig_bind(self, addr)

    _socket.socket.bind = _bind

    out = (sample / "logs" / "e2e-server.out").open("a", encoding="utf-8", buffering=1)
    sys.stdout = out
    sys.stderr = out
    print(f"[E2E-SERVE] port={port}", flush=True)
    for k, v in info.items():
        print(f"[E2E-SERVE] {k}={v}", flush=True)

    import runpy
    os.chdir(str(REPO))
    runpy.run_path(str(REPO / "server.py"), run_name="__main__")
    print("[E2E-SERVE] server.py 返回（进程即将退出）", flush=True)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="L4 端到端交付验收")
    ap.add_argument("--sample", default=str(DEFAULT_SAMPLE))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--json", default="")
    ap.add_argument("--sd-delay", type=float, default=0.4)
    ap.add_argument("--serve", default="", metavar="SAMPLE",
                    help="内部使用：以隔离样本启动真实 server.py")
    args = ap.parse_args()

    if args.serve:
        run_serve(Path(args.serve).resolve(), args.port)
        return 0

    sample = Path(args.sample).resolve()

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    stages: list = []
    meta: dict = {"script": str(Path(__file__).resolve()), "payload_version": PAYLOAD_VERSION,
                  "started": started, "sample": str(sample), "mock": {}}
    fatal = None
    proc = None
    sd = None

    guard_sample(sample)          # 先哨兵（在任何 mkdir/写文件之前）
    sample.mkdir(parents=True, exist_ok=True)
    (sample / "logs").mkdir(parents=True, exist_ok=True)
    # ---- 假 SD + 样本配置（mock LLM / 回环 SD）----
    sd = FakeSD(delay=args.sd_delay)
    sd.start()
    meta["mock"] = {"llm": "mock（config 无 api_key）", "sd": f"127.0.0.1:{sd.port} 回环桩",
                    "fake_sd_endpoints": ["txt2img", "img2img", "interrogate", "options"]}
    cfg = {"image": {"provider": "sdwebui", "base_url": f"http://127.0.0.1:{sd.port}",
                     "api_key": "", "cutout_mode": "white",
                     "tsf_stage_matcher": False, "face_refine": False,
                     "outfit_inpaint": True, "hi_res": False,
                     "sync_webui": False, "auto_tune": False, "model_presets": []},
           "llm": {"api_key": ""}}
    (sample / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1),
                                        encoding="utf-8")

    port = pick_port(args.port)
    meta["port"] = port
    meta["port_note"] = ("使用请求端口" if port == args.port
                         else f"{args.port} 被占用 → 自动顺延到 {port}（不杀他人进程）")
    cli = Client(f"http://127.0.0.1:{port}")
    serve_log = (sample / "logs" / "e2e-serve-stderr.out").open("a", encoding="utf-8", buffering=1)
    env_failed = False

    try:
        # ---------------- ① 启动 ----------------
        st = Stage("①", "启动（等服务就绪，轮询 /api/version）")
        t0 = time.time()
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve", str(sample),
             "--port", str(port)],
            cwd=str(REPO), stdin=subprocess.DEVNULL,
            stdout=serve_log, stderr=serve_log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready = False
        ver = None
        last_err = ""
        while time.time() - t0 < 120:
            if proc.poll() is not None:
                last_err = f"进程提前退出（returncode={proc.returncode}）"
                break
            code, raw = cli.get("/api/version", timeout=3)
            if code == 200:
                ver = jload(raw) or {}
                ready = True
                break
            last_err = f"status={code} {raw[:80]}"
            time.sleep(0.25)
        cold_ms = int((time.time() - t0) * 1000)
        stages.append(st.finish(
            ready and bool(ver and ver.get("version")),
            expect="GET /api/version 返回 200 且含非空 version；进程存活",
            actual=f"status=200 version={ver.get('version') if ver else None} 冷启动 {cold_ms}ms",
            reason="" if ready else f"就绪轮询失败：{last_err}",
            details={"cold_start_ms": cold_ms, "version": (ver or {}).get("version"),
                     "pid": proc.pid}))
        if not ready:
            # 把服务端日志尾部带进结果，避免「退出码 2 但没有任何线索」
            tail = ""
            sl = sample / "logs" / "e2e-serve-stderr.out"
            if sl.is_file():
                tail = sl.read_text(encoding="utf-8", errors="replace")[-1200:]
            st.details["server_log_tail"] = tail
            raise EnvFailure(f"服务未在 120s 内就绪：{last_err}")

        # ---------------- ② 开局（双模式） ----------------
        st = Stage("②", "开局（TSF 模式 + 普通模式，各一次）")
        made = {}
        for mode, title, world in (("tsf", "E2E-TSF", "端到端验收用世界观（mock）"),
                                   ("vn", "E2E-VN", "端到端验收用世界观（mock）")):
            code, raw = cli.post("/api/game/start",
                                 {"mode": mode, "title": title, "world": world})
            obj = jload(raw) or {}
            sid = obj.get("sid") or obj.get("session_id") or ""
            state_ok, state_raw = (0, "")
            if sid:
                state_ok, state_raw = cli.get(f"/api/game/{sid}")
            made[mode] = {"start_status": code, "sid": sid, "state_status": state_ok,
                          "title": obj.get("title", title)}
        ok = all(m["sid"] and m["start_status"] == 200 and m["state_status"] == 200
                 for m in made.values())
        stages.append(st.finish(
            ok, expect="两种模式 POST /api/game/start → 200 且 sid 非空；GET /api/game/{sid} → 200",
            actual=json.dumps({k: {"start": v["start_status"], "state": v["state_status"],
                                   "sid": v["sid"][:12]} for k, v in made.items()},
                              ensure_ascii=False),
            reason="" if ok else "开局未通过：见 details",
            details={"sessions": made}))
        tsf_sid, vn_sid = made["tsf"]["sid"], made["vn"]["sid"]

        # ---------------- ③ 推进剧情（≥2 幕） ----------------
        st = Stage("③", "推进剧情（TSF 局推进 ≥2 幕，history/turn 增长）")
        def hist_len(sid):
            c, r = cli.get(f"/api/game/{sid}/history")
            o = jload(r) or {}
            h = o.get("history") if isinstance(o, dict) else None
            return len(h) if isinstance(h, list) else 0
        h0 = hist_len(tsf_sid)
        adv = []
        for i in range(2):
            code, raw = cli.post(f"/api/game/{tsf_sid}/advance", {"choice_index": 0})
            adv.append({"status": code, "body": raw[:120]})
            time.sleep(0.4)
        h1 = hist_len(tsf_sid)
        ok = all(a["status"] == 200 for a in adv) and h1 >= h0 + 2
        stages.append(st.finish(
            ok, expect="两次 POST /advance 均 200，且 history 条目 +≥2",
            actual=f"advance={[a['status'] for a in adv]} history {h0} → {h1}",
            reason="" if ok else "推进未达 2 幕或接口非 200",
            details={"advance": adv, "history_before": h0, "history_after": h1}))

        # ---------------- ④ 资源就绪 ----------------
        st = Stage("④", "资源就绪（立绘/背景 pending 归零）")
        tasks = wait_assets_idle(cli, tsf_sid, timeout=120)
        assets = tasks.get("assets") or {}
        pend = assets.get("pending", -1)
        ok = (pend == 0)
        degraded = (assets.get("error", 0) or 0) > 0
        stages.append(st.finish(
            ok, expect="pending == 0（允许 ready 或 error，绝不允许永久 pending）",
            actual=json.dumps({"assets": assets, "waited_s": tasks.get("_waited_s"),
                               "timeout": tasks.get("_timeout", False)}, ensure_ascii=False),
            reason="" if ok else f"资源未就绪：pending={pend}（超时）",
            details={"tasks": {k: v for k, v in tasks.items() if k != "sd"},
                     "degraded_path": degraded}))
        if degraded:
            stages[-1]["notes"].append(
                "本局存在 error 资产 ⇒ 该环节同时覆盖了「降级链」（详见报告 §降级路径）")
        stages[-1]["notes"].append("SD 走 127.0.0.1 假 SD：覆盖真实 HTTP 生图链路，但不是真模型出图")

        # ---------------- ⑤ 换装 ----------------
        st = Stage("⑤", "换装（资产进入 ready 或走完兜底链）")
        _, st_raw = cli.get(f"/api/game/{tsf_sid}")
        st_obj = jload(st_raw) or {}
        prot = st_obj.get("protagonist") or {}
        prot_name = prot.get("name") if isinstance(prot, dict) else str(prot or "")
        code, raw = cli.post(f"/api/game/{tsf_sid}/outfit",
                             {"character": prot_name, "outfit": "校服"})
        outfit_ok = code == 200
        t2 = wait_assets_idle(cli, tsf_sid, timeout=120)
        a2 = t2.get("assets") or {}
        ok = outfit_ok and a2.get("pending", -1) == 0
        stages.append(st.finish(
            ok, expect="POST /outfit → 200，且随后 pending 归零",
            actual=f"outfit_status={code} character={prot_name!r} "
                   f"assets={json.dumps(a2, ensure_ascii=False)} waited={t2.get('_waited_s')}s",
            reason="" if ok else f"换装未通过：status={code} pending={a2.get('pending')}",
            details={"outfit_status": code, "character": prot_name,
                     "outfit_body": raw[:200], "tasks": a2,
                     "degraded_path": (a2.get("error", 0) or 0) > 0}))

        # ---------------- ⑥ 重画 ----------------
        st = Stage("⑥", "重画/差分（regenerate，资产回到 ready 或兜底）")
        code, raw = cli.post(f"/api/game/{tsf_sid}/regenerate",
                             {"target": "protagonist", "force_fullgen": True})
        regen_ok = code == 200
        t3 = wait_assets_idle(cli, tsf_sid, timeout=150)
        a3 = t3.get("assets") or {}
        ok = regen_ok and a3.get("pending", -1) == 0
        stages.append(st.finish(
            ok, expect="POST /regenerate → 200，且随后 pending 归零",
            actual=f"regen_status={code} assets={json.dumps(a3, ensure_ascii=False)} "
                   f"waited={t3.get('_waited_s')}s",
            reason="" if ok else f"重画未通过：status={code} pending={a3.get('pending')}",
            details={"regen_status": code, "regen_body": raw[:200], "tasks": a3,
                     "degraded_path": (a3.get("error", 0) or 0) > 0}))

        # ---------------- ⑦ CG（截场景 / 重绘 其一） ----------------
        st = Stage("⑦", "CG（至少「截场景」或「重绘」一个走通）")
        cg_try = []
        cg_ok = False
        cg_entry = None
        for ctype in ("scene", "manual"):
            code, raw = cli.post(f"/api/game/{tsf_sid}/cg",
                                 {"type": ctype, "note": "端到端验收"})
            obj = jload(raw) or {}
            cg_try.append({"type": ctype, "status": code, "body": raw[:160]})
            if code == 200:
                cg_ok = True
                cg_entry = obj
                break
        meta_path = sample / "data" / "cg" / "meta.json"
        meta_valid = False
        meta_items = 0
        if meta_path.is_file():
            try:
                j = json.loads(meta_path.read_text(encoding="utf-8"))
                meta_valid = isinstance(j, (list, dict))
                meta_items = len(j) if isinstance(j, list) else len((j or {}).get("items", []) or [])
            except Exception as e:
                cg_try.append({"cg_meta_parse_error": str(e)})
        gallery_code, gallery_raw = cli.get("/api/cg")
        gallery = jload(gallery_raw) or {}
        n_gallery = len(gallery.get("items") or []) if isinstance(gallery, dict) else 0
        ok = cg_ok and meta_valid
        stages.append(st.finish(
            ok, expect="POST /cg（type=scene 优先，manual 兜底）→ 200；cg/meta.json 存在且 JSON 合法",
            actual=json.dumps({"tries": cg_try, "meta_valid": meta_valid,
                               "meta_items": meta_items, "cg_gallery_status": gallery_code,
                               "cg_gallery_items": n_gallery}, ensure_ascii=False)[:600],
            reason="" if ok else "截场景与重绘均未成功，或 cg/meta.json 非法",
            details={"tries": cg_try, "cg_meta_path": str(meta_path),
                     "cg_meta_valid": meta_valid, "cg_meta_items": meta_items,
                     "gallery_status": gallery_code, "gallery_items": n_gallery}))

        # ---------------- ⑧ 存档 ----------------
        st = Stage("⑧", "存档（slot 文件存在且 JSON 合法）")
        # 幂等清理：同名旧存档先删掉，避免第二次运行读到上一次的
        slots_code, slots_raw = cli.get("/api/slots")
        slots_obj = jload(slots_raw) or {}
        slot_name = "E2E-验收存档"
        removed = []
        for it in (slots_obj.get("slots") or slots_obj.get("items") or []):
            if isinstance(it, dict) and it.get("name") == slot_name:
                sid_del = it.get("slot_id") or it.get("id")
                if sid_del:
                    dc, _ = cli._req("DELETE", f"/api/slots/{sid_del}")
                    removed.append({"slot_id": sid_del, "delete_status": dc})
        state_before_code, state_before_raw = cli.get(f"/api/game/{tsf_sid}")
        state_before = jload(state_before_raw) or {}
        code, raw = cli.post(f"/api/game/{tsf_sid}/slots", {"name": slot_name})
        saved = jload(raw) or {}
        slot_id = saved.get("slot_id", "")
        slot_file = sample / "data" / "slots" / f"{slot_id}.json" if slot_id else None
        file_ok = bool(slot_file and slot_file.is_file())
        json_ok = False
        snap = {}
        if file_ok:
            try:
                snap = json.loads(slot_file.read_text(encoding="utf-8"))
                json_ok = isinstance(snap, dict)
            except Exception as e:
                saved["parse_error"] = str(e)
        ok = code == 200 and file_ok and json_ok
        stages.append(st.finish(
            ok, expect="POST /slots → 200；<DATA_DIR>/slots/<slot_id>.json 存在且 JSON 合法",
            actual=json.dumps({"save_status": code, "slot_id": slot_id,
                               "file_exists": file_ok, "json_valid": json_ok,
                               "turn_no": saved.get("turn_no")}, ensure_ascii=False),
            reason="" if ok else f"存档失败：status={code} file={file_ok} json={json_ok}",
            details={"save_status": code, "slot_id": slot_id, "removed_same_name": removed,
                     "slot_file": str(slot_file) if slot_file else None,
                     "snapshot_keys": sorted(snap.keys())[:25]}))

        # ---------------- ⑨ 读档 ----------------
        st = Stage("⑨", "读档（关键字段与存档时一致 + 资源可解析）")
        code, raw = cli.post(f"/api/slots/{slot_id}/load", {})
        loaded = jload(raw) or {}
        sid_after = loaded.get("sid") or tsf_sid
        s_after_code, s_after_raw = cli.get(f"/api/game/{sid_after}")
        s_after = jload(s_after_raw) or {}
        def key_of(state):
            """只取**存档前后都存在的公共字段**做比对。

            ⚠️ 不能拿「槽文件里的原始 session 快照」和「读档后的 public_state」比：
            前者是内部结构（有 `log`），后者是公开视图（有 `dialogue`、没有 `log`），
            字段集不同会假报不一致（本脚本第一版就踩了：log_len 3 vs 0）。
            """
            state = state or {}
            prot = state.get("protagonist") or {}
            dlg = state.get("dialogue")
            return {"protagonist": (prot.get("name") if isinstance(prot, dict) else prot),
                    "content_rating": state.get("content_rating"),
                    "title": state.get("title"),
                    "scene": state.get("scene"),
                    "background_id": state.get("background_id"),
                    "dialogue_len": len(dlg) if isinstance(dlg, list) else None}
        k_before, k_after = key_of(state_before), key_of(s_after)
        fields = [f for f in ("protagonist", "content_rating", "title", "scene",
                              "background_id", "dialogue_len")
                  if (k_before or {}).get(f) is not None]
        mismatches = [f for f in fields if k_before.get(f) != k_after.get(f)]
        # 资源可解析：用 /assets 端点（public_state 不含 assets）
        ac, ar = cli.get(f"/api/game/{sid_after}/assets")
        aobj = jload(ar) or {}
        files_checked, files_ok = 0, 0
        for grp in (aobj.get("assets") or {}).values():
            for it in (grp or {}).values():
                f = it.get("file") or ""
                if it.get("status") == "ready" and f:
                    files_checked += 1
                    p = sample / "data" / "cache" / f
                    if p.is_file() and p.stat().st_size > 0:
                        files_ok += 1
        ok = code == 200 and not mismatches
        stages.append(st.finish(
            ok, expect="POST /slots/{id}/load → 200；mode/protagonist/log_len/content_rating 与存档一致",
            actual=json.dumps({"load_status": code, "sid_after": sid_after,
                               "compare_fields": fields, "mismatches": mismatches,
                               "resources": f"{files_ok}/{files_checked} 个 ready 资源文件存在"},
                              ensure_ascii=False),
            reason="" if ok else f"读档失败：status={code} 字段不一致={mismatches}",
            details={"load_status": code, "key_before": k_before, "key_after": k_after,
                     "mismatches": mismatches, "files_checked": files_checked,
                     "files_ok": files_ok, "load_body_head": raw[:200]}))

        # ---------------- ⑩ 优雅关闭 ----------------
        st = Stage("⑩", "优雅关闭（退出耗时 + 无残留进程）")
        dbg = sample / "data" / "debug.log"
        pre_lines = dbg.read_text(encoding="utf-8", errors="replace").splitlines() if dbg.is_file() else []
        t0 = time.time()
        code, raw = cli.post("/api/shutdown", {}, timeout=20)
        exited = False
        while time.time() - t0 < 30:
            if proc.poll() is not None:
                exited = True
                break
            time.sleep(0.2)
        down_ms = int((time.time() - t0) * 1000)
        if not exited:
            proc.kill()
        resid = []
        if exited and proc.returncode is not None:
            resid = []
        port_released = None
        t1 = time.time()
        while time.time() - t1 < 10:
            if port_free(port):
                port_released = True
                break
            time.sleep(0.25)
        if port_released is None:
            port_released = False
        post_lines = dbg.read_text(encoding="utf-8", errors="replace").splitlines() if dbg.is_file() else []
        new_lines = post_lines[len(pre_lines):]
        persist_line = next((ln for ln in new_lines if "persisted" in ln and "session" in ln), "")
        ok = exited and port_released
        stages.append(st.finish(
            ok, expect="POST /api/shutdown → 进程在 30s 内退出；端口释放；无残留",
            actual=json.dumps({"shutdown_status": code, "exit_ms": down_ms,
                               "exited": exited, "port_released": port_released,
                               "returncode": proc.returncode,
                               "persist_log": persist_line[:160]}, ensure_ascii=False),
            reason="" if ok else "进程未退出或端口未释放（存在残留）",
            details={"shutdown_status": code, "exit_ms": down_ms, "exited": exited,
                     "port_released": port_released, "returncode": proc.returncode,
                     "persist_log": persist_line[:200]}))

    except EnvFailure as e:
        fatal = f"[ENV] {e}"
        env_failed = True
    except SystemExit:
        raise
    except Exception as e:
        fatal = f"{type(e).__name__}: {e}"
    finally:
        try:
            if proc is not None and proc.poll() is None:
                cli.post("/api/shutdown", {}, timeout=10)
                for _ in range(30):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.2)
                if proc.poll() is None:
                    proc.kill()
        except Exception:
            try:
                if proc is not None:
                    proc.kill()
            except Exception:
                pass
        if sd is not None:
            sd.stop()
        # 残留复检（只针对本次进程；不杀别人的）
        try:
            out_txt = (sample / "logs" / "e2e-server.out").read_text(
                encoding="utf-8", errors="replace") if (sample / "logs" / "e2e-server.out").is_file() else ""
        except Exception:
            out_txt = ""

    passed = sum(1 for s in stages if s["status"] == "pass")
    failed = [s for s in stages if s["status"] == "fail"]
    skipped = [s for s in stages if s["status"] == "skip"]
    result = {
        "payload_version": PAYLOAD_VERSION,
        "started": started, "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sample": str(sample), "port": meta.get("port"), "meta": meta,
        "stages": stages,
        "summary": {"total": len(stages), "passed": passed,
                    "failed": len(failed), "skipped": len(skipped),
                    "failed_ids": [s["id"] for s in failed],
                    "skipped_ids": [s["id"] for s in skipped],
                    "verdict": ("PASS" if not failed and len(stages) >= 10 and not skipped
                                else ("FAIL" if failed else "INCOMPLETE"))},
        "fatal": fatal,
    }
    exit_code = 2 if env_failed else (1 if (failed or fatal or len(stages) < 10) else 0)

    # ---- 结果落盘：JSON + latest + debug.log 里一行机读摘要 ----
    out_dir = Path(args.json).resolve().parent if args.json else (HERE / "e2e-results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_json = Path(args.json).resolve() if args.json else (out_dir / f"e2e-{stamp}.json")
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    one_line = json.dumps({"E2E": result["summary"]["verdict"],
                           "at": result["finished"],
                           "passed": passed, "failed": len(failed), "skipped": len(skipped),
                           "failed_ids": result["summary"]["failed_ids"],
                           "json": str(out_json)}, ensure_ascii=False)
    try:
        with (sample / "data" / "debug.log").open("a", encoding="utf-8") as f:
            f.write(f"\n[E2E] {one_line}\n")
    except Exception:
        pass

    # ---- 人读摘要 ----
    print("=" * 78)
    print(f"L4 端到端验收　{result['summary']['verdict']}　"
          f"通过 {passed}/{len(stages)}  失败 {len(failed)}  跳过 {len(skipped)}")
    print(f"样本：{sample}")
    print(f"端口：{meta.get('port')}（{meta.get('port_note')}）")
    print("-" * 78)
    for s in stages:
        mark = {"pass": "[PASS]", "fail": "[FAIL]", "skip": "[SKIP]"}.get(s["status"], "[??]")
        print(f" {mark} {s['id']} {s['name']}　[{s['duration_ms']}ms]")
        if s["status"] != "pass":
            print(f"      期望：{s['expect']}")
            print(f"      实际：{s['actual']}")
            if s["reason"]:
                print(f"      原因：{s['reason']}")
    if fatal:
        print(f" [FATAL] {fatal}")
    print("-" * 78)
    print(f"结果 JSON：{out_json}")
    print(f"摘要已追加：{sample / 'data' / 'debug.log'}")
    print("=" * 78)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

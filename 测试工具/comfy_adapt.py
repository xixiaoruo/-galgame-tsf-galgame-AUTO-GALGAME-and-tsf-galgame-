"""ComfyUI(本机) 一键适配程序 —— 纯标准库，无第三方依赖。

流程：
  1) 扫描本机 ComfyUI 服务（常见端口，默认 8000/8188/8189/6006/8080）；
     未发现则尝试启动 ComfyUI Desktop（探测到的合法路径）并等待最多 4 分钟；
  2) 读取可用 checkpoint，优先选择已验证的动漫/写实模型；
  3) 备份并写入 项目根/config.json（provider=comfyui + 地址 + 模型 + 推荐参数）；
  4) 可选“冒烟测试”：提交 512x512 工作流，60 秒内给出成败与示例图路径。

安全边界：本程序只与本机 127.0.0.1 的固定端口通信（本地绘画服务）——
所有 URL 经 _get() 护栏校验（协议/主机/禁重定向），绝不访问外网或任意地址。

用法：python comfy_adapt.py [--test]
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(r"D:\TSF_Galgame")
CONFIG = ROOT / "config.json"
OUT_SAMPLE = Path(r"D:\TSF_Galgame\测试样本")
PORTS = (8000, 8188, 8189, 6006, 8080)
DESKTOP_CANDIDATES = (
    Path("D:/COMFYUI/ComfyUI.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ComfyUI" / "ComfyUI.exe",
    Path.home() / "ComfyUI" / "ComfyUI.exe",
)
PREFERRED = ("zukiAnimeILL", "miaomiaoRealskin")


def _loopback_only(url: str) -> bool:
    """护栏：仅允许 http + 127.0.0.1（本机本地绘画服务），拒绝一切外部地址。"""
    try:
        u = urllib.parse.urlparse(url)
        return (u.scheme == "http" and u.hostname == "127.0.0.1"
                and not u.username and not u.password)
    except Exception:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 绝不跟随重定向（防被带离本机白名单）


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(url: str, timeout: float = 4.0):
    if not _loopback_only(url):
        raise ValueError("非法地址：本机适配程序只允许 127.0.0.1")
    req = urllib.request.Request(url, headers={"User-Agent": "tsf-adapt"})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read()


def probe(url: str) -> bool:
    try:
        _get(f"{url}/system_stats", 2.5)
        return True
    except Exception:
        return False


def scan() -> str | None:
    for port in PORTS:
        url = f"http://127.0.0.1:{port}"
        if probe(url):
            return url
    return None


def launch_desktop() -> bool:
    for p in DESKTOP_CANDIDATES:
        if p.is_file():
            try:
                os.startfile(str(p))  # noqa: S606
                return True
            except OSError:
                continue
    return False


def wait_until_up(seconds: int) -> str | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        url = scan()
        if url:
            return url
        time.sleep(5)
    return None


def checkpoints(base: str) -> list[str]:
    try:
        data = json.loads(_get(f"{base}/object_info/CheckpointLoaderSimple", 10))
        names = (data.get("CheckpointLoaderSimple", {})
                 .get("input", {}).get("required", {})
                 .get("ckpt_name", [None, []])[0] or [])
        return [str(n) for n in names]
    except Exception:
        return []


def pick_checkpoint(names: list[str]) -> str:
    for pref in PREFERRED:
        for n in names:
            if pref.lower() in n.lower():
                return n
    return names[0] if names else ""


def patch_config(base: str, ckpt: str) -> None:
    if not CONFIG.is_file():
        print("  未找到 config.json（将先启动后再由游戏生成）；跳过写入")
        return
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as e:
        print("  config.json 解析失败：", e)
        return
    bak = CONFIG.with_name("config.json.comfy.bak")
    if not bak.exists():
        bak.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print("  已备份原配置 → config.json.comfy.bak")
    img = cfg.setdefault("image", {})
    img["provider"] = "comfyui"
    img["base_url"] = base
    img["comfy_checkpoint"] = ckpt
    img.setdefault("sd_sampler", "euler")
    img.setdefault("sd_scheduler", "normal")
    img.setdefault("sd_cfg", 5.5)
    img.setdefault("sd_steps", 24)
    img.setdefault("sd_portrait_size", "512x768")
    img.setdefault("sd_background_size", "768x512")
    img.setdefault("vram_mode", "mid")
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"  已写入 config.json：provider=comfyui → {base}（模型：{ckpt}）")


def smoke_test(base: str, ckpt: str) -> bool:
    """极小工作流冒烟：512x512 1 步，验证提交→轮询→取图链路。"""
    import uuid
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "test, girl", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "bad", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": 1234, "steps": 1, "cfg": 4.5,
                         "sampler_name": "euler", "scheduler": "normal",
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "tsf_adapt_smoke",
                         "images": ["6", 0]}},
    }
    try:
        body = json.dumps({"prompt": wf, "client_id": uuid.uuid4().hex}).encode()
        req = urllib.request.Request(f"{base}/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        with _OPENER.open(req, timeout=20) as r:
            pid = json.loads(r.read())["prompt_id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            with _OPENER.open(f"{base}/history/{pid}", timeout=10) as r:
                node = json.loads(r.read()).get(pid) or {}
            if (node.get("status") or {}).get("status_str") == "error":
                print("  ❌ 冒烟失败：工作流运行出错")
                return False
            for _, out in (node.get("outputs") or {}).items():
                for im in out.get("images") or []:
                    img_url = (f"{base}/view?filename="
                               f"{urllib.parse.quote(im['filename'])}"
                               f"&subfolder={urllib.parse.quote(im.get('subfolder',''))}"
                               f"&type={im.get('type','output')}")
                    data = _get(img_url, 30)
                    OUT_SAMPLE.mkdir(exist_ok=True)
                    p = OUT_SAMPLE / "comfy_adapt_smoke.png"
                    p.write_bytes(data)
                    print(f"  ✅ 冒烟成功（{len(data)} 字节）示例图：{p}")
                    return True
            time.sleep(2)
        print("  ⚠ 冒烟 60 秒未完成（首次生成偏慢，可再次运行本工具重试）")
        return False
    except Exception as e:
        print("  ⚠ 冒烟异常：", e)
        return False


def main() -> int:
    print("=" * 52)
    print("  自动化AI Galgame · ComfyUI 本机一键适配")
    print("=" * 52)
    base = scan()
    if not base:
        print("① 未发现在线 ComfyUI 服务，尝试启动桌面端…")
        if launch_desktop():
            print("   已启动 ComfyUI Desktop（首次初始化可能较慢）")
        else:
            print("   未找到 ComfyUI Desktop 程序，请手动启动后重跑")
            return 1
        base = wait_until_up(240)
        if not base:
            print("   等待 4 分钟仍未就绪，请检查桌面端首次初始化；先启动游戏"
                  "不影响（可在设置页再探测）")
            return 1
    print(f"① 已连接：{base}")
    names = checkpoints(base)
    if not names:
        print("   ⚠ 未读到任何 checkpoint（模型目录为空），请先放置模型")
        return 1
    print("② 可用模型：", ", ".join(names))
    ckpt = pick_checkpoint(names)
    print(f"   选用模型：{ckpt}")
    print("③ 写入游戏配置…")
    patch_config(base, ckpt)
    if "--test" in sys.argv:
        print("④ 冒烟测试…")
        smoke_test(base, ckpt)
    print("=" * 52)
    print("  完成。现在可直接启动游戏；AI 设置页亦可用")
    print("  「🔌 自动探测并连接桌面端」一键连接。")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    sys.exit(main())

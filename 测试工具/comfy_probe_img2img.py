"""ComfyUI 图生图 / 掩膜重绘 真实接口探针（只读外部服务，不改项目数据）。

用途：在改 `app/image.py` 之前，先用本机正在运行的 ComfyUI 验证三件事：
  1) POST /upload/image 上传 PNG 后，LoadImage 能直接按文件名取到；
  2) LoadImage -> VAEEncode -> KSampler(denoise<1) -> VAEDecode 能出图，
     且未遮罩区域基本保持不变（换装/脸部细化的前提）；
  3) LoadImageMask -> SetLatentNoiseMask 的掩膜路径可用（脸部 inpaint 用）。

输出：`测试样本/comfy_probe/` 下的 init / img2img / inpaint 三张 PNG，
以及逐行计时与像素差异统计。失败时打印 ComfyUI 返回的 node_errors。
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

BASE = "http://127.0.0.1:8188"
ROOT = Path(r"D:\TSF_Galgame")
OUT = ROOT / "测试样本" / "comfy_probe"
SRC = ROOT / "测试样本" / "1.6.74_差分终验" / "千代_happy.png"


def get(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(path: str, payload: dict):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def upload(data: bytes, name: str) -> dict:
    boundary = "----tsfprobe" + uuid.uuid4().hex
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput\r\n".encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                  f"filename=\"{name}\"\r\nContent-Type: image/png\r\n\r\n").encode()
                 + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{BASE}/upload/image", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def build_workflow(kind: str, image_name: str, mask_name: str, ckpt: str,
                   prompt: str, neg: str, w: int, h: int, seed: int,
                   steps: int, cfg: float, denoise: float) -> dict:
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": neg, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "VAEEncode",
              "inputs": {"pixels": ["4", 0], "vae": ["1", 2]}},
    }
    if kind == "inpaint":
        wf["6"] = {"class_type": "LoadImageMask",
                   "inputs": {"image": mask_name, "channel": "red"}}
        wf["7"] = {"class_type": "SetLatentNoiseMask",
                   "inputs": {"samples": ["5", 0], "mask": ["6", 0]}}
        latent = ["7", 0]
    else:
        latent = ["5", 0]
    wf["8"] = {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                          "sampler_name": "euler", "scheduler": "normal",
                          "denoise": denoise, "model": ["1", 0],
                          "positive": ["3", 0], "negative": ["2", 0],
                          "latent_image": latent}}
    wf["9"] = {"class_type": "VAEDecode",
               "inputs": {"samples": ["8", 0], "vae": ["1", 2]}}
    wf["10"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "tsf_probe", "images": ["9", 0]}}
    return wf


def run(kind: str, image: bytes, mask: bytes | None, ckpt: str,
        prompt: str, neg: str, w: int, h: int, denoise: float) -> bytes:
    tag = uuid.uuid4().hex[:8]
    up = upload(image, f"tsf_probe_{kind}_{tag}.png")
    name = up.get("name") or f"tsf_probe_{kind}_{tag}.png"
    mask_name = ""
    if mask is not None:
        mu = upload(mask, f"tsf_probe_mask_{tag}.png")
        mask_name = mu.get("name") or f"tsf_probe_mask_{tag}.png"
    wf = build_workflow(kind, name, mask_name, ckpt, prompt, neg, w, h,
                        seed=12345, steps=12, cfg=5.5, denoise=denoise)
    t0 = time.time()
    body = post_json("/prompt", {"prompt": wf, "client_id": uuid.uuid4().hex})
    if body.get("node_errors"):
        raise SystemExit(f"{kind}: node_errors={body['node_errors']}")
    pid = body["prompt_id"]
    print(f"{kind}: submitted {pid} (upload {name})")
    deadline = time.time() + 240
    while time.time() < deadline:
        hist = get(f"/history/{pid}")
        node = hist.get(pid) or {}
        status = node.get("status", {})
        if status.get("status_str") == "error":
            raise SystemExit(f"{kind}: comfy error {status.get('messages')}")
        outs = node.get("outputs") or {}
        for out in outs.values():
            imgs = out.get("images") or []
            if imgs:
                im = imgs[0]
                q = urllib.parse.urlencode({"filename": im["filename"],
                                            "subfolder": im.get("subfolder", ""),
                                            "type": im.get("type", "output")})
                with urllib.request.urlopen(f"{BASE}/view?{q}", timeout=60) as r:
                    data = r.read()
                print(f"{kind}: OK in {time.time() - t0:.1f}s "
                      f"({len(data)} bytes, {im['filename']})")
                return data
        time.sleep(1.0)
    raise SystemExit(f"{kind}: timeout")


def main() -> int:
    import urllib.parse  # noqa: F401  (run() 用到，供局部作用域解析)
    OUT.mkdir(parents=True, exist_ok=True)
    im = Image.open(SRC).convert("RGBA")
    # 白底合成到 640x960 画布（模拟游戏里送进底图的形状）
    w, h = 640, 960
    canvas = Image.new("RGB", (w, h), (222, 222, 226))
    s = min(w / im.width, h / im.height)
    im2 = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))))
    canvas.paste(im2, ((w - im2.width) // 2, (h - im2.height) // 2), im2)
    init = io.BytesIO()
    canvas.save(init, "PNG")
    (OUT / "init.png").write_bytes(init.getvalue())
    print("init:", canvas.size, len(init.getvalue()), "bytes")

    info = get("/object_info/CheckpointLoaderSimple")
    ckpts = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    ckpt = next((c for c in ckpts if "waiIllustrious" in c), ckpts[0])
    print("checkpoint:", ckpt)

    prompt = ("1girl, adult woman, wearing a black turtleneck sweater and long "
              "pleated skirt, same person, same face, same hair, natural pose, "
              "anime visual novel illustration, clean lineart, soft lighting")
    neg = "lowres, bad anatomy, extra limbs, blurry, watermark, text"

    data = run("img2img", init.getvalue(), None, ckpt, prompt, neg, w, h, 0.55)
    (OUT / "img2img.png").write_bytes(data)
    a = Image.open(io.BytesIO(data)).convert("RGB").resize((w, h))
    b = canvas.convert("RGB")
    diff = ImageChops.difference(a, b)
    stat = sum(diff.convert("L").getdata()) / (w * h)
    print(f"img2img mean abs diff vs init: {stat:.2f} (越大改动越多)")

    # 脸部椭圆掩膜（与 refine_face 同形状：上 1/3 区域中央）
    mask = Image.new("L", (w, h), 0)
    dr = ImageDraw.Draw(mask)
    cx, cy = w // 2, int(h * 0.20)
    dr.ellipse((cx - int(w * 0.18), cy - int(h * 0.10),
                cx + int(w * 0.18), cy + int(h * 0.12)), fill=255)
    mb = io.BytesIO()
    mask.save(mb, "PNG")
    data2 = run("inpaint", init.getvalue(), mb.getvalue(), ckpt,
                prompt + ", detailed face, refined eyes", neg, w, h, 0.40)
    (OUT / "inpaint.png").write_bytes(data2)
    a2 = Image.open(io.BytesIO(data2)).convert("RGB").resize((w, h))
    m = mask.resize((w, h))
    outside = ImageChops.difference(a2, b)
    # 掩膜外平均差异（应显著小于掩膜内）
    px_out, px_in = [], []
    d_out, d_in = outside.convert("L").getdata(), diff.convert("L")
    for i, v in enumerate(d_out):
        (px_in if m.getdata()[i] > 128 else px_out).append(v)
    print(f"inpaint: outside-mask mean {sum(px_out)/max(1,len(px_out)):.2f}, "
          f"inside-mask mean {sum(px_in)/max(1,len(px_in)):.2f}")
    print("saved ->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())

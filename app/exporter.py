"""立绘导出插件：把生成的立绘整理为修图软件友好的目录结构。

每个角色一个文件夹，文件以「角色名_变体.png」命名（无哈希）；
同时提供白底平铺 JPG（供不支持透明的软件/快速预览）与 manifest.json
清单（含尺寸、变体、角色、时间），方便批处理脚本与修图软件读取。
"""
import json
import time
from pathlib import Path

from PIL import Image

from . import config as cfgmod


def export_session(session: dict) -> dict:
    """把一个对局中已就绪的角色立绘导出到 data/exports/ 下，返回导出信息。"""
    title = "".join(c for c in session["title"] if c.isalnum() or c in "_-")[:24] or "galgame"
    out_root = cfgmod.DATA_DIR / "exports" / f"{title}_{session['sid']}"
    roles = {session["protagonist"]["name"]: "主角"}
    for c in session["characters"]:
        roles[c["name"]] = "NPC"

    manifest = {
        "title": session["title"],
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "PNG 为透明底原片；*_白底.jpg 为白底平铺预览版，可直接导入修图软件。",
        "characters": [],
    }
    count = 0
    for name, role in roles.items():
        d = out_root / name
        items = []
        for item in session.get("assets", {}).get("portraits", {}).values():
            if item.get("status") != "ready" or not item.get("label", "").startswith(name + "·"):
                continue
            src_path = cfgmod.CACHE_DIR / item["file"]
            if not src_path.is_file():
                continue
            variant = item["label"].split("·", 1)[1]
            safe = "".join(c for c in variant if c.isalnum() or c in "_-") or "x"
            d.mkdir(parents=True, exist_ok=True)
            im = Image.open(src_path).convert("RGBA")
            png = d / f"{name}_{safe}.png"
            im.save(png)
            flat = Image.new("RGB", im.size, (255, 255, 255))
            flat.paste(im, (0, 0), im)
            flat.save(d / f"{name}_{safe}_白底.jpg", quality=92)
            items.append({"file": png.name, "size": list(im.size), "variant": variant})
            count += 1
        if items:
            manifest["characters"].append({"name": name, "role": role, "images": items})
    (out_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(out_root), "files": count}

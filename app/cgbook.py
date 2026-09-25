"""CG 回廊：跨会话保存的 CG 收藏库（data/cg/）。

两种收藏类型：
- bridge：剧情/命令轮 LLM 生成的高清电影感桥段图（文件在图片缓存中，
  生成完成前记录为「生成中」，清单时通过 resolver 解析）；
- scene / command / manual：把当前画面（背景 + 在场角色立绘）合成为一张
  1600x900 的场景 CG，立即可用。
"""
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

from . import config as cfgmod

log = logging.getLogger("galgame.cgbook")

CANVAS_W, CANVAS_H = 1600, 900
SPRITE_H = 820
MAX_CG = 300


def _meta_load() -> list[dict]:
    try:
        raw = json.loads(cfgmod.CG_META_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _meta_save(items: list[dict]) -> None:
    cfgmod.CG_DIR.mkdir(parents=True, exist_ok=True)
    cfgmod.CG_META_PATH.write_text(
        json.dumps(items[:MAX_CG], ensure_ascii=False, indent=1), encoding="utf-8")


def _new_id() -> str:
    return f"cg_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def list_entries(resolver=None) -> list[dict]:
    """画廊清单（新→旧）；bridge 条目通过 resolver(sid, cg_id) 解析图片文件。

    解析不到的旧桥段图（早期版本只登记引用、未落盘，缓存已被清理）标记
    file_lost，前端据此提示「源图已丢失」，而不是一直显示「生成中」。
    """
    items = _meta_load()[:MAX_CG]
    now = time.time()
    for it in items:
        if it.get("type") == "bridge" and not it.get("file") and resolver:
            it["file"] = resolver(it.get("sid", ""), it.get("cg_id", ""))
        it["ready"] = bool(it.get("file"))
        if (not it["ready"] and it.get("type") == "bridge"
                and now - float(it.get("time") or 0) > 3600):
            it["file_lost"] = True
    return items


def delete(cg_id: str) -> bool:
    items = _meta_load()
    hit = [i for i in items if i.get("id") == cg_id]
    if not hit:
        return False
    items = [i for i in items if i.get("id") != cg_id]
    _meta_save(items)
    try:
        (cfgmod.CG_DIR / f"{cg_id}.png").unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _cover_background(canvas: Image.Image, path: Path) -> None:
    img = Image.open(path).convert("RGB")
    ratio = max(CANVAS_W / img.width, CANVAS_H / img.height)
    img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                     Image.LANCZOS)
    left = (img.width - CANVAS_W) // 2
    top = (img.height - CANVAS_H) // 2
    canvas.paste(img.crop((left, top, left + CANVAS_W, top + CANVAS_H)))


def _gradient_placeholder(canvas: Image.Image) -> None:
    top, bottom = (64, 52, 96), (20, 18, 38)
    draw = ImageDraw.Draw(canvas)
    for y in range(CANVAS_H):
        t = y / CANVAS_H
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (CANVAS_W, y)], fill=color)


def _pick_portrait(session: dict, turn: dict, name: str) -> dict | None:
    """挑选该角色的立绘素材：优先当前换装差分 → 本轮表情 → 回退任意已就绪。"""
    assets = session.get("assets", {}).get("portraits", {})
    ready = [a for a in assets.values()
             if a.get("status") == "ready" and a.get("file")
             and a.get("label", "").startswith(name + "·")]
    if not ready:
        return None
    outfit = session.get(f"{name}|outfit", "")
    emotions = [d["emotion"] for d in turn.get("dialogue", [])
                if d.get("character") == name]
    emotion = emotions[-1] if emotions else "neutral"
    for prefix in (f"{name}·{outfit[:12]}", f"{name}·{emotion}",
                   f"{name}·neutral", f"{name}·"):
        for a in ready:
            if prefix in a["label"]:
                return a
    return ready[0]


def compose_scene(session: dict, turn: dict, cg_type: str = "manual",
                  note: str = "") -> dict:
    """把当前轮次的画面（背景 + 在场立绘）合成为场景 CG 并入库。"""
    cfgmod.CG_DIR.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (20, 18, 38))

    bg_id = turn.get("background_id")
    bg_file = ""
    if bg_id:
        key = session.get("bg_map", {}).get(bg_id)
        entry = (session.get("assets", {}).get("backgrounds", {}) or {}).get(key or "")
        if entry and entry.get("status") == "ready" and entry.get("file"):
            bg_file = entry["file"]
    if bg_file:
        try:
            _cover_background(canvas, cfgmod.CACHE_DIR / bg_file)
        except OSError as e:
            log.warning("cg bk background read failed: %s", e)
            _gradient_placeholder(canvas)
    else:
        _gradient_placeholder(canvas)

    prot_name = session["protagonist"]["name"]
    members = [prot_name]
    for who in (turn.get("present") or []):
        if who != prot_name and who not in members:
            members.append(who)
    sprites = [(name, a) for name in members
               for a in [_pick_portrait(session, turn, name)]
               if a]
    if sprites:
        n = len(sprites)
        for i, (name, entry) in enumerate(sprites):
            try:
                img = Image.open(cfgmod.CACHE_DIR / entry["file"]).convert("RGBA")
            except OSError as e:
                log.warning("cg bk sprite read failed: %s", e)
                continue
            scale = SPRITE_H / img.height
            img = img.resize((max(1, int(img.width * scale)), SPRITE_H), Image.LANCZOS)
            x = (i + 1) * CANVAS_W // (n + 1) - img.width // 2
            y = CANVAS_H - img.height
            canvas.paste(img, (x, y), img)

    canvas = canvas.convert("RGBA")
    label = f"{turn.get('scene', 'CG')}" + (f" · {note}" if note else "")
    # 场景标签移到左上（底部让位给剧情对话框）
    strip = Image.new("RGBA", (300, 44), (0, 0, 0, 120))
    canvas.paste(strip, (16, 16), strip)
    ImageDraw.Draw(canvas).text((28, 24), label[:26],
                                 fill=(255, 255, 255, 235))

    # ---- 1.7.9 剧情对话框：像游戏截图一样包含台词 ----
    try:
        from PIL import ImageFont

        def _font(sz):
            for f in ("C:/Windows/Fonts/msyh.ttc",
                      "C:/Windows/Fonts/simhei.ttf"):
                try:
                    return ImageFont.truetype(f, sz)
                except OSError:
                    continue
            return ImageFont.load_default()

        dlg = turn.get("dialogue") or []
        last = dlg[-1] if dlg else None
        if last and str(last.get("text", "")).strip():
            bx0, by0 = 80, CANVAS_H - 210
            bx1, by1 = CANVAS_W - 80, CANVAS_H - 46
            panel = Image.new("RGBA", (bx1 - bx0 + 40, by1 - by0 + 20),
                              (0, 0, 0, 0))
            dp = ImageDraw.Draw(panel)
            dp.rounded_rectangle((0, 0, panel.width - 8, panel.height - 12),
                                 radius=18, fill=(24, 20, 40, 228),
                                 outline=(160, 140, 210, 210), width=2)
            roi = canvas.crop((bx0 - 20, by0, bx1 + 20, by1))
            roi.alpha_composite(panel, (0, 0))
            canvas.paste(roi, (bx0 - 20, by0))
            dr = ImageDraw.Draw(canvas)
            name = str(last.get("character", "")).strip()
            if name and name != "旁白":
                dr.text((bx0 + 24, by0 + 14), name, font=_font(30),
                        fill=(255, 206, 235, 255))
            text = str(last.get("text", ""))
            lines, cur = [], ""
            for ch in text:
                if len(cur) >= 40:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                lines.append(cur)
            for i, ln in enumerate(lines[:4]):
                dr.text((bx0 + 24, by0 + 54 + i * 33), ln, font=_font(28),
                        fill=(240, 237, 252, 255))
    except Exception as e:
        log.warning("cg dialog compose skipped: %s", e)

    cg_id = _new_id()
    canvas.convert("RGB").save(cfgmod.CG_DIR / f"{cg_id}.png", "PNG")
    item = {
        "id": cg_id,
        "file": f"{cg_id}.png",
        "scene": turn.get("scene", ""),
        "note": note,
        "type": cg_type,
        "characters": members,
        "time": time.time(),
    }
    items = _meta_load()
    items.insert(0, item)
    _meta_save(items)
    return item


def record_bridge(session: dict, turn: dict, note: str = "") -> dict:
    """登记桥段 CG（LLM 生成的电影感图）：图片就绪后由清单接口补全路径。"""
    cg_id = turn.get("cg_id", "")
    item = {
        "id": cg_id,
        "file": "",
        "sid": session.get("sid", ""),
        "cg_id": cg_id,
        "scene": (turn.get("cg") or {}).get("title", "") or turn.get("scene", ""),
        "note": note,
        "type": "bridge",
        "characters": turn.get("present", []),
        "time": time.time(),
    }
    items = _meta_load()
    items = [i for i in items if i.get("id") != cg_id]  # 同桥段不重复
    items.insert(0, item)
    _meta_save(items)
    return item


def _find(cg_id: str) -> dict | None:
    return next((i for i in _meta_load() if i.get("id") == cg_id), None)


def saved_file(cg_id: str) -> str:
    """CG 库里已落盘的文件名（不带路径）；没有则空串。

    桥段 CG 原先只在清单里记一个指向「会话缓存」的引用，而 /cg/<文件名>
    只服务 CG 目录、且不接受带斜杠的路径——缓存一旦被清理，图库里就只剩
    一条打不开的空记录。落盘后与缓存无关，永久可用。
    """
    item = _find(cg_id)
    if not item or not item.get("file"):
        return ""
    name = str(item["file"])
    if "/" in name or "\\" in name:
        return ""          # 历史数据里的缓存相对路径，不具备可用性
    return name if (cfgmod.CG_DIR / name).is_file() else ""


def save_bridge_image(cg_id: str, cache_rel: str) -> str:
    """把桥段 CG 成品图从缓存复制进 CG 库，写回清单并返回库内文件名。

    生成完成时调用一次即可永逸；图库清单再次遇到未落盘的老条目时会
    顺带补做（自愈历史数据，只要缓存里那张图还在）。
    """
    if not cg_id:
        return ""
    existing = saved_file(cg_id)
    if existing:
        return existing
    if not cache_rel:
        return ""
    items = _meta_load()
    item = next((i for i in items if i.get("id") == cg_id), None)
    if not item or item.get("type") != "bridge":
        return ""
    src = cfgmod.CACHE_DIR / str(cache_rel)
    if not src.is_file():
        return ""
    cfgmod.CG_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{cg_id}.png"
    try:
        shutil.copyfile(src, cfgmod.CG_DIR / name)
    except OSError as e:
        log.warning("cg save failed %s: %s", cg_id, e)
        return ""
    item["file"] = name          # 在同一个清单对象上改，再整体写回
    item["saved"] = time.time()
    _meta_save(items)
    log.info("cg saved to library: %s", name)
    return name

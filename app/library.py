"""角色库：把会话角色（含 AI 自动引入的）连同立绘与设定导出到项目内
「角色库/」文件夹 —— 玩家可直接打开观看，也可作为二次素材使用。

每个角色一个文件夹，同名角色来自不同对局时自动编号（主角001/002…）：
  <角色名>/ 或 <角色名>001/
    ├── <角色名>_<表情>.png      所有立绘差分（透明底）
    ├── 角色设定.json            名字/外貌/性格/来源局/标记
    └── 角色卡.html              本地可打开的角色卡（缩略图 + 设定）
"""
import html as _html
import json
import shutil
import time
from pathlib import Path

from . import config as cfgmod

ROSTER_DIR = cfgmod.ROOT / "角色库"


def _safe_name(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum() or ch in "_- ") or "角色"


def _read_roster_dir(d: Path) -> dict | None:
    """读取一个角色目录的角色设定.json；损坏/缺失返回 None。"""
    p = d / "角色设定.json"
    if not p.is_file():
        return None
    try:
        summary = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(summary, dict) or not str(summary.get("name", "")).strip():
        return None
    # 校验变体文件确实存在，避免库目录被手动改动后给出死引用
    variants = []
    for v in summary.get("variants") or []:
        f = v.get("file", "")
        if f and (d / f).is_file():
            variants.append(v)
    return {
        "name": str(summary["name"]).strip()[:12],
        "dir": d.name,
        "role": str(summary.get("role", "NPC")).strip(),
        "appearance": str(summary.get("appearance", "")).strip(),
        "personality": str(summary.get("personality", "")).strip(),
        "anchor": str(summary.get("anchor", "")).strip(),
        "auto_added": bool(summary.get("auto_added")),
        "source_game": str(summary.get("source_game", "")).strip(),
        "exported_at": str(summary.get("exported_at", "")).strip(),
        "variants": variants,
    }


def list_roster() -> list[dict]:
    """角色库全部角色（可按需要直接填充开局表单 / 复用已有立绘）。"""
    if not ROSTER_DIR.is_dir():
        return []
    out = []
    for d in sorted(ROSTER_DIR.iterdir()):
        if not d.is_dir():
            continue
        info = _read_roster_dir(d)
        if info:
            out.append(info)
    out.sort(key=lambda x: (x["role"] != "主角", x["name"]))
    return out


def get_roster_char(name: str, dir_name: str = "") -> dict | None:
    """按角色名精确查找；同名多来源（如多个对局都叫「主角」）时用 dir_name
    指定哪一份（库内角色名+目录名联合唯一）。"""
    for info in list_roster():
        if info["name"] == name and (not dir_name or info["dir"] == dir_name):
            return info
    return None


def _target_dir(safe: str, role_name: str, source_game: str) -> Path:
    """决定导出目录：同名多来源自动加序号（主角001、主角002…），
    「同名 + 同来源局 + 同角色」视为同一份，覆盖更新；否则另立新库。"""
    def same_source(d: Path) -> bool:
        try:
            ex = json.loads((d / "角色设定.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return (str(ex.get("source_game", "")) == source_game
                and str(ex.get("name", "")) == role_name)

    d = ROSTER_DIR / safe
    if not d.exists():
        return d
    if same_source(d):
        return d
    i = 1
    while True:
        candidate = ROSTER_DIR / f"{safe}{i:03d}"
        if not candidate.exists() or same_source(candidate):
            return candidate
        i += 1


def variant_path(info: dict, variant: str) -> Path | None:
    """库内某变体立绘的绝对路径；不存在返回 None。"""
    for v in info.get("variants") or []:
        if v.get("variant") == variant and v.get("file"):
            p = ROSTER_DIR / info["dir"] / v["file"]
            if p.is_file():
                return p
    return None


def export_roster(session: dict) -> dict:
    """导出会话的全部角色到角色库，返回统计。"""
    ROSTER_DIR.mkdir(parents=True, exist_ok=True)
    exported = 0
    errors = []

    roles = [dict(session["protagonist"], role="主角")]
    for c in session.get("characters", []):
        roles.append(dict(c, role="NPC"))

    for role in roles:
        name = role["name"]
        safe = _safe_name(name)
        # 同名不同来源局 → 自动编号独立库（主角001/002…），防止不同对局的
        # 主角/同 NPC 互相抢占设定与阶段立绘
        d = _target_dir(safe, name, session.get("title", ""))
        d.mkdir(parents=True, exist_ok=True)

        # ---- 1) 设定 JSON（二次利用素材） ----
        summary = {
            "name": name,
            "role": role.get("role", "NPC"),
            "appearance": role.get("appearance") or role.get("anchor", ""),
            "personality": role.get("personality", ""),
            "anchor": role.get("anchor", ""),
            "auto_added": bool(role.get("auto_added")),
            "first_seen": role.get("first_seen", ""),
            "source_game": session.get("title", ""),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "variants": [],
        }

        # ---- 2) 立绘差分复制 ----
        images = []
        for key, item in (session.get("assets", {}).get("portraits", {}) or {}).items():
            if item.get("status") != "ready":
                continue
            label = item.get("label", "")
            if not label.startswith(name + "·"):
                continue
            src = cfgmod.CACHE_DIR / item.get("file", "")
            if not src.is_file():
                continue
            variant = label[len(name) + 1:]
            fname = f"{safe}_{variant}.png"
            try:
                shutil.copy2(src, d / fname)
            except OSError as e:
                errors.append(f"{name}/{variant}: {e}")
                continue
            images.append(variant)
            summary["variants"].append({
                "variant": variant,
                "file": fname,
                "size": list(Path(src).stat().st_size and __import__("PIL").Image.open(src).size),
            })
            exported += 1

        (d / "角色设定.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        # ---- 3) 角色卡 HTML（本地双击即可观看） ----
        cards = []
        for v in summary["variants"]:
            cards.append(
                f'<figure class="card"><img src="{_html.escape(v["file"])}" '
                f'alt="{_html.escape(v["variant"])}">'
                f'<figcaption>{_html.escape(v["variant"])}</figcaption></figure>')
        if not cards:
            cards = ['<p class="empty">（该角色暂无已生成的立绘）</p>']
        card_html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>{_html.escape(name)} · 角色卡</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;background:#141320;color:#eceaf6;padding:32px}}
h1{{font-size:24px}} .tag{{color:#b9a8ff;font-size:13px;margin-left:8px}}
.meta{{background:#1d1b32;border:1px solid #33324d;border-radius:12px;padding:16px;margin:14px 0;line-height:1.9}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}
figure{{margin:0;background:#1d1b32;border-radius:12px;padding:14px;text-align:center}}
figure img{{max-width:100%;max-height:360px;object-fit:contain;border-radius:8px}}
figcaption{{color:#dcd6ff;margin-top:8px;font-size:13px}}
.empty{{color:#8a87a8}}
a{{color:#9fe3b0}}
</style></head><body>
<h1>{_html.escape(name)}<span class="tag">{
    "主角" if role.get("role") == "主角" else "NPC"}{" · AI 引入" if role.get("auto_added") else ""}</span></h1>
<div class="meta">
<b>外貌：</b>{_html.escape(role.get("appearance") or role.get("anchor") or "（未填写）")}<br>
<b>性格：</b>{_html.escape(role.get("personality", "") or "（未填写）")}<br>
<b>出处：</b>{_html.escape(session.get("title", ""))}（导出 {time.strftime("%Y-%m-%d %H:%M")}）
</div>
<h2>立绘差分</h2>
<div class="grid">{''.join(cards)}</div>
<p style="color:#8a87a8;margin-top:20px">透明底 PNG 可直接用于二次创作；
设定数据见同目录「角色设定.json」。</p>
</body></html>"""
        (d / "角色卡.html").write_text(card_html, encoding="utf-8")

    return {"ok": True, "path": str(ROSTER_DIR), "portraits": exported,
            "characters": len(roles), "errors": errors[:5]}

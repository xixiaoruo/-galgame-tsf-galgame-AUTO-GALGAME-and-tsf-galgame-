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
import logging
import shutil
import time
from pathlib import Path

from . import config as cfgmod
from . import safe as safemod

log = logging.getLogger("galgame.library")

ROSTER_DIR = cfgmod.ROOT / "角色库"


def roster_dir() -> Path:
    """真实角色库目录：源码/冻结都优先项目根，其次 exe 目录。

    1.9.0 修复：冻结 exe 的 ROOT=dist/TSF_Galgame，原来直接拼
    `ROOT/角色库` → 用 dist 启动时角色库永远是空的（玩家以为角色丢了）。
    单测仍可通过 monkeypatch `ROSTER_DIR` 覆盖（它存在时优先）。
    """
    if ROSTER_DIR.is_dir():
        return ROSTER_DIR
    for root in cfgmod.project_roots():
        d = root / "角色库"
        if d.is_dir():
            return d
    return ROSTER_DIR


def _safe_name(name: str) -> str:
    """角色名→目录/文件名安全段：只留字母数字汉字与 `_- `，并去掉尾随空格/点
    （Windows 会吃掉尾随空格与点，可能撞名或落到别的路径段）。"""
    cleaned = "".join(ch for ch in str(name) if ch.isalnum() or ch in "_- ")
    return cleaned.strip(" .") or "角色"


def _read_roster_dir(d: Path) -> dict | None:
    """读取一个角色目录的角色设定.json；损坏/缺失返回 None。

    B-01 同批：目录名与被引用的变体文件名都先过 safe 原语——被手工篡改的
    「角色设定.json」里即使写了 `../../config.json`，也不会被当成库内立绘。
    """
    if not d.is_dir():
        return None
    try:
        safemod.safe_name(d.name, what="角色库目录名")
    except ValueError:
        return None
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
        f = str(v.get("file", "") or "")
        if not f:
            continue
        try:
            fp = safemod.safe_join(d, f, what="角色库立绘")
        except ValueError as e:
            log.warning("roster variant skipped (%s/%s): %s", d.name, f, e)
            continue
        if fp.is_file():
            variants.append(dict(v, file=f))
    return {
        "name": str(summary["name"]).strip()[:12],
        "dir": d.name,
        "role": str(summary.get("role", "NPC")).strip(),
        "appearance": str(summary.get("appearance", "")).strip(),
        "personality": str(summary.get("personality", "")).strip(),
        "background": str(summary.get("background", "")).strip(),
        "schema": int(summary.get("schema", 1) or 1),
        "anchor": str(summary.get("anchor", "")).strip(),
        "auto_added": bool(summary.get("auto_added")),
        "source_game": str(summary.get("source_game", "")).strip(),
        "exported_at": str(summary.get("exported_at", "")).strip(),
        "variants": variants,
    }


def list_roster() -> list[dict]:
    """角色库全部角色（可按需要直接填充开局表单 / 复用已有立绘）。"""
    root = roster_dir()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
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

    root = roster_dir()
    d = root / safe
    if not d.exists():
        return d
    if same_source(d):
        return d
    i = 1
    while True:
        candidate = root / f"{safe}{i:03d}"
        if not candidate.exists() or same_source(candidate):
            return candidate
        i += 1


def variant_path(info: dict, variant: str) -> Path | None:
    """库内某变体立绘的绝对路径；不存在返回 None。

    B-01 同批：目录名与文件名都经 safe_join（逐段白名单 + resolve 越界断言），
    越界/非法一律返回 None，绝不把库外文件当成立绘返回。
    """
    for v in info.get("variants") or []:
        if v.get("variant") == variant and v.get("file"):
            try:
                p = safemod.safe_join(roster_dir(), str(info.get("dir", "")),
                                      str(v["file"]), what="角色库立绘")
            except ValueError as e:
                log.warning("roster variant rejected (%s): %s",
                            info.get("dir", ""), e)
                return None
            if p.is_file():
                return p
    return None


def write_card_html(d: Path, summary: dict) -> Path:
    """按角色设定.json 重写角色卡.html（导出与旧卡适配共用）。

    1.9.0：新增「身份 / 角色背景 / 关系」展示；旧卡适配时也会调用本函数，
    所以旧角色卡双击打开后能看到补写的背景，而不是只有外貌和表情图。
    """
    name = str(summary.get("name", "") or "角色")
    role = str(summary.get("role", "NPC") or "NPC")
    appearance = str(summary.get("appearance", "") or summary.get("anchor", "") or "（未填写）")
    personality = str(summary.get("personality", "") or "（未填写）")
    background = str(summary.get("background", "") or "（尚未谱写背景：在开局页角色卡点「✨ 谱写背景」即可自动补写）")
    traits = "、".join(str(x) for x in (summary.get("traits") or []) if str(x)) or "（未填写）"
    age = summary.get("age") or 0
    height = summary.get("height_cm") or 0
    body_type = str(summary.get("body_type", "") or "（未填写）")
    measurements = str(summary.get("measurements", "") or "（未填写）")
    birthday = str(summary.get("birthday", "") or "（未填写）")
    speech = str(summary.get("speech_style", "") or "（未填写）")
    likes = str(summary.get("likes", "") or "（未填写）")
    dislikes = str(summary.get("dislikes", "") or "（未填写）")
    _lock_cn = {
        "personality": "性格", "traits": "性格标签", "speech_style": "说话风格",
        "age": "年龄", "height_cm": "身高", "body_type": "体型",
        "measurements": "三围", "birthday": "生日", "appearance": "外貌",
        "role": "身份", "background": "背景", "likes": "喜欢", "dislikes": "厌恶",
    }
    locks = "、".join(_lock_cn.get(str(x), str(x))
                     for x in (summary.get("locked_fields") or [])) or "无"
    source_game = str(summary.get("source_game", "") or "")
    exported_at = str(summary.get("exported_at", "") or "")
    variants = list(summary.get("variants") or [])

    cards = []
    for v in variants:
        f = str(v.get("file", ""))
        if not f:
            continue
        cards.append(
            f'<figure class="card"><img src="{_html.escape(f)}" '
            f'alt="{_html.escape(str(v.get("variant", "")))}" loading="lazy">'
            f'<figcaption>{_html.escape(str(v.get("variant", "")))}</figcaption></figure>')
    if not cards:
        cards = ['<p class="empty">（该角色暂无已生成的立绘）</p>']
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>{_html.escape(name)} · 角色卡</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;background:#141320;color:#eceaf6;padding:32px;max-width:1100px;margin:auto}}
h1{{font-size:24px}} .tag{{color:#b9a8ff;font-size:13px;margin-left:8px}}
.meta{{background:#1d1b32;border:1px solid #33324d;border-radius:12px;padding:16px;margin:14px 0;line-height:1.9}}
.meta b{{color:#b9a8ff}} .bg{{white-space:pre-wrap}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}
figure{{margin:0;background:#1d1b32;border-radius:12px;padding:14px;text-align:center}}
figure img{{max-width:100%;max-height:360px;object-fit:contain;border-radius:8px}}
figcaption{{color:#dcd6ff;margin-top:8px;font-size:13px}}
.empty{{color:#8a87a8}}
a{{color:#9fe3b0}}
</style></head><body>
<h1>{_html.escape(name)}<span class="tag">{_html.escape(role)}{
    " · AI 引入" if summary.get("auto_added") else ""}</span></h1>
<div class="meta">
<b>身份：</b>{_html.escape(role)}<br>
<b>年龄：</b>{age or "（未填写）"}　<b>身高：</b>{f"{height} cm" if height else "（未填写）"}　<b>体型：</b>{_html.escape(body_type)}<br>
<b>三围：</b>{_html.escape(measurements)}　<b>生日：</b>{_html.escape(birthday)}<br>
<b>性格标签：</b>{_html.escape(traits)}<br>
<b>性格：</b>{_html.escape(personality)}<br>
<b>说话风格：</b>{_html.escape(speech)}<br>
<b>喜欢：</b>{_html.escape(likes)}　<b>厌恶：</b>{_html.escape(dislikes)}<br>
<b>固定属性：</b>{_html.escape(locks)}<br>
<b>外貌：</b>{_html.escape(appearance)}<br>
<b>背景：</b><span class="bg">{_html.escape(background)}</span><br>
<b>出处：</b>{_html.escape(source_game)}（导出 {_html.escape(exported_at)}）
</div>
<h2>立绘差分</h2>
<div class="grid">{''.join(cards)}</div>
<p style="color:#8a87a8;margin-top:20px">透明底 PNG 可直接用于二次创作；
设定数据见同目录「角色设定.json」。</p>
</body></html>"""
    path = d / "角色卡.html"
    path.write_text(html_text, encoding="utf-8")
    return path


def export_roster(session: dict) -> dict:
    """导出会话的全部角色到角色库，返回统计。"""
    root = roster_dir()
    root.mkdir(parents=True, exist_ok=True)
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
            "background": role.get("background", ""),
            "traits": list(role.get("traits") or []),
            "age": int(role.get("age") or 0),
            "height_cm": int(role.get("height_cm") or 0),
            "body_type": role.get("body_type", ""),
            "measurements": role.get("measurements", ""),
            "birthday": role.get("birthday", ""),
            "speech_style": role.get("speech_style", ""),
            "likes": role.get("likes", ""),
            "dislikes": role.get("dislikes", ""),
            "personality_locked": bool(role.get("personality_locked")),
            "locked_fields": list(role.get("locked_fields") or []),
            "anchor": role.get("anchor", ""),
            "auto_added": bool(role.get("auto_added")),
            "first_seen": role.get("first_seen", ""),
            "source_game": session.get("title", ""),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "schema": 3,
            "variants": [],
        }

        # ---- 2) 立绘差分复制 ----
        # B-01 同批：源文件与目标文件名都经 safe 原语（标签里的表情/服装词来自
        # LLM 与玩家输入，`..\..\x` 这类变体名不会再写到角色库之外）
        images = []
        for key, item in (session.get("assets", {}).get("portraits", {}) or {}).items():
            if item.get("status") != "ready":
                continue
            label = item.get("label", "")
            if not label.startswith(name + "·"):
                continue
            try:
                src = safemod.safe_join(cfgmod.CACHE_DIR,
                                        str(item.get("file", "") or ""),
                                        what="立绘文件")
            except ValueError as e:
                errors.append(f"{name}/{label}: {e}")
                continue
            if not src.is_file():
                continue
            variant = label[len(name) + 1:]
            try:
                fname = safemod.safe_name(f"{safe}_{variant}.png", what="角色库文件名")
                dst = safemod.safe_join(d, fname, what="角色库文件")
            except ValueError as e:
                errors.append(f"{name}/{variant}: {e}")
                continue
            try:
                shutil.copy2(src, dst)
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

        # B-04/t30：角色设定.json 是导出给玩家复用的数据，改走原子写
        # （含 Windows 共享冲突退避重试）；indent=2 透传保持格式不变。
        cfgmod.atomic_write_json(d / "角色设定.json", summary, indent=2)

        # ---- 3) 角色卡 HTML（本地双击即可观看；统一由 write_card_html 生成） ----
        write_card_html(d, summary)

    return {"ok": True, "path": str(root), "portraits": exported,
            "characters": len(roles), "errors": errors[:5]}

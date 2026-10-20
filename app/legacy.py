"""旧档案找回与适配（1.9.0）。

目标：把旧版本遗留在项目目录/备份目录里的
  · 设定档案（data/storylines 的 JSON、`档案备份/*.json`、回收站）
  · 角色库旧角色卡（`角色库/*/角色设定.json`）
  · 对局归档 zip
扫描出来，补齐 1.9.0 的新字段（role / background / schema / stage_count /
uniform_name），再放回当前数据目录。**不覆盖玩家现有内容**：
同 id 且内容不同的档案另存新 id；同 id 且内容相同的只补字段。

只做本地读写：不联网、不调用 LLM（可选背景补写在 restore 里单独开关）。
"""
from __future__ import annotations

import hashlib
import json
import time
import zipfile
from pathlib import Path

from . import characters as charm
from . import config as cfgmod
from . import library
from . import story

MAX_ARCHIVE_MB = 500
_MAX_STORY_FILES = 200
_MAX_ROSTER = 200


def _roots() -> list[Path]:
    """源码/冻结两种形态都可能放角色库与档案备份的候选根。"""
    return cfgmod.project_roots()


def _dirs(name: str) -> list[Path]:
    out = []
    for root in _roots():
        d = root / name
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def _safe_read(path: Path, limit: int = 4 * 1024 * 1024) -> dict | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _story_sources() -> list[tuple[str, Path]]:
    """可能存有旧设定档案的位置，按可信度排序（源码/冻结都覆盖）。"""
    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for root in _roots():
        for label, d in (("档案备份", root / "档案备份"),
                         ("项目旧数据", root / "data" / "storylines")):
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.json"))[:_MAX_STORY_FILES]:
                rp = p.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                out.append((label, p))
    # 回收站只有运行时数据目录一处
    trash = cfgmod.DATA_DIR / "storylines_trash"
    if trash.is_dir():
        for p in sorted(trash.glob("*.json"))[:_MAX_STORY_FILES]:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(("回收站", p))
    return out


def _fingerprint(st: dict) -> str:
    """档案内容指纹：用于判断同 id 是否真的是同一份档案。"""
    payload = json.dumps({
        "title": st.get("title", ""),
        "world": st.get("world", ""),
        "protagonist": st.get("protagonist", {}),
        "characters": st.get("characters", []),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _raw_story(sid: str) -> dict | None:
    """读取当前档案库里的原始 JSON（不要 story.get——它会清洗并补默认字段，
    导致旧档案被误判成「已适配」）。"""
    if not sid:
        return None
    try:
        return _safe_read(story._path(sid))
    except (ValueError, OSError):
        return None


def _is_test_story(label: str, data: dict) -> bool:
    """回收站里的自动验收/临时局（t22-verify、状态测试局…）不算「旧档案」，
    避免一键找回后把测试垃圾塞回档案列表。"""
    if label != "回收站":
        return False
    text = (str(data.get("name", "")) + " " + str(data.get("title", ""))).lower()
    return any(k in text for k in ("verify", "测试", "t22", "接口验证", "样式局"))


def _story_status(st: dict) -> tuple[str, str]:
    """返回 (状态, 说明)。状态：new / adapt / exists / conflict。"""
    sid = str(st.get("id") or "")
    raw = _raw_story(sid)
    if not raw:
        return "new", "当前档案库中没有这份档案"
    if _fingerprint(raw) != _fingerprint(st):
        return "conflict", "同 id 但内容不同，会另存为副本（不覆盖）"
    missing = []
    if int(raw.get("schema", 0) or 0) < 2:
        missing.append("schema")
    if not raw.get("stage_count"):
        missing.append("stage_count")
    if any(not c.get("role") or "background" not in c
           for c in (raw.get("characters") or [])):
        missing.append("角色卡字段")
    if missing:
        return "adapt", "字段待补齐：" + "、".join(missing)
    return "exists", "已适配"


def scan() -> dict:
    """只读扫描，返回可找回的档案/角色卡/zip 清单。"""
    story_items = []
    seen_fp = set()
    for label, p in _story_sources():
        data = _safe_read(p)
        if not data or not str(data.get("world", "")).strip():
            continue
        if _is_test_story(label, data):
            continue
        fp = _fingerprint(data)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        status, note = _story_status(data)
        story_items.append({
            "source": label, "file": p.name, "path": str(p),
            "id": str(data.get("id") or ""), "name": str(data.get("name", "")),
            "title": str(data.get("title", "")), "mode": str(data.get("mode", "tsf")),
            "characters": len(data.get("characters") or []),
            "status": status, "note": note, "fingerprint": fp,
        })

    roster_items = []
    for roster in _dirs("角色库"):
        for d in sorted(roster.glob("*"))[:_MAX_ROSTER]:
            if not d.is_dir():
                continue
            summary = _safe_read(d / "角色设定.json", limit=1024 * 1024)
            if not summary or not str(summary.get("name", "")).strip():
                continue
            needs = []
            if not summary.get("role"):
                needs.append("role")
            if "background" not in summary:
                needs.append("background")
            if int(summary.get("schema", 0) or 0) < 2:
                needs.append("schema")
            if needs:
                roster_items.append({
                    "dir": d.name, "name": str(summary.get("name", "")),
                    "role": str(summary.get("role", "")),
                    "source_game": str(summary.get("source_game", "")),
                    "variants": len(summary.get("variants") or []),
                    "needs": needs, "path": str(d),
                })

    zip_items = []
    for d in _dirs("档案备份"):
        label = "档案备份"
        for p in sorted(d.glob("*.zip")):
            try:
                size = p.stat().st_size
                if size > MAX_ARCHIVE_MB * 1024 * 1024:
                    continue
                with zipfile.ZipFile(p) as zf:
                    names = zf.namelist()
                    if "session.json" not in names:
                        continue
                    snap = json.loads(zf.read("session.json").decode("utf-8"))
                zip_items.append({
                    "source": label, "file": p.name, "path": str(p),
                    "size": size, "title": str(snap.get("title", "")),
                    "mode": str(snap.get("mode", "tsf")),
                })
            except (OSError, zipfile.BadZipFile, json.JSONDecodeError,
                    UnicodeDecodeError, KeyError):
                continue

    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "storylines": story_items,
        "roster": roster_items,
        "archives": zip_items,
        "counts": {
            "storylines": len(story_items),
            "roster": len(roster_items),
            "archives": len(zip_items),
            "need_action": sum(1 for x in story_items if x["status"] in ("new", "adapt", "conflict"))
            + len(roster_items) + len(zip_items),
        },
    }


def _adapt_story(data: dict, source: str, *, new_id: bool = False) -> dict:
    """补齐新字段；new_id=True 时换一个 id（内容冲突时另存副本）。"""
    import uuid
    out = dict(data)
    if new_id or not str(out.get("id") or "").strip():
        out["id"] = uuid.uuid4().hex[:10]
    prot = out.get("protagonist") if isinstance(out.get("protagonist"), dict) else {}
    out["characters"] = charm.normalize_character_list(
        out.get("characters") or [], limit=4,
        protagonist=str(prot.get("name", "")))
    try:
        sc = int(out.get("stage_count", 6))
    except (TypeError, ValueError):
        sc = 6
    out["stage_count"] = sc if sc in (4, 6, 10) else 6
    out["uniform_name"] = str(out.get("uniform_name", "") or "").strip()[:40]
    out["schema"] = 2
    out["recovered_from"] = str(source)[:120]
    out["recovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if not str(out.get("name", "")).strip():
        out["name"] = str(out.get("title", "") or "找回的档案")[:30]
    return out


def _restore_storylines() -> dict:
    created = adapted = 0
    errors = []
    seen = set()
    for label, p in _story_sources():
        data = _safe_read(p)
        if not data or not str(data.get("world", "")).strip():
            continue
        if _is_test_story(label, data):
            continue
        fp = _fingerprint(data)
        if fp in seen:       # 同一份档案在备份/旧数据里各有一份：只处理一次
            continue
        seen.add(fp)
        sid = str(data.get("id") or "")
        raw = _raw_story(sid)
        try:
            if raw and _fingerprint(raw) == _fingerprint(data):
                # 同一份档案：只补字段，不覆盖玩家后续编辑的标题/世界。
                # 用原始 JSON 做底（而不是 story.get 的清洗结果），否则
                # 旧角色卡的空 role/background 会被清洗掉、无法判断缺失。
                merged = _adapt_story(raw, label)
                story.save(merged)
                adapted += 1
            else:
                out = _adapt_story(data, label, new_id=bool(raw))
                story.save(out)
                created += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p.name}: {type(e).__name__}: {e}")
    return {"created": created, "adapted": adapted, "errors": errors[:8]}


async def _restore_roster(*, generate_backgrounds: bool = False) -> dict:
    adapted = generated = 0
    errors = []
    dirs = []
    for root in _dirs("角色库"):
        dirs.extend(d for d in sorted(root.glob("*"))[:_MAX_ROSTER]
                    if d.is_dir())
    for d in dirs:
        summary = _safe_read(d / "角色设定.json", limit=1024 * 1024)
        if not summary or not str(summary.get("name", "")).strip():
            continue
        changed = False
        if not summary.get("role"):
            summary["role"] = "主角" if d.name.startswith("主角") else "NPC"
            changed = True
        if "background" not in summary:
            summary["background"] = ""
            changed = True
        if generate_backgrounds and not str(summary.get("background", "")).strip():
            try:
                r = await charm.write_background(
                    name=str(summary.get("name", "")),
                    role=str(summary.get("role", "")),
                    appearance=str(summary.get("appearance", "")),
                    personality=str(summary.get("personality", "")),
                    world="", title=str(summary.get("source_game", "")),
                    protagonist="",
                    existing=[])
                summary["background"] = r["background"]
                summary["background_source"] = r.get("source", "")
                generated += 1
                changed = True
            except Exception as e:  # noqa: BLE001
                errors.append(f"{d.name}: {e}")
        if int(summary.get("schema", 0) or 0) < 2:
            summary["schema"] = 2
            changed = True
        try:
            if changed:
                cfgmod.atomic_write_json(d / "角色设定.json", summary, indent=2)
                adapted += 1
            # HTML 始终重写：即使 JSON 已适配，旧版角色卡也可能还没有背景栏
            library.write_card_html(d, summary)
        except OSError as e:
            errors.append(f"{d.name}: {e}")
    return {"adapted": adapted, "backgrounds": generated, "errors": errors[:8]}


def _restore_archives() -> dict:
    imported = []
    errors = []
    for d in _dirs("档案备份"):
        for p in sorted(d.glob("*.zip")):
            try:
                if p.stat().st_size > MAX_ARCHIVE_MB * 1024 * 1024:
                    continue
                with zipfile.ZipFile(p) as zf:
                    if "session.json" not in zf.namelist():
                        continue
                from . import archive
                res = archive.import_archive(str(p))
                imported.append({"file": p.name, "sid": res.get("sid", ""),
                                 "title": (res.get("state") or {}).get("title", "")})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.name}: {type(e).__name__}: {e}")
    return {"imported": imported, "errors": errors[:8]}


async def restore(*, storylines: bool = True, roster: bool = True,
                  archives: bool = False,
                  generate_backgrounds: bool = False) -> dict:
    """执行找回/适配；各分支独立，失败不影响其他分支。"""
    out: dict = {"ok": True, "storylines": {}, "roster": {}, "archives": {}}
    if storylines:
        try:
            out["storylines"] = _restore_storylines()
        except Exception as e:  # noqa: BLE001
            out["storylines"] = {"error": f"{type(e).__name__}: {e}"}
    if roster:
        try:
            out["roster"] = await _restore_roster(
                generate_backgrounds=generate_backgrounds)
        except Exception as e:  # noqa: BLE001
            out["roster"] = {"error": f"{type(e).__name__}: {e}"}
    if archives:
        try:
            out["archives"] = _restore_archives()
        except Exception as e:  # noqa: BLE001
            out["archives"] = {"error": f"{type(e).__name__}: {e}"}
    return out

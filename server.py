"""AI Galgame 服务入口。运行：python server.py → http://127.0.0.1:8765"""
import json
import logging
import sys
import time
from pathlib import Path

# 控制台可能为 GBK：统一 UTF-8 输出，避免特殊字符 print 崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 启动进度：在导入重依赖（rembg/onnxruntime）之前就显示（控制台行 + Tk 进度窗）
from app import config as cfgmod
from app import startup as startup_mod

startup_mod.set_status_file(cfgmod.DATA_DIR / "startup.json")
startup_mod.mark(1, "loading runtime environment...", "加载运行环境…")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import archive as archive_mod
from app import cgbook
from app import exporter
from app import game
from app import image as image_mod
from app import inject
from app import library
from app import plugins as plugins_mod
from app import story
from app import vn
from app.llm import LLMError

import sys as _sys

debug_level = logging.DEBUG if "--debug" in _sys.argv else logging.INFO
logging.basicConfig(level=debug_level,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
# 调试日志落盘：data/debug.log（含 API 请求与生成错误，便于排查）
cfgmod.DATA_DIR.mkdir(parents=True, exist_ok=True)  # 先建目录：冷启动时 data/ 可能不存在
_debug_fh = logging.FileHandler(cfgmod.DATA_DIR / "debug.log", encoding="utf-8")
_debug_fh.setLevel(logging.INFO)
_debug_fh.setFormatter(logging.Formatter(
    "%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_debug_fh)
logging.getLogger("httpx").setLevel(logging.WARNING)

# 写入区根目录（exe 所在目录 / 源码项目目录）；冻结模式 __file__ 在临时解压区
ROOT = cfgmod.ROOT
# 显式引用 AI 抠图引擎：让 PyInstaller 完整收集 rembg（抠图质量保障），无环境时静默
try:
    import rembg  # noqa: F401
    import onnxruntime  # noqa: F401
except Exception:
    pass
startup_mod.mark(2, "runtime ready, preparing data dirs...", "运行环境就绪，准备数据目录…")
cfgmod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
cfgmod.SAVES_DIR.mkdir(parents=True, exist_ok=True)
cfgmod.CG_DIR.mkdir(parents=True, exist_ok=True)
startup_mod.mark(3, "migrating legacy data...", "迁移旧版本数据…")
_migrated = cfgmod.migrate_legacy_data()
if _migrated.get("migrated"):
    logging.info("已迁移旧数据到统一数据目录：%s", "、".join(_migrated["migrated"][:20]))
startup_mod.mark(4, "loading sessions...", "加载对局…")
game.load_all()
startup_mod.mark(5, "relocating portraits into session dirs...", "立绘归位到对局目录…")
_cache_migrated = game.migrate_cache_to_session_dirs()
if _cache_migrated.get("migrated"):
    logging.info("历史立绘已归位到各自对局文件夹：%d 张", _cache_migrated["migrated"])
startup_mod.mark(6, "cleaning up orphan assets...", "清理过期立绘…")
_cleaned = game.cleanup_orphan_cache()
if _cleaned.get("deleted"):
    logging.info("已清理无引用的过期立绘：%d 张", _cleaned["deleted"])
startup_mod.mark(7, "naming session dirs...", "对局目录可读化命名…")
_renamed = game.rename_session_dirs_to_readable()
if _renamed.get("renamed"):
    logging.info("对局立绘目录已改为可读命名：%d 个", _renamed["renamed"])

app = FastAPI(title="自动化AI Galgame")


# 1.7.34 全局 500 兜底：未捕获异常落盘 debug.log + 返回可读错误（调试与排障）


@app.exception_handler(Exception)
async def _unhandled_exception(request, exc):
    logging.exception("unhandled %s %s: %r",
                      request.method, request.url.path, exc)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={
        "detail": f"服务器内部错误：{type(exc).__name__}: {exc}"})

GAME_VERSION = "1.7.49"


@app.get("/api/version")
def version():
    return {"version": GAME_VERSION}


@app.get("/api/update-state")
def update_state():
    """是否有待安装的更新包（update/ 目录存在新 exe）。"""
    return {"update_ready": bool((ROOT / "update" / "TSF_Galgame.exe").is_file()),
            "version": GAME_VERSION}


@app.middleware("http")
async def no_cache_middleware(request, call_next):
    """禁用浏览器缓存：页面与脚本始终取最新版，避免升级后新旧文件混搭报错。"""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# ---------- 配置 ----------

class ConfigPayload(BaseModel):
    config: dict


@app.get("/api/config")
def get_config():
    cfg = cfgmod.load_config()
    model_detected = None
    if cfg["image"].get("provider", "openai") in ("sdwebui", "comfyui"):
        try:
            import httpx as _httpx
            base = (cfg["image"].get("base_url") or "").rstrip("/")
            with _httpx.Client(timeout=3) as c:
                if cfg["image"].get("provider") == "comfyui":
                    r = c.get(f"{base}/system_stats")
                    ck = json.loads(r.text).get("system", {})\
                        .get("comfyui_version", "")
                    model_detected = {"checkpoint": f"ComfyUI {ck}", "preset_name": "ComfyUI"}
                else:
                    r = c.get(f"{base}/sdapi/v1/options")
                    ck = r.json().get("sd_model_checkpoint", "") or ""
                    from app.image import match_model_preset
                    preset = match_model_preset(ck)
                    model_detected = {
                        "checkpoint": ck,
                        "preset_name": preset["name"],
                "steps": preset["steps"],
                "cfg": preset["cfg"],
                "sampler": preset["sampler"],
                "hr_fix": preset["enable_hr"],
                "hr_scale": preset.get("hr_scale", 2),
            }
        except Exception:
            model_detected = None
    from app.llm import LLMClient
    llm_client = LLMClient(cfg)
    return {
        "config": cfgmod.masked(cfg),
        "llm_ready": cfgmod.is_llm_configured(cfg),
        "llm_mock": llm_client.mock,
        "image_ready": cfgmod.is_image_configured(cfg),
        "model_detected": model_detected,
    }


@app.post("/api/config")
def update_config(payload: ConfigPayload):
    incoming = payload.config
    current = cfgmod.load_config()
    # key 安全合并：打码值不覆盖真实 key；空 key 视为"未修改"也不覆盖
    # （否则一次无关的设置保存会把已填好的 key 清空 → 玩家被永久锁在演示模式）
    for section in ("llm", "image"):
        if section in incoming and isinstance(incoming[section], dict):
            key = str(incoming[section].get("api_key", ""))
            if not key:
                incoming[section].pop("api_key", None)
            elif "*" in key or "填入" in key:
                incoming[section].pop("api_key", None)
    merged = dict(current)
    for k, v in incoming.items():
        if isinstance(v, dict):
            merged[k] = {**merged.get(k, {}), **v}
        else:
            merged[k] = v
    cfgmod.save_config(merged)
    return {"ok": True}


# ---------- 设定辅助 ----------

@app.post("/api/setup/random")
async def random_setup(mode: str = "tsf"):
    try:
        if mode == "vn":
            data = await vn.random_setup()
        else:
            data = await game.random_setup()
    except LLMError as e:
        raise HTTPException(500, str(e))
    if not isinstance(data, dict) or not data.get("characters"):
        raise HTTPException(500, "生成的设定格式异常，请重试")
    return data


# ---------- 游戏 ----------

class StartPayload(BaseModel):
    mode: str = "tsf"
    title: str = ""
    world: str = ""
    characters: list[dict] = []
    protagonist: dict = {}
    outline: str = ""
    lorebook: list[dict] = []
    directives: list[dict] = []
    catchphrases: list[str] = []
    content_rating: str = "18"
    r18_enabled: bool = True
    adult_intensity: str = "浓烈"
    allow_forced: bool = True
    library_imports: list[dict] = []
    stat_config: list[dict] = []
    uniform_en: str = ""
    uniform_neg: str = ""
    # 1.7.33 主角最终变化样式（身材/胸/瞳色/发色/气质/最终衣物）
    tsf_target: dict = {}


class OutlinePayload(BaseModel):
    world: str = ""
    protagonist: str = ""
    characters: list[dict] = []


class AdvancePayload(BaseModel):
    choice_index: int


class DirectivesPayload(BaseModel):
    directives: list[dict] = []


class RegeneratePayload(BaseModel):
    target: str = "protagonist"
    note: str = ""
    use_initial: bool = False
    # 1.7.31：force_fullgen=True 时重画=全量精美重画（内建 3 种子轮换+质检，
    # 跨性别变化只有全量做得到）；False 走链式保守（重画需求的温和路径）
    force_fullgen: bool = False

class DebugPayload(BaseModel):
    action: str = ""
    stat: str = ""
    value: int = 0
    character: str = ""
    outfit: str = ""





@app.post("/api/game/{sid}/debug")
async def debug_action(sid: str, payload: DebugPayload, request: Request):
    """隐藏调试面板接口：仅接受本机（127.0.0.1）请求。"""
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅限本机调试")
    try:
        s = game.get_session(sid)
        if s.get("mode") == "vn":
            # 普通模式仅支持重画/换装类调试动作
            if payload.action == "regen_all":
                return vn.regenerate(sid, "all")
            if payload.action == "force_outfit":
                return vn.apply_outfit(sid, payload.character or
                                       s["protagonist"]["name"], payload.outfit)
            raise game.GameError("普通模式不支持该调试动作")
        return game.debug_action(sid, payload.action, payload.stat,
                                 payload.value, payload.character,
                                 payload.outfit)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))



    target: str = "protagonist"


class SpeakPayload(BaseModel):
    text: str = ""


@app.post("/api/game/start")
async def start_game(payload: StartPayload):
    try:
        if payload.mode == "vn":
            return await vn.start(payload.model_dump())
        return await game.start_game(payload.model_dump())
    except game.GameError as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


class NewGamePlusPayload(BaseModel):
    slot_id: str = ""
    sid: str = ""
    title: str = ""


@app.post("/api/game/newgame-plus")
async def newgame_plus(payload: NewGamePlusPayload):
    """二周目（继承角色/场景 + 前情已被压缩），从存档槽或当前对局创建新局。"""
    try:
        if payload.slot_id:
            src = game.load_slot_raw(payload.slot_id)
        elif payload.sid:
            src = game.get_session(payload.sid)
        else:
            raise HTTPException(400, "请提供 slot_id 或 sid")
        return await game.new_game_plus(src, payload.title)
    except game.GameError as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        logging.exception("newgame-plus failed: %r", e)
        raise HTTPException(500, f"二周目失败：{e}")


@app.post("/api/outline")
async def generate_outline(payload: OutlinePayload):
    try:
        return await game.generate_outline(
            payload.world, payload.protagonist, payload.characters)
    except (game.GameError, LLMError) as e:
        raise HTTPException(502, str(e))




# ---------- 剧本管理 ----------

class StoryPayload(BaseModel):
    id: str = ""
    mode: str = "tsf"
    name: str = ""
    title: str = ""
    world: str = ""
    protagonist: dict = {}
    characters: list[dict] = []
    outline: str = ""
    lorebook: list[dict] = []
    directives: list[dict] = []
    catchphrases: list[str] = []
    content_rating: str = "all"
    r18_enabled: bool = False
    allow_forced: bool = False
    stat_config: list[dict] = []
    uniform_en: str = ""
    uniform_neg: str = ""
    tsf_target: dict = {}


@app.get("/api/storylines")
def story_list():
    return {"storylines": story.list_all()}


@app.get("/api/storylines/{sid}")
def story_get(sid: str):
    st = story.get(sid)
    if not st:
        raise HTTPException(404, "剧本不存在")
    return st


@app.post("/api/storylines")
def story_save(payload: StoryPayload):
    return story.save(payload.model_dump())


@app.delete("/api/storylines/{sid}")
def story_delete(sid: str):
    if not story.delete(sid):
        raise HTTPException(404, "剧本不存在")
    return {"ok": True}


# ---------- 插件管理 ----------

class PluginPayload(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    kind: str = "story"
    directives: list[dict] = []


@app.get("/api/plugins")
def plugin_list():
    return {"plugins": plugins_mod.list_all()}


@app.post("/api/plugins")
def plugin_save(payload: PluginPayload):
    return plugins_mod.save(payload.model_dump())


@app.delete("/api/plugins/{pid}")
def plugin_delete(pid: str):
    if not plugins_mod.delete(pid):
        raise HTTPException(404, "插件不存在")
    return {"ok": True}


# ---------- API 前置注入 ----------

class InjectPayload(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    text: str = ""
    enabled: bool = False


@app.get("/api/injects")
def inject_list():
    return {"injects": inject.list_all()}


@app.post("/api/injects")
def inject_save(payload: InjectPayload):
    return inject.save(payload.model_dump())


@app.delete("/api/injects/{pid}")
def inject_delete(pid: str):
    if not inject.delete(pid):
        raise HTTPException(404, "注入不存在")
    return {"ok": True}



class DeleteByIdPayload(BaseModel):
    id: str = ""


@app.post("/api/admin/wipe")
async def wipe_data(request: Request):
    """清空全部游戏历史数据（立绘缓存/存档/全局存档/世界观档案/插件），
    保留 API 配置（config.json）。需 body confirm=="WIPE" 且本机请求。"""
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅限本机调试")
    import shutil as _sh
    payload = await request.json()
    if payload.get("confirm") != "WIPE":
        raise HTTPException(400, "确认词应为 WIPE")
    import glob as _g
    cleared = {}
    for name, pattern in (
        ("cache", "data/cache"),
        ("saves", "data/saves"),
        ("slots", "data/slots"),
        ("storylines", "data/storylines"),
    ):
        d = cfgmod.DATA_DIR / name if name != "slots" else cfgmod.DATA_DIR / "slots"
        n = 0
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        n += 1
                    except OSError:
                        pass
        cleared[name] = n
    pf = cfgmod.DATA_DIR / "plugins.json"
    if pf.is_file():
        pf.unlink()
        cleared["plugins"] = 1
    game.SESSIONS.clear()
    for sf in cfgmod.SAVES_DIR.glob("*_slots"):
        _sh.rmtree(sf, ignore_errors=True)
    return {"ok": True, "cleared": cleared}


@app.post("/api/shutdown")
async def shutdown_server(request: Request):
    """关闭游戏：仅限本机调用；只结束游戏自身进程（不影响绘图服务等其它程序）。"""
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "仅限本机")
    import socket as _sock
    sd_running = False
    try:
        with _sock.create_connection(("127.0.0.1", 7860), timeout=0.8):
            sd_running = True
    except OSError:
        pass
    import os
    import threading
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return {"ok": True, "sd_running": sd_running}


@app.post("/api/storylines/delete")
def story_delete_by_id(payload: DeleteByIdPayload):
    if not story.delete(payload.id):
        raise HTTPException(404, "剧本不存在")
    return {"ok": True}


@app.post("/api/plugins/delete")
def plugin_delete_by_id(payload: DeleteByIdPayload):
    if not plugins_mod.delete(payload.id):
        raise HTTPException(404, "插件不存在")
    return {"ok": True}


# ---------- 会话列表（启动器分级调整用） ----------

@app.get("/api/game/sessions")
def session_list():
    out = []
    for s in game.SESSIONS.values():
        out.append({
            "sid": s["sid"],
            "title": s.get("title", ""),
            "content_rating": s.get("content_rating", "all"),
            "r18_enabled": bool(s.get("r18_enabled")),
            "saved_at": s.get("saved_at", 0),
        })
    out.sort(key=lambda x: x.get("saved_at", 0), reverse=True)
    return {"sessions": out[:10]}


@app.get("/api/directives/presets")
def directive_presets():
    return {"presets": plugins_mod.merged_presets()}


@app.get("/api/game/{sid}/directives")
def get_directives(sid: str):
    try:
        return {"directives": game.get_directives(sid)}
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/game/{sid}/directives")
def set_directives(sid: str, payload: DirectivesPayload):
    try:
        return {"directives": game.set_directives(sid, payload.directives)}
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class PolicyPayload(BaseModel):
    content_rating: str = "all"
    r18_enabled: bool = False
    adult_intensity: str = "成熟"
    allow_forced: bool | None = None   # None = 保留当前值（启动器等旧调用不会误关）
    sid: str = ""


# ---------- 命令 / 换装 / CG 收藏 ----------

class CommandPayload(BaseModel):
    character: str = ""
    action: str = ""


class OutfitPayload(BaseModel):
    character: str = ""
    outfit: str = ""


class CgPayload(BaseModel):
    type: str = "manual"
    note: str = ""


class PortraitDeletePayload(BaseModel):
    label: str = ""
    file: str = ""


@app.post("/api/game/{sid}/portrait-delete")
async def portrait_delete(sid: str, payload: PortraitDeletePayload):
    """立绘历程「删除此立绘」（1.7.38）：移除历史条目+删除文件+解除固定。"""
    try:
        return game.delete_portrait_version(sid, payload.label, payload.file)
    except game.GameError as e:
        raise HTTPException(400, str(e))


@app.post("/api/game/{sid}/context-reset")
async def context_reset(sid: str):
    """压缩上下文·重置 AI（1.7.36）：长局剧情压缩为摘要只留近 6 幕。"""
    try:
        return await game.context_reset(sid)
    except game.GameError as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        logging.exception("context-reset failed: %r", e)
        raise HTTPException(500, f"压缩上下文失败：{e}")


@app.post("/api/game/{sid}/command")
async def command(sid: str, payload: CommandPayload):
    try:
        if game.get_session(sid).get("mode") == "vn":
            return await vn.apply_command(sid, payload.character, payload.action)
        return await game.apply_command(sid, payload.character, payload.action)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/game/{sid}/outfit")
async def change_outfit(sid: str, payload: OutfitPayload):
    # async：换装会启动后台立绘生成任务（asyncio.create_task），必须有事件循环
    try:
        if game.get_session(sid).get("mode") == "vn":
            return vn.apply_outfit(sid, payload.character, payload.outfit)
        return game.apply_outfit(sid, payload.character, payload.outfit)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class CgRegenPayload(BaseModel):
    cg_id: str = ""


@app.post("/api/game/{sid}/cg-regen")
async def cg_regen(sid: str, payload: CgRegenPayload):
    """「刷新 CG / 重绘 CG」：按已记录的桥段画面描述重新生成（旧图保留）。
    async：CG 任务创建需要事件循环。"""
    try:
        return game.regen_cg(sid, payload.cg_id)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/cg")
def save_cg(sid: str, payload: CgPayload):
    try:
        return game.save_cg(sid, payload.type, payload.note)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.get("/api/cg")
def cg_gallery():
    return {"items": cgbook.list_entries(game.resolve_cg_file)}


@app.delete("/api/cg/{cg_id}")
def cg_delete(cg_id: str):
    if not cgbook.delete(cg_id):
        raise HTTPException(404, "CG 不存在")
    return {"ok": True}


@app.get("/cg/{filename}")
def serve_cg(filename: str):
    # 文件名由服务端生成，仍做白名单校验防目录穿越
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = cfgmod.CG_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)



@app.post("/api/game/sessions/policy")
def session_policy(payload: PolicyPayload):
    """按 body 中的 sid 应用内容分级（启动器用，避免动态 URL）。"""
    if not payload.sid:
        raise HTTPException(400, "缺少会话 id")
    try:
        return game.update_policy(payload.sid,
                                  payload.content_rating, payload.r18_enabled,
                                  payload.adult_intensity, payload.allow_forced)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/policy")
def update_policy(sid: str, payload: PolicyPayload):
    """切换内容分级（下一幕生效）。"""
    try:
        return game.update_policy(sid, payload.content_rating, payload.r18_enabled,
                                  payload.adult_intensity, payload.allow_forced)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/speak")
async def speak(sid: str, payload: SpeakPayload):
    """玩家固定话语：直接作为本轮行动注入，不进选项列表。"""
    try:
        if game.get_session(sid).get("mode") == "vn":
            return await vn.speak(sid, payload.text)
        return await game.speak(sid, payload.text)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


class RecutPayload(BaseModel):
    character: str = ""


@app.post("/api/game/{sid}/reload-assets")
def reload_assets(sid: str):
    """手动校正：重新读取本局全部立绘（历史路径自适应），修复后刷新显示。"""
    try:
        return game.reload_assets(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.get("/api/game/{sid}/portrait-history")
def portrait_history(sid: str):
    """该局全部角色立绘的版本历程（含归档旧版与当前版）。"""
    try:
        return game.portrait_history_view(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/game/{sid}/recut")
async def recut_portraits(sid: str, payload: RecutPayload):
    """按当前抠图模式，对全部（或指定角色）立绘的原始图重新抠图。"""
    try:
        return game.recut_portraits(sid, payload.character)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class CutPreviewPayload(BaseModel):
    character: str = ""


class CutApplyPayload(BaseModel):
    character: str = ""
    mode: str = "ai"


@app.post("/api/game/{sid}/cutpreviews")
def cut_previews(sid: str, payload: CutPreviewPayload):
    """生成指定角色的 4 种抠图方案预览图，供玩家选择。"""
    try:
        return game.cut_previews(sid, payload.character)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/cutapply")
def cut_apply(sid: str, payload: CutApplyPayload):
    """把选定的抠图方案应用到该角色全部立绘。"""
    try:
        return game.apply_cut(sid, payload.character, payload.mode)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class RegenLabelPayload(BaseModel):
    label: str = ""


@app.post("/api/game/{sid}/regenerate-label")
async def regenerate_label(sid: str, payload: RegenLabelPayload):
    """立绘历程「重绘差分」：只重画指定的一张表情差分（旧版归档）。
    非 neutral 表情走「以 neutral 为底的图生图只变表情」管线。
    async：需要事件循环创建后台生图任务。"""
    try:
        return game.regenerate_label(sid, payload.label)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/restore-raw")
async def restore_raw(sid: str):
    """立绘还原为去除背景之前的原始图。"""
    try:
        return game.restore_raw_portraits(sid)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.post("/api/game/{sid}/regenerate")
async def regenerate_portrait(sid: str, payload: RegeneratePayload):
    """针对性重新生成某个角色的立绘（防畸形/不满意时只重画该角色）。

    async 端点保证在事件循环内执行，从而能创建后台生图任务。
    """
    try:
        if game.get_session(sid).get("mode") == "vn":
            return vn.regenerate(sid, payload.target, payload.note)
        return game.regenerate_portrait(sid, payload.target, payload.note,
                                        use_initial=payload.use_initial,
                                        force_fullgen=payload.force_fullgen)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


@app.get("/api/game/{sid}/scenes")
def scene_list(sid: str):
    try:
        return {"scenes": game.scene_summary(sid)}
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.get("/api/slots")
def list_all_slots():
    """全局存档列表（固定位置，与会话无关）。"""
    return {"slots": game.list_slots()}


@app.get("/api/game/{sid}/slots")
def list_slots(sid: str):
    # 兼容旧前端：同样返回全局存档列表
    return {"slots": game.list_slots()}


class SaveSlotPayload(BaseModel):
    name: str = "存档"


@app.post("/api/game/{sid}/slots")
def save_slot(sid: str, payload: SaveSlotPayload):
    try:
        return game.save_slot(sid, payload.name)
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/slots/{slot_id}/load")
def load_slot(slot_id: str):
    try:
        r = game.load_slot(slot_id)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    s = game.get_session(r.get("sid", ""))
    if s.get("mode") == "vn":
        return vn.public_state(s)
    return r


@app.post("/api/game/{sid}/slots/{slot_id}/load")
def load_slot_compat(sid: str, slot_id: str):
    # 兼容旧前端：读档改用全局存档库
    try:
        r = game.load_slot(slot_id)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    s = game.get_session(r.get("sid", ""))
    if s.get("mode") == "vn":
        return vn.public_state(s)
    return r


@app.delete("/api/slots/{slot_id}")
def remove_slot(slot_id: str):
    return {"ok": game.delete_slot(slot_id)}





@app.post("/api/game/{sid}/library")
def export_library(sid: str):
    """导出全部角色到「角色库/」文件夹（立绘 + 设定 + 角色卡），并打开文件夹。"""
    import os
    import sys
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    result = library.export_roster(s)
    if result["portraits"] == 0:
        raise HTTPException(400, "本局还没有已生成的立绘可导出")
    try:
        path = result["path"]
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass
    return result


@app.get("/api/library")
def list_library():
    """角色库清单：开局页可选角色（设定+已有立绘变体），供导入复用。"""
    return {"characters": library.list_roster()}


@app.get("/api/library/{name}/preview")
def library_preview(name: str, dir: str = ""):
    """角色库角色缩略图（取第一张变体立绘，只读库内已存在文件）。
    同名多来源时用 ?dir= 指定具体那一份。"""
    info = library.get_roster_char(name, dir)
    if not info or not info.get("variants"):
        raise HTTPException(404, "该角色没有立绘")
    p = library.variant_path(info, str(info["variants"][0]["variant"]))
    if not p:
        raise HTTPException(404, "立绘文件不存在")
    from fastapi.responses import FileResponse
    return FileResponse(p)


class ComfyCheckPayload(BaseModel):
    base_url: str = "http://127.0.0.1:8188"


class ComfyLaunchPayload(BaseModel):
    path: str = ""


@app.post("/api/image/comfy/scan")
async def comfy_scan():
    """探测本机 ComfyUI 服务与桌面端程序（连接助手用）。"""
    return {"services": await image_mod.scan_local_comfy(),
            "desktop_apps": image_mod.desktop_comfy_paths()}


@app.post("/api/image/comfy/check")
async def comfy_check(payload: ComfyCheckPayload):
    """校验 ComfyUI 地址并回传可用模型列表。"""
    return await image_mod.comfy_checkpoints(payload.base_url)


@app.post("/api/image/comfy/launch")
async def comfy_launch(payload: ComfyLaunchPayload):
    """一键启动本机 ComfyUI 桌面端（仅白名单已知路径，防止任意执行）。"""
    known = set(image_mod.desktop_comfy_paths())
    if payload.path not in known:
        raise HTTPException(400, "只允许启动本机探测到的 ComfyUI 桌面端")
    try:
        import os as _os
        _os.startfile(payload.path)  # noqa: S606
    except OSError as e:
        raise HTTPException(500, f"启动失败：{e}")
    return {"ok": True, "path": payload.path}


class RestorePayload(BaseModel):
    label: str = ""
    file: str = ""
    pin: bool = False


@app.post("/api/game/{sid}/portrait-restore")
def restore_portrait(sid: str, payload: RestorePayload):
    """立绘历程「一键还原」：把归档旧版恢复为当前立绘；pin=True 表示
    「立刻使用此立绘」——该角色舞台显示固定为此版本（直到重画或取消固定）。"""
    try:
        return game.restore_portrait(sid, payload.label, payload.file,
                                     pin=payload.pin)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) or "归档" in str(e) else 400, str(e))


class UnpinPayload(BaseModel):
    name: str = ""


@app.post("/api/game/{sid}/portrait-unpin")
def unpin_portrait(sid: str, payload: UnpinPayload):
    """立绘历程「取消固定」：恢复该角色按当前阶段/表情/服装自动取图。"""
    try:
        return game.unpin_portrait(sid, payload.name)
    except game.GameError as e:
        raise HTTPException(404, str(e))


class PinLabelPayload(BaseModel):
    label: str = ""


@app.post("/api/game/{sid}/portrait-pin")
def pin_portrait(sid: str, payload: PinLabelPayload):
    """立绘历程「替换为此立绘」：指定标签即刻固定为该角色当前显示
    （不动文件；标「当前」但未生效的版本也能直接使用）。"""
    try:
        return game.pin_portrait(sid, payload.label)
    except game.GameError as e:
        raise HTTPException(404, str(e))


class TsfTargetPayload(BaseModel):
    hair: str = ""
    build: str = ""
    aura: str = ""
    hair_color: str = ""
    keep_hair_color: bool = True


@app.post("/api/game/{sid}/tsf-target")
async def set_tsf_target(sid: str, payload: TsfTargetPayload):
    """主角「转变目标设定」（仅 TSF 主角）：保存并立即按新目标重画当前阶段。
    必须 async——内部 create_task 需要事件循环（同 regenerate-label 教训）。"""
    try:
        return game.set_tsf_target(sid, payload.model_dump())
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.post("/api/game/{sid}/archive")
def export_archive(sid: str):
    """把整局打包为 data/archives/ 下的 zip 档案（可分享/可再导入）。"""
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    try:
        return {"ok": True, **archive_mod.export_session(s)}
    except archive_mod.ArchiveError as e:
        raise HTTPException(400, str(e))


@app.get("/api/archives")
def list_archives():
    """档案库列表（标题/轮数/大小/时间）。"""
    return {"archives": archive_mod.list_archives()}


@app.post("/api/archives/import")
async def import_archive(request: Request):
    """导入对局档案：请求体为 zip 原始字节（Content-Type: application/zip）。"""
    data = await request.body()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > 500 * 1024 * 1024:
        raise HTTPException(413, "档案超过 500MB")
    try:
        return archive_mod.import_archive(data)
    except archive_mod.ArchiveError as e:
        raise HTTPException(400, str(e))


@app.get("/api/archives/{name}/download")
def download_archive(name: str):
    """下载档案文件（分享给他人）。"""
    import re as _re
    if not _re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9_\-（）() ·——]{1,90}\.zip", name):
        raise HTTPException(400, "非法文件名")
    p = archive_mod._archives_dir() / name
    if not p.is_file():
        raise HTTPException(404, "档案不存在")
    from fastapi.responses import FileResponse
    return FileResponse(p, filename=name, media_type="application/zip")


@app.post("/api/archives/{name}/import")
def import_local_archive(name: str):
    """导入本地档案库中的一份 zip（继续玩/另存为独立对局）。"""
    import re as _re
    if not _re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9_\-（）() ·——]{1,90}\.zip", name):
        raise HTTPException(400, "非法文件名")
    p = archive_mod._archives_dir() / name
    if not p.is_file():
        raise HTTPException(404, "档案不存在")
    try:
        return archive_mod.import_archive(str(p))
    except archive_mod.ArchiveError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/archives/{name}")
def delete_archive(name: str):
    if archive_mod.delete_archive(name):
        return {"ok": True}
    raise HTTPException(404, "档案不存在")


@app.post("/api/game/{sid}/export")
def export_sprites(sid: str):
    """把本局立绘导出为修图软件友好的目录（干净命名+白底版+manifest）并打开。"""
    import os
    import sys
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    result = exporter.export_session(s)
    if result["files"] == 0:
        raise HTTPException(400, "本局还没有已就绪的立绘可导出")
    try:
        if sys.platform == "win32":
            os.startfile(result["path"])  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", result["path"]])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", result["path"]])
    except OSError:
        pass
    return result


class SceneSetPayload(BaseModel):
    name: str = ""
    action: str = "enter"   # enter = 进入/更换场景；rerender = 重生成当前场景背景


@app.post("/api/game/{sid}/scene-set")
def scene_set(sid: str, payload: SceneSetPayload):
    """「读取场景」：进入已有场景（复用固定背景）/ 更换到新场景 / 重生成当前背景。"""
    try:
        return game.scene_set(sid, payload.name, payload.action)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))


class OpenFolderPayload(BaseModel):
    sid: str = ""


@app.post("/api/open-folder")
def open_folder(payload: OpenFolderPayload = OpenFolderPayload()):
    """在系统资源管理器中打开「当前对局的立绘目录」（portraits 等子目录），
    未进入对局时打开立绘缓存根目录。"""
    import os
    import subprocess
    import sys
    path = str(cfgmod.CACHE_DIR.resolve())
    sid = (payload.sid or "").strip()
    if sid:
        try:
            session = game.get_session(sid)
            path = str((cfgmod.CACHE_DIR / game._session_dir_name(session)
                        / "portraits").resolve())
        except game.GameError:
            pass
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"ok": True, "path": path}
    except OSError as e:
        raise HTTPException(500, f"无法打开文件夹：{e}")


@app.post("/api/game/{sid}/advance")
async def advance(sid: str, payload: AdvancePayload):
    try:
        if game.get_session(sid).get("mode") == "vn":
            return await vn.advance(sid, payload.choice_index)
        return await game.advance(sid, payload.choice_index)
    except game.GameError as e:
        raise HTTPException(404 if "不存在" in str(e) else 400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/api/game/{sid}")
def game_state(sid: str):
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    game.ensure_scene_bg(s)   # 进入对局：当前幕背景不可用时立即补生成
    if s.get("mode") == "vn":
        d = vn.public_state(s)
    else:
        d = game.public_state(s)
    d["pinned"] = s.get("pinned_portraits", {})
    d["tsf_target"] = s.get("tsf_target", {})
    return d


@app.get("/api/game/{sid}/assets")
def game_assets(sid: str):
    try:
        s = game.get_session(sid)
    except game.GameError as e:
        raise HTTPException(404, str(e))
    return {"assets": s["assets"], "bg_map": s["bg_map"]}


@app.get("/api/game/{sid}/history")
def game_history(sid: str):
    try:
        return {"history": game.history_view(game.get_session(sid))}
    except game.GameError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/game/{sid}")
def remove_game(sid: str):
    game.delete_session(sid)
    return {"ok": True}


@app.get("/img/{filename:path}")
def serve_image(filename: str):
    # 路径通配：支持 三层（sid/{portraits|backgrounds|raw|preview}/文件）、
    # 两层（sid/文件）与根级（旧哈希）；白名单防目录穿越
    import re as _re
    if ".." in filename or "\\" in filename or filename.startswith("/"):
        raise HTTPException(400, "非法文件名")
    # 文件名白名单：允许空格（历史场景名「星冠学园 · 走廊」会进入文件名，
    # 此前白名单缺空格 → /img 400 → 背景 img onerror → 旧档背景永远黑屏）
    _FN = r"[A-Za-z0-9_.\-~ \u4e00-\u9fa5（）()·——]+\.(png|jpg|jpeg|webp)"
    _DIR = r"[\u4e00-\u9fa5A-Za-z0-9_\-]{1,32}"
    ok = bool(_re.fullmatch(_FN, filename))
    parts = filename.split("/")
    if len(parts) == 2 and _re.fullmatch(_DIR, parts[0]):
        ok = ok or bool(_re.fullmatch(_FN, parts[1]))
    if len(parts) == 3 and _re.fullmatch(_DIR, parts[0]) \
            and parts[1] in ("portraits", "backgrounds", "raw", "preview", "versions"):
        ok = ok or bool(_re.fullmatch(_FN, parts[2]))
    if not ok:
        raise HTTPException(400, "非法文件名")
    path = cfgmod.CACHE_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """旧缓存页面可能仍请求 /favicon.ico，返回 SVG 图标避免 404。"""
    from fastapi.responses import Response
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
           "<text y='26' font-size='26'>✦</text></svg>")
    return Response(content=svg, media_type="image/svg+xml")


# 冻结（exe）模式下 static 在包内只读区；源码运行用项目 static/
import sys as _sys
_static_dir = (cfgmod.BUNDLE / "static" if getattr(cfgmod, "BUNDLE", None)
               else ROOT / "static")
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


if __name__ == "__main__":
    import socket

    import uvicorn

    PORT = 8765
    # ---- 更新检测：update/ 有新包时引导一键 bat 更新（数据固定，替换安全）----
    import os as _os
    if (_os.path.basename(_sys.executable).lower().endswith(".exe")
            and "python" not in _os.path.basename(_sys.executable).lower()):
        try:
            _upd = ROOT / "update" / "TSF_Galgame.exe"
            if _upd.is_file():
                _bat = ROOT / "安装更新.bat"
                print("=" * 46)
                print("  [UPDATE] 检测到新版本更新包 (update\\TSF_Galgame.exe)")
                if _bat.is_file():
                    print("  [UPDATE] 在文件夹中双击「安装更新.bat」即可自动完成更新；")
                    print("  [UPDATE] 或者：先点 [X 关闭游戏]，把 update\\ 里的新 exe 覆盖即可")
                print("  [UPDATE] 进度/档案不会丢失（数据在 D:\\TSF_Galgame_Data）")
                print("=" * 46)
        except OSError:
            pass
    socks = []
    # Windows 上 SO_REUSEADDR 会允许多实例静默共存同一端口，导致请求被旧进程
    # 接走；仅在非 Windows 平台设置，让重复启动直接报"端口被占用"
    import sys
    reuse = socket.SO_REUSEADDR if sys.platform != "win32" else 0
    # IPv6（设 v6only 避免与 IPv4 抢占），系统不支持时自动跳过
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        if reuse:
            s6.setsockopt(socket.SOL_SOCKET, reuse, 1)
        s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s6.bind(("::", PORT))
        s6.listen(128)
        socks.append(s6)
    except OSError as e:
        print(f"IPv6 监听未启用（不影响 IPv4 使用）：{e}")
    # IPv4 必须可用
    s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if reuse:
        s4.setsockopt(socket.SOL_SOCKET, reuse, 1)
    try:
        s4.bind(("0.0.0.0", PORT))
        s4.listen(128)
    except OSError:
        for s in socks:
            s.close()
        # 复用旧实例：端口被占时先探测是不是本游戏服务，是则直接打开浏览器退出
        try:
            import json as _json
            import urllib.request as _ureq
            with _ureq.urlopen(f"http://127.0.0.1:{PORT}/api/config", timeout=2) as _r:
                _probe = _json.loads(_r.read().decode("utf-8"))
            if isinstance(_probe, dict) and "llm_mock" in _probe:
                print(f"检测到游戏已在运行（端口 {PORT}），直接为你打开浏览器…")
                import webbrowser
                webbrowser.open(f"http://127.0.0.1:{PORT}")
                sys.exit(0)
        except Exception:
            pass
        print(f"端口 {PORT} 已被其他进程占用，无法启动（不是本游戏服务）。")
        print("· 最简单方式：双击项目根目录的 start.bat（会按端口结束后再启动）")
        print("· 或手动结束：taskkill /PID <占用进程PID> /F  后重新运行 python server.py")
        sys.exit(1)
    socks.append(s4)

    lan_ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
    except OSError:
        pass

    print()
    print("=" * 52)
    print("  自动化AI Galgame 已启动，请用浏览器打开：")
    print("  · http://127.0.0.1:8765")
    print("  · http://localhost:8765")
    if lan_ip:
        print(f"  · 局域网设备（如手机）: http://{lan_ip}:8765")
    print("  （保持本窗口开启；关闭窗口即停止服务）")
    print("=" * 52)
    print()
    if getattr(sys, "frozen", False):
        # 分享版 exe：启动后自动打开浏览器，双击即玩
        import webbrowser
        webbrowser.open("http://127.0.0.1:8765")
    startup_mod.mark(8, "starting web service...", "启动服务（打开浏览器）…")

    # 服务端口实际可连时关闭启动进度窗（避免窗口盖住游戏页面）
    def _close_splash_online():
        import socket as _sock
        try:
            for _ in range(150):
                try:
                    with _sock.create_connection(("127.0.0.1", PORT), timeout=0.3):
                        startup_mod.finish()
                        return
                except OSError:
                    time.sleep(0.2)
        except Exception:
            pass
        startup_mod.finish()

    import threading as _threading
    _threading.Thread(target=_close_splash_online, daemon=True).start()

    server = uvicorn.Server(uvicorn.Config(app, log_level="info"))
    server.run(sockets=socks)

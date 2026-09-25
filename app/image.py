"""生图客户端：OpenAI 兼容 /images/generations，未配置时用 Pillow 生成占位图。

另含白底转透明处理（立绘抠白）。
"""
import asyncio
import colorsys
import hashlib
import io
import logging
import re
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("galgame.image")

PALETTES = [
    ((119, 84, 158), (236, 200, 255)),
    ((31, 97, 141), (168, 216, 234)),
    ((192, 87, 116), (255, 214, 224)),
    ((39, 134, 93), (198, 240, 213)),
    ((214, 138, 61), (255, 234, 197)),
]



# 常见外观词中译英（SD 模型对英文颜色/服饰词服从度远高于中文描述）
APPEARANCE_EN_MAP = {
    "银白色": "silver white", "银白": "silver white", "银色": "silver",
    "银灰": "silver grey", "银灰色": "silver grey",
    "金色": "golden", "金色长发": "golden long hair",
    "黑色长发": "black long hair", "黑色短发": "black short hair",
    "浅棕色": "light brown", "栗色": "chestnut brown", "酒红色": "wine red",
    "亚麻色": "flaxen", "淡紫色": "pale purple", "深紫色": "deep purple",
    "翠绿色": "emerald green", "灰蓝色": "grey blue", "琥珀色": "amber",
    "深黑色": "black", "冰蓝色": "ice blue",
    "水手服": "sailor uniform", "白大褂": "white coat",
    "眼镜": "glasses", "长发": "long hair",
    "连衣裙": "dress", "白色连衣裙": "white dress", "风衣": "trench coat",
    "西装": "suit", "卫衣": "hoodie", "便装": "casual clothes",
    "长直发": "long straight hair", "齐刘海": "blunt bangs",
    "双马尾": "twin tails", "马尾": "ponytail", "卷发": "curly hair",
}


CINEMATIC_SUFFIX = (", cinematic film still, cinematic lighting, dramatic "
                     "light and shadow, film grain, shallow depth of field, "
                     "highly detailed, masterpiece quality")

# 粉色柔性光源：角色轮廓柔和粉光（不违和的立绘边缘，让抠图后边缘自然）
PINK_RIM_SUFFIX = (", soft pink rim light on the character silhouette edges, "
                   "subtle pink glow, gentle backlight halo, delicate pink "
                   "edge lighting")

# 1.7.35 发色偏置（用户：白发太容易出、指定发色做不到、NPC 发色缺随机）：
# ①外观含发色词（中/英）→ 加权前插（(black hair:1.3)——前段 token 权
# 重高，把「设定色」钉住，不再漂成银白；
# ②外观**无发色词**（NPC 常见）→ 从发色池随机注入（不发白发——模型
# 默认白发偏置）+ 负面封杀银白（设定明确白/银时自动摘除）。
_HAIR_BIAS_MAP = [
    ("银白色", "silver white hair"), ("银白", "silver white hair"),
    ("银发", "silver hair"), ("白发", "white hair"), ("银灰", "silver grey hair"),
    ("金发", "golden hair"), ("金色长发", "golden long hair"),
    ("棕发", "brown hair"), ("栗色", "chestnut hair"), ("黑发", "black hair"),
    ("紫发", "purple hair"), ("红发", "red hair"), ("深蓝发", "dark blue hair"),
    ("蓝发", "blue hair"), ("粉发", "pink hair"), ("绿发", "green hair"),
    ("silver", "silver hair"), ("white hair", "white hair"),
    ("golden hair", "golden hair"), ("black hair", "black hair"),
    ("brown hair", "brown hair"), ("purple hair", "purple hair"),
    ("blonde hair", "blonde hair"),
]
_HAIR_RANDOM_POOL = [
    "black", "dark brown", "chestnut", "auburn", "wine red", "purple",
    "dark blue", "pink", "golden", "honey brown",
]

# 中文发色正则（「黑色短发」「深紫色长发」等任意长度组合都识别）
import re as _re_hair
_HAIR_CN_RE = _re_hair.compile(
    r"(银白色|银白|银发|白发|银灰|黑色|金色|棕色|栗色|紫色|红色|蓝色|"
    r"粉色|绿色|亚麻色|酒红色|淡紫色|深紫色)(?:的)?(?:短发|长发|头发|发|色)")
_HAIR_CN_EN = {
    "银白色": "silver white hair", "银白": "silver white hair",
    "银发": "silver hair", "白发": "white hair",
    "银灰": "silver grey hair", "黑色": "black hair",
    "金色": "golden hair", "棕色": "brown hair", "栗色": "chestnut hair",
    "紫色": "purple hair", "红色": "red hair", "蓝色": "blue hair",
    "粉色": "pink hair", "绿色": "green hair", "亚麻色": "flaxen hair",
    "酒红色": "wine red hair", "淡紫色": "pale purple hair",
    "深紫色": "deep purple hair",
}


def hair_bias(appearance: str, rng=None):
    """发色偏置（1.7.35）→ (正面词, 负面词)。发色词含「白/银」时不出负面。"""
    import random as _rnd
    rng = rng or _rnd
    text = str(appearance or "")
    low = text.lower()
    found = []
    for m in _HAIR_CN_RE.finditer(text):
        en = _HAIR_CN_EN.get(m.group(1))
        if en and en not in found:
            found.append(en)
    for zh, en in _HAIR_BIAS_MAP:
        if (zh in text or zh in low) and en not in found:
            found.append(en)
    if found:
        pos = ", ".join(f"({w}:1.3)" for w in found[:2])
        # 设定白/银发 → 不加白发负面（颜色随设定走）；只看「发色」词，
        # 不误判「白T恤/白衬衫」之类的服装白
        if any(k in text or k in low for k in (
                "银白色", "银白", "银发", "白发", "银灰", "银",
                "silver", "white hair", "blonde")):
            neg = ""
        else:
            neg = "silver grey hair, white hair, washed out hair color"
        return pos, neg
    # 无发色设定：随机池注入 + 封杀银白（模型白发默认偏置反制）
    en = rng.choice(_HAIR_RANDOM_POOL)
    pos = f"({en} hair:1.15)"
    return pos, "(silver hair:1.25), (white hair:1.2), silver grey hair"


# 便装/日常服装关键词（1.7.32 扩展）：玩家外观描述含这些词时——
# ① 着装正面词用「便装」版；② 摘除制服锁（uniform_en/uniform_neg）——
# 否则全局制服负面（"casual clothes, streetwear, jeans, t-shirt"）与
# 「白色T恤黑色长裤」类正面描述互撕 → 构图失稳/裸体（实机 s0 裸体真凶）。
_CASUAL_HINTS = ("便装", "便服", "日常", "休闲", "casual", "civilian",
                 "t恤", "T恤", "短袖", "卫衣", "牛仔裤", "长裤", "短裤",
                 "t-shirt", "hoodie", "jeans")


def apply_style_mode(style_suffix: str, style_mode: str) -> str:
    """风格模式：电影档追加电影质感英文标签（插件固定风格）。"""
    if (style_mode or "default") == "cinematic":
        return style_suffix + CINEMATIC_SUFFIX
    return style_suffix


def append_appearance_en(appearance: str) -> str:
    """把中文描述里的常见外观词翻译成英文标签追加，提升模型服从度。"""
    tags = [v for k, v in APPEARANCE_EN_MAP.items() if k in appearance]
    out = appearance
    for tag in tags:
        if tag not in out.lower():
            out += f", {tag}"
    return out



# ---- 表情英文强化标签（1.6.76）----
# SD 对中文表情描述感知弱：happy 只写「开心微笑的表情」会被模型画成面无表情。
# 这里给每个表情专用英文词（带权），generate_portrait / 差分图生图统一注入；
# 权重控制在 1.2~1.35，只强化「表情/神态」不触碰身份/身体/服装。
EMOTION_EN_TAGS = {
    "neutral": "(calm neutral expression:1.2), gentle relaxed face, "
               "soft gaze, serene smile",
    "happy": "(big happy smile:1.35), (joyful expression:1.3), "
             "smiling eyes, cheerful bright look, happy grin, "
             "radiant smile, cheeks lifted",
    "shy": "(blushing face:1.3), (bashful expression:1.25), "
           "shy smile, side glance, flustered cheeks thin blush",
    "sad": "(sad expression:1.35), teary eyes, drooping eyebrows, "
           "downturned mouth corners, sorrowful look, sad eyes",
    "angry": "(angry expression:1.3), (puffed cheeks:1.2), "
             "furrowed brows, angry glare, pouting face",
    "surprised": "(surprised expression:1.3), wide open eyes, "
                 "raised eyebrows, surprised mouth, taken aback look",
    "aroused": "(flushed face:1.3), half-lidded eyes, heavy breathing look, "
               "moist lips slightly parted, blushing deeply, "
               "intense gaze",
    "flustered": "(flustered expression:1.3), wide flustered eyes, "
                 "slight blush, panicked look, surprised fluster, "
                 "dizzy expression",
}


# ---- CG 质量档（1.6.95）：16:9 宽幅，比普通立绘/背景尺寸大 ----
CG_QUALITY_MODES = {
    "low":  {"size": "768x432",  "steps_cap": 20},
    "mid":  {"size": "1152x648", "steps_cap": 28},
    "high": {"size": "1536x864", "steps_cap": 32},
}


def effective_cg_quality(cfg: dict) -> str:
    q = str(cfg.get("cg_quality") or "mid").lower()
    if q not in CG_QUALITY_MODES:
        q = "mid"
    return q


def effective_cg(cfg: dict) -> tuple[str, int]:
    """CG 出图尺寸与步数封顶（质量档 cfg.image.cg_quality）。"""
    m = CG_QUALITY_MODES[effective_cg_quality(cfg)]
    return m["size"], int(m["steps_cap"])


def emotion_en_tags(emotion: str) -> str:
    """表情的英文强化标签（未知表情回退原词）。"""
    return EMOTION_EN_TAGS.get(emotion, str(emotion or ""))


# 边缘消除配件开关（config.json 的 image.edge_clean / edge_shrink）
_EDGE_CLEAN_ENABLED = False  # 保守默认：不收缩边缘（留边沿＞删配件）
_EDGE_CLEAN_SHRINK = 1
_EDGE_CLEAN_FEATHER = 0.8




def _pick_sampler(cfg_sampler: str, opts_sampler: str,
                  sampler_names: list[str], auto_sdxl: bool) -> str:
    """选择采样器：config 显式指定 > 检测到 SDXL Styles（自动）> WebUI 页面默认。

    Forge 系整合包采样器列表含 "SDXL Styles" 时，SDXL 风格出图更契合；
    用户显式设置 sd_sampler 时优先，但会校验其真实存在于 WebUI 采样器列表，
    不存在时回退到常用的 Karras/默认，避免 422/500。"""
    cfg_sampler = (cfg_sampler or "").strip()
    if cfg_sampler:
        if sampler_names and cfg_sampler not in sampler_names:
            log.warning("sampler '%s' not in WebUI, fallback", cfg_sampler)
            for cand in ("DPM++ 2M Karras", "SDXL Styles", "DPM++ 2M"):
                if cand in sampler_names:
                    return cand
            return opts_sampler or "DPM++ 2M"
        return cfg_sampler
    if auto_sdxl and any(s == "SDXL Styles" for s in sampler_names):
        return "SDXL Styles"
    return opts_sampler or "DPM++ 2M"




# 模型适配预设：按 WebUI 当前 checkpoint 名匹配（子串、不区分大小写）。
# 未显式配置 sd_steps/sd_cfg/sd_sampler/尺寸时自动套用。
MODEL_PRESETS = {
    # 写实模型：低步数+低 CFG 防过饱和。直出 1024x1760（此前实测稳定），
    # 低显存机器可把 hr_fix 打开用 HR 修复（512 基底放大），OOM 时自动降级。
    # 质量微调（负载小幅提升）：30->32 步(+6%)、CFG 5.0->5.5（写实更生动）
    "miaomiao": {
        "name": "写实模型（miaomiaoRealskin）",
        "steps": 32,
        "cfg": 5.5,
        "sampler": "DPM++ 2M",
        "scheduler": "karras",
        "portrait_size": "1024x1760",
        "background_size": "768x512",
        "negative_extra": ("anime, cartoon, illustration, 3d render, painting, "
                           "sketch, oversaturated, plastic skin, airbrushed, "
                           "heavy makeup, doll"),
        # 模型页推荐的 HR 2x 流程需 12GB+；RTX 5060 8GB + --medvram-sdxl 下
        # HR 第二遍（1024x1792 latent）实测 OOM，且 OOM 后 WebUI 模型状态
        # 会分裂（cpu/cuda:0 device mismatch），此后全部请求 500。
        # 8GB 出图方案：直出 1024x1760（30 步 + CFG5.0 + karras 已实测稳定）。
        "enable_hr": False,
        "hr_upscaler": "4x-AnimeSharp",
        "hr_scale": 2,
        "hr_steps": 10,
        "hr_denoise": 0.25,
    },
    # 动漫模型：中步数 + 常规 CFG；直出大图（实测稳定）
    "zuki": {
        "name": "动漫模型（zukiAnimeILL）",
        "steps": 30,
        "cfg": 7.0,
        "sampler": "DPM++ 2M",
        "portrait_size": "1024x1760",
        "background_size": "768x512",
        "negative_extra": ("photorealistic, real person, real skin, 3d render, "
                           "painting, bad anatomy"),
        "enable_hr": False,
        "hr_upscaler": "Latent",
        "hr_scale": 2,
        "hr_steps": 12,
        "hr_denoise": 0.5,
    },
    # wai-illustrious 系（1.7.43 初版 → 1.7.46 **特殊适配定稿**：sd-webui 实机
    # TSF 渐变序列 v14/v15 十七帧全链验证配方）：30 步 / CFG 6 / Euler a /
    # **ClipSkip 2**（官方推荐，经 override_settings 注入）/ Hires 1.5x
    # 4x-AnimeSharp(15步/0.35)；立绘 2:3 尺寸（1024x1536，SDXL 兼容比例，
    # 实机 1152x1728 同比例）；负面镇压该模型两大偏置：①眼型偏细长
    # ②**默认白发偏置**（无发色设定时 NPC/主角会漂成银白——发色词缺失
    # 时的系统性倾向，实机 v11 银发漂移实锤）。
    "wai": {
        "name": "Illustrious 系（waiIllustriousSDXL）",
        "steps": 30,
        "cfg": 6.0,
        "sampler": "Euler a",
        "scheduler": "normal",
        "clip_skip": 2,
        "portrait_size": "1024x1536",
        "background_size": "768x512",
        "negative_extra": ("small eyes, narrow eyes, cross-eye, dead eyes, "
                           "flat face, asymmetric eyes, "
                           "(silver hair:1.2), (pale hair:1.15), "
                           "(green hair:1.2), (teal hair:1.25), "
                           "(white hair:1.1), (grey-green hair:1.2)"),
        "enable_hr": True,
        "hr_upscaler": "4x-AnimeSharp",
        "hr_scale": 1.5,
        "hr_steps": 15,
        "hr_denoise": 0.35,
    },
    # NoobAI-XL 系：28 步 / CFG 5.5 / Euler a；直出 832x1216 更稳；
    # 正面 newest 前缀由管线通用风格词覆盖，负面压旧画风。
    "noobai": {
        "name": "NoobAI-XL 系",
        "steps": 28,
        "cfg": 5.5,
        "sampler": "Euler a",
        "scheduler": "normal",
        "portrait_size": "1024x1760",
        "background_size": "768x512",
        "negative_extra": "old, early, vintage anime style, dated art style",
        "enable_hr": False,
        "hr_upscaler": "4x-AnimeSharp",
        "hr_scale": 1.5,
        "hr_steps": 12,
        "hr_denoise": 0.4,
    },
}



# 显存档位（移动端 5060 等 8GB 卡用 mid；4-6GB 卡用 low；12GB+ 用 high）
VRAM_MODES = {
    "low":  {"portrait": "512x768",  "background": "640x448", "steps_cap": 20},
    "mid":  {"portrait": "768x1344", "background": "768x512", "steps_cap": 28},
    "high": {"portrait": "1024x1760", "background": "1024x640", "steps_cap": 32},
}

# 生图质量档（角色/背景 × 低中高）：分辨率+步数组合。
# 显存档位是显存 OOM 红线继续生效为上限：实际分辨率取「质量档 vs 显存档位」
# 逐维较小值，步数封顶取两者较小值；手动 sd_portrait_size/sd_background_size 优先。
QUALITY_MODES = {
    "portrait": {
        "low":  {"size": "512x768",   "steps_cap": 20},
        "mid":  {"size": "768x1344",  "steps_cap": 28},
        "high": {"size": "1024x1760", "steps_cap": 32},
    },
    "background": {
        "low":  {"size": "640x448",  "steps_cap": 20},
        "mid":  {"size": "768x512",  "steps_cap": 28},
        "high": {"size": "1024x640", "steps_cap": 32},
    },
}


def effective_quality(cfg: dict, category: str) -> str:
    q = str((cfg.get("portrait_quality") if category == "portrait"
             else cfg.get("background_quality")) or "mid").lower()
    if q not in QUALITY_MODES[category]:
        q = "mid"
    return q


def _smaller_size(a: str, b: str) -> str:
    """逐维取较小分辨率（如质量档 1024x1760 vs 显存 low 512x768 → 512x768）。"""
    try:
        wa, ha = (int(x) for x in str(a).lower().split("x"))
        wb, hb = (int(x) for x in str(b).lower().split("x"))
    except (ValueError, AttributeError):
        return b or a
    return f"{min(wa, wb)}x{min(ha, hb)}"


def effective_sd_size(cfg: dict, category: str, manual_key: str) -> str:
    """SD/Comfy 线路的实际出图尺寸：手动指定 > 质量档（与显存档位取较小值）。"""
    manual = str(cfg.get(manual_key) or "").strip()
    if manual:
        return manual
    vram_key = "portrait" if category == "portrait" else "background"
    q = QUALITY_MODES[category][effective_quality(cfg, category)]
    v = VRAM_MODES[effective_vram_mode(cfg)][vram_key]
    return _smaller_size(q["size"], v)


def effective_steps_cap(cfg: dict, category: str) -> int:
    """质量档与显存档位中较小的步数封顶。"""
    q = QUALITY_MODES[category][effective_quality(cfg, category)]
    v = VRAM_MODES[effective_vram_mode(cfg)]
    return min(int(q["steps_cap"]), int(v["steps_cap"]))


def effective_vram_mode(cfg: dict) -> str:
    mode = str(cfg.get("vram_mode") or "mid").lower()
    if mode not in VRAM_MODES:
        mode = "mid"
    return mode


DEFAULT_PRESET = {
    "name": "默认（未识别模型）",
    "steps": 30,
    "cfg": 7.0,
    "sampler": "DPM++ 2M",
    "portrait_size": "1024x1760",
    "background_size": "768x512",
    "negative_extra": "",
    "enable_hr": False,
    "hr_upscaler": "Latent",
    "hr_scale": 2,
    "hr_steps": 10,
    "hr_denoise": 0.5,
}


def match_model_preset(checkpoint: str) -> dict:
    """按 checkpoint 名匹配预设（子串不区分大小写），未命中返回默认。"""
    ck = (checkpoint or "").lower()
    for key, preset in MODEL_PRESETS.items():
        if key in ck:
            return dict(DEFAULT_PRESET, **preset)
    return dict(DEFAULT_PRESET)


def is_wai_checkpoint(checkpoint: str) -> bool:
    """wai/illustrious 系判定（1.7.46 特适配开关用：配方/尺寸/ClipSkip/
    阶段词 wai 叠加层全部跟随）。"""
    ck = (checkpoint or "").lower()
    return "wai" in ck or "illust" in ck or "zuki" in ck


def wai_adapt_enabled(cfg: dict, checkpoint: str = "") -> bool:
    """wai 特适配总开关（config.image.wai_adapt，默认开；只有 wai 系模型生效）。"""
    try:
        if not bool(cfg.get("image", {}).get("wai_adapt", True)):
            return False
    except Exception:
        return False
    return is_wai_checkpoint(checkpoint or str(cfg.get("image", {}).get("model") or ""))


def best_chain_steps(cfg: dict, cap: int | None = None) -> int:
    """链式 img2img 的自主最佳步数（1.7.43）：img2img 底图已带结构，
    不需要全量步数——预设步数基础上减 4（保细节下限 18，显存/质量档
    cap 封顶）。模型预设（如 wai 30）→ 链式自动 26。"""
    try:
        ck = str(cfg.get("model") or "")
        preset = match_model_preset(ck)
        base = int(preset.get("steps", 30))
    except Exception:
        base = 30
    if cap:
        base = min(base, int(cap))
    return max(18, base - 4)




_RMBG_SESSION = None
_RMBG_LOCK = None


def _get_rmbg_session():
    """rembg 会话单例：176MB 模型只加载一次（此前每张立绘都重新加载，
    导致多任务时事件循环被反复阻塞、NPC/新角色立绘卡死）。"""
    global _RMBG_SESSION, _RMBG_LOCK
    if _RMBG_SESSION is not None:
        return _RMBG_SESSION
    import threading
    if _RMBG_LOCK is None:
        _RMBG_LOCK = threading.Lock()
    with _RMBG_LOCK:
        if _RMBG_SESSION is None:
            from rembg import new_session
            _RMBG_SESSION = new_session("u2net")
    return _RMBG_SESSION


class ImageGenError(Exception):
    pass


# 全局 SD WebUI 生成信号量：所有任务（无论哪个 ImageClient 实例）共享，
# 真正串行请求——避免多任务并发排队导致 A1111 内部队列堆积/超时卡死
_GLOBAL_SD_SEM = None
# 背景/CG 独立生成链：立绘（1024x1760 大图）排长队时不再挤占背景/CG，
# 两条链各自串行、互不阻塞——「CG/背景没请求到 SD」的感知多数来自被
# 立绘长队列压住（无提交日志时像没发请求）。
_GLOBAL_BG_SEM = None
# CG 独享一条生成链（1.6.95）：CG 使用比普通背景更大的画布（16:9 宽幅），
# 单独串行防止与背景/立绘并发把显存顶爆。
_GLOBAL_CG_SEM = None


def _get_sd_sem():
    global _GLOBAL_SD_SEM
    if _GLOBAL_SD_SEM is None:
        _GLOBAL_SD_SEM = asyncio.Semaphore(1)
    return _GLOBAL_SD_SEM


def _get_bg_sem():
    global _GLOBAL_BG_SEM
    if _GLOBAL_BG_SEM is None:
        _GLOBAL_BG_SEM = asyncio.Semaphore(1)
    return _GLOBAL_BG_SEM


def _get_cg_sem():
    global _GLOBAL_CG_SEM
    if _GLOBAL_CG_SEM is None:
        _GLOBAL_CG_SEM = asyncio.Semaphore(1)
    return _GLOBAL_CG_SEM


def _hi_res_size(spec: str, max_pixels: int = 4624220) -> str:
    """超高清尺寸（1.7.41）：基准尺寸长宽×2，超出图方舟面积上限（约
    4.62MP，实测 2048x3520 400、1728x2560/2048x2048 可）时等比缩回。
    例：1024x1760 → 1728x2560（4.42MP）；1024x1024 → 2048x2048。"""
    try:
        w, h = (int(x) for x in str(spec).lower().split("x")[:2])
    except (ValueError, AttributeError):
        return "1024x1024"
    w, h = w * 2, h * 2
    if w * h > max_pixels:
        scale = (max_pixels / (w * h)) ** 0.5
        w = max(512, int(w * scale // 16 * 16))
        h = max(512, int(h * scale // 16 * 16))
    return f"{w}x{h}"


def _caption_word_hits(caption: str, words: list[str]) -> list[str]:
    """interrogate 打标（自由英文文本）vs 词集匹配：词边界 + 容忍常见词尾
    （s/es/d/ed/ing——"girls"/"growing" 也能命中 "girl"/"grow"）。"""
    cap = (caption or "").lower()
    hits = []
    for w in words:
        if not w:
            continue
        if re.search(r"\b" + re.escape(w) + r"(?:s|es|d|ed|ing)?\b", cap):
            hits.append(w)
    return hits


def tsf_stage_score(caption: str, feat: dict, nude_ok: bool = False) -> float:
    """转变判定打分（1.7.32 转变技能内建的核心判据）：
    成品图经 A1111 interrogate 打标后，与**该段特征词集**比对——
    core（该段核心特征）命中 +3、expected（应出现特征）+1、
    forbidden（该段不应出现——阶段错位/跳变/裸体）每个 -8。
    nude_ok=False（默认）：forbidden 补「裸体」词——着装保护（1.6.89 用户
    规则：默认穿着，只有明确裸体/内衣需求才放开）；多候选挑最高分即
    「游戏内部自动挑转变正确 + 着装正确」的种子。"""
    score = 0.0
    score += 3.0 * len(_caption_word_hits(caption, feat.get("core") or []))
    score += 1.0 * len(_caption_word_hits(caption, feat.get("expected") or []))
    forb = list(feat.get("forbidden") or [])
    if not nude_ok:
        forb += ["naked", "nude", "topless", "breast"]
    score -= 8.0 * len(_caption_word_hits(caption, forb))
    return score


def _rough_mean_color(data: bytes) -> tuple[float, float, float] | None:
    """成品图主体平均色（alpha≥190 像素；抠图后透明背景不参与）。
    转变判定器做「段间同人」软加分：阶段递进发色/瞳色/服装同锚时，
    主色差应小——色差大（换人/漂色）则压低候选分。"""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((64, 96))
        px = list(im.getdata())
        sel = [(r, g, b) for r, g, b, a in px if a >= 190]
        if len(sel) < 50:
            return None
        n = len(sel)
        return (sum(p[0] for p in sel) / n,
                sum(p[1] for p in sel) / n,
                sum(p[2] for p in sel) / n)
    except Exception:
        return None


def _color_dist(a: tuple[float, float, float],
                b: tuple[float, float, float]) -> float:
    return sum(((x - y) ** 2 for x, y in zip(a, b))) ** 0.5


def _hair_mean_color(data: bytes) -> tuple[float, float, float] | None:
    """立绘发区（顶部 30% 高 × 中央 94% 宽）主体平均色——段间「发色统一」
    的度量位（1.7.32 发色漂移修复）。未抠图原图背景为浅灰底：
    高亮度+低饱和像素（灰底）剔除后再平均。"""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((96, 140))
        w, h = im.size
        px = list(im.getdata())
        sel = []
        for y in range(0, int(h * 0.30)):
            row = y * w
            for x in range(int(w * 0.03), int(w * 0.97)):
                r, g, b = px[row + x]
                mx, mn = max(r, g, b), min(r, g, b)
                sat = (mx - mn) / 255.0
                if mx / 255.0 > 0.88 and sat < 0.10:
                    continue  # 浅灰纯色背景
                sel.append((r, g, b))
        if len(sel) < 40:
            return None
        n = len(sel)
        return (sum(p[0] for p in sel) / n,
                sum(p[1] for p in sel) / n,
                sum(p[2] for p in sel) / n)
    except Exception:
        return None


def _hair_shift_penalty(a: tuple[float, float, float],
                        b: tuple[float, float, float]) -> float:
    """发区色差罚分：0.15 内（同色系）不罚，0.5+ 满罚 8 分——多候选挑优
    时自动淘汰「发色漂移/换人」的候选（0 极差=441 归一化）。"""
    d = _color_dist(a, b) / 441.0
    if d <= 0.15:
        return 0.0
    return min(8.0, (d - 0.15) / 0.35 * 8.0)


def _validate_url(url: str, allow_local: bool) -> None:
    """出站请求地址校验：仅允许 http/https；默认拒绝本地/内网/保留地址（防 SSRF）。

    allow_local=True 仅供用户显式选择的本地后端（如 SD WebUI）使用。
    """
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ImageGenError(f"仅支持 http/https 地址，当前为：{parsed.scheme or '空'}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ImageGenError("无效的服务地址")
    if allow_local:
        return
    try:
        addr = ipaddress.ip_address(host)
        local = (addr.is_loopback or addr.is_private or addr.is_reserved
                 or addr.is_unspecified or addr.is_link_local or addr.is_multicast)
    except ValueError:
        local = host.lower() in ("localhost",) or host.lower().endswith(".local")
    if local:
        raise ImageGenError("该生图服务类型不允许使用本地/内网地址；本地整合包请将服务类型切换为 SD WebUI")


def _hash_key(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def gen_params_tag(cfg: dict) -> str:
    """生成参数签名：步数/CFG/采样器/HR 方案变化时自动作废旧图缓存。

    预览参数与出图质量强相关；若缓存键只含提示词，改参数后仍会命中旧图，
    用户会误以为修改无效。此签名与 PROMPT_VERSION 一起拼进 asset_key。
    """
    try:
        ic = cfg.get("image", {})
        meta = {
            "steps": ic.get("sd_steps", ""),
            "cfg": ic.get("sd_cfg", ""),
            "sampler": ic.get("sd_sampler", ""),
            "sched": ic.get("sd_scheduler", ""),
            "vram": ic.get("vram_mode", ""),
            "pq": ic.get("portrait_quality", ""),
            "bq": ic.get("background_quality", ""),
        }
        preset = None
        try:
            preset = match_model_preset(str(ic.get("model") or ""))
        except Exception:
            preset = None
        if preset:
            meta.update({
                "hr": preset.get("enable_hr"),
                "hru": preset.get("hr_upscaler"),
                "hrs": preset.get("hr_steps"),
                "hrd": preset.get("hr_denoise"),
            })
        return "-".join(f"{k}={meta[k]}" for k in meta)
    except Exception:
        return "params-unknown"


def white_to_transparent(img: Image.Image, threshold: int = 238, feather: int = 1) -> Image.Image:
    """把接近白色的像素转透明（简单立绘抠白，含边缘羽化）。"""
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if r >= threshold and g >= threshold and b >= threshold:
                px[x, y] = (r, g, b, 0)
            elif r >= threshold - feather and g >= threshold - feather and b >= threshold - feather:
                # 边缘半透明，柔化白边
                m = min(r, g, b)
                alpha = int(255 * (threshold - m) / feather)
                px[x, y] = (r, g, b, max(0, min(255, alpha)) * a // 255)
    return img


# 立绘/背景的英文技术指令：保证「角色-背景可区分」与可抠图性（对 SD 类模型尤其有效）
PORTRAIT_EN_SUFFIX = (
    ", (solo:1.45), (one person only:1.4), (single character:1.4), "
    "only one single character in the whole image, "
    "isolated single character illustration, full body standing pose, "
    "head to toe complete figure, entire character fully inside frame, "
    "eye-level camera angle, stable consistent character design, "
    "centered composition, reasonable framing distance, "
    # 1.7.42 日式精致插画立绘风（API/SD 通用）：visual novel key art /
    # cel shading / 细腻线稿与光影——把「角色插画立绘」气质钉得更精致
    "anime visual novel character illustration, polished anime key art, "
    "clean cel shading, delicate refined lineart, soft airbrushed shading, "
    "luminous detailed eyes with sparkling highlights, flowing hair strands, "
    "elegant color palette, refined polished finish, "
    "(facing camera:1.35), (looking at viewer:1.3), (front view:1.25), "
    "direct eye contact, gaze at the lens, facing the viewer, "
    "(full body head to toe:1.3), (entire figure inside frame:1.25), "
    "sharp focus, crisp details, natural skin texture, high quality, "
    "plain solid light gray background (#DCDCDC), seamless gray studio "
    "backdrop, consistent flat gray backdrop, (plain flat background:1.2), "
    "no background scenery, no shadow on background"
)
# 自动附加的负面提示词：强力压制「一张图画出多个角色」的常见失败模式
PORTRAIT_NEG_EN = (
    "legs only, lower body only, partial body shot, extreme closeup, "
    "zoomed in crop, character head out of frame, torso missing, "
    "legs focus, thigh focus, foot focus, limb focus, waist-up only, "
    "upper body only, neck down, cropped half body, half body, "
    "back view, viewed from behind, back turned, facing away, "
    "side profile, looking away, turn away, eyes closed, closed eyes, "
    "looking down, looking off camera, "
    "out of focus, motion blur, blurry, lowres, low quality, jpeg artifacts, "
    "mask, veil, face covering, helmet, blindfold, faceless, covered face, "
    # 1.7.37 多人物负面向加权（模型把「转变中的同一人」具象化为两人并排——
    # 上周实机连续两张双人同框（裸体版/双人着装版））
    "(two people:1.35), (multiple people:1.35), (multiple characters:1.3), "
    "(2girls:1.35), (2boys:1.35), (1girl 1boy:1.35), (twins:1.35), "
    "(standing together:1.3), multiple people, multiple characters, 2girls, "
    "2boys, 1girl 1boy, twins, "
    "couple, group, crowd, side by side, standing side by side, "
    "duplicate, cloned character, "
    "mirrored character, multi-panel, multiple views, comic panels, split image, "
    "cropped, out of frame, cut off, partial body, head out of frame, "
    "legs cut off, face cropped, top of head cut, waist-up only, "
    "perspective error, warped anatomy, extra limbs, missing limbs, "
    "floating elements, disconnected parts, color shift, oversaturated colors, "
    "blurry face, low detail, deformed hands, bad proportions, "
    "background scenery, gradient background, complex background, "
    "white-on-white, washed out, faded, low contrast, "
    "text, watermark, shadow on background"
)

# ---- 差分铁律（1.6.72）----
# 同一角色多表情/多阶段差分的锁定词（generate_portrait 统一注入）：

# 正面锁定：同人同设计（发型/发色/服装/帽饰/配件不漂移）——表情差分「同一个人换表情」；
# keep 词按网络性别转变 AI 绘画教学补充（same eye shape / nose shape / facial
# proportions / recognizable identical person——写实保持「同一人」的最关键项）
DIFF_LOCK_EN = (
    ", identical hairstyle, identical hair color, identical outfit, "
    "identical headwear, identical accessories, identical clothing design, "
    "consistent uniform, same character design, same identity, "
    "same eye shape, same nose shape, same facial proportions, "
    "recognizable identical person"
)
# 负面封杀：换发型/换发色/换装/换帽饰（精简版——负面堆太多会压垮构图，
# 反而产出过曝/空白废图；只保留最关键的指向性词）
DIFF_LOCK_NEG = (
    "different hairstyle, hair changed, hair color change, "
    "different outfit, different hat, different headwear"
)
# 眼部精致无差错（恒定正面 + 最小负面）
EYE_LOCK_EN = (
    ", intricate iris detail, sparkling eye highlights, crisp eyelashes, "
    "clear defined pupils, highly detailed eyes, beautiful detailed eyes"
)
EYE_NEG = "cross-eyed, mismatched eyes"
# 单眼镜头/单片镜类道具（模型把「眼」相关词自由发挥成镜头状饰物——实测常见，
# 恒定负面封杀；设定要「眼罩/单片镜/镜片」的除外在 hints 例外）
EYE_PROP_NEG = (
    "monocle, eyepatch, goggle over one eye, single lens, circular lens, "
    "glass lens over eye, camera-lens eye, lens on face, one-eyed lens"
)
# 相机/手机道具：设定未提「摄影/拍照」时禁止（曾致差分随机出相机——
# 1.7.33 用户反馈「大部分角色都没相机设定却随机生成相机」：全量/换装图
# 相机伪影是该模型顽疾——负面前置+加权压制；设定明确要相机的才摘除
# 并补正面相机词）
CAPTURE_NEG = (
    "(camera:1.4), (film camera:1.3), camera prop, holding camera, "
    "taking photograph, photographing, photographing equipment, "
    "smartphone in hand, holding phone, selfie pose, camera strap, "
    "vintage camera, photography equipment, unrelated props"
)
# 设定明确含相机/摄影时注入的正面词（负面摘除后仍往特征方向引导）
CAPTURE_EN = "holding a vintage film camera, taking photos"
# 手部压头姿势（最小集）：脸被手挡/头顶手位构图失稳
HAND_POSE_NEG = (
    "hand over head, hand covering face, hand blocking eye"
)

BACKGROUND_EN_SUFFIX = (
    ", scenery only, no humans, no characters, no people, "
    "wide establishing shot, empty landscape"
)

# イベントCG 专用（galgame 概念：关键剧情节点的一枚完整插画——必然包含
# 角色演出 + 横版构图 + 氛围光影，与背影管线「no humans」本质相反）
CG_EN_SUFFIX = (
    ", (cinematic event CG illustration:1.3), (character showcase:1.25), "
    "(16:9 film composition:1.2), dramatic composition, depth of field, "
    "cinematic lighting, emotional story moment, "
    "characters acting in the scene, detailed background scenery, "
    "high quality visual novel key visual"
)
CG_NEG = (
    "blurry, low quality, jpeg artifacts, watermark, text, "
    "deformed face, bad anatomy, extra limbs, missing limbs, "
    "caption, logo, border, frame"
)


def _suppress_shadow(src: Image.Image, alpha: Image.Image,
                     bg_rgb: tuple, max_depth: int = 10) -> None:
    """接触阴影侵蚀：从已透明边界向内 BFS，蚕食符合阴影色（低饱和、亮度略低于
    背景）的像素。SD 白底图常在角色脚下生成灰色投影，泛洪因色差无法穿过它们。

    只处理与透明区连通的阴影色像素，角色内部深色衣物不受影响。
    """
    from collections import deque

    from PIL import ImageChops

    w, h = src.size
    sp = src.load()
    ap = alpha.load()
    bg_lum = sum(bg_rgb) / 3

    zero = alpha.point(lambda v: 255 if v == 0 else 0)
    band = ImageChops.subtract(zero.filter(ImageFilter.MaxFilter(3)), zero)
    q2 = deque()
    depth = {}
    for i, v in enumerate(band.getdata()):
        if v:
            q2.append(((i % w, i // w), 0))
    # 从透明边界向内 BFS，带深度限制：阴影厚 10px 内可除，浅色衣物
    # 只损失贴边十余像素、内部完整（先前整件浅色衣物被吞的问题根源）
    while q2:
        (x, y), dep = q2.popleft()
        if dep > max_depth:
            continue
        if ap[x, y] == 0:
            continue
        r, g, b = sp[x, y][:3]
        lum = (r + g + b) / 3
        sat = max(r, g, b) - min(r, g, b)
        if sat < 26 and bg_lum - 55 < lum < bg_lum + 8:
            ap[x, y] = 0
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if (0 <= nx < w and 0 <= ny < h and ap[nx, ny] != 0
                        and (nx, ny) not in depth):
                    depth[(nx, ny)] = dep + 1
                    q2.append(((nx, ny), dep + 1))


def _strip_frame(mask: Image.Image):
    """检测并裁掉贴边的装饰边框（金色雕花相框等）。

    从每条边向内扫描：跳过「贴边的高不透明条带」（边框）与其后的透明间隙，
    停在角色内容之前。边框宽度自适应（上限 22%）；没有边框的图不受影响。
    返回小图坐标下的裁剪框 (left, top, right, bottom)；无需裁剪返回 None。
    """
    w, h = mask.size
    px = mask.load()

    def band_opaque(idx, vertical):
        if vertical:
            y0, y1 = int(h * 0.2), int(h * 0.8)
            vals = [px[idx, y] for y in range(y0, y1, 2)]
        else:
            x0, x1 = int(w * 0.2), int(w * 0.8)
            vals = [px[x, idx] for x in range(x0, x1, 2)]
        return sum(1 for v in vals if v > 0) / max(1, len(vals))

    def scan(fn, length, vertical):
        limit = int(length * 0.22)
        i = 0
        while i < limit and fn(i, vertical) > 0.55:
            i += 2
        if i <= 2:
            return 0  # 该边没有贴边条带
        # 跳过边框后的透明间隙（最多 10%），无间隙则只裁边框本身
        j = i
        gap_limit = i + int(length * 0.10)
        while j < gap_limit and fn(j, vertical) < 0.25:
            j += 2
        return j if j > i else i

    left = scan(band_opaque, w, True)
    right = w - 1 - scan(lambda i, v: band_opaque(w - 1 - i, v), w, True)
    top = scan(band_opaque, h, False)
    bottom = h - 1 - scan(lambda i, v: band_opaque(h - 1 - i, v), h, False)
    if left >= right - w * 0.3 or top >= bottom - h * 0.3:
        return None  # 扫描异常，不裁
    if left == 0 and top == 0 and right >= w - 1 and bottom >= h - 1:
        return None  # 四边都无边框
    return (left, top, right + 1, bottom + 1)


def _strip_gold_band(rgb_img: Image.Image, mask: Image.Image) -> None:
    """把「贴边的金色像素」从前景中抹除（装饰金框的最后一个顽疾）。

    背景被泛洪清除后，金框往往与人物连通（手臂/发丝贴到框上），中心连通块
    保主会把它当作人物一部分保留。金色相特征（R>G>B，红蓝差大）与金发接近，
    这里限定为「距画面边缘 ≤ 9% 的真贴边像素」：金框环贴边、必中；
    中央金发、外围非贴边的金色装饰不受影响。
    """
    px = rgb_img.load()
    m = mask.load()
    w, h = mask.size
    edge_limit = max(28, int(min(w, h) * 0.05))
    for y in range(h):
        for x in range(w):
            if m[x, y] == 0:
                continue
            if min(x, w - 1 - x, y, h - 1 - y) > edge_limit:
                continue
            r, g, b = px[x, y][:3]
            if (r > 150 and g > 100 and b < 100 and r - b > 60
                    and g - b > 40 and r > g):
                m[x, y] = 0








def _ai_speckle_clean(img: Image.Image, min_ratio: float = 0.008) -> Image.Image:
    """AI 抠图结果清理：移除与主体不连通的独立碎块（白屑/残片）。

    - 独立块面积 < 主块 0.8% 且距主块 >3px → 透明（白屑）
    - 距主块 ≤3px 的连接件/挂件完全保留
    """
    from collections import deque
    img = img.convert("RGBA")
    alpha = img.getchannel("A")
    w, h = img.size
    m = alpha.load()
    visited = bytearray(w * h)
    comps = []
    for sy in range(h):
        for sx in range(w):
            if m[sx, sy] < 40 or visited[sy * w + sx]:
                continue
            q = deque([(sx, sy)])
            visited[sy * w + sx] = 1
            cells = []
            while q:
                cx, cy = q.popleft()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and m[nx, ny] >= 40 and not visited[ny * w + nx]:
                        visited[ny * w + nx] = 1
                        q.append((nx, ny))
            comps.append(cells)
    if not comps:
        return img
    main = max(comps, key=len)
    limit = max(4, int(len(main) * min_ratio))

    def near_main(cells, rr=3):
        for cx, cy in cells:
            x0, x1 = max(0, cx - rr), min(w, cx + rr + 1)
            y0, y1 = max(0, cy - rr), min(h, cy + rr + 1)
            if any(m[xx, yy] >= 40 for yy in range(y0, y1) for xx in range(x0, x1)):
                return True
        return False

    for cells in comps:
        if len(cells) < limit and not near_main(cells):
            for cx, cy in cells:
                m[cx, cy] = 0
    img.putalpha(alpha)
    return img


def _ai_white_fringe_clean(img: Image.Image) -> Image.Image:
    """AI 抠图后处理：清除人物轮廓外的「白雾边」。

    rembg 的半透明边缘常混入白背景色（R/G/B 均高 + alpha 半透），
    在人物轮廓外形成一圈白雾。处理规则：
    - 白色半透明（alpha<235 且 min(rgb)>190）像素：按白度衰减 alpha，
      白雾被压掉，留下平滑半透过渡；
    - 不透明实色（alpha>=235，白色衣服/配件）完全不动；
    - 最后轻羽化让边缘自然。
    """
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a >= 235:
                continue
            if min(r, g, b) > 235:
                # 仅「接近纯白」的雾像素才衰减；浅色衣物/肤色高光（190-235）
                # 完全保护——否则米色衣裤边缘会被啃成锯齿
                whiteness = min(1.0, (min(r, g, b) - 235) / 20.0)
                px[x, y] = (r, g, b, max(0, int(a * (1.0 - whiteness))))
    alpha = img.getchannel("A").filter(ImageFilter.GaussianBlur(0.6))
    img.putalpha(alpha)
    return img


def _is_uniform_bg_block(rgb_img: Image.Image, cells, med: tuple) -> bool:
    """判定连通块是否为「无纹理的近背景色大块」（发丝环包围的封闭背景岛）。

    背景岛特征：平均色接近边缘背景色、且内部颜色高度均匀（标准差小）。
    白色服装有褶皱/阴影纹理，标准差明显更大，不受影响。
    """
    import statistics
    if len(cells) < 64:
        return False
    rs, gs, bs = [], [], []
    for cx, cy in cells:
        r, g, b = rgb_img.getpixel((cx, cy))
        rs.append(r); gs.append(g); bs.append(b)
    mr, mg, mb = statistics.mean(rs), statistics.mean(gs), statistics.mean(bs)
    if abs(mr - med[0]) + abs(mg - med[1]) + abs(mb - med[2]) > 16:
        return False
    dev = (statistics.pstdev(rs) + statistics.pstdev(gs) + statistics.pstdev(bs)) / 3
    # 近纯色（无纹理）才算背景岛；服装褶皱/阴影使标准差明显更大
    return dev < 2.5


def _drop_speckles_small(mask: Image.Image, min_ratio: float = 0.0015) -> None:
    """保守模式唯一清理：清除面积 < 0.15% 的孤立小碎片（噪点/飞溅点）。

    配件（肩饰/佩剑/飘带/挂件）面积远大于此阈值，绝不会被误删；
    背景已被泛洪清除，保配件优先于清残余。
    """
    from collections import deque
    w, h = mask.size
    px = mask.load()
    visited = bytearray(w * h)
    limit = max(2, int(w * h * min_ratio))
    for sy in range(h):
        for sx in range(w):
            if px[sx, sy] == 0 or visited[sy * w + sx]:
                continue
            q = deque([(sx, sy)])
            visited[sy * w + sx] = 1
            cells = []
            while q:
                cx, cy = q.popleft()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and px[nx, ny] and not visited[ny * w + nx]:
                        visited[ny * w + nx] = 1
                        q.append((nx, ny))
            if len(cells) < limit:
                for cx, cy in cells:
                    px[cx, cy] = 0


def _keep_main_blobs(mask: Image.Image, keep_ratio: float = 0.05,
                     rgb_img: Image.Image | None = None,
                     med: tuple = (0, 0, 0), deep: bool = True) -> Image.Image:
    """只保留主体：以「与画面中央区域相交的连通块」为主体（人物必居中），
    清除其余所有连通块——金框环、渐变残环、水印、噪点碎片，无论其面积
    多大（这是泛洪后最可靠的通用去残手段）。主体的小伴生块保留。

    keep_ratio: 伴生块面积 ≥ 主体块 × keep_ratio 时保留。
    """
    from collections import deque

    w, h = mask.size
    px = mask.load()
    visited = bytearray(w * h)
    comps = []
    for sy in range(h):
        for sx in range(w):
            if px[sx, sy] == 0 or visited[sy * w + sx]:
                continue
            q = deque([(sx, sy)])
            visited[sy * w + sx] = 1
            cells = []
            while q:
                cx, cy = q.popleft()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and px[nx, ny] and not visited[ny * w + nx]:
                        visited[ny * w + nx] = 1
                        q.append((nx, ny))
            comps.append(cells)
    if not comps:
        return mask

    # 中央判定区（画面 20%~80% 带）：人物必然覆盖这里
    cx0, cy0, cx1, cy1 = int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8)

    def touches_center(cells):
        return any(cx0 <= x < cx1 and cy0 <= y < cy1 for x, y in cells)

    body = max((c for c in comps if touches_center(c)), key=len, default=None)
    if body is None:
        # 中央没有前景（人物极偏/异常），退回最大块
        body = max(comps, key=len)
    # 邻域伴生判定：主体向外膨胀 8px，任何与之邻近的块都算角色的
    # 配件（肩饰、佩剑、飘带、垂发），不因「+断桥/贴边」而误删
    neighborhood = [0] * (w * h)
    from collections import deque as _dq
    q = _dq()
    bl = body
    cells_fast = set(bl)
    for (cx, cy) in bl:
        neighborhood[cy * w + cx] = 1
        q.append((cx, cy))
    R = 8
    while q:
        cx, cy = q.popleft()
        for nx in range(max(0, cx - 1), min(w, cx + 2)):
            for ny in range(max(0, cy - 1), min(h, cy + 2)):
                if neighborhood[ny * w + nx] == 0:
                    if nx <= cx + 1 and ny <= cy + 1 and abs(nx - cx) + abs(ny - cy) <= 1:
                        neighborhood[ny * w + nx] = 1
                        q.append((nx, ny))
    # 简化：直接保留「触碰中央区」或「距主体 ≤R」的块
    def near_body(cells, rr=R):
        for cx, cy in cells:
            x0, x1 = max(0, cx - rr), min(w, cx + rr + 1)
            y0, y1 = max(0, cy - rr), min(h, cy + rr + 1)
            for yy in range(y0, y1):
                for xx in range(x0, x1):
                    if neighborhood[yy * w + xx]:
                        return True
        return False

    threshold = max(48, int(len(body) * keep_ratio))
    keep_ids = set()
    for cells in comps:
        if cells is body:
            keep_ids.add(id(cells))
        elif near_body(cells):
            # 邻域伴生（配件）保留；deep 模式下删除无纹理的封闭背景岛
            if (not deep) or rgb_img is None or                     not _is_uniform_bg_block(rgb_img, cells, med):
                keep_ids.add(id(cells))
        elif touches_center(cells):
            keep_ids.add(id(cells))
        elif len(cells) >= threshold:
            # 与主体不相邻的大块：仅当其不包围中央时保守保留
            if any(cx0 <= x < cx1 and cy0 <= y < cy1 for x, y in cells):
                keep_ids.add(id(cells))
    for cells in comps:
        if id(cells) not in keep_ids:
            for cx, cy in cells:
                px[cx, cy] = 0
    return mask


def _fallback_white_cut(src: Image.Image) -> Image.Image:
    """回退路径（复杂/渐变背景、或泛洪疑似吞人时）：近白全局透明 + 精修。

    相比旧的单纯白阈值：增加主体连通块清理（去碎片）、贴边收缩（消 3~8px
    白晕残留带）、发丝区二次透明、以及角色包围盒裁剪。
    """
    from PIL import ImageChops

    out = white_to_transparent(src, threshold=228, feather=6)
    w, h = out.size
    alpha = out.getchannel("A")

    # 主体连通块清理（1/3 分辨率跑，快）
    m = alpha.resize((max(32, w // 3), max(32, h // 3)), Image.BILINEAR)
    m = m.point(lambda v: 255 if v > 96 else 0)
    _keep_main_blobs(m)
    m = m.resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(2))
    m = m.point(lambda v: 255 if v > 127 else 0)

    alpha = ImageChops.multiply(alpha, m)
    # 贴边收缩：吃掉紧贴轮廓的灰白残留带，再柔化
    alpha = alpha.filter(ImageFilter.MinFilter(5))
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.2))
    out.putalpha(alpha)

    bbox = alpha.getbbox()
    if bbox:
        margin = int(max(w, h) * 0.03)
        box = (max(0, bbox[0] - margin), max(0, bbox[1] - margin),
               min(w, bbox[2] + margin), min(h, bbox[3] + margin))
        out = out.crop(box)
    return out


def _refine_and_defringe(src: Image.Image, alpha: Image.Image) -> Image.Image:
    """边缘带精修 v3.2：更宽的精修带 + 接触阴影抑制 + 去白边（defringe）。

    - 按「前景色/背景色代表模型」的比例距离重估边缘 alpha，发丝衣缘更干净；
    - 白底上的灰色接触阴影（低饱和、亮度略低于背景）被压至近全透明；
    - defringe：把混合了背景色的边缘颜色按不透明度推回纯前景色，消除白晕。
    """
    w, h = src.size
    sp = src.load()
    ap = alpha.load()

    bg_acc = [0, 0, 0, 0]
    fg_acc = [0, 0, 0, 0]
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            r, g, b = sp[x, y][:3]
            a = ap[x, y]
            if a < 8:
                for i, v in enumerate((r, g, b)):
                    bg_acc[i] += v
                bg_acc[3] += 1
            elif a > 247:
                for i, v in enumerate((r, g, b)):
                    fg_acc[i] += v
                fg_acc[3] += 1
    if bg_acc[3] < 4 or fg_acc[3] < 4:
        return alpha
    bg = tuple(bg_acc[i] / bg_acc[3] for i in range(3))
    fg = tuple(fg_acc[i] / fg_acc[3] for i in range(3))
    bg_lum = sum(bg) / 3

    for y in range(h):
        for x in range(w):
            a = ap[x, y]
            if a < 3 or a > 252:
                continue
            r, g, b = sp[x, y][:3]
            # 粉光保护：粉色柔光边缘（r>g>b、红蓝差显著）是刻意生成的轮廓光，
            # 保留颜色与透明度，不做 defringe 推色（否则柔光会被误当白晕吃掉）
            if r > 175 and r - b >= 30 and g - b >= 12 and r >= g:
                ap[x, y] = max(a, 96)
                continue
            d_bg = abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2])
            d_fg = abs(r - fg[0]) + abs(g - fg[1]) + abs(b - fg[2])
            t = d_bg / (d_bg + d_fg + 1e-6)
            # 接触阴影抑制：低饱和 + 亮度在背景附近的暗色 → 大幅透明
            lum = (r + g + b) / 3
            sat = max(r, g, b) - min(r, g, b)
            if sat < 26 and bg_lum - 72 < lum < bg_lum + 14:
                t *= 0.22
            if t <= 0.04:
                ap[x, y] = 0
            elif t >= 0.96:
                ap[x, y] = 255
            else:
                na = max(2, min(249, int(255 * t + 0.5)))
                # defringe：un-premultiply 推回纯前景色
                f = 255.0 / na
                sp[x, y] = (
                    max(0, min(255, int(bg[0] + (r - bg[0]) * f))),
                    max(0, min(255, int(bg[1] + (g - bg[1]) * f))),
                    max(0, min(255, int(bg[2] + (b - bg[2]) * f))),
                    na,
                )
    return alpha




def cut_out(img: Image.Image, mode: str = "auto") -> Image.Image:
    """四种抠图标准，玩家可在设置中选择其一用于立绘：

    - auto/standard(默认): 保守管线——保配件优先（宁可留边沿）
    - precise: 最严格去背景（含金色贴边剥离/封闭背景岛清除，边缘更硬）
    - standard: 经典泛洪+保主+边缘精修（速度与效果平衡）
    - fast: 近白全局透明+包围盒裁剪（最快，浅色服饰场景慎用）
    """
    mode = (mode or "auto").lower()
    if mode == "fast":
        out = white_to_transparent(img, threshold=228, feather=4)
        bbox = out.getchannel("A").getbbox()
        if bbox:
            w, h = out.size
            margin = int(max(w, h) * 0.03)
            box = (max(0, bbox[0] - margin), max(0, bbox[1] - margin),
                   min(w, bbox[2] + margin), min(h, bbox[3] + margin))
            out = out.crop(box)
        return out
    if mode == "ai":
        # AI 智能抠图（rembg·U2-Net）：专业前景分割，配件与浅色边缘判断准确；
        # 失败时自动降级保守模式，绝不中断。
        # 会话单例 + 线程池执行（不阻塞事件循环，多立绘任务不再卡死）
        try:
            from rembg import remove as _rmbg
            session = _get_rmbg_session()
            result = _rmbg(img.convert("RGBA"), session=session)
            out = result.convert("RGBA")
            out = _ai_white_fringe_clean(out)
            out = _ai_speckle_clean(out)
            bbox = out.getchannel("A").getbbox()
            if bbox:
                ww, hh = out.size
                margin = int(max(ww, hh) * 0.03)
                box = (max(0, bbox[0] - margin), max(0, bbox[1] - margin),
                       min(ww, bbox[2] + margin), min(hh, bbox[3] + margin))
                out = out.crop(box)
            return out
        except Exception as e:
            print(f"[cutout] AI 抠图不可用，降级保守模式: {e}")
            return remove_background(img, deep=False)
    if mode in ("standard", "auto", "conservative"):
        # 保守模式（默认）：连通背景清除 + 极小碎片清理，绝不侵蚀配件
        return remove_background(img, deep=False)
    if mode == "precise":
        # 严格模式：含金框剥离/背景岛/边缘精修（已知会误删部分配件，慎用）
        return remove_background(img, deep=True)
    # auto 兜底：一律保守（配件优先）
    return remove_background(img, deep=False)



def remove_background(img: Image.Image, deep: bool = True) -> Image.Image:
    """内置自动抠图 v3（通用立绘抠图）。

    流程：降采样 → 边缘背景色估计（自适应容差）→ 多种子泛洪标记连通背景 →
    连通碎片清理 → 蒙版放大 + 边缘形态学 → 边缘带 alpha 重估与去白边 →
    裁剪到角色包围盒。只清除与画面边缘连通的背景，不误删角色浅色部分；
    背景无法区分时回退近白全局透明。
    """
    import statistics

    src = img.convert("RGBA")
    w, h = src.size
    if w < 8 or h < 8:
        return src

    # 1) 降采样到最长边 ≤512 做泛洪（提速 + 蒙版放大后边缘更平滑）
    scale = min(1.0, 512 / max(w, h))
    fw, fh = max(32, int(w * scale)), max(32, int(h * scale))
    small = src.resize((fw, fh), Image.LANCZOS).convert("RGB")
    px = small.load()

    # 2) 边缘像素估计背景色与离散度，自适应容差
    step = max(1, min(fw, fh) // 24)
    border = []
    for x in range(0, fw, step):
        border.append(px[x, 0])
        border.append(px[x, fh - 1])
    for y in range(0, fh, step):
        border.append(px[0, y])
        border.append(px[fw - 1, y])
    med = tuple(int(statistics.median(c[i] for c in border)) for i in range(3))
    deviation = sum(sum(abs(c[i] - med[i]) for i in range(3)) for c in border) / len(border)
    # 容差上限 72：过大的容差会把白背景上的浅色衣物一起吞掉
    if deviation < 6:
        # 近纯色背景（绝大多数立绘）：用紧凑阈值保护浅色服装（与其色差≥12）
        thresh = 11
    else:
        thresh = int(min(72, max(18, 34 + deviation * 1.8)))

    # 3) 多种子泛洪：画面边缘一圈 + 内缩 12% 一圈（跨过模型爱加的装饰边框）。
    #    内缩种子必须先校验颜色接近边缘背景色，避免种子恰好落在人物身上
    #    时把人物整体染成背景。
    work = small.copy()
    fill = (250, 0, 250)

    def run_flood(seed_list, ref_med, ref_thresh):
        for s in seed_list:
            if not (0 <= s[0] < fw and 0 <= s[1] < fh):
                continue
            if work.getpixel(s) == fill:
                continue
            if (abs(work.getpixel(s)[0] - ref_med[0])
                    + abs(work.getpixel(s)[1] - ref_med[1])
                    + abs(work.getpixel(s)[2] - ref_med[2])) > ref_thresh:
                continue
            try:
                ImageDraw.floodfill(work, s, fill, thresh=ref_thresh)
            except Exception:
                continue

    edge_seeds = [(1, 1), (fw - 2, 1), (1, fh - 2), (fw - 2, fh - 2)]
    for x in range(0, fw, max(1, fw // 10)):
        edge_seeds += [(x, 1), (x, fh - 2)]
    for y in range(0, fh, max(1, fh // 10)):
        edge_seeds += [(1, y), (fw - 2, y)]
    run_flood(edge_seeds, med, thresh)

    def count_removed():
        n = 0
        for yy in range(0, fh, 2):
            for xx in range(0, fw, 2):
                if work.getpixel((xx, yy)) == fill:
                    n += 1
        return n * 4  # 步长2采样还原总数

    # 3.5) 装饰边框会把边缘背景色估计污染成边框色（金色），导致框内真正的
    # 背景没被清除。若首轮清除不充分，用边框内侧的像素重新估计背景再泛洪。
    if count_removed() < fw * fh * 0.40:
        ix0, iy0 = int(fw * 0.16), int(fh * 0.16)
        inner = []
        for x in range(ix0, fw - ix0, max(1, fw // 14)):
            inner += [work.getpixel((x, iy0)), work.getpixel((x, fh - 1 - iy0))]
        for y in range(iy0, fh - iy0, max(1, fh // 14)):
            inner += [work.getpixel((ix0, y)), work.getpixel((fw - 1 - ix0, y))]
        inner = [c for c in inner if c != fill]
        if inner:
            med2 = tuple(int(statistics.median(c[i] for c in inner)) for i in range(3))
            dev2 = sum(sum(abs(c[i] - med2[i]) for i in range(3)) for c in inner) / len(inner)
            if dev2 < 6:
                thresh2 = 11
            else:
                thresh2 = int(min(72, max(18, 34 + dev2 * 1.8)))
            inner_seeds = []
            for x in range(ix0, fw - ix0, max(1, fw // 10)):
                inner_seeds += [(x, iy0), (x, fh - 1 - iy0)]
            for y in range(iy0, fh - iy0, max(1, fh // 10)):
                inner_seeds += [(ix0, y), (fw - 1 - ix0, y)]
            run_flood(inner_seeds, med2, thresh2)

    # 4) 背景蒙版（小分辨率）+ 覆盖率检查 + 碎片清理
    mask = Image.new("L", (fw, fh), 255)
    m = mask.load()
    removed = 0
    for yy in range(fh):
        for xx in range(fw):
            if work.getpixel((xx, yy)) == fill:
                m[xx, yy] = 0
                removed += 1
    total = fw * fh
    if removed > total * 0.90 or removed < total * 0.05:
        # 疑似吞人（浅色衣物连通背景）或背景无法区分：走升级版回退
        return _fallback_white_cut(src)

    # 4.5) 保守模式（deep=False，默认）：仅清理极小碎片，不做任何
    #      侵蚀性操作（无金框剥离/无断桥/无中心保主/无阴影侵蚀/无白边收缩）
    #      ——配件优先，即便留下立绘边沿也接受
    if not deep:
        _drop_speckles_small(mask)
        alpha = mask.resize((w, h), Image.BILINEAR)
        alpha = alpha.filter(ImageFilter.GaussianBlur(1.0))
        src.putalpha(alpha)
        bbox = alpha.getbbox()
        if bbox:
            margin = int(max(w, h) * 0.03)
            box = (max(0, bbox[0] - margin), max(0, bbox[1] - margin),
                   min(w, bbox[2] + margin), min(h, bbox[3] + margin))
            src = src.crop(box)
        return src

    # 4.5b) precise 模式才剥离贴边装饰边框（金色雕花相框等），同步裁剪原图
    fbox = _strip_frame(mask)
    if fbox and (fbox[2] - fbox[0]) >= fw * 0.35 and (fbox[3] - fbox[1]) >= fh * 0.35:
        rx, ry = w / fw, h / fh
        mask = mask.crop(fbox)
        fw, fh = mask.size
        ffull = (int(fbox[0] * rx), int(fbox[1] * ry),
                 int(fbox[2] * rx), int(fbox[3] * ry))
        src = src.crop(ffull)
        w, h = src.size

    # precise 模式：金色贴边剥离 + 形态学断桥 + 中心连通块保主
    if deep:
        _strip_gold_band(small, mask)
    # 断桥仅限「贴边 15% 外围带」：切断金框饰件与人物在画面边缘的细连接；
    # 画面中央的配件连接保持连通，不会被误伤
    bw_, bh_ = mask.size
    bx0, by0 = int(bw_ * 0.15), int(bh_ * 0.15)
    px_m = mask.load()
    for y in range(bh_):
        for x in range(bw_):
            if x < bx0 or x >= bw_ - bx0 or y < by0 or y >= bh_ - by0:
                continue
            # 记录贴边带像素是否为前景，随后仅在此带内做 3x3 最小滤波
    inner = mask.crop((bx0, by0, bw_ - bx0, bh_ - by0))
    outer_a = mask.filter(ImageFilter.MinFilter(3))
    outer_b = Image.new("L", (bw_, bh_), 255)
    outer_b.paste(outer_a, (0, 0))
    outer_b.paste(inner, (bx0, by0))
    mask = outer_b
    _keep_main_blobs(mask, rgb_img=small, med=med)

    # 5) 蒙版放大回原尺寸：柔化 → 收缩去白边 → 阴影侵蚀（须在改色前）→ 边缘精修
    alpha = mask.resize((w, h), Image.BILINEAR)
    alpha = alpha.filter(ImageFilter.GaussianBlur(2.0))
    alpha = alpha.filter(ImageFilter.MinFilter(3))
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.4))
    _suppress_shadow(src, alpha, med)
    alpha = _refine_and_defringe(src, alpha)
    alpha = alpha.point(lambda v: 0 if v < 8 else (255 if v > 247 else v))
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.5))
    src.putalpha(alpha)

    # 6) 裁剪到角色包围盒（留 3% 边距）
    bbox = alpha.getbbox()
    if bbox:
        margin = int(max(w, h) * 0.03)
        box = (max(0, bbox[0] - margin), max(0, bbox[1] - margin),
               min(w, bbox[2] + margin), min(h, bbox[3] + margin))
        src = src.crop(box)
    return src


def frame_for_display(img: Image.Image, target_aspect: float = 3.4) -> Image.Image:
    """全身立绘保留完整（不再截成膝上半身）。

    全身立绘正常比例 2.0~3.4 一律原样返回（从头到脚完整）；
    仅当极端细长（>3.2，模型畸形）时才按目标比例轻微矫正。
    """
    w, h = img.size
    if w and h > w * target_aspect:
        keep = target_aspect / (h / w)
        img = img.crop((0, 0, w, int(h * keep)))
    return img


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def mock_portrait(name: str, emotion: str, size: int = 768) -> bytes:
    """生成一张占位立绘 PNG：渐变底上的半身剪影 + 角色名/表情标签。"""
    hue = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:4], 16) % 360
    top = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue / 360, 0.72, 0.55))
    bottom = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue / 360, 0.40, 0.55))

    img = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    # 竖向渐变背景
    for y in range(size):
        t = y / size
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        draw.line([(0, y), (size, y)], fill=color)

    # 半身人形剪影
    cx = size // 2
    head_r = size // 7
    head_cy = size // 3
    shoulder_w = size // 2.6
    body_top = head_cy + head_r + size // 30
    sil = (30, 30, 50, 235)
    draw.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r], fill=sil
    )
    draw.rounded_rectangle(
        [cx - shoulder_w, body_top, cx + shoulder_w, size], radius=size // 6, fill=sil
    )
    # 表情差异：在头部画简单的表情符号
    eye_y = head_cy - head_r // 6
    eye_dx = head_r // 2
    eye_r = max(6, head_r // 10)
    white = (255, 255, 255, 255)
    marks = {
        "happy": "^ ^",
        "shy": "> <",
        "sad": "; ;",
        "angry": "> >",
        "surprised": "O O",
    }
    if emotion in marks:
        draw.text((cx, eye_y), marks[emotion], font=_load_font(head_r // 2),
                  fill=white, anchor="mm")
    else:
        draw.ellipse([cx - eye_dx - eye_r, eye_y - eye_r, cx - eye_dx + eye_r, eye_y + eye_r], fill=white)
        draw.ellipse([cx + eye_dx - eye_r, eye_y - eye_r, cx + eye_dx + eye_r, eye_y + eye_r], fill=white)

    # 名字标签
    tag_font = _load_font(size // 12)
    label = f"{name} · {emotion}"
    bbox = draw.textbbox((0, 0), label, font=tag_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = size // 40
    box = [size // 2 - tw // 2 - pad, size - th - pad * 4, size // 2 + tw // 2 + pad, size - pad]
    draw.rounded_rectangle(box, radius=pad, fill=(20, 20, 30, 200))
    draw.text((size // 2, (box[1] + box[3]) // 2), label, font=tag_font,
              fill=white, anchor="mm")
    draw.text((size // 2, size // 14), "MOCK 立绘（未配置生图 API）",
              font=_load_font(size // 20), fill=(255, 255, 255, 160), anchor="mm")

    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def mock_background(hint: str, size: int = 1024) -> bytes:
    """生成一张占位背景 PNG：黄昏/夜色渐变 + 地平线 + 提示文字。"""
    img = Image.new("RGB", (size, size // 2))
    draw = ImageDraw.Draw(img)
    sky = [(255, 168, 108), (120, 90, 160), (40, 40, 80)]
    for y in range(img.height):
        t = y / img.height
        if t < 0.5:
            k = t / 0.5
            c = tuple(int(sky[0][i] + (sky[1][i] - sky[0][i]) * k) for i in range(3))
        else:
            k = (t - 0.5) / 0.5
            c = tuple(int(sky[1][i] + (sky[2][i] - sky[1][i]) * k) for i in range(3))
        draw.line([(0, y), (size, y)], fill=c)
    horizon = int(img.height * 0.78)
    draw.rectangle([0, horizon, size, img.height], fill=(25, 22, 40))
    draw.ellipse([size // 3, horizon - 90, size // 3 + 180, horizon + 90],
                 fill=(255, 220, 150))
    font = _load_font(30)
    draw.text((size // 2, img.height // 3), f"MOCK 背景（未配置生图 API）",
              font=font, fill=(255, 255, 255, 220), anchor="mm")
    draw.text((size // 2, img.height // 3 + 50), hint[:40], font=_load_font(22),
              fill=(255, 255, 255, 180), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()










def _min_size_issue(img: Image.Image) -> str | None:
    """最小尺寸守护：立绘成品过小（模型裁到局部/缩小）判异常。"""
    try:
        w, h = img.size
        if w < 280 or h < 380:
            return f"成品过小（{w}x{h}）"
        return None
    except Exception:
        return None


def _completeness_issue(img: Image.Image) -> str | None:
    """全身完整性检查：防止「只有下半身/腿特写/无头无肩」的失败构图。

    在原始生成图上做（不依赖抠图）：非背景 bbox 宽高比 < 1.15 视为横宽特写；
    bbox 顶部 25% 带内前景占比 < 12% 视为缺少头肩（头部在画面外）。
    命中即由生成侧换 seed 重画——这是「固定全身模型」的技术保障。

    前景/背景差异阈值取 26（原 40）：浅灰画布上的银白发/白衣等
    「白主体」与背景只差约 30，40 会被误判为无前景（曾致 bbox 塌缩，
    整张白描构图被误判「横宽/腿特写」白白重画 3 次）。
    """
    _FG_DIFF = 26
    try:
        im = img.convert("RGB")
        im.thumbnail((200, 200), Image.LANCZOS)
        w, h = im.size
        if w < 24 or h < 24:
            return None
        px = im.load()
        # 边缘背景估计
        border = [px[x, 0] for x in range(0, w, max(1, w // 8))]
        border += [px[x, h - 1] for x in range(0, w, max(1, w // 8))]
        border += [px[0, y] for y in range(0, h, max(1, h // 8))]
        border += [px[w - 1, y] for y in range(0, h, max(1, h // 8))]
        bg = tuple(sum(c[i] for c in border) / len(border) for i in range(3))
        minx, miny, maxx, maxy = w, h, 0, 0
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y]
                if (abs(r - bg[0]) > _FG_DIFF or abs(g - bg[1]) > _FG_DIFF
                        or abs(b - bg[2]) > _FG_DIFF):
                    if x < minx: minx = x
                    if x > maxx: maxx = x
                    if y < miny: miny = y
                    if y > maxy: maxy = y
        if maxx <= minx or maxy <= miny:
            return "空白/过曝构图（主体缺失）"
        ratio = (maxy - miny) / max(4, (maxx - minx))
        if ratio < 1.15:
            return "构图横宽/腿部特写"
        # 主体面积占比：局部特写（腿部横条/半身zoom）bbox 面积明显偏小
        area = (maxx - minx + 1) * (maxy - miny + 1) / (w * h)
        if area < 0.20:
            return "主体面积过小（疑似局部特写）"
        # 头部检测带：bbox 顶部 14% 带 + 第二带（14%-28% 藏脸区）。
        # 正常全身图顶部 = 头发/皮肤圆顶；腿特写顶部 = 裤装/裙装。
        # 判别信号：肤色（r 显著高于 b）或发丝（低饱和浅发/深发）或
        # 纹理（带内亮度标准差，发丝反光）。任一成立视为头部，全不成立
        # 才判腿/肢体特写——银灰/黑色长发与浅色服装不再被误杀，黑裤
        # 腿特写仍会被纹理+发丝信号拦下。
        band_h = max(2, int((maxy - miny) * 0.14))
        total = fg = skin = 0
        for y in range(miny, miny + band_h):
            for x in range(minx, maxx + 1):
                r, g, b = px[x, y]
                total += 1
                if (abs(r - bg[0]) > _FG_DIFF or abs(g - bg[1]) > _FG_DIFF
                        or abs(b - bg[2]) > _FG_DIFF):
                    fg += 1
                    if r > 150 and g > 100 and r - b > 12:
                        skin += 1
        if total and fg / total < 0.10:
            # 弱阈值复核：白发/白衣在浅灰底上可能差不到 26——
            # 若弱阈值（14）下带内前景充足，仍视为头部带，仅两种阈值
            # 都空才判「头部在画面外」（真无头/大面积留白构图）。
            fg_w = total_w = 0
            for y in range(miny, miny + band_h):
                for x in range(minx, maxx + 1):
                    r, g, b = px[x, y]
                    total_w += 1
                    if (abs(r - bg[0]) > 14 or abs(g - bg[1]) > 14
                            or abs(b - bg[2]) > 14):
                        fg_w += 1
            if total_w and fg_w / total_w >= 0.35:
                fg = fg_w
            else:
                return "上半身缺失（头部在画面外）"
        if not (total and fg / total > 0.45):
            return None

        def _band_strong_legs(y0: int, y1: int) -> bool:
            """强信号腿检：顶带横满条（裤/裙横切铺满全宽）或 纯皮肤无发
            （腿/臂特写）→ 明确的肢体特写；白色/浅色发与浅色背景对比弱时
            纹理信号可能不足，不再仅凭弱信号判犯规（曾误杀近白构图）。"""
            fg2 = skin2 = hair2 = 0
            y_end = min(y1, maxy + 1)
            col_ratios = []
            for x in range(minx, maxx + 1):
                col_fg = 0
                for y in range(y0, y_end):
                    r, g, b = px[x, y]
                    if (abs(r - bg[0]) > _FG_DIFF or abs(g - bg[1]) > _FG_DIFF
                            or abs(b - bg[2]) > _FG_DIFF):
                        col_fg += 1
                        fg2 += 1
                        if r > 150 and g > 100 and r - b > 12:
                            skin2 += 1
                        elif (max(r, g, b) - min(r, g, b) < 28
                              and min(r, g, b) > 145):
                            hair2 += 1
                col_ratios.append(col_fg / max(1, y_end - y0))
            if fg2 < 3:
                return False
            # 顶带横满条 = 腿/肢体特写（裤装/裙装横切，全宽铺满）；
            # 全身像顶部是圆拱（两侧背景），只有横条会 >0.85。
            if sum(col_ratios) / len(col_ratios) > 0.85:
                return True
            # 顶部带几乎全是皮肤且无发色 = 肢体（腿/臂）特写，不是头部
            if skin2 / fg2 > 0.55 and hair2 < max(2, fg2 * 0.05):
                return True
            return False

        if _band_strong_legs(miny, miny + band_h) and \
                _band_strong_legs(miny + band_h, miny + band_h * 2):
            return "疑似腿/肢体特写（顶部非头部）"
        return None
    except Exception:
        return None


def _nan_issue(raw: bytes) -> str | None:
    """服务劣化检测（绿噪点/色彩噪声图）：相邻像素差均值过大 → NaN 落盘。

    A1111 长时间高强度大图生成后模型/采样状态可能劣化，输出整屏高饱和
    噪点（绿/蓝/紫）。该信号指涉「服务状态」而非构图——命中后由
    _sd_retry 触发一次 reload-checkpoint 恢复，并拒绝把噪声图当成品。
    """
    try:
        import io as _io
        im = Image.open(_io.BytesIO(raw)).convert("RGB")
        im.thumbnail((48, 48), Image.LANCZOS)
        pw, ph = im.size
        px = list(im.getdata())
        n = len(px)
        if n < 64:
            return "图片解码失败"
        # 1.7.27 修复：只比较**同一行内相邻**像素——原实现把展平序列的
        # 「行尾→下一行首」也当相邻（竖向长立绘：白底撞人物深色，行间差
        # 必然超阈值 → 正常图被误判「NaN 噪点」，误触发 reload/误重试）
        diff = 0.0
        cnt = 0
        for y in range(ph):
            for x in range(pw - 1):
                a, b = px[y * pw + x], px[y * pw + x + 1]
                diff += abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
                cnt += 1
        if cnt:
            diff /= cnt   # 1.7.27：同行相邻均值（旧尾行再除 n-1 = 双重归一）
        # 阈值 140（1.7.27 实测校准）：正常立绘 ~70、真高饱和噪点 ~250；
        # 旧阈值 55 会把正常立绘的衣褶/边缘差误判为 NaN（误触发 reload）
        return "服务劣化（NaN 噪点）" if diff > 140 else None
    except Exception:
        return "图片解码失败"


def _purple_blob_issue(raw: bytes) -> str | None:
    """1.7.30 颜色污染检测（紫斑/绿噪点）：SD 溢出失败的两种形态——
    ①低周波「淡紫色彩斑」糊化；②**绿色高饱和噪点溢出**（1.7.29 六段 80%
    实锤：整图绿色噪点+人物触手化，_nan_issue 与紫判据均漏检）。
    判据（实测校准，缩图 48×48）：紫=偏蓝紫像素（B−R>15）、绿=绿显著像素
    （G>R+25 且 G>B+25）；劣化图紫 1.5%/绿 23%，正常立绘（棕灰肤色系、
    灰紫发色、绿衣）均 ≤0.1% → 统一阈值 0.8%。"""
    try:
        import io as _io
        im = Image.open(_io.BytesIO(raw)).convert("RGB")
        im.thumbnail((48, 48), Image.LANCZOS)
        px = list(im.getdata())
        n = len(px)
        if n < 64:
            return None
        purple = sum(1 for r, g, b in px if b - r > 15)
        green = sum(1 for r, g, b in px if g > r + 25 and g > b + 25)
        if purple / n > 0.008:
            return "颜色污染（紫斑糊化）"
        if green / n > 0.008:
            return "颜色污染（绿噪点）"
        # 1.7.37 头部带绿噪专项：小面积脸部绿噪（全图占比 <0.8% 漏网，
        # 实机 010：单人但全脸绿噪）——顶部 22% 带内绿占比 >3% 即拒
        im2 = Image.open(_io.BytesIO(raw)).convert("RGB")
        w, h = im2.size
        band = im2.crop((0, 0, w, int(h * 0.22)))
        band.thumbnail((48, 48), Image.LANCZOS)
        px2 = list(band.getdata())
        n2 = len(px2)
        if n2 >= 64:
            g2 = sum(1 for r, g, b in px2
                     if g > r + 25 and g > b + 25)
            if g2 / n2 > 0.03:
                return "颜色污染（头部绿噪）"
        return None
    except Exception:
        return None


def _green_noise_issue(data: bytes) -> str | None:
    """绿色噪点检查（1.7.37，cutout 复检专用）：绿判据（G>R+25 且 G>B+25）
    全图 >0.8% 或**顶部带（22%）>3%**——只查绿：**紫判据在抠图后会把
    蓝裙/蓝制服误判紫斑**（实测标定：正常立绘绿占比 0.0、废图 3-5%）。"""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        imt = im.copy()
        imt.thumbnail((48, 48), Image.LANCZOS)
        px = list(imt.getdata())
        n = len(px)
        if n >= 64 and sum(1 for r, g, b in px
                           if g > r + 25 and g > b + 25) / n > 0.008:
            return "颜色污染（绿噪点）"
        w, h = im.size
        band = im.crop((0, 0, w, int(h * 0.22)))
        band.thumbnail((48, 48), Image.LANCZOS)
        px2 = list(band.getdata())
        n2 = len(px2)
        if n2 >= 64 and sum(1 for r, g, b in px2
                            if g > r + 25 and g > b + 25) / n2 > 0.03:
            return "颜色污染（头部绿噪）"
        return None
    except Exception:
        return None


def _multi_figure_issue(data: bytes) -> str | None:
    """双人同框检测（1.7.37 硬拦截）：模型会把「转变中的同一人」具象化
    成**两人并排**（实机连续两张：裸体双人/双人着装版）。
    判据=**小腿段（下部 78%~96% 高度带）的「腿柱」**：单人=单组腿
    （两腿间距窄、合并为一柱），双人=两柱且有 ≥0.05w 宽的深谷——
    对散开长发/张开手臂的单身姿势不敏感（那些不在下带）；
    cutout 用 alpha≥128，raw 用与边缘背景色差前景。"""
    try:
        im = Image.open(io.BytesIO(data))
        w0 = 220
        if im.mode in ("RGBA", "LA"):
            a = im.convert("RGBA").getchannel("A")
            a.thumbnail((w0, 260), Image.LANCZOS)
            w, h = a.size
            px = a.load()
            y0, y1 = int(h * 0.25), int(h * 0.55)

            def fg(x, y):
                return px[x, y] >= 128
        else:
            im2 = im.convert("RGB").copy()
            im2.thumbnail((w0, 260), Image.LANCZOS)
            w, h = im2.size
            px = im2.load()
            y0, y1 = int(h * 0.25), int(h * 0.55)
            border = ([px[x, 0] for x in range(0, w, max(1, w // 8))]
                      + [px[0, y] for y in range(0, h, max(1, h // 8))]
                      + [px[w - 1, y] for y in range(0, h, max(1, h // 8))])
            bg = tuple(sum(c[i] for c in border) / len(border) for i in range(3))

            def fg(x, y):
                r, g, b = px[x, y]
                return (abs(r - bg[0]) > 26 or abs(g - bg[1]) > 26
                        or abs(b - bg[2]) > 26)
        cols = [sum(1 for y in range(y0, y1) if fg(x, y)) / max(1, (y1 - y0))
                for x in range(w)]

        def _peaks(band_cols, on_thr, seg_min, gap_min, gap_thr):
            segs = []
            in_seg = start = -1
            for x, v in enumerate(band_cols):
                on = v >= on_thr
                if on and in_seg < 0:
                    in_seg, start = x, x
                elif not on and in_seg >= 0:
                    if x - start >= max(3, int(w * seg_min)):
                        segs.append((start, x))
                    in_seg = -1
            if in_seg >= 0 and w - start >= max(3, int(w * seg_min)):
                segs.append((start, w))
            for a0, a1 in zip(segs, segs[1:]):
                gap = band_cols[a0[1]:a1[0]] if a0[1] < a1[0] else []
                if gap and min(gap) <= gap_thr \
                        and (a1[0] - a0[1]) >= max(3, int(w * gap_min)):
                    return True
            return False

        # 判据① 上身柱（0.25-0.55h 带）：两人躯干分离（1.7.37 一版）
        if _peaks(cols, 0.40, 0.06, 0.03, 0.15):
            return "画面出现两人以上（双人同框）"
        # 判据② 头部双峰（0.03-0.15h 带）：紧贴双人肩贴肩时上身 gap
        # 过窄漏检——「两个头」是最稳的两人信号（单人单头；发饰/耳朵
        # 列值低不构成峰）
        y0h, y1h = int(h * 0.03), int(h * 0.15)
        hcols = [sum(1 for y in range(y0h, y1h) if fg(x, y))
                 / max(1, (y1h - y0h)) for x in range(w)]
        if _peaks(hcols, 0.45, 0.04, 0.06, 0.18):
            return "画面出现两人以上（双人同框·头部双峰）"
        return None
    except Exception:
        return None


def _skin_excess_issue(data: bytes) -> str | None:
    """裸露检查（1.7.37）：cutout 上肤色像素占比 >0.80 判「裸体/缺衣」。
    仅对已抠图透明立绘做；泳装/命令裸体等设定由调用方 nude_ok 跳检。"""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((120, 160), Image.LANCZOS)
        w, h = im.size
        px = im.convert("RGB").load()
        al = im.getchannel("A").load()
        total = skin = 0
        for y in range(h):
            for x in range(w):
                if al[x, y] < 128:
                    continue
                r, g, b = px[x, y]
                hh, ss, vv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                if (hh <= 0.09 and 0.12 <= ss <= 0.85 and 0.35 <= vv <= 1.0):
                    skin += 1
                total += 1
        if total > 200 and skin / total > 0.80:
            return "裸露占比过高（疑似裸体/缺衣）"
        return None
    except Exception:
        return None


def _portrait_check(raw: bytes) -> str | None:
    """立绘质检（统一用于 SD/API 两条线路）：构图 + 尺寸 + 面部黑斑 + 噪声
    + 颜色污染（1.7.27 紫斑糊化）。"""
    try:
        img = Image.open(io.BytesIO(raw))
        # 1.7.32 顺序修正：噪点/污染判据**前置**——劣化图（绿蓝噪点雪花）
        # 曾被 _completeness_issue 抢先误述为「疑似腿特写」，绿判据
        # 短路漏检 → 不触发 reload+大步长换簇（实机：目标版两张字节级
        # 相同的噪点废图，簇内确定性崩溃；顺序修正后先报「颜色污染」）
        # 1.7.37 双人同框硬拦截（raw 级：模型把转变中同一人画成两人并排）
        comp = (_nan_issue(raw) or _purple_blob_issue(raw)
                or _completeness_issue(img) or _min_size_issue(img)
                or _multi_figure_issue(raw))
        if comp:
            return comp
        return _fast_face_test(raw)
    except Exception:
        return "图片解码失败"


def _bg_check(raw: bytes) -> str | None:
    """背景质检：解码正常 + 有合理尺寸即可（背景无构图要求）。"""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return _min_size_issue(img)
    except Exception:
        return "图片解码失败"


def _junction_ok(im: Image.Image, fracs=(0.40, 0.58)) -> bool:
    """躯干/腰臀交界带不透明度检查：防「身体零部件脱落/接缝断裂」。
    只查腰部与臀部两个单躯干带（双腿分开属正常姿势，0.66 以下不查），
    中央区带内不透明占比过低（<40%）即判衔接异常。"""
    try:
        a = im.convert("RGBA").getchannel("A")
        bbox = a.getbbox()
        if not bbox:
            return False
        x0, y0, x1, y1 = bbox
        bw, bh = x1 - x0, y1 - y0
        if bw < 40 or bh < 80:
            return False
        px = a.load()
        for f in fracs:
            y = y0 + int(bh * f)
            cnt = opq = 0
            for x in range(x0 + int(bw * 0.25), x1 - int(bw * 0.25),
                           max(2, bw // 40)):
                cnt += 1
                if px[x, y] >= 200:
                    opq += 1
            if cnt and opq / cnt < 0.40:
                return False
        return True
    except Exception:
        return True


def _face_box(im: Image.Image) -> tuple[int, int, int, int] | None:
    """透明立绘上的脸部区域估算（不依赖人脸检测库）：
    人物包围盒顶部 2%~15% 带、中央 40% 宽的方块，用作 img2img 图生图区域。"""
    try:
        a = im.convert("RGBA").getchannel("A")
    except Exception:
        return None
    bbox = a.getbbox()
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    if bw < 40 or bh < 40:
        return None
    fx0 = x0 + int(bw * 0.30)
    fx1 = x1 - int(bw * 0.30)
    fy0 = y0 + int(bh * 0.02)
    fy1 = y0 + int(bh * 0.16)
    if fx1 <= fx0 or fy1 <= fy0:
        return None
    return fx0, fy0, fx1, fy1


def _fast_face_test(raw: bytes) -> str | None:
    """极小代价的面部黑斑预检（生成后立即调用，每张约 20ms）。

    降采样到 160px 内做「近白/近黑背景估透明」得到人物粗 alpha，取
    面部带（bbox 顶部 8%~30%）黑色占比判定。与全量抠图后的 _quality_issue
    互为补充；此处只用第一道低成本筛选。
    """
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((160, 160), Image.LANCZOS)
        w, h = im.size
        if w < 24 or h < 24:
            return None
        px = im.load()
        # 边缘采样估背景
        border = [px[x, 0] for x in range(0, w, max(1, w // 8))]
        border += [px[x, h - 1] for x in range(0, w, max(1, w // 8))]
        border += [px[0, y] for y in range(0, h, max(1, h // 8))]
        border += [px[w - 1, y] for y in range(0, h, max(1, h // 8))]
        br = sum(c[0] for c in border) / len(border)
        bg = sum(c for c in border) / (len(border) * 3)
        # 人物粗掩码：明显非背景的像素
        minx, miny, maxx, maxy = w, h, 0, 0
        for y in range(0, h, 1):
            for x in range(0, w, 1):
                r, g, b = px[x, y]
                if abs(r - bg) > 45 and abs(g - bg) > 45 and abs(b - bg) > 45:
                    if x < minx: minx = x
                    if x > maxx: maxx = x
                    if y < miny: miny = y
                    if y > maxy: maxy = y
        if maxx <= minx or maxy <= miny:
            return None
        fx0 = minx + int((maxx - minx) * 0.28)
        fx1 = maxx - int((maxx - minx) * 0.28)
        fy0 = miny + int((maxy - miny) * 0.08)
        fy1 = miny + int((maxy - miny) * 0.30)
        total = black = 0
        for y in range(fy0, fy1, 1):
            for x in range(fx0, fx1, 1):
                r, g, b = px[x, y]
                total += 1
                if (r + g + b) / 3 < 42:
                    black += 1
        if total and black / total > 0.80:
            return "面部黑斑/黑纱"
        return None
    except Exception:
        return None


def _quality_issue(img: Image.Image) -> str | None:
    """生成质量检查：人物面部异常黑斑（模型画出的黑纱/黑面罩类瑕疵）。

    在「已抠图」的画面上检测（透明底，只需判断人物内部的黑色占比）：
    面部检测区 = 人物包围盒顶部 5%~26% 带、中央 44% 宽（脸部必然覆盖）。
    黑色占比过高判为生成瑕疵，由生成侧自动换 seed 重画。
    """
    try:
        a = img.convert("RGBA").getchannel("A")
    except Exception:
        return None
    bbox = a.getbbox()
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    if bw < 32 or bh < 32:
        return None
    fx0, fx1 = x0 + int(bw * 0.28), x1 - int(bw * 0.28)
    fy0, fy1 = y0 + int(bh * 0.08), y0 + int(bh * 0.30)
    px = img.convert("RGBA").load()
    total = black = 0
    for y in range(fy0, fy1, max(1, bh // 60)):
        for x in range(fx0, fx1, max(1, bw // 60)):
            r, g, b, al = px[x, y]
            if al < 128:
                continue
            total += 1
            if (r + g + b) / 3 < 42:
                black += 1
    if total and black / total > 0.75:
        return "面部黑斑/黑纱"
    return None




def edge_cleanup(img: Image.Image, shrink: int = 1, feather: float = 0.8) -> Image.Image:
    """内置「消除立绘边缘」配件：对已抠图立绘做最后的白边/毛边清理。

    - shrink：向角色内部收缩 N 像素（吃掉贴着轮廓的 1px 白晕/杂色边）；
    - feather：收缩后的超轻羽化（保留发丝质感的半透明过渡）。
    全身像最后一步调用，可放心重复执行；不会影响大块主体。
    """
    if shrink <= 0:
        return img
    img = img.convert("RGBA")
    alpha = img.getchannel("A")
    new_a = alpha.filter(ImageFilter.MinFilter(shrink * 2 + 1))
    if feather > 0:
        new_a = new_a.filter(ImageFilter.GaussianBlur(feather))
    img.putalpha(new_a)
    return img





import asyncio as _asyncio

_AI_CUT_SEM = None


def _get_ai_cut_sem():
    global _AI_CUT_SEM
    if _AI_CUT_SEM is None:
        _AI_CUT_SEM = _asyncio.Semaphore(1)
    return _AI_CUT_SEM


async def process_portrait_async(data: bytes, cutout_mode: str = "auto") -> bytes:
    """异步后处理：AI 抠图在线程池执行且全局串行（一次一张），
    避免 rembg CPU 推理阻塞事件循环、多任务争抢导致生成失败。"""
    if cutout_mode == "ai":
        async with _get_ai_cut_sem():
            return await _asyncio.to_thread(process_portrait, data, cutout_mode)
    return await _asyncio.to_thread(process_portrait, data, cutout_mode)


def process_portrait(data: bytes, cutout_mode: str = "auto") -> bytes:
    """立绘后处理：去背景（按玩家选择的抠图标准）→ 膝上构图 → 边缘白边清理。"""
    img = Image.open(io.BytesIO(data))
    img = cut_out(img, cutout_mode)
    img = frame_for_display(img)
    # 内置边缘消除配件：去除紧贴轮廓的 1px 白晕毛边（config 可关）
    import os
    try:
        if _EDGE_CLEAN_ENABLED:
            img = edge_cleanup(img, shrink=_EDGE_CLEAN_SHRINK,
                               feather=_EDGE_CLEAN_FEATHER)
    except Exception:
        pass
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _comfy_workflow(prompt: str, negative: str, width: int, height: int,
                    seed: int, steps: int, cfg_scale: float,
                    sampler: str, scheduler: str,
                    checkpoint: str, filename_prefix: str = "tsf_galgame") -> dict:
    """ComfyUI API 版工作流（txt2img）：Checkpoint -> 双 CLIP 条件 ->
    KSampler -> VAE 解码 -> 保存。节点编号固定，结果从 9/9 取图。"""
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint or ""}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed % (2 ** 31), "steps": steps,
                         "cfg": cfg_scale, "sampler_name": sampler,
                         "scheduler": scheduler or "normal", "denoise": 1.0,
                         "model": ["1", 0], "positive": ["3", 0],
                         "negative": ["2", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": filename_prefix,
                         "images": ["6", 0]}},
    }


def _comfy_extract_image(history: dict, node_id: str = "7") -> dict | None:
    """从 ComfyUI /history 响应中提取第一张图 {filename, subfolder, type}。"""
    try:
        for _, node in history.items():
            for out_node in node.get("outputs", {}).values():
                imgs = out_node.get("images") or []
                if imgs:
                    return imgs[0]
    except Exception:
        return None
    return None


class ImageClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg["image"]
        self.provider = (self.cfg.get("provider") or "openai").lower()
        if self.provider == "sdwebui":
            # 本地 SD WebUI 无需 Key，只要填了地址即可用
            self.mock = not self.cfg.get("base_url")
            # SD WebUI 一次只能跑一张图：多表情/多角色并发任务如果不在此处
            # 串行化，会全部打进 A1111 队列等待，靠后的请求等待+生成超过
            # 请求超时而被错误地标记失败。
            self._sem = _get_sd_sem()
            self._opts_cache = None
            self._opts_at = 0.0
            self._samp_cache = None
            self._samp_at = 0.0
            self._up_cache = None
            self._up_at = 0.0
            self._sch_cache = None
            self._sch_at = 0.0
            self._model_preset = None  # 当前 WebUI 模型匹配到的适配预设
            self._wai_adapt = False    # 1.7.46 wai 特适配（配方/尺寸/ClipSkip/阶段词）
            self._tagger_ok = -1       # -1=未探测 0=无 wd14-tagger 1=有（1.7.47）
            # 服务劣化恢复（1.6.74）：连续坏图（NaN 噪点/空白）计数后
            # reload-checkpoint 重载模型（A1111 官方恢复手段），期间拒绝
            # 把噪声图当成品；软接受/回退仅用于构图类问题。
            self._degraded_count = 0
            self._recovered = False
            # 1.7.32 质检失败累计（reload 后不清零）：驱动大步长换组号
            # ——reload 前后如果都在同一「崩溃种子簇」（+1997 小步同簇），
            # 只有 +1000003×k 大步长才能跳出去；此前 count 被 reload 清零
            # 导致大步长条件失效（目标版连续两次同簇废图实锤）
            self._issue_count = 0
        elif self.provider == "comfyui":
            # 本地 ComfyUI：/prompt + /history 轮询，无需 Key；同样串行化
            self.mock = not self.cfg.get("base_url")
            self._sem = _get_sd_sem()
        else:
            self.mock = not (
                self.cfg.get("api_key")
                and "填入" not in self.cfg.get("api_key", "")
                and self.cfg.get("base_url")
            )

    async def generate_portrait(
        self, name: str, appearance: str, emotion: str, emotion_cn: str,
        style_suffix: str, extra_negative: str = "", seed: int | None = None,
        strict: bool = True, identity_lock: bool = True,
        uniform_en: str | None = None, uniform_neg: str | None = None,
        matcher=None
    ) -> bytes:
        """生成角色立绘。mock 模式返回占位剪影图。

        matcher（1.7.32 转变判定器）：主角阶段跃迁全量时传入——async
        (raw)->float，多候选挑「转变符合度」最优（见 _sd_retry）。

        seed：同一角色（名字）固定一个种子，保证各表情差分立绘是
        「同一个人换表情」而非 4 个随机人（这是表情一致性的关键）。
        strict=False（换装差分）：质检重试耗尽后接受最后一次结果（软接受），
        保证「换装」永远能换出立绘，不会因构图挑剔整张失败。
        identity_lock=True（默认）：差分强锁——发型/发色/服装/帽饰/配件
        与身份不可漂移；主角 TSF 渐变（anchor 内含男性/女性两段描述）传
        False 放行，让全量生成按 anchor 演化，跨阶段渐变仍由 refine 锁定。
        uniform_en/uniform_neg（1.6.90 会话级制服）：本局统一制服关键词——
        同一场游戏所有角色同款；None = 回退全局 config（跨局互不干涉）。
        """
        if self.mock:
            await asyncio.sleep(0.8)
            return mock_portrait(name, emotion, size=768)

        # 1.7.13 着装分档前置词（默认着衣柜/内衣档/裸体档）——插到提示词**最前**：
        # A1111 前段 token 权重高、末端词会被稀释（实测同样种子：服装词在
        # 末尾=裸体+相机伪影；在开头一段普通服装词=完整着装）——这是 NSFW 向
        # realskin 模型裸体偏向的正解，不是词条本身无效
        _explicit_nude = any(k in appearance for k in (
            "裸体", "一丝不挂", "未着寸缕", "全裸", "nude", "naked"))
        if _explicit_nude:
            _dress_front = ""
        elif any(k in appearance for k in (
                "内衣", "内裤", "underwear", "lingerie")):
            _dress_front = "wearing underwear only, covered torso, lingerie set"
        else:
            _casual = any(k in appearance for k in _CASUAL_HINTS)
            _dress_front = ("fully dressed, wearing clothes, "
                            "wearing everyday casual outfit, shirt and pants"
                            if _casual
                            else "fully dressed, wearing clothes, "
                                 "proper outfit covering the body")
        emo_en = emotion_en_tags(emotion)
        # 1.7.35 发色偏置：设定发色加权前插（钉住不再漂银白）；无设定
        # 发色则随机池注入 + 银白负面（反制模型「默认白发」偏置）
        hair_pos, hair_neg = hair_bias(appearance)
        # 1.7.32 风格矛盾词清洗：config style_suffix 历史遗留「半身像/
        # 纯白色背景」与下方强制「必须全身像/浅灰色纯色背景」自相矛盾
        # （1.6.x v7 修过一半、config.json 仍是旧词）——矛盾组合×转变
        # 目标词会诱导该 realskin 模型系统性崩溃（跨 5 种子簇全噪点，
        # 实机复现：游戏 style×目标词 vs 清洗 style×目标词 同种子对照）
        style_suffix = (str(style_suffix or "")
                        .replace("半身像", "全身像")
                        .replace("纯白色背景", "浅灰色纯色背景"))
        prompt = (f"{_dress_front}，{hair_pos}，{append_appearance_en(appearance)}，"
                  f"{emotion_cn}，{emo_en}，{style_suffix}，"
                  f"必须全身像（从头到脚完整呈现），必须浅灰色纯色背景无阴影"
                  f"{PORTRAIT_EN_SUFFIX}")
        if hair_neg:
            extra_negative = ", ".join(x for x in [hair_neg, extra_negative] if x)
        if self.cfg.get("pink_rim", True):
            prompt += PINK_RIM_SUFFIX
        # 固定制服关键词（防偏差）：学园/制服类世界观锁定服装风格；
        # 会话级 uniform_en/uniform_neg（开局设定）优先，None 回退全局 config；
        # 「日常便装/休闲」外观不套制服锁——制服正面词与负面封杀词互相
        # 打架会让构图失稳（曾致全身像反复被模型边缘化/过曝失败）。
        # 1.6.91：裸体/内衣档**同样摘除制服锁**——制服正面词会把「换装为
        # 裸体」重新拽回校服（实测裸体换装回退全量时被 uniform 拽回水手服）。
        unif_en = (str(uniform_en).strip() if uniform_en is not None
                   else str(self.cfg.get("uniform_keyword_en", "") or "").strip())
        unif_neg = (str(uniform_neg).strip() if uniform_neg is not None
                    else str(self.cfg.get("uniform_keyword_neg", "") or "").strip())
        if any(k in appearance for k in _CASUAL_HINTS) or any(
                k in appearance for k in ("裸体", "一丝不挂", "内衣", "内裤",
                                          "nude", "naked", "underwear",
                                          "lingerie")):
            unif_en = unif_neg = ""
        if unif_en and unif_en.split(",")[0].strip().lower() not in prompt.lower():
            prompt += f"，{unif_en}"
        if unif_neg:
            extra_negative = ", ".join(x for x in [unif_neg, extra_negative] if x)
        # —— 着装保护（1.6.89）：默认「已着装」；设定/命令明确裸体或内衣才放开 ——
        # 之前默认立绘提示词既无着装正面词也无裸体负面，模型随性发挥会导致
        # 「开场就裸体/无衣物设定」——现在与内容分级/换装系统关联：默认穿好，
        # 只有剧情明确换内衣/裸体（18+ 允许）时才出现对应状态。
        # 1.7.13 着装正面词改用 `_dress_front` **插到提示词最前**（见函数头）：
        # A1111 前段 token 权重高，放在末尾会被稀释（实测同样种子：末尾加权
        # 服装词=裸体+相机伪影；最前一段普通服装词=完整着装）。
        _explicit_nude = any(k in appearance for k in (
            "裸体", "一丝不挂", "未着寸缕", "全裸", "nude", "naked"))
        if _explicit_nude:
            extra_negative = extra_negative  # 明确裸体：无着装正面词，无裸体负面
        elif any(k in appearance for k in (
                "内衣", "内裤", "underwear", "lingerie")):
            extra_negative = ", ".join(x for x in [
                "nude body, complete nudity, full frontal nudity, naked, "
                "no underwear", extra_negative] if x)
        else:
            extra_negative = ", ".join(x for x in [
                "nude, naked, nude body, topless, breast exposure, "
                "no clothes, missing outfit, completely undressed, "
                "strip tease, panties only", extra_negative] if x)
        # ---- 差分铁律（1.6.72）：统一后台关键词层 ----
        hints = f"{appearance} {emotion_cn}".lower()
        if identity_lock:
            # 正面：同一角色/同一套设计（发型/发色/服装/帽饰零漂移）
            prompt += DIFF_LOCK_EN
        # 眼部：精致无差错（恒定，所有线路）
        prompt += EYE_LOCK_EN
        # 相机/手机：设定（外观/重画需求）明确提到才允许，否则禁生成
        # 1.7.33：确认有相机设定时**取消相机负面 + 正面注入相机词**（用户
        # 规则：无设定=负面压制，有设定=取消负面且增加提示词）
        if not any(k in hints for k in ("相机", "摄影", "拍照", "摄像机",
                                        "camera", "photograph", "photo shoot",
                                        "selfie")):
            extra_negative = ", ".join(x for x in [CAPTURE_NEG, extra_negative] if x)
        else:
            prompt += f"，{CAPTURE_EN}"
        # 举手压头：设定明确要「举手/挥手/敬礼」类动作时不压制
        if not any(k in hints for k in ("举手", "挥手", "敬礼", "扬起手",
                                        "hand up", "raise hand", "salute", "wave",
                                        "hold up")):
            extra_negative = ", ".join(x for x in [HAND_POSE_NEG, extra_negative] if x)
        if identity_lock:
            # 发型/服装漂移负面（同正面锁定配套）
            extra_negative = ", ".join(x for x in [DIFF_LOCK_NEG, extra_negative] if x)
        # 单眼镜头/单片镜类道具（设定含「眼罩/单片镜/镜片」时放行）
        if not any(k in hints for k in ("眼罩", "单片", "镜片", "monocle",
                                        "eyepatch", "goggle")):
            extra_negative = ", ".join(x for x in [EYE_PROP_NEG, extra_negative] if x)
        extra_negative = ", ".join(x for x in [EYE_NEG, extra_negative] if x)
        if self.provider == "comfyui":
            w, h = self._parse_size(
                effective_sd_size(self.cfg, "portrait", "sd_portrait_size"))
            neg = ", ".join(x for x in [
                PORTRAIT_NEG_EN, extra_negative,
                self.cfg.get("uniform_keyword_neg", "")] if x)
            cap = effective_steps_cap(self.cfg, "portrait")
            return await self._sd_retry(
                prompt, w, h, neg, seed, check=_portrait_check,
                soft=not strict, soft_last=True, matcher=matcher,
                steps=int(self._tune("sd_steps", min(30, cap), min(30, cap))),
                cfg_scale=float(self._tune("sd_cfg", 6.5, 6.5)),
                sampler=str(self._tune("sd_sampler", "DPM++ 2M", "DPM++ 2M")),
                scheduler=str(self._tune("sd_scheduler", "karras", "karras")),
            )
        if self.provider == "sdwebui":
            preset = (await self.current_model_preset()
                      if self.cfg.get("model_presets", True) else dict(DEFAULT_PRESET))
            # 1.7.49 修复（用户：一直生成但无新图）——wai 特配尺寸通道
            # 只在 vram_mode=high（12GB+）启用：1024x1536 + Hires 1.5x 在
            # 8GB medvram 长跑下系统性紫斑糊化（04:11-04:38 八种子连环
            # 污染、reload 无效、同实例 640x448 背景全部正常——定标的
            # 尺寸+HR 组合失稳）；mid/low 回退原 768x1344 稳定线。
            # 直出验证：768x1344+ClipSkip2+无HR → 紫斑 0.0000%（CLEAN）。
            # 1.7.49 修复（用户：一直生成但无新图）——wai 特配尺寸通道
            # 只在 vram_mode=high（12GB+）启用：1024x1536 + Hires 1.5x 在
            # 8GB medvram 长跑下系统性紫斑糊化（04:11-04:38 八种子连环
            # 污染、reload 无效、同实例 640x448 背景全部正常——定标的
            # 尺寸+HR 组合失稳）；mid/low 回退原 768x1344 稳定线。
            # 直出验证：768x1344+ClipSkip2+无HR → 紫斑 0.0000%（CLEAN）。
            wai_hi = self._wai_adapt and effective_vram_mode(self.cfg) == "high"
            manual_sz = str(self.cfg.get("sd_portrait_size") or "").strip()
            if wai_hi and not manual_sz:
                qsize = QUALITY_MODES["portrait"][effective_quality(self.cfg, "portrait")]
                w, h = self._parse_size(_smaller_size(
                    str(preset.get("portrait_size", "")), qsize["size"]))
            else:
                w, h = self._parse_size(
                    effective_sd_size(self.cfg, "portrait", "sd_portrait_size"))
            neg = ", ".join(x for x in [PORTRAIT_NEG_EN, extra_negative,
                                        preset.get("negative_extra", "")] if x)
            cap = effective_steps_cap(self.cfg, "portrait")
            preset_steps = min(int(preset.get("steps", 30)), cap)
            # HR（Hires 修复）同规则：模型预设 HR 仅在 high 档开启——
            # 8GB 卡 HR 二遍 latent 在游戏长跑中是紫斑糊化触发器（实锤）
            hr_on = bool(preset.get("enable_hr")) and (
                effective_vram_mode(self.cfg) == "high" or not self._wai_adapt)
            return await self._sd_retry(
                prompt, w, h, neg, seed, check=_portrait_check,
                soft=not strict, soft_last=True, matcher=matcher,
                steps=int(self._tune("sd_steps", preset_steps, preset_steps)),
                cfg_scale=float(self._tune("sd_cfg", preset.get("cfg", 6.5),
                                           preset.get("cfg", 6.5))),
                sampler=str(self._tune("sd_sampler", preset.get("sampler", "DPM++ 2M"),
                                       preset.get("sampler", "DPM++ 2M"))),
                scheduler=str(self._tune("sd_scheduler", preset.get("scheduler", ""),
                                         preset.get("scheduler", ""))),
                hr=hr_on,
                hr_upscaler=preset.get("hr_upscaler", "Latent"),
                hr_scale=float(preset.get("hr_scale", 2)),
                hr_steps=int(preset.get("hr_steps", 10)),
                hr_denoise=float(preset.get("hr_denoise", 0.5)),
            )
        # OpenAI 兼容线路：同样执行质检与重试（供应商尺寸受限，只 2 次）
        return await self._request_retry(prompt, check=_portrait_check,
                                         soft=not strict)

    async def generate_background(self, hint: str, style_suffix: str) -> bytes:
        """生成场景背景图 / 桥段 CG（背景专用生成链，与立绘互不挤占）。"""
        if self.mock:
            await asyncio.sleep(0.8)
            return mock_background(hint)
        # 1.7.7：背景回归**全局唯一串行链**（ImageClient.__init__ 默认为
        # _get_sd_sem()，不再改写）——此前背景/CG 独立链与立绘链三路并发，
        # 大图同占显存 → 8GB 卡显存爆掉软件停止；串行一张接一张不叠加。

        prompt = (f"{hint}，{style_suffix}"
                  f"{BACKGROUND_EN_SUFFIX}，画面中不得出现任何人物，"
                  f"构图简洁干净（clean simple composition）")
        if self.provider == "comfyui":
            w, h = self._parse_size(
                effective_sd_size(self.cfg, "background", "sd_background_size"))
            neg = ", ".join(x for x in [
                self.cfg.get("negative_prompt",
                             "lowres, bad anatomy, bad hands, watermark, "
                             "extra digits, blurry"),
                ("duplicate objects, cloned buildings, extra windows, "
                 "warped perspective, jpeg artifacts, text, signature, "
                 "cluttered composition, twisted geometry")] if x)
            cap = effective_steps_cap(self.cfg, "background")
            return await self._sd_retry(
                prompt, w, h, neg, None, check=_bg_check,
                steps=int(self._tune("sd_steps", min(25, cap), min(25, cap))),
                cfg_scale=float(self._tune("sd_cfg", 6.5, 6.5)),
                sampler=str(self._tune("sd_sampler", "DPM++ 2M", "DPM++ 2M")),
                scheduler=str(self._tune("sd_scheduler", "karras", "karras")),
            )
        if self.provider == "sdwebui":
            preset = (await self.current_model_preset()
                      if self.cfg.get("model_presets", True) else dict(DEFAULT_PRESET))
            w, h = self._parse_size(
                effective_sd_size(self.cfg, "background", "sd_background_size"))
            neg = ", ".join(x for x in [
                self.cfg.get("negative_prompt",
                             "lowres, bad anatomy, bad hands, watermark, "
                             "extra digits, blurry"),
                ("duplicate objects, cloned buildings, extra windows, "
                 "warped perspective, jpeg artifacts, text, signature, "
                 "cluttered composition, twisted geometry"),
                preset.get("negative_extra", "")] if x)
            cap = effective_steps_cap(self.cfg, "background")
            preset_steps = min(int(preset.get("steps", 30)), cap)
            return await self._sd_retry(
                prompt, w, h, neg, None, check=_bg_check,
                steps=int(self._tune("sd_steps", preset_steps, preset_steps)),
                cfg_scale=float(self._tune("sd_cfg", preset.get("cfg", 6.5),
                                           preset.get("cfg", 6.5))),
                sampler=str(self._tune("sd_sampler", preset.get("sampler", "DPM++ 2M"),
                                       preset.get("sampler", "DPM++ 2M"))),
                scheduler=str(self._tune("sd_scheduler", preset.get("scheduler", ""),
                                         preset.get("scheduler", ""))),
                hr=preset.get("enable_hr"),
                hr_upscaler=preset.get("hr_upscaler", "Latent"),
                hr_scale=float(preset.get("hr_scale", 2)),
                hr_steps=int(preset.get("hr_steps", 10)),
                hr_denoise=float(preset.get("hr_denoise", 0.5)),
            )
        return await self._request_retry(prompt, check=_bg_check)

    @staticmethod
    def _parse_size(text: str) -> tuple[int, int]:
        try:
            w, h = str(text).lower().split("x")
            return max(64, int(w)), max(64, int(h))
        except (ValueError, AttributeError):
            return 512, 768

    def _auth_headers(self) -> dict:
        key = self.cfg.get("api_key", "")
        if key and "填入" not in key:
            return {"Authorization": f"Bearer {key}"}
        return {}

    async def _request(self, prompt: str) -> bytes:
        """OpenAI 兼容 /images/generations（火山方舟 Seedream 等实测可用：
        POST {base_url}/images/generations，model 用控制台模型 ID——
        Seedream 5.0 Pro = doubao-seedream-5-0-pro-260628；
        自定义尺寸上限约 4.62MP（2048x3520 超限、1728x2560 可行）。"""
        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=False)
        url = f"{base}/images/generations"
        size = str(self.cfg.get("size", "1024x1024"))
        if self.cfg.get("hi_res"):
            size = _hi_res_size(size)
        payload = {
            "model": self.cfg.get("model", "dall-e-3"),
            "prompt": prompt,
            "size": size,
            "response_format": "b64_json",
        }
        headers = self._auth_headers()
        last_err = None
        for i in range(2):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                break
            except httpx.HTTPStatusError as e:
                last_err = ImageGenError(
                    f"生图 API 返回 {e.response.status_code}：{e.response.text[:300]}")
            except httpx.HTTPError as e:
                last_err = ImageGenError(f"生图 API 连接失败：{e}")
            if i == 0:
                await asyncio.sleep(2.0)
        else:
            raise last_err

        import base64
        try:
            item = data["data"][0]
        except (KeyError, IndexError) as e:
            raise ImageGenError(f"生图 API 响应格式异常：{data}") from e
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(item["url"])
                r.raise_for_status()
                return r.content
        raise ImageGenError("生图 API 响应中既无 b64_json 也无 url")

    async def _request_comfyui(self, prompt: str, width: int, height: int,
                               extra_negative: str = "", seed: int | None = None,
                               steps: int | None = None,
                               cfg_scale: float | None = None,
                               sampler: str = "", scheduler: str = "",
                               hr: bool = False, **kwargs) -> bytes:
        """本地 ComfyUI 文本出图：POST /prompt（API 工作流）→ 轮询 /history
        {prompt_id} → GET /view 取回 PNG。全程以同一信号量串行化。"""
        import base64
        import time as _time
        import uuid as _uuid
        base = (self.cfg.get("base_url") or "http://127.0.0.1:8188").rstrip("/")
        _validate_url(base, allow_local=True)
        steps = steps or int(self.cfg.get("sd_steps", 25))
        cfg_scale = float(cfg_scale if cfg_scale is not None
                          else self.cfg.get("sd_cfg", 6.5))
        sampler = sampler or self.cfg.get("sd_sampler") or "euler"
        scheduler = scheduler or (self.cfg.get("sd_scheduler") or "")
        sampler, scheduler = _comfy_sampler(sampler, scheduler)
        ckpt = str(self.cfg.get("comfy_checkpoint", "") or "")
        if not ckpt:
            ckpt = await self._comfy_default_checkpoint(base)
        seed_val = int(seed if seed is not None else _time.time()) % (2 ** 31)
        workflow = _comfy_workflow(prompt, extra_negative, width, height,
                                   seed_val, steps, cfg_scale,
                                   sampler, scheduler, ckpt)
        client_id = _uuid.uuid4().hex
        headers = self._auth_headers()
        last_err = None
        for attempt in range(2):
            try:
                async with self._sem:
                    async with httpx.AsyncClient(timeout=120) as client:
                        resp = await client.post(
                            f"{base}/prompt",
                            json={"prompt": workflow, "client_id": client_id},
                            headers=headers)
                        resp.raise_for_status()
                        body = resp.json()
                        # node_errors 非空 = 工作流校验失败（模型缺失/节点错误），
                        # 重试无意义，直接快速失败
                        errors = body.get("node_errors") or {}
                        if errors:
                            raise ImageGenError(
                                f"ComfyUI 工作流校验失败：{list(errors)[:3]}")
                        prompt_id = body["prompt_id"]
                        log.info("comfy submitted prompt %s", prompt_id)
                    # 轮询直到完成（状态 error 提前退出）：独立客户端，避免
                    # POST 的请求上下文关闭后继续复用导致连接错误
                    deadline = _time.time() + 900
                    async with httpx.AsyncClient(timeout=30) as poll:
                        while _time.time() < deadline:
                            r = await poll.get(f"{base}/history/{prompt_id}")
                            r.raise_for_status()
                            data = r.json()
                            node = data.get(prompt_id) or {}
                            status = node.get("status", {})
                            if status.get("status_str") == "error":
                                err = (node.get("status", {}).get("messages")
                                       or [["error"]])[0]
                                raise ImageGenError(f"ComfyUI 生成失败：{err}")
                            # 注意：_comfy_extract_image 接收整个 history 字典
                            #（pid → node），传单个 node 会永远取不到图
                            img = _comfy_extract_image(data)
                            if img:
                                fn = img.get("filename", "")
                                sub = img.get("subfolder", "")
                                typ = img.get("type", "output")
                                log.info("comfy image fetched: %s", fn)
                                view = await poll.get(
                                    f"{base}/view",
                                    params={"filename": fn, "subfolder": sub,
                                            "type": typ})
                                view.raise_for_status()
                                return view.content
                            await asyncio.sleep(1.5)
                        raise ImageGenError("ComfyUI 生成超时（>15 分钟）")
            except (ImageGenError, ValueError, KeyError) as e:
                last_err = e if isinstance(e, ImageGenError) \
                    else ImageGenError(f"ComfyUI 响应异常：{e}")
                await asyncio.sleep(3.0)
            except httpx.HTTPError as e:
                last_err = ImageGenError(f"ComfyUI 请求失败：{e}")
                await asyncio.sleep(3.0)
        raise last_err

    async def _comfy_default_checkpoint(self, base: str) -> str:
        """查询 ComfyUI 可用 checkpoint 列表，取配置项或第一个。"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{base}/object_info/CheckpointLoaderSimple")
                r.raise_for_status()
                info = r.json().get("CheckpointLoaderSimple", {})
                names = (info.get("input", {}).get("required", {})
                         .get("ckpt_name", [])[0] or [])
                if names:
                    cfg_pick = str(self.cfg.get("comfy_checkpoint", "") or "")
                    if cfg_pick and cfg_pick in names:
                        return cfg_pick
                    return _prefer_checkpoint([str(n) for n in names])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
            log.warning("comfy checkpoint list failed: %s", e)
        raise ImageGenError("ComfyUI 未配置模型（comfy_checkpoint 为空且无法枚举）")

    async def _sd_retry(self, prompt: str, width: int, height: int,
                        extra_negative: str, seed: int | None,
                        check=None, attempts: int = 3, soft: bool = False,
                        soft_last: bool = False,
                        init_b64: str | None = None,
                        denoise: float = 0.55,
                        matcher=None, **kwargs) -> bytes:
        """SD WebUI 统一生成：外层 attempts 次「变体 seed」重试 + 可选质检回调。

        转变判定器（1.7.32）：matcher 非空时（主角阶段跃迁全量），
        **多候选收集**——每张通过质检的变体都交给 matcher 打分（转变符合度
        +段间同人色差），全部分析完挑最高分返回；这就是「游戏内部自动挑
        转变正确的种子」的实现（不靠外部挑种子）。matcher=None 时行为与
        旧版完全一致（首张合格即收），零额外开销。
        单次请求内部的 OOM/设备错误重试仍由 _request_sdwebui 负责——两层各有
        明确职责：底层救显存/设备分裂，外层救构图/质量不合格。
        soft=True（换装等必须出图的场景）：重试耗尽后接受最后一次结果，
        只记录警告，不再整体失败（否则换装永远无立绘可换）。
        soft_last=True（立绘兜底）：前两次仍严格拦下质检不过的构图，仅第
        三次耗尽后接受并告警——实测同 seed 单独复现能通过、串行队列里却
        连续误杀（构图启发式对低对比/渐变背景不稳），error 死局比一张
        「待定级」立绘伤害更大（玩家可再点重画）。
        init_b64 非空（1.7.8 CG 角色参考底图）：走 img2img（denoise 参数），
        模型从参考底图重绘——角色相似度由底图保证。"""
        import time as _time
        base_seed = seed if seed is not None else int(_time.time()) % (2 ** 31)
        issue = None
        last_raw = None

        async def _recover_if_bad(issue_text: str) -> None:
            """服务劣化信号连续出现 → 重载模型恢复。1.7.32：**任何质检
            issue 都计入**——目标版实机中「疑似腿特写」连续 2 次实为
            同一簇确定性噪点复现（旧条件只认 NaN/主体缺失，漏恢复）；
            每个 ImageClient 实例最多触发一次，避免并发任务反复 reload。"""
            if self._recovered:
                return
            self._degraded_count += 1
            if self._degraded_count >= 2:
                self._recovered = await self._recover_webui()
                self._degraded_count = 0

        async def _variant(s):
            """按 provider 取一版生成（无重试，外层负责 seed 变体与质检）。"""
            if init_b64 and self.provider == "sdwebui":
                return await self._request_sdwebui_img2img(
                    init_b64, prompt, width, height, denoise, s,
                    negative=extra_negative, steps=kwargs.get("steps"))
            if self.provider == "comfyui":
                return await self._request_comfyui(
                    prompt, width, height, extra_negative=extra_negative,
                    seed=s, steps=kwargs.get("steps"),
                    cfg_scale=kwargs.get("cfg_scale"),
                    sampler=kwargs.get("sampler", ""),
                    scheduler=kwargs.get("scheduler", ""))
            return await self._request_sdwebui(
                prompt, width, height, extra_negative=extra_negative,
                seed=s, **kwargs)

        candidates: list[tuple[float, bytes]] = []

        async def _consider(raw_b: bytes) -> bool:
            """质检通过 → 记候选（有 matcher 时打分入列；无 matcher 直接收）。"""
            nonlocal last_raw, issue
            last_raw = raw_b
            if check is None:
                issue = None
                return True
            issue = check(raw_b)
            if issue:
                log.warning("sdwebui quality issue: %s", issue)
                self._issue_count += 1
                await _recover_if_bad(issue)
                return False
            if matcher is not None:
                try:
                    sc = await matcher(raw_b)
                except Exception:
                    sc = None
                if sc is None:
                    # 判定器不可用（interrogate 失败/降级）：首张合格即收
                    # （旧行为）——绝不为判定器多生成、多打标
                    return True
                if float(sc) >= 3.0:
                    # 达标（段核心特征命中且无阶段错位/裸体大罚、
                    # 同人/发色无大漂）：首张即收——interrogate 高频
                    # 会拖死该整合包 API（1.7.32 实机教训），仅不达标
                    # 才追加候选（matcher 模式最多 2 候选）
                    return True
                candidates.append((float(sc), raw_b))
                return False  # 不达标：收集候选，最后挑最优
            issue = None
            return True  # 无判定器：首张合格即收（旧行为）

        # 判定器模式降额（1.7.32）：最多 2 候选（达标即收为理想路径，
        # 不达标最多补 1 张）——3 候选×3 次打标是 SD API 卡死元凶
        if matcher is not None:
            attempts = min(attempts, 2)
        for attempt in range(attempts):
            s = (base_seed + attempt * 1997) if seed is not None else None
            raw = await _variant(s)
            if await _consider(raw):
                return raw
            await asyncio.sleep(1.0)
        if last_raw is not None and ("主体缺失" in str(issue)
                                     or self._issue_count >= 2):
            # 1.7.32：空白/过曝/连续质检失败（含同簇确定性噪点复现：
            # 0/1997/3994 小步链同簇）——用大步长换一组种子再试一轮
            # （每张立绘最多 6 次变体，仍严格质检）。
            for k in range(1, 4):
                s = (base_seed + 1000003 * k) % (2 ** 31) if seed is not None else None
                raw = await _variant(s)
                if await _consider(raw):
                    return raw
        # 转变判定器收尾：所有质量合格候选里挑「转变符合度 + 同人色差」最优者
        if matcher is not None and candidates:
            candidates.sort(key=lambda c: -c[0])
            best_score, best = candidates[0]
            if len(candidates) > 1:
                log.info("tsf stage matcher: picked score %.2f of %d",
                         best_score, len(candidates))
            return best
        if soft and last_raw is not None:
            log.warning("soft-accept portrait with issue: %s", issue)
            return last_raw
        if soft_last and last_raw is not None:
            # 构图类问题（横宽/面积/顶部带）可软接受；空白/过曝类不收——
            # 空白立绘入库比 error 更糟（回退旧版立绘也比纯白好）。
            if "主体缺失" in str(issue):
                raise ImageGenError(f"生成检测未通过（{issue}），已自动重试 {attempts} 次")
            log.warning("soft-accept last attempt with issue: %s", issue)
            return last_raw
        raise ImageGenError(f"生成检测未通过（{issue}），已自动重试 {attempts} 次")

    async def _request_retry(self, prompt: str, check=None,
                             attempts: int = 2, soft: bool = False) -> bytes:
        """OpenAI 兼容线路统一生成：失败/质检不过时按次数重试（尺寸受供应商
        限制，重试次数少于 SD 线路，避免浪费配额）。soft=True 时耗尽后接受
        最后一次结果（换装必须出图）。"""
        issue = None
        last_raw = None
        for attempt in range(attempts):
            try:
                raw = await self._request(prompt)
            except ImageGenError as e:
                issue = str(e)
                await asyncio.sleep(2.0)
                continue
            last_raw = raw
            if check is None:
                return raw
            issue = check(raw)
            if not issue:
                return raw
            log.warning("openai quality issue: %s", issue)
            await asyncio.sleep(1.5)
        if soft and last_raw is not None:
            log.warning("soft-accept portrait with issue: %s", issue)
            return last_raw
        raise ImageGenError(f"生成检测未通过（{issue}），已自动重试 {attempts} 次")



    async def current_model_preset(self) -> dict:
        """当前 WebUI checkpoint 匹配到的适配预设（缓存 60 秒）。"""
        import time as _time
        if self._model_preset is not None:
            return self._model_preset
        if self.mock:
            self._model_preset = dict(DEFAULT_PRESET)
            return self._model_preset
        opts = await self._webui_options()
        checkpoint = opts.get("sd_model_checkpoint", "") or ""
        self._model_preset = match_model_preset(checkpoint)
        self._wai_adapt = wai_adapt_enabled(self.cfg, checkpoint)
        return self._model_preset

    async def _webui_samplers(self) -> list[str]:
        """读取 WebUI 支持的采样器列表（60 秒缓存），用于检测 SDXL Styles。"""
        import time
        now = time.time()
        if self._samp_cache is not None and now - self._samp_at < 60:
            return self._samp_cache
        try:
            base = self.cfg["base_url"].rstrip("/")
            _validate_url(base, allow_local=True)
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{base}/sdapi/v1/samplers")
                r.raise_for_status()
                self._samp_cache = [s.get("name", "") for s in r.json()]
                self._samp_at = now
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self._samp_cache = self._samp_cache or []
        return self._samp_cache

    async def _webui_upscalers(self) -> list[str]:
        """读取 WebUI 支持的放大算法列表（60 秒缓存），用于校验 HR 放大器存在性。"""
        import time
        now = time.time()
        if self._up_cache is not None and now - self._up_at < 60:
            return self._up_cache
        try:
            base = self.cfg["base_url"].rstrip("/")
            _validate_url(base, allow_local=True)
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{base}/sdapi/v1/upscalers")
                r.raise_for_status()
                self._up_cache = [s.get("name", "") for s in r.json()]
                self._up_at = now
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self._up_cache = self._up_cache or []
        return self._up_cache

    async def _reload_checkpoint(self) -> None:
        """强制 WebUI 从磁盘重载当前模型（复位 OOM 后的 CPU/GPU 状态分裂）。

        失败不抛出——仅记录日志；调用方随后会睡眠并重试请求。
        """
        try:
            base = self.cfg["base_url"].rstrip("/")
            _validate_url(base, allow_local=True)
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(f"{base}/sdapi/v1/reload-checkpoint")
                if r.status_code >= 400:
                    log.warning("reload-checkpoint returned %s", r.status_code)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("reload-checkpoint failed: %s", e)

    async def _webui_schedulers(self) -> list[str]:
        """读取 WebUI 支持的调度器列表（60 秒缓存）；老版本无此端点时返回 []。

        Forge/aki 系把 Karras/Exponential 等调度器从采样器名中拆了出来，
        请求参数是 sampler_name + scheduler 两个字段；老 A1111 无此字段。
        """
        import time
        now = time.time()
        if self._sch_cache is not None and now - self._sch_at < 60:
            return self._sch_cache
        try:
            base = self.cfg["base_url"].rstrip("/")
            _validate_url(base, allow_local=True)
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{base}/sdapi/v1/schedulers")
                r.raise_for_status()
                self._sch_cache = [s.get("name", "") for s in r.json()]
                self._sch_at = now
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self._sch_cache = self._sch_cache or []
        return self._sch_cache

    async def _webui_options(self) -> dict:
        """读取 WebUI 当前设置（= 页面上的参数），60 秒缓存。

        让 galgame 生成与用户在 WebUI 页面看到/设置的参数一致：
        采样器、CFG、步数、当前模型。读取失败忽略（回退到请求默认），
        保证 WebUI 刚启动时也不阻塞。
        """
        import time
        now = time.time()
        if self._opts_cache is not None and now - self._opts_at < 60:
            return self._opts_cache
        try:
            base = self.cfg["base_url"].rstrip("/")
            _validate_url(base, allow_local=True)
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{base}/sdapi/v1/options")
                r.raise_for_status()
                self._opts_cache = r.json()
                self._opts_at = now
        except (httpx.HTTPError, ValueError, KeyError):
            self._opts_cache = self._opts_cache or {}
        return self._opts_cache

    async def _sd_alive(self) -> None:
        """SD WebUI 前置健康探测（3 秒快速失败）：SD 掉线/半死时任务立即
        error（资产回退旧版、玩家可继续），绝不让主请求以分钟级超时卡住
        全局串行队列（「游戏卡在加载中」的元凶之一，1.7.23）。"""
        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=True)
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(f"{base}/sdapi/v1/options")
        except httpx.HTTPError as e:
            raise ImageGenError(f"SD WebUI 未响应（{e}）") from e

    async def _request_sdwebui(self, prompt: str, width: int, height: int,
                               extra_negative: str = "",
                               seed: int | None = None,
                               steps: int | None = None,
                               cfg_scale: float | None = None,
                               sampler: str = "",
                               scheduler: str = "",
                               hr: bool = False,
                               hr_upscaler: str = "Latent",
                               hr_scale: float = 2.0,
                               hr_steps: int = 10,
                               hr_denoise: float = 0.5) -> bytes:
        """本地 SD WebUI（A1111/Forge）: POST /sdapi/v1/txt2img。无需 API Key。

        全程以信号量串行化（一次一张），并针对单张失败重试一次；
        超时提高到 15 分钟，防止排队+生成超限被误判为失败。
        """
        import base64

        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=True)
        url = f"{base}/sdapi/v1/txt2img"
        negative = ", ".join(x for x in [
            self.cfg.get("negative_prompt",
                         "lowres, bad anatomy, bad hands, watermark, "
                         "extra digits, blurry"),
            extra_negative,
        ] if x)
        sync = bool(self.cfg.get("sync_webui", True))
        opts = await self._webui_options() if sync else {}
        sampler_names = (await self._webui_samplers()
                         if sync and self.cfg.get("auto_sdxl_styles", True) else [])
        if not sampler:
            sampler = _pick_sampler(
                self.cfg.get("sd_sampler", ""),
                opts.get("sampler_name", ""), sampler_names,
                bool(self.cfg.get("auto_sdxl_styles", True)))
        # 步数/CFG：调用方已按模型预设或显式配置传入；未传时才走面积启发/页面值
        if steps is None:
            area = width * height
            steps = 34 if area >= 1500000 else (30 if area >= 1000000 else 26)
        if cfg_scale is None:
            cfg_scale = (float(self.cfg["sd_cfg"])
                         if self.cfg.get("sd_cfg") else opts.get("cfg_scale", 7))
        if scheduler and sync:
            sched_list = await self._webui_schedulers()
            if scheduler not in sched_list:
                log.warning("scheduler '%s' not in WebUI, fallback automatic",
                            scheduler)
                scheduler = ""
        payload = {
            "prompt": prompt,
            "negative_prompt": negative,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "sampler_name": sampler,
            "batch_size": 1,
            "n_iter": 1,
        }
        if scheduler:
            payload["scheduler"] = scheduler
        # HR 修复（模型预设开启时）：512 基底生成 + 放大去噪，写实/动漫模型
        # 高分辨率直出易重复肢体，HR 修复让解剖保持正确并提升细节
        if hr:
            if sync:
                upscalers = await self._webui_upscalers()
                if upscalers and hr_upscaler not in upscalers:
                    log.warning("upscaler '%s' not in WebUI, fallback Latent",
                                hr_upscaler)
                    hr_upscaler = "Latent"
            payload.update({
                "enable_hr": True,
                "hr_upscaler": hr_upscaler,
                "hr_scale": hr_scale,
                "hr_second_pass_steps": hr_steps,
                "denoising_strength": hr_denoise,
            })
        # 1.7.46 wai 特适配：ClipSkip 2（官方推荐；override_settings 仅本次请求生效）
        if getattr(self, "_wai_adapt", False) and (self._model_preset or {}).get("clip_skip") == 2:
            payload["override_settings"] = {"CLIP_stop_at_last_layers": 2}
        if seed is not None:
            # 固定种子：同角色不同表情共享同一 seed，人物形象保持一致
            payload["seed"] = int(seed) % (2 ** 31)
        headers = self._auth_headers()
        last_err = None
        degraded = False
        reloaded = False
        for attempt in range(3):
            try:
                async with self._sem:
                    await self._sd_alive()
                    async with httpx.AsyncClient(
                            timeout=httpx.Timeout(300.0, connect=5.0,
                                                  read=280.0)) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        resp.raise_for_status()
                        data = resp.json()
                log.info("sd image generated (%dx%d, steps=%s, seed=%s, hr=%s)",
                         width, height,
                         payload.get("steps"), payload.get("seed"),
                         bool(payload.get("enable_hr")))
                return base64.b64decode(data["images"][0])
            except httpx.HTTPStatusError as e:
                body = e.response.text[:400]
                is_oom = ("OutOfMemory" in body or "out of memory" in body.lower())
                # 显存不足：HR 修复在高分辨率第二遍最易爆显存——自动降级重试一次
                if e.response.status_code == 500 and is_oom                         and payload.get("enable_hr") and not degraded:
                    degraded = True
                    payload["enable_hr"] = False
                    log.warning("SD WebUI OOM, wait 10s then retry without HR")
                    await asyncio.sleep(10)
                    continue
                # OOM 后 WebUI 模型权重可能分裂到 CPU/GPU（device mismatch），
                # 之后所有请求都会 500："Expected all tensors to be on the same
                # device"——此时 reload-checkpoint 强制复位模型再重试。
                broken = ("Expected all tensors to be on the same device" in body
                          or is_oom or "device mismatch" in body.lower())
                if e.response.status_code == 500 and broken and not reloaded:
                    reloaded = True
                    log.warning("SD WebUI cuda device state broken, reload "
                                "checkpoint and retry")
                    await self._reload_checkpoint()
                    await asyncio.sleep(10)
                    continue
                last_err = ImageGenError(
                    f"SD WebUI 返回 {e.response.status_code}：{body}")
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                last_err = ImageGenError(
                    f"SD WebUI 生成失败（第 {attempt + 1} 次）：{e}")
        raise last_err

    async def _request_sdwebui_img2img(self, init_b64: str, prompt: str,
                                       width: int, height: int,
                                       denoise: float, seed: int,
                                       negative: str = "",
                                       steps: int | None = None) -> bytes:
        """本地 SD WebUI img2img（脸部/换装细化用）：单请求重试一次。"""
        import base64
        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=True)
        payload = {
            "init_images": [init_b64],
            "prompt": prompt,
            "negative_prompt": negative,
            "width": width,
            "height": height,
            # 1.7.43：链式步数自主调控——按模型预设步数-4（下限 18），
            # 显式传入仍优先
            "steps": steps or best_chain_steps(self.cfg),
            "cfg_scale": float(self.cfg.get("sd_cfg", 5.5)),
            "sampler_name": self.cfg.get("sd_sampler") or "DPM++ 2M",
            "denoising_strength": denoise,
            "batch_size": 1,
            "n_iter": 1,
            "seed": int(seed) % (2 ** 31),
        }
        # 1.7.46 wai 特适配：ClipSkip 2（链式 img2img 同样生效）
        if getattr(self, "_wai_adapt", False) and (self._model_preset or {}).get("clip_skip") == 2:
            payload["override_settings"] = {"CLIP_stop_at_last_layers": 2}
        headers = self._auth_headers()
        last_err = None
        for attempt in range(2):
            try:
                async with self._sem:
                    await self._sd_alive()
                    async with httpx.AsyncClient(
                            timeout=httpx.Timeout(300.0, connect=5.0,
                                                  read=280.0)) as client:
                        resp = await client.post(
                            f"{base}/sdapi/v1/img2img", json=payload, headers=headers)
                        resp.raise_for_status()
                        data = resp.json()
                return base64.b64decode(data["images"][0])
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                last_err = ImageGenError(
                    f"SD WebUI 图生图失败（第 {attempt + 1} 次）：{e}")
                await asyncio.sleep(3.0)
        raise last_err

    async def _request_sdwebui_inpaint(self, init_b64: str, mask_b64: str,
                                       prompt: str, width: int, height: int,
                                       denoise: float, seed: int,
                                       negative: str = "",
                                       steps: int | None = None,
                                       padding: int = 32,
                                       full_res: bool = True) -> bytes:
        """本地 SD WebUI inpaint（掩膜区域重绘，换装限定服装区域用）。

        1.6.96：padding 参数化——换装管线用 16：A1111 full-res inpaint 会把
        掩膜外围 padding 像素揽入重绘以实现无缝，32px 会把下巴/脸缘拽进去
        （「换装后脸下半扭曲」根因之一）。"""
        import base64
        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=True)
        payload = {
            "init_images": [init_b64],
            "mask": mask_b64,
            "prompt": prompt,
            "negative_prompt": negative,
            "width": width,
            "height": height,
            "steps": (min(int(steps), 32) if steps
                      else min(int(self.cfg.get("sd_steps", 20)), 20)),
            "cfg_scale": float(self.cfg.get("sd_cfg", 5.5)),
            "sampler_name": self.cfg.get("sd_sampler") or "DPM++ 2M",
            "denoising_strength": denoise,
            "inpaint_full_res": full_res,
            "inpaint_full_res_padding": padding,
            "inpainting_fill": 1,  # 以原图像素为底重绘（保留身形/姿势线索）
            "mask_blur": 6,
            "batch_size": 1,
            "n_iter": 1,
            "seed": int(seed) % (2 ** 31),
        }
        # 1.7.46 wai 特适配：ClipSkip 2（inpaint 换装同样生效）
        if getattr(self, "_wai_adapt", False) and (self._model_preset or {}).get("clip_skip") == 2:
            payload["override_settings"] = {"CLIP_stop_at_last_layers": 2}
        headers = self._auth_headers()
        last_err = None
        for attempt in range(2):
            try:
                async with self._sem:
                    await self._sd_alive()
                    async with httpx.AsyncClient(
                            timeout=httpx.Timeout(300.0, connect=5.0,
                                                  read=280.0)) as client:
                        resp = await client.post(
                            f"{base}/sdapi/v1/img2img", json=payload, headers=headers)
                        resp.raise_for_status()
                        data = resp.json()
                return base64.b64decode(data["images"][0])
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                last_err = ImageGenError(
                    f"SD WebUI 限定重绘失败（第 {attempt + 1} 次）：{e}")
                await asyncio.sleep(3.0)
        raise last_err

    def _tune(self, cfg_key: str, auto_val, fallback):
        """自主调参（image.auto_tune，默认开）：忽略用户手动写入的
        sd_steps/sd_cfg/sd_sampler/sd_scheduler，按「模型预设 + 质量档」
        自动取值；auto_tune 关闭时才尊重用户显式配置。"""
        if not self.cfg.get("auto_tune", True) and self.cfg.get(cfg_key):
            return self.cfg[cfg_key]
        return auto_val if auto_val is not None else fallback

    async def generate_cg(self, hint: str, style_suffix: str,
                          cast_en: str = "",
                          sprite_cutouts: list[bytes] | None = None) -> bytes:
        """桥段 CG 生成（イベントCG 专用链）：角色演出 + 横版构图 + 氛围光影。

        1.7.8 角色相似度：传入**角色立绘 PNG**（主角+女主当前立绘）时走
        img2img——立绘缩放到画布高约 70%、从左至右铺在灰底画布上作为
        参考底图（首次 denoise 0.55）：模型从"这两个立绘"重绘成事件 CG，
        发色/瞳色/服装/脸型天然高相似；场景/动作/氛围由提示词（LLM 提炼 +
        角色词 + CG 风格后缀）驱动。无立绘时回退 txt2img。"""
        if self.mock:
            await asyncio.sleep(0.8)
            return mock_background(hint)
        prompt = (f"{cast_en}，{hint}，{style_suffix}{CG_EN_SUFFIX}"
                  if cast_en else f"{hint}，{style_suffix}{CG_EN_SUFFIX}")
        if self.provider in ("sdwebui", "comfyui"):
            size, cg_cap = effective_cg(self.cfg)
            w, h = self._parse_size(size)
            preset = (await self.current_model_preset()
                      if self.provider == "sdwebui" and self.cfg.get(
                          "model_presets", True) else None)
            preset_steps = min(int(preset.get("steps", 30)), cg_cap) if preset else min(25, cg_cap)
            preset_cfg = preset.get("cfg", 6.5) if preset else 6.5
            preset_sampler = preset.get("sampler", "DPM++ 2M") if preset else "DPM++ 2M"
            preset_sched = preset.get("scheduler", "karras") if preset else "karras"
            neg = ", ".join(x for x in [
                self.cfg.get("negative_prompt", ""), CG_NEG] if x)
            # —— 立绘参考底图（角色相似度）——
            if sprite_cutouts and self.provider == "sdwebui":
                try:
                    import base64 as _b64
                    from PIL import Image as _Pim
                    canvas = _Pim.new("RGB", (w, h), (222, 222, 226))
                    sprites = []
                    for raw in sprite_cutouts[:3]:
                        try:
                            sprites.append(_Pim.open(io.BytesIO(raw)).convert("RGBA"))
                        except Exception:
                            continue
                    if sprites:
                        target_h = max(8, int(h * 0.70))
                        total_w = 0
                        scaled = []
                        for s in sprites:
                            sw = max(8, int(s.width) * target_h // max(1, int(s.height)))
                            scaled.append((s.resize((sw, target_h), _Pim.LANCZOS), sw))
                            total_w += sw
                        gap = max(8, (w - total_w) // (len(scaled) + 1))
                        x = gap
                        for s, sw in scaled:
                            canvas.paste(s, (x, h - target_h), s)
                            x += sw + gap
                        buf = io.BytesIO()
                        canvas.save(buf, "PNG")
                        prompt += (", the same girls as in the reference image, "
                                   "same character designs, same hair and eye "
                                   "colors, same outfits")
                        return await self._sd_retry(
                            prompt, w, h, neg, None, check=_bg_check,
                            steps=int(self._tune("sd_steps", preset_steps, preset_steps)),
                            cfg_scale=float(self._tune("sd_cfg", preset_cfg, preset_cfg)),
                            sampler=str(self._tune("sd_sampler", preset_sampler, preset_sampler)),
                            scheduler=str(self._tune("sd_scheduler", preset_sched, preset_sched)),
                            init_b64=_b64.b64encode(buf.getvalue()).decode(),
                            denoise=0.55)
                except Exception as e:
                    log.warning("cg sprite reference skipped: %s", e)
            return await self._sd_retry(
                prompt, w, h, neg, None, check=_bg_check,
                steps=int(self._tune("sd_steps", preset_steps, preset_steps)),
                cfg_scale=float(self._tune("sd_cfg", preset_cfg, preset_cfg)),
                sampler=str(self._tune("sd_sampler", preset_sampler, preset_sampler)),
                scheduler=str(self._tune("sd_scheduler", preset_sched, preset_sched)),
            )
        return await self._request_retry(prompt, check=_bg_check)

    async def _interrogate(self, image_b64: str,
                           model: str = "clip",
                           timeout: float = 180.0) -> str:
        """A1111 interrogate：给生成图打英文描述（转变判定器用）。

        显存安全（1.7.32 闪退教训）：8GB 卡 + SDXL 驻留时，A1111 为
        interrogate 再加载 BLIP+CLIP 标注模型（约 +1GB）会直接 OOM 崩
        WebUI——故先 `unloadapi` 卸载主模型（打标仅需 ~1GB），打完标后
        下一次生成由 A1111 自动重载主模型（几十秒，仅阶段跃迁时发生）。
        失败返回空串——判定器只做「挑优」，绝不因它抛错影响出图。
        仅 sdwebui 线路支持（comfyui 无此端点，调用方控制）。"""
        if self.provider != "sdwebui":
            return ""
        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=True)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # A1111 的 interrogate 内部自带 send_everything_to_cpu +
                # torch_gc（1.7.32 闪退排查结论）——主模型先挪出显存再加载
                # BLIP+CLIP，无需游戏侧 unload（unload 反而让下一次生成
                # 额外重载主模型）
                r = await client.post(f"{base}/sdapi/v1/interrogate",
                                      json={"image": image_b64, "model": model})
                r.raise_for_status()
            return str(r.json().get("caption") or "")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("interrogate failed: %s", e)
            return ""

    async def _has_tagger(self) -> bool:
        """wd14-tagger 插件探测（1.7.47）：GET /tagger/v1/interrogators 判存；
        结果缓存整会话。失败/未装返回 False——判定器自动回退旧 interrogate。"""
        if self.provider != "sdwebui" or not self.cfg.get("tsf_tagger_boost", True):
            return False
        if self._tagger_ok >= 0:
            return bool(self._tagger_ok)
        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=True)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{base}/tagger/v1/interrogators")
                r.raise_for_status()
                ok = bool(r.json())
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            ok = False
        self._tagger_ok = 1 if ok else 0
        log.info("wd14-tagger %s", "available" if ok else "not found (fallback interrogate)")
        return ok

    async def _tagger_interrogate(self, image_b64: str,
                                  timeout: float = 180.0) -> str:
        """wd14-tagger 打标（1.7.47）：booru 标签集——与转变判定器的
        core/expected/forbidden 英文词集天然匹配（比 BLIP/CLIP 整句准）。
        失败返回空串（调用方回退 _interrogate）。"""
        base = self.cfg["base_url"].rstrip("/")
        _validate_url(base, allow_local=True)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{base}/tagger/v1/interrogate",
                                      json={"image": image_b64, "threshold": 0.35})
                r.raise_for_status()
            return str(r.json().get("caption") or "")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("tagger id failed: %s", e)
            return ""

    async def _recover_webui(self) -> bool:
        """服务劣化恢复：reload-checkpoint 重载当前模型（A1111 官方恢复手段，
        清 NaN 采样状态与 VAE 缓存）。重载耗时较长，只发生一次。"""
        if self.provider != "sdwebui":
            return False
        try:
            import httpx as _httpx
            base = self.cfg["base_url"].rstrip("/")
            async with _httpx.AsyncClient(timeout=180) as c:
                await c.post(f"{base}/sdapi/v1/reload-checkpoint")
            log.warning("webui degraded -> reload-checkpoint issued")
            self._opts_cache = None
            self._samp_cache = None
            await asyncio.sleep(6.0)
            return True
        except Exception as e:
            log.warning("reload-checkpoint failed: %s", e)
            return False

    async def refine_face(self, cut_png: bytes, ref_png: bytes,
                          prompt: str, seed: int,
                          denoise: float = 0.35, steps: int = 24) -> bytes:
        """脸部精细五官重绘（仅 SD WebUI 线路）：裁「脸部放大上下文」
        （脸部 ~2.6 倍宽、~2.4 倍高的窗口：含发际线/双肩）→ 无损放大到
        min 边长 640（8 倍数）→ 脸部椭圆掩膜 inpaint（24 步 / 0.35 低噪）。

        相比旧版（整幅缩到 512x1280、脸部只占小半画布），脸部在放大后的
        canvas 上占比高得多，五官（虹膜/睫毛/唇）更精致；原图仅脸区重绘，
        发型/服装/姿势零改动 → 表情差分「同一个人换表情」。alpha 硬阈值
        （≥190）防叠脸前的半透明发丝鬼影；异常原样返回。"""
        if self.mock or self.provider != "sdwebui":
            return cut_png
        try:
            import base64
            from PIL import ImageChops as _chops
            from PIL import ImageDraw, ImageFilter
            im = Image.open(io.BytesIO(cut_png)).convert("RGBA")
            w, h = im.size
            if w < 120 or h < 200:
                return cut_png
            a = im.getchannel("A")
            box = _face_box(im)
            if not box:
                return cut_png
            fx0, fy0, fx1, fy1 = box
            fw, fh = fx1 - fx0, fy1 - fy0
            # 脸部上下文窗口（头顶多留发际空间），钳制在画布内
            ctx_w = max(fw * 3, int(fw * 2.6))
            ctx_h = max(fh * 3, int(fh * 2.4))
            cx0 = max(0, fx0 - int((ctx_w - fw) * 0.5))
            cy0 = max(0, fy0 - int((ctx_h - fh) * 0.55))
            cx1 = min(w, cx0 + int(ctx_w))
            cy1 = min(h, cy0 + int(ctx_h))
            cx0 = max(0, cx1 - int(ctx_w))
            cy0 = max(0, cy1 - int(ctx_h))
            crop = im.crop((cx0, cy0, cx1, cy1))
            cw, ch = crop.size
            if min(cw, ch) < 160:
                return cut_png
            s = max(1.0, 640.0 / min(cw, ch))
            nw, nh = int(cw * s), int(ch * s)
            nw, nh = ((nw + 7) // 8) * 8, ((nh + 7) // 8) * 8
            crop_up = crop.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGB", (nw, nh), (222, 222, 226))
            canvas.paste(crop_up, (0, 0), crop_up.getchannel("A"))
            # 脸部椭圆掩膜（crop 画布坐标：含眉~下巴）
            fbx0 = int((fx0 - cx0) * s)
            fby0 = int((fy0 - cy0) * s)
            fbx1 = int(max((fx1 - cx0) * s, fbx0 + 40))
            fby1 = int(max((fy1 - cy0) * s, fby0 + 40))
            mask = Image.new("L", (nw, nh), 0)
            ImageDraw.Draw(mask).ellipse((fbx0, fby0, fbx1, fby1), fill=255)
            mask = mask.filter(ImageFilter.GaussianBlur(12))
            # alpha 硬阈值：只重画实心皮肤区，叠脸前的半透明发丝不动（防鬼影）
            a_up = crop_up.getchannel("A").point(lambda v: 255 if v >= 190 else 0)
            full = Image.new("L", (nw, nh), 0)
            full.paste(a_up, (0, 0))
            mask = _chops.multiply(mask, full)
            buf = io.BytesIO()
            canvas.save(buf, "PNG")
            mbuf = io.BytesIO()
            mask.save(mbuf, "PNG")
            neg = PORTRAIT_NEG_EN + (", blurry face, smudged features, "
                                     "deformed face, melted face, soft blur, "
                                     "flat face, no detail, different person, "
                                     "different hairstyle, different outfit")
            new_raw = await self._request_sdwebui_inpaint(
                base64.b64encode(buf.getvalue()).decode(),
                base64.b64encode(mbuf.getvalue()).decode(),
                prompt + (", beautiful detailed face, refined facial features, "
                          "delicate expressive eyes with detailed iris, "
                          "neat eyelashes, smooth soft skin, polished finish, "
                          "same person, same hairstyle, same hair color, "
                          "same outfit"),
                nw, nh, denoise, seed, negative=neg, steps=steps)
            new_img = Image.open(io.BytesIO(new_raw)).convert("RGB").resize((cw, ch))
            rgb = im.convert("RGB").copy()
            # 只用「脸部椭圆掩膜」粘贴（非裁切区整块粘贴）：椭圆外的
            # 身体/发丝/背景像素完全不动——「只变表情」由粘贴语义保证
            paste_mask = mask.resize((cw, ch), Image.BILINEAR)
            rgb.paste(new_img, (cx0, cy0), paste_mask)
            rgb = rgb.convert("RGBA")
            rgb.putalpha(a)
            out = io.BytesIO()
            rgb.save(out, "PNG")
            out_png = out.getvalue()
            # ----「只变表情」保障（1.6.75）：贴回后逐像素校验 ----
            try:
                from PIL import ImageChops as _chop, ImageDraw as _draw2
                out_rgb = Image.open(io.BytesIO(out_png)).convert("RGB")
                base_rgb = im.convert("RGB")
                delta = _chop.difference(out_rgb, base_rgb).convert("L")
                # 1) 脸部区域：必须有实际变化（表情确实变了）
                chk_face = Image.new("L", (w, h), 0)
                ew, eh = int(fw * 0.15), int(fh * 0.15)
                _draw2.Draw(chk_face).ellipse(
                    (max(0, fx0 - ew), max(0, fy0 - eh),
                     min(w, fx1 + ew), min(h, fy1 + eh)), fill=255)
                face_delta = _chop.multiply(delta, chk_face)
                fpx = list(face_delta.resize((48, 48)).getdata())
                face_changed = sum(1 for v in fpx if v > 8)
                if face_changed < 12:
                    log.warning("refine_face 脸部无变化（表情未生效）→ 弃用")
                    return cut_png
                # 2) 裁切区外：必须零漂移（身体/背景不在变更区）
                chk_out = Image.new("L", (w, h), 0)
                _draw2.Draw(chk_out).rectangle((cx0, cy0, cx1, cy1), fill=255)
                outside = _chop.subtract(Image.new("L", (w, h), 255), chk_out)
                out_diff = _chop.multiply(delta, outside)
                opx = list(out_diff.resize((96, 96)).getdata())
                out_ratio = sum(1 for v in opx if v > 8) / max(1, len(opx))
                if out_ratio > 0.002:
                    log.warning("refine_face 脸外漂移 %.4f → 弃用", out_ratio)
                    return cut_png
                # 3) 裁切区内、脸部椭圆外的边缘混洗（发丝/半透明羽化）容忍
                face_mid = _chop.multiply(delta, chk_out)
                mid = _chop.subtract(face_mid, _chop.multiply(delta, chk_face))
                mpx = list(mid.resize((96, 96)).getdata())
                mid_ratio = sum(1 for v in mpx if v > 8) / max(1, len(mpx))
                if mid_ratio > 0.02:
                    log.warning("refine_face 脸外局部漂移 %.3f → 弃用", mid_ratio)
                    return cut_png
            except Exception as e:
                log.warning("refine_face guard skipped: %s", e)
            return out_png
        except Exception as e:
            log.warning("face refine skipped: %s", e)
            return cut_png

    async def refine_transition(self, cut_png: bytes, prompt: str, seed: int,
                                denoise: float = 0.42,
                                body_pass: bool = False,
                                body_denoise: float = 0.42,
                                hair_words: str = "",
                                negative_extra: str = "",
                                hair_mask: bool = True) -> bytes:
        """TSF 演化引擎（仅 SD WebUI 线路）——两段式：
        第一遍·整幅 img2img：整幅采用「低重绘度」——脸部结构不乱、五官不
        崩（教学：保身份 0.30~0.45），整体只是缓慢软化/渐变的底；
        第二遍·掩膜精细演化（body_pass=True 或跨阶段）：身体曲线区（胸/
        腰/臀/大腿连通掩膜）+ **发区联动**（1.7.15：头肩发带掩膜剔除脸部
        椭圆锚定——发长在身体掩膜外，整幅低噪不会长头发，必须给发区单独
        0.45~0.55 重绘；脸部/四肢零干扰）。
        hair_words 按同化率档由调用方传入（发长渐进词）。任何一步异常都会
        回退（保留已有结果），绝不破坏成品。"""
        if self.mock or self.provider != "sdwebui":
            return cut_png
        try:
            import base64
            from PIL import ImageDraw, ImageFilter, ImageChops
            im = Image.open(io.BytesIO(cut_png)).convert("RGBA")
            w, h = im.size
            if w < 120 or h < 200:
                return cut_png
            a = im.getchannel("A")

            # ---- 第一步：整幅演化 ----
            s = min(1.0, 608.0 / w, 1664.0 / h)
            nw, nh = max(64, int(w * s)), max(64, int(h * s))
            canvas_w, canvas_h = ((nw + 7) // 8) * 8, ((nh + 7) // 8) * 8
            canvas = Image.new("RGB", (canvas_w, canvas_h), (222, 222, 226))
            canvas.paste(im.resize((nw, nh)),
                         ((canvas_w - nw) // 2, (canvas_h - nh) // 2),
                         a.resize((nw, nh)))
            buf = io.BytesIO()
            canvas.save(buf, "PNG")
            step1_raw = await self._request_sdwebui_img2img(
                base64.b64encode(buf.getvalue()).decode(),
                prompt + (", same character evolving gradually, soft smooth "
                          "transition, gentle change, keep same pose"),
                canvas_w, canvas_h, denoise, seed,
                # 1.7.26：并入会话档位负面（stage_negative_relaxed）——
                # 链式两遍此前只带通用负面，没有「当前段该压制的性别特征」
                # 档位词——实测 20→30% 一步被模型直接转女（中性词+无封杀
                # = 模型默认少女）。负面增补后档位才真正守得住
                negative=PORTRAIT_NEG_EN + (
                    ", blurry face, deformed body, extra limbs, melt, "
                    "hair color change, teal hair, green hair, mint hair, "
                    "blonde hair, pink hair, red hair"
                    + (", " + negative_extra if negative_extra else "")),
                steps=min(int(self.cfg.get("sd_steps", 20)), 18))
            step1 = Image.open(io.BytesIO(step1_raw)).convert("RGB").resize((w, h))
            result = Image.new("RGBA", (w, h))
            result.paste(step1, (0, 0), a)
            result.putalpha(a)

            # ---- 第二步：「躯干+腰臀+大腿」连通掩膜精细演化 ----
            # （连通一体且覆盖到大腿上段：确保身形变化时上下身比例同步适配，
            #   绝不会出现「胸腰变了腿没跟上」的接缝/脱落感；
            #   脸部/四肢不在掩膜内——脸部由第一遍低噪整幅渐进演化）
            if denoise >= 0.40 or body_pass:
                try:
                    bbox = a.getbbox()
                    if bbox:
                        x0, y0, x1, y1 = bbox
                        bw, bh = x1 - x0, y1 - y0
                        if bw >= 60 and bh >= 120:
                            by0 = y0 + int(bh * 0.20)   # 肩下
                            by1 = y0 + int(bh * 0.72)   # 大腿中段
                            bx0 = x0 + int(bw * 0.18)
                            bx1 = x1 - int(bw * 0.18)
                            # —— 第二步（1.7.15 重写）：「躯干+发区」**视窗裁剪**
                            # inpaint——整幅 inpaint 会被模型整体重构（换人/
                            # 换姿势），junction 守卫永远拒绝 → 回退整幅低噪 →
                            # 变化不可见（阶段 2↔3 同图真凶）。现改为：裁剪
                            # 「头顶~大腿中段」视窗 → 视窗内掩膜 inpaint
                            # （0.45~0.55, full_res=False）→ **只按掩膜把新图
                            # 贴回**原画布——脸/四肢/服装边缘零接触，模型在
                            # 视窗内局部演进（refine_face/换装同款已验证架构）
                            wx0, wy0, wx1, wy1 = x0, y0, x1, y0 + int(bh * 0.75)
                            padx = int(bw * 0.04)
                            wx0 = max(0, wx0 - padx)
                            wx1 = min(w, wx1 + padx)
                            wy1 = min(h, wy1 + int(bh * 0.02))
                            cw_, ch_ = wx1 - wx0, wy1 - wy0
                            if cw_ >= 60 and ch_ >= 120:
                                # hair_mask=False（1.7.30 阶段跃迁高重绘）：
                                # 剥离发区掩膜——发长交给整幅 0.45 驱动，
                                # 免「发区掩膜×高重绘」画出晶簇碎片（80 实测）
                                fbox = _face_box(im) if hair_mask else None
                                s3 = min(1.0, 720.0 / ch_, 608.0 / cw_)
                                nw3 = max(8, int(cw_ * s3) // 8 * 8)
                                nh3 = max(8, int(ch_ * s3) // 8 * 8)
                                crop = result.crop((wx0, wy0, wx1, wy1))
                                crop_a = a.crop((wx0, wy0, wx1, wy1))
                                c3 = Image.new("RGB", (nw3, nh3), (222, 222, 226))
                                c3.paste(crop.resize((nw3, nh3), Image.LANCZOS),
                                         (0, 0), crop_a.resize((nw3, nh3)))
                                # 视窗坐标掩膜：躯干曲线区 ∪ 发区带 − 脸椭圆
                                m3 = Image.new("L", (nw3, nh3), 0)
                                dr3 = ImageDraw.Draw(m3)
                                by0v, by1v = by0 - wy0, by1 - wy0
                                bx0v, bx1v = bx0 - wx0, bx1 - wx0
                                dr3.ellipse((int(bx0v * s3), int(by0v * s3),
                                             int(bx1v * s3), int(by1v * s3)),
                                            fill=255)
                                dr3.rectangle((int(bx0v * 1.06 * s3),
                                               int((by0v + (by1v - by0v)
                                                    * 0.45) * s3),
                                               int(bx1v * 0.94 * s3),
                                               int(by1v * s3)), fill=255)
                                hbx0, hbx1 = (x0 + int(bw * 0.02) - wx0,
                                              x1 - int(bw * 0.02) - wx0)
                                # 1.7.15：发带高度随进度（短发期 0.45、长发期
                                # 0.58——长发垂过胸口需要掩膜覆盖到胸下）
                                hair_h = int(bh * (0.58 if "very long" in
                                                   hair_words or
                                                   "reaching chest" in
                                                   hair_words else 0.45))
                                dr3.rectangle((int(hbx0 * s3), 0,
                                               int(hbx1 * s3),
                                               int((y0 + hair_h
                                                    - wy0) * s3)), fill=255)
                                if fbox:
                                    fx0, fy0, fx1, fy1 = fbox
                                    fcxv = (fx0 + fx1) / 2 - wx0
                                    fcyv = (fy0 + fy1) / 2 - wy0
                                    frx3 = int((fx1 - fx0) * 0.62 * s3)
                                    fry3 = int((fy1 - fy0) * 1.0 * s3)
                                    dr3.ellipse((int(fcxv * s3) - frx3,
                                                 int(fcyv * s3) - fry3,
                                                 int(fcxv * s3) + frx3,
                                                 int(fcyv * s3) + fry3),
                                                fill=0)
                                m3 = m3.filter(ImageFilter.GaussianBlur(8))
                                hard3 = crop_a.resize((nw3, nh3)).point(
                                    lambda v: 255 if v >= 240 else 0)
                                m3 = ImageChops.multiply(m3, hard3)
                                bbuf3 = io.BytesIO()
                                c3.save(bbuf3, "PNG")
                                mbuf3 = io.BytesIO()
                                m3.save(mbuf3, "PNG")
                                # 掩膜引导词**置于最前**（位置坑：末尾被稀释）
                                hair_suffix = ((", " + hair_words)
                                               if hair_words else "")
                                step2_raw = await self._request_sdwebui_inpaint(
                                    base64.b64encode(bbuf3.getvalue()).decode(),
                                    base64.b64encode(mbuf3.getvalue()).decode(),
                                    ", body gradually developing feminine "
                                    "curves, chest softly growing, waist "
                                    "narrowing, hips widening, fuller rounded "
                                    "thighs, feminine figure visible in "
                                    "clothing, same shirt and pants, nothing "
                                    "added to the outfit, same clothes, "
                                    "same hair color, same eye color"
                                    + hair_suffix
                                    + ", " + prompt
                                    + ", same face identity, soft gentle "
                                      "body change",
                                    nw3, nh3, body_denoise, seed + 1,
                                    negative=PORTRAIT_NEG_EN
                                    + (", masculine body, muscular, wide "
                                       "shoulders, flat chest, deformed body, "
                                       "extra limbs, detached body, "
                                       "disconnection, missing limbs, wrong "
                                       "proportions, mismatched parts, body "
                                       "parts falling apart, hair color "
                                       "change, teal hair, green hair, "
                                       "mint hair, jacket tied around waist, "
                                       "sweater tied around waist, item on "
                                       "waist, bag at waist, added garment, "
                                       "extra accessories, clothes around "
                                       "waist, props"
                                       + (", " + negative_extra
                                          if negative_extra else "")),
                                    full_res=False, padding=0)
                                step2 = Image.open(io.BytesIO(step2_raw)).convert(
                                    "RGB").resize((cw_, ch_))
                                rgb = result.convert("RGB").copy()
                                # 贴回掩膜软边缘：全分辨率羽化 6px，消除
                                # 接缝/鬼影（贴回边界硬 = 可见缝）
                                pm = m3.resize((cw_, ch_),
                                               Image.BILINEAR).filter(
                                    ImageFilter.GaussianBlur(6))
                                rgb.paste(step2, (wx0, wy0), pm)
                                cand = rgb.convert("RGBA")
                                cand.putalpha(a)
                                if _junction_ok(cand):
                                    result = cand
                                    log.info("transition body-pass window "
                                             "refine ok")
                                else:
                                    log.warning("transition body-pass rejected "
                                                "(junction broken), keep step1")
                except Exception as e:
                    log.warning("transition body-pass skipped: %s", e)
            out = io.BytesIO()
            result.save(out, "PNG")
            return out.getvalue()
        except Exception as e:
            log.warning("transition refine skipped: %s", e)
            return cut_png

    async def refine_outfit(self, cut_png: bytes, prompt: str, seed: int,
                            denoise: float = None,
                            nude_mode: str = "dressed") -> bytes:
        """换装图生图（仅 SD WebUI 线路）：整图等比缩放作底图，掩膜限定
        「领口以下（含手臂）到脚底」的服装区域重绘——新装上身，脸/头发/姿势
        原封不动；比全量生成立绘省算力且零表情损失。异常时原样返回。

        1.6.88 适配：服装掩膜起点避开面部（0.22）+ 脸部保护带回贴锚定 +
        denoise 保守（脸绝不偏移）。
        1.6.91 换装目标动态适配（nude_mode）：
        · dressed：denoise 0.50，负面含裸体/内衣封杀（默认着装）；
        · underwear（内衣/内裤）：denoise 0.45，仅禁全裸——允许内衣状态；
        · nude（裸体）：denoise 0.40，「只脱衣不重塑身形」——负面移除全部
          裸体/内衣类（此前硬编码封杀 nude 导致「画裸体又被禁止」→ 模型
          画出怪异半裸畸形：腿部过长/身材杆状/形体错乱）。
        三者提示词均附加「同身形/同比例/同姿态」锁词。"""
        if self.mock or self.provider != "sdwebui":
            return cut_png
        try:
            import base64
            from PIL import ImageDraw, ImageFilter, ImageChops
            im = Image.open(io.BytesIO(cut_png)).convert("RGBA")
            w, h = im.size
            a = im.getchannel("A")
            bbox = a.getbbox()
            if not bbox:
                return cut_png
            x0, y0, x1, y1 = bbox
            bw, bh = x1 - x0, y1 - y0
            if bw < 60 or bh < 120:
                return cut_png
            # 服装掩膜（原图坐标）：**以脸盒下缘为服装上端**（自适应任意
            # 身高/比例/构图——不再是固定比例 0.155：矮个/高个/坐姿/侧面
            # 都能正确对位）。此前「掩膜上端 0.155 < 锚定下缘 0.17」之间的
            # 抠回带会把旧高领残留在下巴正下方（"深灰高领环"）。
            face_box_ = _face_box(im)
            if face_box_:
                cloth_y = max(0, min(h, int(face_box_[3])))
            else:
                cloth_y = max(0, min(h, y0 + int(bh * 0.155)))
            mx0 = max(0, x0 - int(bw * 0.04))
            mx1 = min(w, x1 + int(bw * 0.04))
            # 画布：整图等比缩放（保比例，上限 608x1664），避免比例畸变
            s = min(1.0, 608.0 / w, 1664.0 / h)
            nw, nh = max(64, int(w * s)), max(64, int(h * s))
            canvas_w, canvas_h = ((nw + 7) // 8) * 8, ((nh + 7) // 8) * 8
            canvas = Image.new("RGB", (canvas_w, canvas_h), (222, 222, 226))
            # 关键：带 alpha 粘贴——透明区的 RGB 常为纯黑，直接 convert('RGB')
            # 会把画布变成黑底，模型就会把服装画成黑色团块
            canvas.paste(im.resize((nw, nh)),
                         ((canvas_w - nw) // 2, (canvas_h - nh) // 2),
                         a.resize((nw, nh)))
            ofx, ofy = (canvas_w - nw) // 2, (canvas_h - nh) // 2
            mask = Image.new("L", (canvas_w, canvas_h), 0)
            dr = ImageDraw.Draw(mask)
            dr.rectangle((ofx + int(mx0 * s), ofy + int(cloth_y * s),
                          ofx + int(mx1 * s), ofy + int(y1 * s)), fill=255)
            mask = mask.filter(ImageFilter.GaussianBlur(8))
            buf = io.BytesIO()
            canvas.save(buf, "PNG")
            mbuf = io.BytesIO()
            mask.save(mbuf, "PNG")
            neg = PORTRAIT_NEG_EN + (", casual clothes, civilian dress, "
                                     "streetwear, jeans, t-shirt, "
                                     "same outfit unchanged, gray blob, "
                                     "featureless mass, solid black outfit, "
                                     "plain black shape, black blob, ")
            # 换装目标动态负面（1.6.95 定稿）：**裸体档不加任何反裸体/反服装词**——
            # 用户明确「什么都不要留、别想反裸体关键词」：限定场合（命令/剧情
            # 明确裸体）时让模型自然生成完整裸体；日常档照旧封杀。
            if nude_mode == "nude":
                pass
            elif nude_mode == "underwear":
                neg += ("nude body, complete nudity, full frontal nudity, "
                        "naked, naked body no clothes, nude scene")
            else:
                neg += ("nude, naked, naked body, topless, breast, "
                        "underwear, lingerie, micro bikini, string outfit, "
                        "skirtless, bottomless, naked thighs")
            if denoise is None:
                # 1.7.4 整幅重绘方案：整图 0.60~0.62（全图重绘需适中重绘度，
                # 过高会重摆身体比例）
                denoise = {"dressed": 0.60, "underwear": 0.60,
                           "nude": 0.62}.get(nude_mode, 0.60)
            # 领口引导（1.7.2）：整领口重绘时给出清晰衣领/内搭指令，
            # 压制旧高领/连帽/围巾残留——此前领口混排成"围巾状"脏区
            prompt += (", same body shape, same body proportions, "
                       "same height, same pose, natural body, "
                       "consistent with original character body, "
                       "(white collared shirt under the blazer:1.2), "
                       "clean neat neckline, proper collar, "
                       "no hood, no turtleneck, no scarf, no neck wrap")
            if nude_mode == "nude":
                prompt += (", (nude:1.15), (naked body:1.1), no clothes, "
                           "natural feminine body, soft skin tones")
            elif nude_mode == "underwear":
                prompt += (", wearing lingerie set, covered torso, "
                           "feminine body shape")
            promotion_extra = ""
            new_raw = await self._request_sdwebui_img2img(
                base64.b64encode(buf.getvalue()).decode(),
                prompt,
                canvas_w, canvas_h, denoise, seed,
                negative=neg)
            new_im = Image.open(io.BytesIO(new_raw)).convert("RGB").resize((w, h))
            # ===== 1.7.4 精确换装（无掩膜边缘方案）=====
            # 整幅重绘后：仅用「原图不透明核心（alpha≥240）」承接新图——
            # 轮廓/发丝/透明边缘保留原图；
            # 头部（脸盒椭圆+发际/头顶带）从原图完整贴回——脸/头发零改动，
            # 绝不出现「领圈/残衣/高领环」等掩膜边缘伪影（模型一次画完整衣领）。
            hard_a = a.point(lambda v: 255 if v >= 240 else 0)
            rgb = im.convert("RGB").copy()
            rgb.paste(new_im, (0, 0), hard_a)
            # 头部锚定：脸盒椭圆（含发际）+ 头顶 0~0.18 人物高 × 中央 84% 宽
            head_guard = Image.new("L", (w, h), 0)
            drh = ImageDraw.Draw(head_guard)
            face_box = _face_box(im)
            hb_w = max(40, int(bw * 0.84))
            hb_x0 = max(0, int((x0 + x1) / 2 - hb_w / 2))
            hb_x1 = min(w, hb_x0 + hb_w)
            drh.rectangle((hb_x0, max(0, y0 - 6), hb_x1,
                           min(h, y0 + int(bh * 0.18))), fill=255)
            if face_box:
                fx0, fy0, fx1, fy1 = face_box
                fw_, fh_ = fx1 - fx0, fy1 - fy0
                drh.ellipse((max(0, int(fx0 - fw_ * 0.30)),
                             max(0, int(fy0 - fh_ * 0.45)),
                             min(w, int(fx1 + fw_ * 0.30)),
                             min(h, int(fy1 + fh_ * 0.18))), fill=255)
            head_guard = head_guard.filter(ImageFilter.GaussianBlur(10))
            rgb.paste(im.convert("RGB"), (0, 0), head_guard)
            rgb = rgb.convert("RGBA")
            rgb.putalpha(a)
            if not _junction_ok(rgb):
                log.warning("outfit junction check advisory: keep result anyway")
            out = io.BytesIO()
            rgb.save(out, "PNG")
            return out.getvalue()
        except Exception as e:
            log.warning("outfit inpaint skipped: %s", e)
            return cut_png


COMFY_PORTS = (8188, 8189, 8187, 6006, 8000, 8080)


def desktop_comfy_paths() -> list[str]:
    """本机常见的 ComfyUI 桌面端安装路径（白名单，供一键启动）。"""
    import os as _os
    cands = [
        Path("D:/COMFYUI/ComfyUI.exe"),
        Path(_os.environ.get("LOCALAPPDATA", "")) / "Programs"
        / "ComfyUI" / "ComfyUI.exe",
        Path(_os.environ.get("LOCALAPPDATA", "")) / "Programs"
        / "ComfyUI-Desktop" / "ComfyUI.exe",
        Path.home() / "ComfyUI" / "ComfyUI.exe",
    ]
    return [str(p) for p in cands if p and p.is_file()]


async def scan_local_comfy() -> list[dict]:
    """探测本机 ComfyUI 服务（常见端口逐一 /system_stats 健康检查）。"""
    out = []
    for port in COMFY_PORTS:
        url = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(timeout=2.5) as c:
                r = await c.get(f"{url}/system_stats")
                r.raise_for_status()
                d = r.json()
            out.append({
                "url": url,
                "version": str(d.get("system", {}).get("comfyui_version", "")),
                "gpu": str((d.get("devices") or [{}])[0].get("name", "")),
                "score": 0 if port == 8188 else 1,  # 8188 优先
            })
        except Exception:
            continue
    out.sort(key=lambda x: (x["score"], x["url"]))
    return out


async def comfy_checkpoints(base_url: str) -> dict:
    """校验 ComfyUI 地址并返回可用 checkpoint 列表（供设置页下拉）。"""
    base = (base_url or "").strip().rstrip("/") or "http://127.0.0.1:8188"
    _validate_url(base, allow_local=True)
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{base}/system_stats")
            r.raise_for_status()
            d = r.json()
            ver = str(d.get("system", {}).get("comfyui_version", ""))
            gpu = str((d.get("devices") or [{}])[0].get("name", ""))
            r = await c.get(f"{base}/object_info/CheckpointLoaderSimple")
            info = r.json().get("CheckpointLoaderSimple", {})
            names = (info.get("input", {}).get("required", {})
                     .get("ckpt_name", [None, []])[0] or [])
        return {"ok": True, "version": ver, "gpu": gpu,
                "checkpoints": [str(n) for n in names]}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        return {"ok": False, "detail": f"无法连接 ComfyUI（{e}）"}


_COMFY_SAMPLER_MAP = {
    "euler": "euler", "euler a": "euler_ancestral", "euler_a": "euler_ancestral",
    "ddim": "ddim", "uni_pc": "uni_pc", "uni_pc_bh2": "uni_pc_bh2",
    "dpm++ 2m": "dpmpp_2m", "dpm++ 2m karras": "dpmpp_2m",
    "dpm++ 2m sde": "dpmpp_2m_sde", "dpm++ sde": "dpmpp_sde",
    "dpm++ sde karras": "dpmpp_sde", "karras": "dpmpp_2m",
    "dpm 2m": "dpm_2m", "lcm": "lcm", "heun": "heun",
}
_COMFY_VALID = ("euler", "euler_ancestral", "ddim", "uni_pc", "uni_pc_bh2",
                "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde", "dpm_2m",
                "lcm", "heun", "dpmpp_2m_ancestral")


def _comfy_sampler(name: str, scheduler: str) -> tuple[str, str]:
    """A1111 风格采样器名 → ComfyUI 名称（并修正调度器），未知回退 euler。"""
    low = (name or "").strip().lower()
    if "karras" in low and not scheduler:
        scheduler = "karras"
    base = low.replace(" karras", "").strip()
    s = _COMFY_SAMPLER_MAP.get(base, "")
    if not s or s not in _COMFY_VALID:
        s = "euler"
    return s, scheduler or "normal"


def _prefer_checkpoint(names: list[str]) -> str:
    """模型优选顺序：非 fp8 → 已验证动漫/写实子串 → 列表首个；全 fp8 时退回首项。"""
    non_fp8 = [n for n in names if "fp8" not in n.lower()]
    pool = non_fp8 or list(names)
    for pref in ("zukiAnimeILL", "miaomiaoRealskin"):
        for n in pool:
            if pref.lower() in n.lower():
                return n
    return pool[0] if pool else ""


def asset_key(*parts: str) -> str:
    return _hash_key(*parts)

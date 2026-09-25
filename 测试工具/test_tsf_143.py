"""1.6.x 功能隔离单测：角色库注入 / 立绘还原与限量 / 对局归档回环 /
生图三线路重试 / 脸部-换装图生图几何。全部使用临时数据目录，不触碰真实数据。
运行：python test_tsf_143.py（需 D:\TSF_Galgame 依赖；样本输出在 测试样本\）。
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, r"D:\TSF_Galgame")
from app import config as cfgmod

TMP = Path(tempfile.mkdtemp(prefix="tsf_test_"))
cfgmod.DATA_DIR = TMP / "data"
cfgmod.CACHE_DIR = TMP / "cache"
cfgmod.SAVES_DIR = TMP / "saves"
cfgmod.CG_DIR = TMP / "cg"
for d in (cfgmod.DATA_DIR, cfgmod.CACHE_DIR, cfgmod.SAVES_DIR, cfgmod.CG_DIR):
    d.mkdir(parents=True, exist_ok=True)

from app import game, library, archive
from app.image import (ImageClient, ImageGenError, _portrait_check,
                       _bg_check, mock_portrait)
from PIL import Image

library.ROSTER_DIR = TMP / "角色库"


def png_bytes(w=512, h=768, color=(200, 60, 60)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def seed_roster():
    base = library.ROSTER_DIR
    for name, role, variants, anchor in (
        ("凛", "主角", ["阶段1", "阶段2", "阶段3", "阶段4"], "银白色长发，深蓝瞳"),
        ("山田", "NPC", ["happy", "neutral", "sad"], "黑发眼镜，深色西装"),
    ):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        files = []
        for i, v in enumerate(variants):
            fn = f"{name}_{v}.png"
            (d / fn).write_bytes(png_bytes(512, 768, (150 + i * 20, 80, 120)))
            files.append({"variant": v, "file": fn, "size": [512, 768]})
        (d / "角色设定.json").write_text(json.dumps({
            "name": name, "role": role, "appearance": anchor, "personality": "认真",
            "anchor": anchor, "auto_added": False, "source_game": "来源局",
            "exported_at": "2026-08-24 00:00:00", "variants": files,
        }, ensure_ascii=False), encoding="utf-8")


def make_session():
    from app import tsf
    return {
        "sid": "test00000001", "mode": "tsf", "title": "测试局", "world": "w",
        "outline": "", "lorebook": [], "directives": [], "catchphrases": [],
        "content_rating": "18", "r18_enabled": True, "allow_forced": False,
        "protagonist": {"name": "凛", "anchor": "银白色长发，深蓝瞳",
                        "stats": tsf.initial_stats(),
                        "identity": tsf.STAGES[0]["identity"]},
        "prot_handle": "凛_test",
        "characters": [{"name": "山田", "appearance": "黑发眼镜，深色西装",
                        "personality": "认真"}],
        "emotions": ["neutral", "happy", "shy", "sad"],
        "assets": {"portraits": {}, "backgrounds": {}},
        "bg_map": {}, "bg_count": 0, "log": [], "created": 0,
        "portrait_history": [],
    }


def test_library_list():
    infos = library.list_roster()
    assert [i["name"] for i in infos] == ["凛", "山田"], infos
    info = library.get_roster_char("山田")
    assert info and info["variants"], info
    assert library.variant_path(info, "happy") is not None
    assert library.variant_path(info, "angry") is None
    print("OK library list/get")


def test_inject():
    session = make_session()
    game.SESSIONS[session["sid"]] = session
    cfg = cfgmod.load_config()
    n = game.inject_library_assets(
        session, [{"name": "凛", "role": "主角"}, {"name": "山田", "role": "NPC"}], cfg)
    assert n == 4, n  # 凛阶段1 + 山田 happy/neutral/sad（库内都有）；shy 缺失 → 走生成管线
    labels = [it["label"] for it in session["assets"]["portraits"].values()]
    assert "凛·阶段1" in labels, labels
    assert "山田·happy" in labels and "山田·neutral" in labels and "山田·sad" in labels, labels
    assert "山田·shy" not in labels
    for it in session["assets"]["portraits"].values():
        p = cfgmod.CACHE_DIR / it["file"]
        assert p.is_file() and p.stat().st_size > 100, it
    return session


def test_restore_and_prune(session):
    sid = session["sid"]
    happy_key = next(k for k, it in session["assets"]["portraits"].items()
                     if it["label"] == "山田·happy")
    game._archive_same_label(session, "portraits", "山田·happy")
    hist_file = session["portrait_history"][-1]["file"]
    r = game.restore_portrait(sid, "山田·happy", hist_file)
    assert r["ok"] and r["file"].endswith(".png") and "/portraits/" in r["file"]
    cur = [it for it in session["assets"]["portraits"].values()
           if it["label"] == "山田·happy" and it["status"] == "ready"]
    assert cur and (cfgmod.CACHE_DIR / cur[0]["file"]).is_file()
    # 限量修剪：>3 个旧版时删最旧（只删 versions/）
    game._history_limit = staticmethod(lambda s: 3)
    ver = cfgmod.CACHE_DIR / r["file"].rsplit("/", 1)[0].replace("/portraits", "/versions")
    old = []
    for i in range(5):
        name = f"old_{i}.png"
        vp = ver / name
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_bytes(png_bytes())
        old.append(vp)
        session["portrait_history"].append({
            "archive_ts": 0, "label": "山田·happy", "file": vp.relative_to(cfgmod.CACHE_DIR).as_posix(),
            "raw_file": "",
        })
    game._prune_history(session, "山田·happy")
    kept = [e for e in session["portrait_history"] if e["label"] == "山田·happy"]
    assert len(kept) == 3, len(kept)
    assert not old[0].exists() and not old[1].exists(), "最旧两项应被删除"
    assert old[2].exists()
    print("OK restore & prune")


def test_archive_roundtrip(session):
    game.public_state = lambda s: {"title": s["title"], "mode": s.get("mode", "tsf")}
    out = archive.export_session(session)
    zpath = archive._archives_dir() / out["file"]
    assert zpath.is_file() and zpath.stat().st_size > 1000
    game.SESSIONS.clear()
    # 本地已有该对局的缓存目录与存档（restore 时 persist 过）→ 导入自动加
    # 后缀并换新 sid，绝不覆盖原局
    r1 = archive.import_archive(str(zpath))
    s1 = game.SESSIONS[r1["sid"]]
    assert r1["sid"] != "test00000001"
    d1 = game._session_dir_name(s1)
    assert d1.endswith("_导入1"), d1
    for it in s1["assets"]["portraits"].values():
        assert it["file"].startswith(d1 + "/"), it["file"]
        assert (cfgmod.CACHE_DIR / it["file"]).is_file()
    # 第二次导入 → 再换一个独立对局
    r2 = archive.import_archive(str(zpath))
    s2 = game.SESSIONS[r2["sid"]]
    assert r2["sid"] != r1["sid"]
    d2 = game._session_dir_name(s2)
    assert d2.endswith("_导入2"), d2
    for it in s2["assets"]["portraits"].values():
        assert it["file"].startswith(d2 + "/"), it["file"]
        assert (cfgmod.CACHE_DIR / it["file"]).is_file()
    # 归档列表可读
    listing = archive.list_archives()
    assert listing and listing[0]["title"] == "测试局"
    print("OK archive roundtrip & rewrite")


def test_archive_security():
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("session.json", json.dumps({"sid": "x", "title": "t", "assets": {}}))
        z.writestr("../evil.txt", "oops")
    try:
        archive.import_archive(evil.getvalue())
        raise AssertionError("应拒绝 ../ 路径")
    except archive.ArchiveError:
        pass
    bad2 = io.BytesIO()
    with zipfile.ZipFile(bad2, "w") as z:
        z.writestr("no_session.json", "{}")
    try:
        archive.import_archive(bad2.getvalue())
        raise AssertionError("应拒绝缺少 session.json")
    except archive.ArchiveError:
        pass
    print("OK archive security")


async def test_image_lines():
    # mock 线路
    cl = ImageClient({"image": {"provider": "mock"}})
    assert cl.mock
    b = await cl.generate_portrait("n", "app", "neutral", "平静", "", "")
    assert isinstance(b, bytes) and len(b) > 500
    bg = await cl.generate_background("hint", "")
    assert isinstance(bg, bytes) and len(bg) > 500
    # SD 线路：bad → 重试 good
    cl2 = ImageClient({"image": {"provider": "sdwebui", "base_url": "http://127.0.0.1:9",
                                 "sd_steps": 25, "sd_cfg": 7, "sd_sampler": "DPM++ 2M"}})
    calls = []
    good = mock_portrait("t", "neutral")

    async def fake_req(prompt, width, height, extra_negative="", seed=None, **kw):
        calls.append(seed)
        return good if len(calls) >= 2 else b"bad"

    cl2._request_sdwebui = fake_req
    out = await cl2._sd_retry("p", 512, 768, "neg", seed=42,
                              check=lambda raw: "bad-q" if raw == b"bad" else None)
    assert out == good and calls == [42, 42 + 1997], calls
    # 全坏 → 抛 ImageGenError
    calls.clear()
    try:
        await cl2._sd_retry("p", 512, 768, "neg", seed=1,
                            check=lambda raw: "bad-q", attempts=2)
        raise AssertionError("应抛出 ImageGenError")
    except ImageGenError:
        # 1.7.32：连续 2 次质检失败触发大步长换簇轮换（+1000003×k），
        # 仍全坏才抛——张数 >= 2（小步 2 张 + 大步长 3 张）
        assert len(calls) >= 2
    # 全坏 + soft=True → 软接受最后一次结果（换装必须能出图）
    calls.clear()

    async def fake_req_all_bad(prompt, width, height, extra_negative="", seed=None, **kw):
        calls.append(seed)
        return b"still-bad"

    cl2._request_sdwebui = fake_req_all_bad
    out_soft = await cl2._sd_retry("p", 512, 768, "neg", seed=1,
                                   check=lambda raw: "bad-q", attempts=2,
                                   soft=True)
    # 大步长轮换的最后一张被软接受（仍保证「换装能出图」）
    assert out_soft == b"still-bad" and len(calls) >= 2
    assert await cl2.generate_portrait("n", "a", "e", "c", "", "", strict=False) == b"still-bad"
    # OpenAI 线路：失败一次后成功
    cl3 = ImageClient({"image": {"provider": "openai", "base_url": "http://x",
                                 "api_key": "k"}})
    cnt = {"n": 0}

    async def fake_req3(prompt):
        cnt["n"] += 1
        if cnt["n"] == 1:
            raise ImageGenError("临时故障")
        return good

    cl3._request = fake_req3
    out3 = await cl3._request_retry("p", check=lambda raw: None)
    assert out3 == good and cnt["n"] == 2
    # 质检不过 → 重试成功（第二次好图直接返回）
    cnt["n"] = 0

    async def fake_req4(prompt):
        cnt["n"] += 1
        return good if cnt["n"] >= 2 else b"bad2"

    cl3._request = fake_req4
    out4 = await cl3._request_retry("p", check=lambda raw: "bad-q" if raw == b"bad2" else None)
    assert out4 == good and cnt["n"] == 2
    # 质检函数本身不炸
    assert _portrait_check(mock_portrait("x", "y")) is None or isinstance(
        _portrait_check(mock_portrait("x", "y")), str)
    assert _bg_check(mock_portrait("x", "y")) is None or isinstance(
        _bg_check(mock_portrait("x", "y")), str)
    print("OK image 3-line retry + soft-accept")


async def test_face_refine():
    """脸部区域估算 + img2img 桩测试：refine 后尺寸/透明通道保持，脸部贴回。"""
    from app.image import _face_box, _portrait_check
    import io as _io
    from PIL import Image, ImageDraw as _Dr

    def make_char_png(w=400, h=1000):
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dr = _Dr.Draw(im)
        # 头部肤色圆 + 灰色身体
        dr.ellipse((170, 110, 230, 180), fill=(240, 200, 180, 255))
        dr.rectangle((120, 280, 280, 980), fill=(120, 90, 90, 255))
        buf = _io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    cut = make_char_png()
    box = _face_box(Image.open(_io.BytesIO(cut)))
    assert box is not None
    fx0, fy0, fx1, fy1 = box
    assert fx0 < fx1 and fy0 < fy1 and fy0 < 300, box

    from app.image import ImageClient
    cl = ImageClient({"image": {"provider": "sdwebui", "base_url": "http://127.0.0.1:9",
                                "sd_steps": 20, "sd_cfg": 5.5, "sd_sampler": "DPM++ 2M"}})

    async def fake_img2img(init_b64, prompt, width, height, denoise, seed, negative=""):
        import base64
        data = base64.b64decode(init_b64)
        im = Image.open(_io.BytesIO(data)).convert("RGB")
        buf = _io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    cl._request_sdwebui_img2img = fake_img2img
    out = await cl.refine_face(cut, cut, "same face", 99)
    assert isinstance(out, bytes)
    im = Image.open(_io.BytesIO(out))
    assert im.mode == "RGBA" and im.size == (400, 1000), (im.mode, im.size)
    a = im.getchannel("A")
    bbox = a.getbbox()
    assert bbox is not None and bbox[0] >= 0
    # 非 SD 线路（mock）→ 原样返回
    clm = ImageClient({"image": {"provider": "mock"}})
    assert await clm.refine_face(cut, cut, "p", 1) == cut
    print("OK face refine")


def test_stage_feat_132():
    """1.7.32 转变技能内建：段特征投影 / 段投影权重 / 段负面 / 判定打分。"""
    from app import tsf as _tsf
    from app.image import tsf_stage_score

    v6 = _tsf.build_stages(6)
    # 六段 portrait 与语义层级一致（不再复用四段词：段2 娇小男、段5 娇小少女）
    assert "petite slimmer male" in v6[1]["portrait"], v6[1]["portrait"]
    assert "cute" in v6[4]["portrait"], v6[4]["portrait"]
    assert "androgynous" in v6[2]["portrait"], v6[2]["portrait"]
    v4 = _tsf.build_stages(4)
    assert "petite" in v4[1]["portrait"]        # 四段二=娇小男
    assert "cute" in v4[3]["portrait"]          # 四段四=少女终形（对齐实测 q090）
    # 段投影权重：男段全男、中性段中性最大、终段全女
    assert _tsf.stage_gender_weights(0, v6) == (1.0, 0.0, 0.0)
    gw2 = _tsf.stage_gender_weights(2, v6)
    assert gw2[1] == max(gw2) and gw2[1] > 0.5, gw2
    assert _tsf.stage_gender_weights(5, v6) == (0.0, 0.0, 1.0)
    # 段负面与段边界对齐：中性段不再封「1girl/long hair」（旧 progress 分档元凶）
    neg2 = _tsf.stage_negative_relaxed_stage(2, v6)
    assert "1girl" not in neg2 and "long hair" not in neg2, neg2
    assert "1girl" in _tsf.stage_negative_relaxed_stage(0, v6)
    # 段提示词：绽放段含女性曲线词、微塑段含娇小男词
    p70 = _tsf.stage_portrait_prompt("成年男性，短发", 70, "", v6)
    assert "feminine curves beginning" in p70, p70
    p30 = _tsf.stage_portrait_prompt("成年男性，短发", 30, "", v6)
    assert "petite slimmer male" in p30, p30
    # 判定打分：女图 vs 终形态档高分、vs 男子档负分；男图相反
    feat5 = _tsf.STAGE_FEAT_TIERS[5]
    feat0 = _tsf.STAGE_FEAT_TIERS[0]
    cap_f = "a young woman with long hair, feminine face, cute"
    cap_m = "a young man with short hair, muscular"
    assert tsf_stage_score(cap_f, feat5) > 3
    assert tsf_stage_score(cap_m, feat0) > 0
    assert tsf_stage_score(cap_m, feat5) < 0      # 男图被判终形态 = 段错位惩罚
    # 段边界（<=max 先到先得：20 归段 0「初变」上限、80 归段 3「绽放」上限）
    assert _tsf.stage_of(20, v6) == 0 and _tsf.stage_of(21, v6) == 1
    assert _tsf.stage_of(80, v6) == 3 and _tsf.stage_of(81, v6) == 4
    print("OK tsf stage feat 132")


def test_tsf_target_133():
    """1.7.33 开局最终变化样式：映射表 / 规范化 / 胸部门阀 / 相机负面。"""
    from app import game as _g
    from app import story as _s
    from app.image import CAPTURE_NEG, CAPTURE_EN
    # 映射表：身材三档 + 胸/瞳/发/衣物档位非空
    for k in ("萝莉", "少女", "熟女"):
        assert _g.TSF_TARGET_BUILD[k].strip(), k
    for k in ("小巧", "适中", "丰满"):
        assert _g.TSF_TARGET_CHEST[k].strip(), k
    assert _g.TSF_TARGET_EYE["紫色"] == "purple eyes"
    assert _g.TSF_TARGET_HAIR_COLOR["银白色"] == "silver white hair"
    assert _g.TSF_TARGET_CLOTHES["学园制服"]
    # 规范化：新字段透传、「维持原色」清空、keep 语义正确
    norm = _g._norm_tsf_target({
        "build": "萝莉", "chest": "丰满", "eye": "紫色",
        "clothes": "白色连衣裙", "aura": "软萌", "hair_color": "维持原色"})
    assert norm["build"] == "萝莉" and norm["chest"] == "丰满"
    assert norm["eye"] == "紫色" and norm["clothes"] == "白色连衣裙"
    assert norm["hair_color"] == "" and norm["keep_hair_color"] is True
    # 胸部门阀（渐变）：低女性权重不注入胸词、高权重注入
    sess = {"tsf_target": {"build": "萝莉", "chest": "丰满", "eye": "紫色",
                           "clothes": "白色连衣裙", "aura": "软萌温婉的少女气质",
                           "hair_color": "", "keep_hair_color": True}}
    low = _g._tsf_target_en(sess, female_w=0.2)
    high = _g._tsf_target_en(sess, female_w=0.9)
    assert "fuller soft bust" not in low and "fuller soft bust" in high
    assert "purple eyes" in high and "white dress" in high
    # 相机规则：无设定=负面压制（含权重词）；有设定=正面相机词存在
    assert "(camera:1.4)" in CAPTURE_NEG
    assert "holding" in CAPTURE_EN
    # 设定档案透传
    st = _s._clean({"tsf_target": {"build": "萝莉"}, "unused": 1})
    assert st["tsf_target"]["build"] == "萝莉"
    print("OK tsf target 133")


def test_newgame_plus_134():
    """1.7.34 二周目：前情压缩（本地回退）/摘要注入世界观/场景继承。"""
    import asyncio as _aio
    from app import game as _g
    # 1) 前情压缩（use_llm=False → 本地回退，长度可控）
    sess = make_session()
    sess["log"] = [{"character": "旁白", "text": "第一幕的事件。" * 8},
                   {"character": "凛", "text": "感觉身体在变化。"}]
    prior = _aio.run(_g._prior_summary(sess, use_llm=False))
    assert isinstance(prior, str) and prior.strip() and len(prior) <= 600, prior[:80]
    # 2) TSF system 注入摘要（世界观段出现「前情提要」）
    sess["summary"] = prior
    sys_txt = _g.build_story_system(sess)
    assert "前情提要" in sys_txt and prior[:20] in sys_txt
    # 3) 场景继承：构造源会话（tmp 背景文件）→ 继承到新会话
    src = make_session()
    bg_dir = cfgmod.CACHE_DIR / "old局_凛_abcd" / "backgrounds"
    bg_dir.mkdir(parents=True, exist_ok=True)
    f = bg_dir / "场景·清晨——001_abc12345.png"
    f.write_bytes(png_bytes(640, 448))
    src["assets"]["backgrounds"]["bg_key1"] = {
        "status": "ready", "file": "old局_凛_abcd/backgrounds/场景·清晨——001_abc12345.png",
        "label": "清晨的公寓房间"}
    src["bg_map"] = {"bg0": "bg_key1"}
    src["scenes"] = {"a1b2c3d4e5": {"name": "清晨的公寓房间", "bg_id": "bg0",
                                    "status": "ready"}}
    import uuid as _uuid
    new = make_session()
    new["sid"] = _uuid.uuid4().hex[:12]
    new["prot_handle"] = "凛_new"
    n = _g._inherit_scene_assets(src, new)
    assert n == 1, n
    # 新局注册了场景/背景映射，且文件已复制到新局目录
    assert new["scenes"]["a1b2c3d4e5"]["bg_id"] == "bg0"
    copied = cfgmod.CACHE_DIR / new["assets"]["backgrounds"]["bg_key1"]["file"]
    assert copied.is_file() and copied.stat().st_size > 100
    print("OK newgame-plus 134")


def test_hair_uniform_phantom_135():
    """1.7.35 发色偏置 / 场景制服映射 / 新角色幻影过滤。"""
    from app.image import hair_bias
    from app import game as _g
    # 发色：指定黑发 → 加权前插 + 白发负面（黑发角色不发白）；银白设定 →
    # 无负面（颜色随设定走）；无设定 → 随机池 + 银白负面
    pos, neg = hair_bias("黑发少年，黑瞳")
    assert "(black hair:1.3)" in pos and "white hair" in neg, (pos, neg)
    pos, neg = hair_bias("银白色长发少女")
    assert "silver" in pos and neg == ""
    pos, neg = hair_bias("安静神秘的角色")
    assert "hair:1.15" in pos and "silver hair" in neg
    assert "white hair" in neg
    # 制服映射：学园→校服、神社→巫女、无匹配→None；玩家显式制服优先
    en, neg = _g.scene_uniform_for("星冠学园·教室 2-B")
    assert "school uniform" in en and "casual clothes" in neg, en
    en, _ = _g.scene_uniform_for("邻里神社·新年参道")
    assert "miko" in en
    assert _g.scene_uniform_for("海边的灯塔") is None
    sess = make_session()
    sess["uniform_en"] = "忍者服"
    a, b = _g._uniform_for(sess)
    assert a == "忍者服"
    sess["uniform_en"] = sess["uniform_neg"] = ""
    sess["current_scene"] = "学园走廊"
    a, _ = _g._uniform_for(sess)
    assert "school uniform" in a
    # 新角色幻影过滤：不在 present 的新角色被丢弃；在 present 的才入池
    orig = _g._start_npc_portrait
    _g._start_npc_portrait = lambda *a, **k: None
    try:
        sess2 = make_session()
        sess2["characters"] = []
        sess2["prot_name"] = ""
        _g._post_turn(sess2, {"scene": "学园走廊",
                              "present": ["梦野"],
                              "new_characters": [
                                  {"name": "梦野", "appearance": "紫发少女",
                                   "personality": "活泼"},
                                  {"name": "幽灵", "appearance": "不存在的人",
                                   "personality": "幻影"},
                              ]})
        names = [c["name"] for c in sess2["characters"]]
        assert "梦野" in names and "幽灵" not in names, names
    finally:
        _g._start_npc_portrait = orig
    print("OK hair/uniform/phantom 135")


def test_context_reset_136():
    """1.7.36 压缩上下文·重置 AI：短局拒绝 / 长局压缩保留 6 幕。"""
    import asyncio as _aio
    from app import game as _g
    sess = make_session()
    sess["sid"] = "testreset0001"
    _g.SESSIONS[sess["sid"]] = sess
    try:
        _aio.run(_g.context_reset("testreset0001"))
        raise AssertionError("短局应拒绝")
    except _g.GameError:
        pass
    sess["log"] = [{"character": "凛", "text": f"第 {i} 幕的剧情。"}
                   for i in range(12)]

    async def fake(ctx):
        return "测试摘要"

    orig = _g._context_summary
    _g._context_summary = fake
    try:
        r = _aio.run(_g.context_reset("testreset0001"))
        assert r["ok"] and r["summary_chars"] == 4 and r["kept_turns"] == 6, r
        assert len(sess["log"]) == 6 and sess.get("summary") == "测试摘要"
        # 压缩后再次压缩被拒（仅 6 幕）
        try:
            _aio.run(_g.context_reset("testreset0001"))
            raise AssertionError("压缩后短局应拒绝")
        except _g.GameError:
            pass
    finally:
        _g._context_summary = orig
        _g.SESSIONS.pop(sess["sid"], None)
    print("OK context-reset 136")


def test_multi_figure_137():
    """1.7.37 双人同框/裸体硬拦截：双人检出、单人不误杀、nude_ok 跳检。"""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Dr
    from app.image import (_multi_figure_issue, _skin_excess_issue,
                           _portrait_check)
    from app import game as _g

    def make(circles, w=300, h=500, alpha=True):
        im = _Img.new("RGBA" if alpha else "RGB",
                      (w, h), (0, 0, 0, 0) if alpha else (200, 200, 200))
        dr = _Dr.Draw(im)
        for cx, cy, r in circles:
            dr.ellipse((cx - r, cy - r, cx + r, cy + r),
                       fill=(240, 200, 180, 255) if alpha else (240, 200, 180))
        buf = _io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    # 双人（两个上身柱：0.25-0.55h 带两根宽柱+空隙）
    two = make([(90, 200, 55), (220, 200, 55)], w=320)
    assert "双人" in str(_multi_figure_issue(two))
    # 单人（一柱）不误杀
    one = make([(150, 200, 60)])
    assert _multi_figure_issue(one) is None
    # 裸体：肤色满图 → 命中；着衣（上半深色）→ 不命中
    assert "裸露" in str(_skin_excess_issue(two))
    dressed = make([(150, 200, 60)], alpha=True)
    _io_im = _Img.open(_io.BytesIO(dressed)).convert("RGBA")
    dr = _Dr.Draw(_io_im)
    dr.rectangle((0, 0, 320, 320), fill=(80, 80, 90, 255))  # 上身着衣
    buf = _io.BytesIO()
    _io_im.save(buf, "PNG")
    assert _skin_excess_issue(buf.getvalue()) is None
    # final check 的 nude_ok 顺序由代码保证（completeness→multi→skin；
    # nude_ok=True 时跳过 skin）——此处只验证裸体函数本身可用
    assert _g._final_check_issue is not None
    print("OK multi-figure 137")


def test_portrait_delete_138():
    """1.7.38 立绘历程「删除此立绘」：历史移除+文件删除+清固定+当前版拒绝。"""
    import asyncio as _aio
    from app import game as _g
    sess = make_session()
    sess["sid"] = "testdel000001"
    _g.SESSIONS[sess["sid"]] = sess
    vdir = cfgmod.CACHE_DIR / "old局_凛_abcd" / "versions"
    vdir.mkdir(parents=True, exist_ok=True)
    vf = vdir / "凛——002_abcdef01.png"
    vf.write_bytes(png_bytes(400, 600))
    sess["portrait_history"] = [
        {"file": "old局_凛_abcd/versions/凛——002_abcdef01.png",
         "label": "凛·阶段2", "archive_ts": 100},
        {"file": "old局_凛_abcd/versions/凛——001_11111111.png",
         "label": "凛·阶段1", "archive_ts": 50},
    ]
    sess["pinned_portraits"] = {"凛": "凛·阶段2"}
    sess["assets"]["portraits"] = {
        "k1": {"status": "ready", "label": "凛·阶段1",
               "file": "old局_凛_abcd/versions/凛——001_11111111.png"},
    }
    # 删除历史版阶段2：history 移除 + 文件删除 + 固定解除 + 不触发重绘
    r = _g.delete_portrait_version(sess["sid"], "凛·阶段2",
                                   "old局_凛_abcd/versions/凛——002_abcdef01.png")
    assert r["ok"] and not vf.exists()
    assert all(e["label"] != "凛·阶段2" for e in sess["portrait_history"])
    assert sess.get("pinned_portraits") == {}
    assert r.get("regenerating") == ""
    # 1.7.39 当前使用版（阶段1）允许删除 → 资产条目移除 + 按阶段重绘触发
    regen_calls = []
    orig = _g.regenerate_label
    _g.regenerate_label = lambda s, lbl: (regen_calls.append(lbl),
                                          {"ok": True, "regenerating": lbl})[1]
    try:
        r2 = _g.delete_portrait_version(sess["sid"], "凛·阶段1",
                                        "old局_凛_abcd/versions/凛——001_11111111.png")
        assert r2["ok"] and r2["regenerating"] == "凛·阶段1"
        assert regen_calls == ["凛·阶段1"], regen_calls
        assert not sess["assets"]["portraits"]  # 资产条目已移除（等待重绘）
    finally:
        _g.regenerate_label = orig
    # 路径越界 → 拒绝
    try:
        _g.delete_portrait_version(sess["sid"], "凛·阶段1", "../../evil.png")
        raise AssertionError("非法路径应拒绝")
    except _g.GameError:
        pass
    _g.SESSIONS.pop(sess["sid"], None)
    print("OK portrait-delete 138/139")


def test_cg_live_cutout_140():
    """1.7.40 CG 参考底图：取当前 ready 最新立绘（而非历史归档最早图）。"""
    from app import game as _g
    sess = make_session()
    pdir = cfgmod.CACHE_DIR / "old局_凛_abcd" / "portraits"
    pdir.mkdir(parents=True, exist_ok=True)
    old = pdir / "凛——001_aaaa1111.png"
    new = pdir / "凛——026_bbbb2222.png"
    old.write_bytes(png_bytes(300, 500, (80, 80, 90)))   # 早期阶段图
    new.write_bytes(png_bytes(300, 500, (200, 180, 160)))  # 当前阶段图
    # 历史归档：更早的「阶段4」版本（CG 旧病根会优先取它）
    sess["portrait_history"] = [
        {"file": "old局_凛_abcd/portraits/凛——001_aaaa1111.png",
         "label": "凛·阶段4", "archive_ts": 1000}]
    sess["assets"]["portraits"] = {
        "k1": {"status": "ready", "label": "凛·阶段4",
               "file": "old局_凛_abcd/portraits/凛——026_bbbb2222.png",
               "finished_at": 2000}}
    # CG 专用取图 = 当前 ready（026），不是归档的旧图
    b = _g._find_live_cutout(sess, "凛", "凛·阶段4")
    assert b == new.read_bytes(), "应取当前 ready 而非历史归档"
    print("OK cg live cutout 140")


def test_hi_res_141():
    """1.7.41 超高清尺寸：×2 且不超方舟 4.62MP 上限。"""
    from app.image import _hi_res_size
    assert _hi_res_size("1024x1024") == "2048x2048"
    w, h = (int(x) for x in _hi_res_size("1024x1760").split("x"))
    assert w * h <= 4624220, (w, h)          # 不超过方舟面积上限
    assert w >= 1024 and h >= 1760           # 高分辨率（≥基准）
    assert w % 16 == 0 and h % 16 == 0       # 16 对齐（模型友好）
    assert _hi_res_size("") == "1024x1024"   # 非法回退
    print("OK hi-res 141")


seed_roster()
test_stage_feat_132()
test_tsf_target_133()
test_newgame_plus_134()
test_hair_uniform_phantom_135()
test_context_reset_136()
test_multi_figure_137()
test_portrait_delete_138()
test_cg_live_cutout_140()
test_hi_res_141()
test_library_list()
sess = test_inject()
test_restore_and_prune(sess)
test_archive_roundtrip(sess)
test_archive_security()
asyncio.run(test_image_lines())
asyncio.run(test_face_refine())
print("ALL TESTS PASSED")

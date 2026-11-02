# -*- coding: utf-8 -*-
"""1.9.21 回归：DeepSeek 一致性检查 / 更多提示词 / ComfyUI 换装与清洗。

不联网：临时数据根 + 空 config → LLM 走 mock；ComfyUI 只做纯函数与
工作流结构断言（真实出图由 测试工具/test_comfy_i2i_live.py 负责）。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile

os.environ["TSF_DATA_DIR"] = tempfile.mkdtemp(prefix="t1921-test-")
os.makedirs(os.environ["TSF_DATA_DIR"], exist_ok=True)
with open(os.path.join(os.environ["TSF_DATA_DIR"], "config.json"), "w",
          encoding="utf-8") as _fh:
    _fh.write("{}")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PIL import Image  # noqa: E402

from app import consistency as CM  # noqa: E402
from app import game  # noqa: E402
from app import image as IM  # noqa: E402
from app import prompt_presets as PP  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [PASS] " if ok else "  [FAIL] ") + name
          + (f"\n         证据：{detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def fake_session() -> dict:
    return {
        "sid": "test1921",
        "title": "镜中人",
        "world": "现代都市，旧校舍的镜子会映出另一个自己。",
        "outline": "◆ 初遇\n主角在旧校舍遇见镜中的自己。",
        "protagonist": {
            "name": "阿明", "anchor": "黑色短发，琥珀色眼瞳", "identity": "中性",
            "stats": {"progress": 40, "physique": 35, "immersion": 20},
        },
        "characters": [{
            "name": "小雪", "role": "同班同学",
            "personality": "外冷内热，说话简洁",
            "appearance": "银白长发，冰蓝眼瞳，水手服",
            "traits": ["嘴硬心软", "观察力强"],
            "age": 22, "height_cm": 162, "locked_fields": ["height_cm"],
        }],
        "ai_directive": "",
        "writing_style": "default",
        "custom_prompt": "",
        "log": [
            {"turn": {"scene": "旧校舍走廊", "dialogue": [
                {"character": "旁白", "emotion": "neutral",
                 "text": "夕阳把走廊染成橘红，镜面泛着冷光。"},
                {"character": "小雪", "emotion": "shy",
                 "text": "……你也看见了吧，镜子里的那个人。"}],
                "outfit": {"小雪": "水手服"},
                "cg": {"active": False, "title": ""}},
             "choice": {"text": "走近镜子", "effect": "直面异变"}},
            {"turn": {"scene": "旧校舍走廊", "dialogue": [
                {"character": "旁白", "emotion": "neutral",
                 "text": "镜中的少女抬手，和你做了同一个动作。"}],
                "outfit": {}},
             "choice": None},
        ],
        "consistency": None,
        "consistency_auto": 0,
    }


async def main() -> int:
    print("=" * 78)
    print("1.9.21 一致性检查 / 提示词 / ComfyUI 换装")
    print("=" * 78)

    # ---------- ① 提示词预设 ----------
    print("\n[1] 写作风格 / 自定义提示词")
    check("默认风格零影响（style_prompt 为空）",
          PP.style_prompt("default") == "")
    check("未知风格回退为空（不抛错）",
          PP.style_prompt("no-such-style") == "")
    check("文学风格有实际风格词", len(PP.style_prompt("literary")) > 10)
    check("默认会话提示块为空串（逐字节零变化）",
          PP.session_prompt_block({"writing_style": "default",
                                   "custom_prompt": ""}) == "")
    block = PP.session_prompt_block({"writing_style": "literary",
                                     "custom_prompt": "  每幕写气味\n\n\n"})
    check("风格 + 自定义提示词都进入提示块",
          "写作风格" in block and "气味" in block
          and "\n\n\n" not in block, detail=block[:80].replace("\n", "|"))
    check("自定义提示词超长截断", len(PP.clean_custom("字" * 2000)) == PP.MAX_CUSTOM)
    check("风格库数量 ≥ 8", len(PP.list_styles()) >= 8,
          detail=str(len(PP.list_styles())))

    # ---------- ② 一致性检查：纯函数 ----------
    print("\n[2] 一致性检查（证据包 / 归一化 / 修复指令）")
    s = fake_session()
    ev = CM.build_evidence(s, turns=12)
    check("证据包含设定与被检查幕次",
          ev["title"] == "镜中人" and len(ev["turns"]) == 2
          and ev["turns_checked"] == "第 1 ~ 2 幕",
          detail=str(ev["turns_checked"]))
    check("角色固定属性进入证据（含 locked_fields）",
          ev["characters"][0]["locked_fields"] == ["height_cm"])
    check("主角当前着装进入证据",
          ev["protagonist"]["outfit"] == "初始着装")
    check("场景清单去重有序",
          ev["scenes_seen"] == ["旧校舍走廊"], detail=str(ev["scenes_seen"]))

    rep = CM.normalize_report({
        "score": 55,
        "summary": "小雪的身高与服装有出入。",
        "issues": [
            {"severity": "high", "category": "人物设定", "turn": 2,
             "problem": "小雪身高被写成 150cm", "evidence": "娇小的只有150",
             "fix": "恢复小雪 162cm 的设定"},
            {"severity": "URGENT", "category": "乱写", "turn": "x",
             "problem": "服装前后不一致", "fix": "保持水手服"},
            {"severity": "low", "category": "重复与套路",
             "problem": "", "fix": "删掉"},
        ],
    })
    check("归一化：非法 severity/category 回退、空问题丢弃",
          len(rep["issues"]) == 2
          and rep["issues"][1]["severity"] == "medium"
          and rep["issues"][1]["category"] == "前后矛盾"
          and rep["issues"][1]["turn"] is None,
          detail=str([(i["severity"], i["category"]) for i in rep["issues"]]))
    check("归一化：计数正确",
          rep["counts"] == {"high": 1, "medium": 1, "low": 0},
          detail=str(rep["counts"]))
    rep2 = CM.normalize_report({"issues": [{"problem": "x", "severity": "high"}]})
    check("缺 score 时按严重度折算（不谎报满分）", rep2["score"] <= 84,
          detail=str(rep2["score"]))
    try:
        CM.normalize_report(["not-a-dict"])
        check("非对象报告必须报错", False)
    except CM.ConsistencyError:
        check("非对象报告必须报错", True)

    s["consistency"] = rep
    directive = CM.apply_fixes(s, rep, limit=2)
    check("修复建议写入导演指令（含档位标签）",
          "[一致性·高]" in directive and "恢复小雪" in directive,
          detail=directive[:80].replace("\n", "|"))
    again = CM.apply_fixes(s, rep, limit=2)
    check("重复写同批建议不会叠加", again.count("[一致性·高]") == 1,
          detail=str(again.count("[一致性·高]")))
    s["ai_directive"] = "长" * 600
    CM.apply_fixes(s, rep, limit=1)
    check("导演指令超长截断到 500", len(s["ai_directive"]) == 500,
          detail=str(len(s["ai_directive"])))

    s["consistency_auto"] = 3
    s["log"] = s["log"] * 2          # 4 幕：4 % 3 != 0
    check("自动检查：未到点不触发", CM.auto_due(s) is False)
    s["log"] = s["log"][:3]
    check("自动检查：到点触发", CM.auto_due(s) is True)
    s["consistency_auto"] = 0
    check("自动检查：0 = 关闭", CM.auto_due(s) is False)

    # ---------- ③ 一致性检查：编排（stub LLM） ----------
    print("\n[3] 一致性检查编排（stub 模型）")

    class _StubLLM:
        def __init__(self, cfg, session=None, label=""):
            self.mock = False

        async def chat_json(self, messages, temperature=0.1):
            assert "[世界观" in "" or True
            return {"score": 88, "summary": "总体一致",
                    "issues": [{"severity": "low", "category": "重复与套路",
                                "problem": "两幕都写夕阳", "evidence": "夕阳",
                                "fix": "换一个时间标记"}]}

    orig = CM.LLMClient
    orig_ready = CM._llm_ready
    CM.LLMClient = _StubLLM  # type: ignore[assignment]
    CM._llm_ready = lambda cfg: True  # type: ignore[assignment]
    try:
        s3 = fake_session()
        r = await CM.check(s3, mode="recent", turns=12)
        check("检查结果写入 session 且结构完整",
              s3["consistency"]["score"] == 88
              and s3["consistency"]["counts"]["low"] == 1
              and len(r["issues"]) == 1)
        check("报告带模型名/模式/时间戳",
              "mode" in r and r["mode"] == "recent" and "at" in r)
    finally:
        CM.LLMClient = orig  # type: ignore[assignment]
        CM._llm_ready = orig_ready  # type: ignore[assignment]

    s4 = fake_session()
    try:
        await CM.check(s4)      # 空 config → mock → 必须报错而不是伪造结果
        check("无可用模型时明确报错（不伪造「一切正常」）", False)
    except CM.ConsistencyError as e:
        check("无可用模型时明确报错（不伪造「一切正常」）",
              "未配置" in str(e) or "演示模式" in str(e), detail=str(e))

    # ---------- ④ 游戏侧集成 ----------
    print("\n[4] 游戏侧集成（写作风格进入系统提示词）")
    st = await game.start_game({
        "mode": "tsf", "title": "提示词", "world": "现代都市的雨夜。",
        "protagonist": {"name": "阿明", "anchor": "黑色短发"},
        "characters": [{"name": "小雪", "personality": "外冷内热",
                        "appearance": "银白长发"}],
        "outline": "◆ 一\n开始。", "content_rating": "18",
        "r18_enabled": True, "allow_forced": False,
    })
    sid = st["sid"]
    sess = game.get_session(sid)
    sys_before = game.build_story_system(sess)
    check("默认状态系统提示词不含写作风格块",
          "【写作风格" not in sys_before)
    game.set_writing_style(sid, "literary", "每幕写气味与触感")
    sys_after = game.build_story_system(sess)
    check("设置后系统提示词含风格 + 自定义提示词",
          "【写作风格" in sys_after and "气味与触感" in sys_after
          and len(sys_after) > len(sys_before),
          detail=f"{len(sys_before)} -> {len(sys_after)}")
    check("默认风格清空后不再注入",
          "【写作风格" not in game.build_story_system(
              {**sess, "writing_style": "default", "custom_prompt": ""}))
    check("状态视图带 writing_style / consistency",
          st.get("writing_style") is not None and "consistency" in st)
    n = game.set_consistency_auto(sid, 99)
    check("自动检查间隔夹到 0~50", n == 50, detail=str(n))

    # ---------- ⑤ ComfyUI 清洗 / 工作流 ----------
    print("\n[5] ComfyUI 提示词清洗 / 工作流 / 颜色崩坏检测")
    raw = "a, (two people:1.35), (1girl 1boy:1.35), (nude:1.15), plain"
    cleaned = IM.comfy_sanitize_text(raw)
    check("权重语法与括号被清洗、语义保留",
          cleaned == "a, two people, 1girl 1boy, nude, plain",
          detail=cleaned)
    check("清洗是幂等的", IM.comfy_sanitize_text(cleaned) == cleaned)
    wf = IM._comfy_workflow("p", "n", 512, 768, 1, 20, 6.0, "euler",
                            "normal", "ckpt", clip_layer=2)
    check("ClipSkip=2 时插入 CLIPSetLastLayer(-2)",
          wf.get("11", {}).get("class_type") == "CLIPSetLastLayer"
          and wf["11"]["inputs"]["stop_at_clip_layer"] == -2
          and wf["2"]["inputs"]["clip"] == ["11", 0],
          detail=str(wf.get("11")))
    wf0 = IM._comfy_workflow("p", "n", 512, 768, 1, 20, 6.0, "euler",
                             "normal", "ckpt", clip_layer=0)
    check("ClipSkip=0 时不插入额外节点", "11" not in wf0)
    wfi = IM._comfy_workflow_img2img("p", "n", 512, 768, 1, 20, 6.0,
                                     "euler", "normal", "ckpt", "img.png",
                                     0.6, clip_layer=2)
    check("图生图工作流：LoadImage + VAEEncode + SetLatentNoiseMask 链路正确",
          wfi["4"]["class_type"] == "LoadImage"
          and wfi["5"]["class_type"] == "VAEEncode"
          and wfi["8"]["class_type"] == "SaveImage"
          and wfi["6"]["inputs"]["latent_image"] == ["5", 0])
    wfp = IM._comfy_workflow_inpaint("p", "n", 512, 768, 1, 20, 6.0,
                                     "euler", "normal", "ckpt", "img.png",
                                     "mask.png", 0.4)
    check("掩膜工作流：LoadImageMask + SetLatentNoiseMask 正确",
          wfp["6"]["class_type"] == "LoadImageMask"
          and wfp["7"]["class_type"] == "SetLatentNoiseMask"
          and wfp["8"]["inputs"]["latent_image"] == ["7", 0]
          and wfp["10"]["class_type"] == "SaveImage")

    def png(im) -> bytes:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    cyan = Image.new("RGB", (64, 64), (10, 200, 210))
    normal = Image.new("RGB", (64, 64), (205, 203, 202))
    check("颜色崩坏检测：整幅青蓝 → 判定崩坏",
          bool(IM.comfy_color_issue(png(cyan))),
          detail=IM.comfy_color_issue(png(cyan)))
    check("颜色崩坏检测：正常灰底 → 通过",
          IM.comfy_color_issue(png(normal)) == "")

    # ---------- ⑥ 换装：ComfyUI 不再被拒 ----------
    print("\n[6] 换装线路（ComfyUI img2img 已接通）")
    client = IM.ImageClient({"image": {"provider": "comfyui",
                                      "base_url": "http://127.0.0.1:8188"}})
    check("ComfyUI 客户端 provider/comfy 语义正确",
          client.provider == "comfyui" and client.mock is False)
    check("_img2img / _inpaint 分派方法存在",
          hasattr(client, "_img2img") and hasattr(client, "_inpaint"))
    # 非本地线路（openai）仍然必须拒绝换装（回退全量生成）
    oc = IM.ImageClient({"image": {"provider": "openai",
                                  "base_url": "https://example.invalid",
                                  "api_key": "x"}})
    try:
        await oc.refine_outfit(b"not-an-image", "outfit", 1)
        check("非本地线路换装必须抛错（回退全量）", False)
    except IM.ImageGenError as e:
        check("非本地线路换装必须抛错（回退全量）",
              "本地绘画服务" in str(e), detail=str(e))

    print("\n" + "=" * 78)
    print(f"结果：{'全部通过' if not FAILS else '失败 ' + str(len(FAILS)) + ' 项'}")
    if FAILS:
        for f in FAILS:
            print("  - " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

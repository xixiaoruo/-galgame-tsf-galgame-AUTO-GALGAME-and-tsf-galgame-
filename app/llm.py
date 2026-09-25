"""LLM 客户端：OpenAI 兼容 /chat/completions，未配置时走 mock。"""
import asyncio
import json
import logging
import random
import re

import httpx

log = logging.getLogger("galgame.llm")

# 演示模式剧本：多场景轮换模板（{scene} 由世界观注入），避免一直卡在同一场景
_mock_counter = 0

MOCK_TURNS = [
    {
        "scene": "{scene} · 初遇",
        "background_hint": "雨后的黄昏街道，霓虹初上",
        "is_new_background": True,
        "dialogue": [
            {"character": "旁白", "emotion": "neutral", "text": "{world_open}。空气里还残留着雨后的潮意。"},
            {"character": "小雪", "emotion": "shy", "text": "……原来是你。没想到会在这里遇见。"},
            {"character": "小雪", "emotion": "neutral", "text": "上次的事，我一直想当面道谢。"},
        ],
        "choices": [
            {"text": "「不用客气，顺路而已」", "effect": "谦虚回应", "bias": "immersion"},
            {"text": "「你看起来气色不错」", "effect": "转移话题", "bias": "habit"},
            {"text": "「要不要一起走走？」", "effect": "主动邀约", "bias": "immersion"},
        ],
    },
    {
        "scene": "{scene} · 咖啡店",
        "background_hint": "暖色调的街角咖啡店，玻璃上蒙着水汽",
        "is_new_background": True,
        "dialogue": [
            {"character": "旁白", "emotion": "neutral", "text": "{world_open}。你推开咖啡店的门，风铃轻轻响了一声。"},
            {"character": "小雪", "emotion": "happy", "text": "这家店的肉桂卷很不错，要不要试试？"},
        ],
        "choices": [
            {"text": "「好啊，再来两杯热可可」", "effect": "欣然同意", "bias": "habit"},
            {"text": "「我只要一杯美式」", "effect": "保持距离", "bias": "immersion"},
            {"text": "沉默地看着菜单，等她推荐", "effect": "依赖对方", "bias": "immersion"},
        ],
    },
    {
        "scene": "{scene} · 河堤",
        "background_hint": "傍晚的河堤步道，晚霞倒映水面",
        "is_new_background": True,
        "dialogue": [
            {"character": "旁白", "emotion": "neutral", "text": "{world_open}。晚风从河面吹来，把她的长发轻轻扬起。"},
            {"character": "小雪", "emotion": "sad", "text": "最近总觉得，有些东西正在悄悄改变……"},
        ],
        "choices": [
            {"text": "「我也会一直陪着你」", "effect": "承诺", "bias": "immersion"},
            {"text": "「改变未必是坏事」", "effect": "开导", "bias": "habit"},
            {"text": "静静倾听，没有打断她", "effect": "倾听", "bias": "immersion"},
        ],
    },
    {
        "scene": "{scene} · 旧书店",
        "background_hint": "堆满旧书的狭小书店，尘埃在斜阳里漂浮",
        "is_new_background": True,
        "dialogue": [
            {"character": "旁白", "emotion": "neutral", "text": "{world_open}。书架深处，她正踮着脚去够一本旧小说。"},
            {"character": "小雪", "emotion": "surprised" if "surprised" else "shy", "text": "啊、你什么时候在那里的……"},
        ],
        "choices": [
            {"text": "「我帮你拿」", "effect": "帮忙", "bias": "habit"},
            {"text": "「这本书我也看过」", "effect": "找共同话题", "bias": "immersion"},
            {"text": "「这里的味道让人安心」", "effect": "抒情", "bias": "immersion"},
        ],
    },
    {
        "scene": "{scene} · 车站月台",
        "background_hint": "夜晚的月台，末班车缓缓进站",
        "is_new_background": True,
        "dialogue": [
            {"character": "旁白", "emotion": "neutral", "text": "{world_open}。末班车进站的鸣笛在夜色里拖得很长。"},
            {"character": "小雪", "emotion": "neutral", "text": "再坐一站吧，我有话想对你说。"},
        ],
        "choices": [
            {"text": "「好，我陪你」", "effect": "答应", "bias": "immersion"},
            {"text": "「就在这里说吧」", "effect": "不愿上车", "bias": "immersion"},
            {"text": "点点头，跟着她走进车厢", "effect": "默许", "bias": "habit"},
        ],
    },
]

MOCK_TURN = {
    "scene": "放学后的旧校舍",
    "background_hint": "夕阳斜照的旧校舍走廊，尘埃在光柱中飞舞",
    "is_new_background": False,
    "dialogue": [
        {"character": "旁白", "emotion": "neutral", "text": "夕阳把旧校舍的走廊染成了橘红色。"},
        {"character": "小雪", "emotion": "shy", "text": "……你还记得这里吗？我们第一次见面的地方。"},
        {"character": "小雪", "emotion": "happy", "text": "那时候你也是这样，一脸认真地帮我捡起了散落的书。"},
    ],
    "choices": [
        {"text": "「当然记得，我从来没忘过」", "effect": "坦诚回应", "bias": "immersion"},
        {"text": "「有点模糊了，你提醒我才想起来」", "effect": "装傻试探", "bias": "habit"},
        {"text": "沉默地看着她，等她继续说下去", "effect": "倾听", "bias": "immersion"},
    ],
}


class LLMError(Exception):
    pass


def strip_json_block(text: str) -> str:
    """剥离可能的 markdown 代码块标记与前后杂文字。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # 截取第一个 { 到最后一个 } 之间，容忍模型前后加话
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return text


class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg["llm"]
        # 常见配置笔误自动修正：DeepSeek 地址补 /v1
        base = (self.cfg.get("base_url") or "").strip()
        if "api.deepseek.com" in base and not base.rstrip("/").endswith("/v1"):
            self.cfg["base_url"] = base.rstrip("/") + "/v1"
        self.mock = not (
            self.cfg.get("api_key")
            and "填入" not in self.cfg.get("api_key", "")
            and self.cfg.get("base_url")
        )

    async def chat(self, messages: list[dict], temperature: float | None = None) -> str:
        if self.mock:
            await asyncio.sleep(0.6)
            world = ""
            for m in messages:
                if m.get("role") == "system":
                    content = m.get("content", "")
                    if "【世界观设定】" in content:
                        rest = content.split("【世界观设定】", 1)[1].strip()
                        lines = [x for x in rest.splitlines() if x.strip()]
                        world = lines[0].strip() if lines else ""
                    break
            world_open = (world[:14] + "……") if len(world) > 14 else (world or "故事从一场细雨开始")
            scene_base = world[:8] or "某处"
            global _mock_counter
            idx = _mock_counter
            _mock_counter = (_mock_counter + 1) % len(MOCK_TURNS)
            template = MOCK_TURNS[idx]
            turn = json.loads(json.dumps(template, ensure_ascii=False))
            for key in ("scene", "background_hint"):
                turn[key] = turn[key].format(scene=scene_base)
            for d in turn.get("dialogue", []):
                d["text"] = d["text"].format(world_open=world_open)
            return json.dumps(turn, ensure_ascii=False)

        base = self.cfg["base_url"].rstrip("/")
        # 出站校验：仅 http/https；本地 LLM（vllm/ollama 等）放行，云端同样校验（见 image._validate_url）
        from .image import ImageGenError, _validate_url
        try:
            _validate_url(base, allow_local=True)
        except ImageGenError as e:
            raise LLMError(str(e)) from e
        url = f"{base}/chat/completions"
        payload = {
            "model": self.cfg.get("model", "gpt-4o-mini"),
            "messages": messages,
            "temperature": (
                self.cfg.get("temperature", 0.9)
                if temperature is None
                else temperature
            ),
        }
        headers = {"Authorization": f"Bearer {self.cfg['api_key']}"}
        # 1.7.23 分段超时：connect 10s 快速失败（平台半死时不再白白挂 120s）、
        # read 120s 长文上限、write 30s——游戏「卡在加载中」元凶之一
        try:
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(180.0, connect=10.0, read=120.0,
                                          write=30.0, pool=10.0)) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300]
            raise LLMError(f"对话 API 返回 {e.response.status_code}：{body}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"对话 API 连接失败：{e}") from e
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"对话 API 响应格式异常：{data}") from e

    async def chat_json(self, messages: list[dict], temperature: float = 0.2) -> dict:
        """请求 JSON 输出并解析；失败时降低温度重试一次。"""
        last_err = None
        for attempt in range(2):
            temp = temperature if attempt == 0 else max(0.0, temperature - 0.2)
            raw = await self.chat(messages, temperature=temp)
            if self.mock:
                return json.loads(raw)
            try:
                return json.loads(strip_json_block(raw))
            except json.JSONDecodeError as e:
                last_err = e
                log.warning("JSON 解析失败（第 %d 次）：%s", attempt + 1, raw[:200])
        raise LLMError(f"模型未返回有效的 JSON：{last_err}")


class MockSetup:
    """随机设定的 mock 数据。"""

    TITLES = ["星霜之约", "夏色的终焉", "雾都夜行"]
    WORLDS = [
        "现代日本的一所海滨高中，传说在毕业前于旧校舍许愿的两人会永远相连。故事从晚春的黄昏开始。",
        "近未来的学院都市，人类与仿生人共同生活，一桩小小的失窃案让两个原本平行的人生交错了。",
    ]
    CHARACTERS = [
        [
            {
                "name": "小雪",
                "appearance": "银白色长直发，冰蓝色眼瞳，身着水手服制服，纤细清冷的少女，发梢微微卷曲",
                "personality": "外冷内热，说话简洁但偶尔毒舌",
            },
            {
                "name": "绫音",
                "appearance": "栗色齐肩短发配发箍，琥珀色眼瞳，制服外搭针织开衫，笑容明朗的少女",
                "personality": "活泼开朗，喜欢调侃别人，行动力强",
            },
        ],
        [
            {
                "name": "星野",
                "appearance": "黑色高马尾，深紫色眼瞳，白色实验服搭配学院制服，眼神锐利的少女",
                "personality": "理性冷静，对感兴趣的事会滔滔不绝",
            },
            {
                "name": "艾莉",
                "appearance": "淡粉色短发，绿色义眼微微发光，身着仿生人专用制服，表情略显生硬",
                "personality": "认真死板，正在学习人类的情感",
            },
        ],
    ]

    @classmethod
    def random(cls) -> dict:
        idx = random.randrange(len(cls.WORLDS))
        return {
            "title": cls.TITLES[idx % len(cls.TITLES)],
            "world": cls.WORLDS[idx],
            "characters": cls.CHARACTERS[idx],
        }

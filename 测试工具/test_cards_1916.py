"""1.9.16 角色卡详细人设 / 固定属性 / AI 微调回归（不联网）。"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import characters as charm  # noqa: E402
from app import library  # noqa: E402
from app import prompts, vn_prompts  # noqa: E402

raw = {
    "name": "宫咲凛", "role": "同班同学", "personality": "慢热",
    "traits": "温柔，慢热", "age": 22, "height_cm": 164,
    "body_type": "纤细高挑", "measurements": "B86 / W58 / H88",
    "birthday": "5月12日", "speech_style": "语速偏慢",
    "likes": "旧书", "dislikes": "吵闹", "personality_locked": True,
}
c = charm.sanitize_character(raw)
assert c["traits"] == ["温柔", "慢热"]
assert c["age"] == 22 and c["height_cm"] == 164
assert c["personality_locked"] is True
for k in ("personality", "traits", "speech_style"):
    assert k in c["locked_fields"]

r = charm.random_character(seed=7)
for k in ("traits", "age", "height_cm", "body_type", "measurements",
          "speech_style", "likes", "dislikes"):
    assert k in r, k

local = charm._local_fine_tune(c, "身高改成 168cm，语气更温柔")
assert local["height_cm"] == 168
assert "更温柔" in local["personality"]

# 无网络：把 LLMClient 换成 mock，验证固定字段在微调中不被覆盖
class _MockLLM:
    mock = True


old = charm.LLMClient
charm.LLMClient = lambda cfg: _MockLLM()
try:
    res = asyncio.run(charm.fine_tune_character(
        c, "身高 170cm，体型改得更纤细，性格改得活泼"))
finally:
    charm.LLMClient = old
assert res["character"]["height_cm"] == 170
assert res["character"]["personality"] == c["personality"]
assert res["character"]["traits"] == c["traits"]
assert res["character"]["speech_style"] == c["speech_style"]

line = prompts.format_characters([c])
assert "身高：164cm" in line and "性格标签：温柔、慢热" in line
assert "身高：164cm" in vn_prompts.format_characters([c])

tmp = Path(tempfile.mkdtemp())
html = library.write_card_html(tmp, dict(c, variants=[]))
text = html.read_text("utf-8")
assert "身高：</b>164 cm" in text and "性格标签" in text
print("OK 1.9.16 character profile / locked fields / fine-tune")

"""换装链路回退契约（1.9.21 更新语义）：

- 本地线路（SD WebUI **与 ComfyUI**）支持换装 img2img；
- 云端/未知线路（如 OpenAI 兼容）必须抛 ImageGenError，让上层回退
  「全量重画」，绝不允许把原图原样当成换装成功；
- 底图非法（不是图片）时任何线路都必须抛错，而不是静默成功。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.image import ImageClient, ImageGenError


class OutfitFallbackTests(unittest.TestCase):
    def test_comfy_invalid_image_still_raises(self) -> None:
        """ComfyUI 已支持换装，但非法底图仍必须失败（不许假装成功）。"""
        client = ImageClient({"image": {"provider": "comfyui", "base_url": "http://127.0.0.1:8188"}})
        with self.assertRaises(ImageGenError):
            asyncio.run(client.refine_outfit(b"not-an-image", "outfit", 1))

    def test_openai_outfit_refine_requests_full_generation_fallback(self) -> None:
        client = ImageClient({"image": {"provider": "openai", "base_url": "https://8.8.8.8", "api_key": "x"}})
        with self.assertRaises(ImageGenError):
            asyncio.run(client.refine_outfit(b"not-an-image", "outfit", 1))


if __name__ == "__main__":
    unittest.main()

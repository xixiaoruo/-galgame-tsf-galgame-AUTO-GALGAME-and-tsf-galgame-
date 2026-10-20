"""换装链路回退契约：非 SD provider 不得把原图当成成功换装。"""
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
    def test_comfy_outfit_refine_requests_full_generation_fallback(self) -> None:
        client = ImageClient({"image": {"provider": "comfyui", "base_url": "http://127.0.0.1:8188"}})
        with self.assertRaises(ImageGenError):
            asyncio.run(client.refine_outfit(b"not-an-image", "outfit", 1))

    def test_openai_outfit_refine_requests_full_generation_fallback(self) -> None:
        client = ImageClient({"image": {"provider": "openai", "base_url": "https://8.8.8.8", "api_key": "x"}})
        with self.assertRaises(ImageGenError):
            asyncio.run(client.refine_outfit(b"not-an-image", "outfit", 1))


if __name__ == "__main__":
    unittest.main()

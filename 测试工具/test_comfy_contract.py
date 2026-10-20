"""ComfyUI 响应契约回归测试：纯本地、零网络、零模型调用。"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.image import _comfy_extract_image, _comfy_sampler, _comfy_workflow


class ComfyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image = {"filename": "result.png", "subfolder": "", "type": "output"}

    def test_standard_history(self) -> None:
        history = {"prompt-id": {"outputs": {"7": {"images": [self.image]}}}}
        self.assertEqual(_comfy_extract_image(history), self.image)

    def test_direct_node_compatibility(self) -> None:
        self.assertEqual(_comfy_extract_image({"outputs": {"7": {"images": [self.image]}}}), self.image)
        self.assertEqual(_comfy_extract_image({"images": [self.image]}), self.image)

    def test_requested_node_has_priority(self) -> None:
        other = {"filename": "other.png", "subfolder": "", "type": "output"}
        history = {"prompt-id": {"outputs": {"7": {"images": [self.image]}, "8": {"images": [other]}}}}
        self.assertEqual(_comfy_extract_image(history, "7"), self.image)

    def test_empty_history_is_safe(self) -> None:
        self.assertIsNone(_comfy_extract_image({}))
        self.assertIsNone(_comfy_extract_image(None))

    def test_workflow_and_sampler_contract(self) -> None:
        workflow = _comfy_workflow("positive", "negative", 512, 768, 1, 20, 6.0, "euler", "normal", "model")
        self.assertEqual(set(workflow), {"1", "2", "3", "4", "5", "6", "7"})
        self.assertEqual(_comfy_sampler("Euler a", "karras"), ("euler_ancestral", "karras"))


if __name__ == "__main__":
    unittest.main()

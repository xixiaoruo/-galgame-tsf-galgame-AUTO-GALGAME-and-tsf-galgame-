"""Image API security regression tests: URL validation without network access."""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.image import ImageGenError, _validate_url


class ImageSecurityTests(unittest.TestCase):
    def test_public_response_url_is_allowed(self) -> None:
        _validate_url("https://8.8.8.8/image.png", allow_local=False)

    def test_loopback_response_url_is_rejected(self) -> None:
        with self.assertRaises(ImageGenError):
            _validate_url("http://127.0.0.1:8000/private.png", allow_local=False)

    def test_private_response_url_is_rejected(self) -> None:
        with self.assertRaises(ImageGenError):
            _validate_url("http://192.168.1.10/image.png", allow_local=False)

    def test_local_provider_explicitly_allows_local_url(self) -> None:
        _validate_url("http://127.0.0.1:7860", allow_local=True)


if __name__ == "__main__":
    unittest.main()

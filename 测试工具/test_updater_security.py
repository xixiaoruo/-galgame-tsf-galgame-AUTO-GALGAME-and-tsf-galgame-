"""更新包路径安全回归测试（标准库，无网络、无真实数据写入）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.updater import _safe_zip_member_path


class UpdaterZipSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tsf-updater-test-"))
        self.base = self.tmp / "staging"
        self.base.mkdir()

    def test_normal_member_stays_inside_staging(self) -> None:
        member = zipfile.ZipInfo("nested/file.txt")
        target = _safe_zip_member_path(self.base, member)
        self.assertTrue(target.is_relative_to(self.base))

    def test_parent_traversal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _safe_zip_member_path(self.base, zipfile.ZipInfo("../escape.txt"))

    def test_symlink_member_is_rejected(self) -> None:
        member = zipfile.ZipInfo("link")
        member.external_attr = 0o120777 << 16
        with self.assertRaises(ValueError):
            _safe_zip_member_path(self.base, member)


if __name__ == "__main__":
    unittest.main()

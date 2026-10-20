#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.8.0 绘画线路优化：成品缓存 / 节能预算 / 统计快照 的隔离单测。

不碰真实配置、不联系生图服务：
  · `TSF_DATA_DIR` 指向 .devdata/perf-test（独立 CACHE_DIR）
  · ImageClient 子类只覆盖 `_request_sdwebui`，返回内存生成的 PNG
  · 断言：同 prompt+参数+seed 第二次不再请求（缓存命中）、
          eco_mode 下失败重试次数更少、matcher 被跳过、统计计数正确。

运行：python 测试工具/test_perf_180.py
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_DATA = ROOT / ".devdata" / "perf-test"
if TEST_DATA.exists():
    shutil.rmtree(TEST_DATA, ignore_errors=True)
(TEST_DATA / "data").mkdir(parents=True, exist_ok=True)
os.environ["TSF_DATA_DIR"] = str(TEST_DATA)

from PIL import Image  # noqa: E402

from app import image as im  # noqa: E402


def _png(color=None, size=(96, 96)) -> bytes:
    """带噪点的 PNG（>1KB）：成品缓存刻意跳过 <1KB 的占位/空白图，
    纯色小图会被拒绝缓存，所以测试图必须像真实成品一样有体积。"""
    buf = io.BytesIO()
    if color is None:
        Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(
            buf, format="PNG")
    else:
        Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class FakeClient(im.ImageClient):
    """只覆盖底层请求：不发网络、不读真实配置。"""

    def __init__(self, eco: bool = False):
        self.cfg = {
            "provider": "sdwebui",
            "base_url": "http://127.0.0.1:7860",
            "gen_cache": True,
            "gen_cache_mb": 16,
            "eco_mode": eco,
        }
        self.provider = "sdwebui"
        self.mock = False
        self.calls = 0
        self._recovered = True          # 避免任何 reload 分支
        self._degraded_count = 0
        self._issue_count = 0

    async def _request_sdwebui(self, prompt, width, height, extra_negative="",
                               seed=None, steps=None, cfg_scale=None,
                               sampler="", scheduler="", hr=False, **kw):
        self.calls += 1
        return _png()   # 96x96 噪点 PNG，体积足够进入成品缓存


async def test_cache() -> None:
    c = FakeClient(eco=False)
    prompt = "unit-cache-hit-180"
    args = dict(prompt=prompt, width=64, height=64, extra_negative="neg",
                seed=424242, check=None, attempts=3, steps=12,
                cfg_scale=6.0, sampler="Euler a", scheduler="karras")
    first = await c._sd_retry(**args)
    assert c.calls == 1, f"第一次应真实请求 1 次，实际 {c.calls}"
    assert first.startswith(b"\x89PNG")
    second = await c._sd_retry(**args)
    assert c.calls == 1, f"第二次应命中缓存不再请求，实际 {c.calls}"
    assert second == first
    snap = im.gen_stats_snapshot()
    assert snap["cache_hits"] >= 1, snap
    assert snap["cache_entries"] >= 1, snap
    print(f"  [cache] requests={snap['requests']} hits={snap['cache_hits']} "
          f"entries={snap['cache_entries']} mb={snap['cache_mb']}")


async def test_eco() -> None:
    # 正常模式：质检永远失败 → 3 次变体 + 大步长补 3 次 = 6 次
    normal = FakeClient(eco=False)
    await normal._sd_retry(
        prompt="unit-eco-normal-180", width=64, height=64,
        extra_negative="neg", seed=111, check=lambda b: "模拟不合格",
        attempts=3, soft=True, steps=10, cfg_scale=6.0,
        sampler="Euler a", scheduler="karras")
    # 节能模式：attempts 压到 2 + 大步长补 1 = 3 次；matcher 被跳过
    eco = FakeClient(eco=True)
    matcher_calls = {"n": 0}

    async def matcher(raw):
        matcher_calls["n"] += 1
        return 5.0

    await eco._sd_retry(
        prompt="unit-eco-eco-180", width=64, height=64,
        extra_negative="neg", seed=222, check=lambda b: "模拟不合格",
        attempts=3, soft=True, steps=10, cfg_scale=6.0,
        sampler="Euler a", scheduler="karras", matcher=matcher)
    assert normal.calls == 6, f"正常模式应 6 次，实际 {normal.calls}"
    assert eco.calls == 3, f"节能模式应 3 次，实际 {eco.calls}"
    assert matcher_calls["n"] == 0, "节能模式不应调用转变判定器"
    print(f"  [eco] normal_calls={normal.calls} eco_calls={eco.calls} "
          f"matcher_calls={matcher_calls['n']}")


async def main() -> int:
    # 清空计数，避免其它测试污染
    im.gen_stats_snapshot(reset=True)
    await test_cache()
    im.gen_stats_snapshot(reset=True)
    await test_eco()
    print("OK · 1.8.0 线路优化单测通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""1.9.18 随机数 / CoC 检定回归（不联网）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import rng  # noqa: E402

r = rng.roll("2d6+3", "测试")
assert r["count"] == 2 and 5 <= r["total"] <= 15
assert len(r["rolls"]) == 2 and r["notation"] == "2d6+3"
assert rng.roll("d%")["sides"] == 100
for bad in ("", "0d6", "1d1", "99d6", "abc"):
    try:
        rng.roll(bad)
    except rng.RngError:
        pass
    else:
        raise AssertionError(f"应当拒绝 {bad!r}")
# CoC 检定使用 tuning（真实数据文件）；只读，不改会话
c = rng.coc_check("STR", 50, "LOW")
assert c["notation"] == "1d12" and 1 <= c["roll"] <= 12
assert c["stat"] == "STR" and c["threat"] == "LOW"
assert "vs roll" in c["reason"]
print("OK 1.9.18 RNG / CoC check")

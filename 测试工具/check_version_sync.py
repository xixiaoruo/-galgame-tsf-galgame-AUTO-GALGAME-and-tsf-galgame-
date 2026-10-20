#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本号单一真源校验（构建期闸门）。

真源（唯一权威）
    server.py 的 ``GAME_VERSION``

派生物（必须与真源逐字一致；由本脚本强制，不再是"三处手抄"）
    static/index.html 里 ``style.css?v=<版本>``
    static/index.html 里 ``main.js?v=<版本>``
    static/index.html 里 ``ui2.js?v=<版本>``（1.8.0 起：UI 2.0 独立编排层）

为什么要这个脚本
    版本号过去靠人工维持三处同步（见 项目档案与开发备忘.md 第 19 条、
    交接上下文.md 的"GAME_VERSION + index.html 两处 ?v= 必须同步"）。
    人工同步迟早漂移，而漂移的表现是"前端加载旧 JS/CSS 混搭报错"，
    排查成本远高于一条闸门。

    注意：**不要**用中间件注入 HTML 去改 ``?v=``——``StaticFiles`` 是流式
    响应、没有 body 可改（交接上下文.md 记录的坑 #10）。所以 ``?v=`` 机制
    原样保留，改由本脚本在构建期校验（并可用 ``--fix`` 从真源派生）。

用法
    python 测试工具/check_version_sync.py                # 校验
    python 测试工具/check_version_sync.py --fix          # 用真源重写全部 ?v=（1.8 起三处）
    python 测试工具/check_version_sync.py --root DIR     # 指定项目根
    python 测试工具/check_version_sync.py --json         # 机器可读

退出码
    0  三处一致
    1  不一致（逐条列出 文件:行 与实际值）
    2  结构问题（文件缺失 / 真源缺失 / ?v= 引用数量不是 3）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------- 真源与派生点的定位规则（改这里即可适配目录调整） ----------

SOURCE_FILE = "server.py"
SOURCE_PATTERN = re.compile(
    r"""^[ \t]*GAME_VERSION[ \t]*=[ \t]*(?P<q>["'])(?P<v>.+?)(?P=q)""",
    re.MULTILINE,
)

DERIVED_FILE = os.path.join("static", "index.html")
DERIVED_PATTERN = re.compile(r"""\?v=(?P<v>[^"'&\s)>]+)""")
DERIVED_EXPECTED_COUNT = 3   # 1.8.0：style.css + main.js + ui2.js

# 版本号允许的字符集：URL 安全、不含 ? & # 空格
VERSION_SHAPE = re.compile(r"""^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$""")


class Structural(Exception):
    """结构问题（退出码 2）：无法定位真源或派生点。"""


def _read(path: Path) -> tuple[str, str]:
    """按 UTF-8 读文本，返回 (文本, 换行风格)。找不到文件抛 Structural。"""
    if not path.is_file():
        raise Structural(f"文件不存在：{path}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - 仓库文件都是 UTF-8
        raise Structural(f"不是 UTF-8 文本：{path}（{exc}）") from exc
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return text, newline


def _line_of(text: str, offset: int) -> int:
    """字符偏移 → 1 起的行号。"""
    return text.count("\n", 0, offset) + 1


def read_source(root: Path) -> dict:
    """读真源：server.py 的 GAME_VERSION。"""
    path = root / SOURCE_FILE
    text, _ = _read(path)
    hits = list(SOURCE_PATTERN.finditer(text))
    if not hits:
        raise Structural(
            f"在 {path} 里找不到 `GAME_VERSION = \"...\"`——真源定义被删或改了写法"
        )
    if len(hits) > 1:
        where = ", ".join(f"{path}:{_line_of(text, h.start())}" for h in hits)
        raise Structural(f"{path} 里出现 {len(hits)} 处 GAME_VERSION 赋值（应只有 1 处）：{where}")
    hit = hits[0]
    value = hit.group("v")
    if not VERSION_SHAPE.match(value):
        raise Structural(f"{path}:{_line_of(text, hit.start())} 的 GAME_VERSION={value!r} 形状不合法")
    return {
        "path": path,
        "display": SOURCE_FILE,
        "line": _line_of(text, hit.start()),
        "value": value,
        "text": text,
    }


def read_derived(root: Path) -> dict:
    """读派生点：static/index.html 的两处 ?v=。"""
    path = root / DERIVED_FILE
    text, _ = _read(path)
    hits = list(DERIVED_PATTERN.finditer(text))
    if len(hits) != DERIVED_EXPECTED_COUNT:
        raise Structural(
            f"{path} 里 ?v= 引用有 {len(hits)} 处，期望 {DERIVED_EXPECTED_COUNT} 处"
            "——资产清单变了，请同步更新本脚本的 DERIVED_EXPECTED_COUNT"
        )
    sites = []
    for hit in hits:
        sites.append({
            "line": _line_of(text, hit.start()),
            "value": hit.group("v"),
            "span": hit.span("v"),
        })
    return {
        "path": path,
        "display": DERIVED_FILE.replace(os.sep, "/"),
        "text": text,
        "newline": _read(path)[1],
        "sites": sites,
    }


def check(root: Path) -> dict:
    """执行一次校验，返回结果字典（不含退出码逻辑）。"""
    source = read_source(root)
    derived = read_derived(root)
    expected = source["value"]

    mismatches = [
        {
            "file": derived["display"],
            "line": site["line"],
            "expected": expected,
            "actual": site["value"],
        }
        for site in derived["sites"]
        if site["value"] != expected
    ]

    # 两处派生点之间也要一致（防止"改了其中一处又改错了另一处"时只看真源漏判）
    derived_values = {site["value"] for site in derived["sites"]}
    derived_internally_split = len(derived_values) > 1

    return {
        "root": str(root),
        "source": {
            "file": source["display"],
            "line": source["line"],
            "value": expected,
        },
        "derived": [
            {"file": derived["display"], "line": site["line"], "value": site["value"]}
            for site in derived["sites"]
        ],
        "mismatches": mismatches,
        "derived_internally_split": derived_internally_split,
        "in_sync": not mismatches and not derived_internally_split,
        "_source": source,
        "_derived": derived,
    }


def render(result: dict, as_json: bool) -> None:
    """输出人类可读的校验报告或 JSON。"""
    if as_json:
        print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")},
                         ensure_ascii=False, indent=2))
        return

    print("版本号单一真源校验")
    print(f"  项目根：{result['root']}")
    src = result["source"]
    print(f"  真源  ：{src['file']}:{src['line']}  GAME_VERSION = {src['value']}")
    for site in result["derived"]:
        mark = "ok " if site["value"] == src["value"] else "!! "
        print(f"  派生  ：{mark}{site['file']}:{site['line']}  ?v={site['value']}")

    if result["in_sync"]:
        print("\n结果：三处一致 ✓")
        return

    print(f"\n结果：不一致 ✗（{len(result['mismatches'])} 处）")
    for item in result["mismatches"]:
        print(f"  - {item['file']}:{item['line']}  期望 {item['expected']}，实际 {item['actual']}")
    if result["derived_internally_split"]:
        print("  - 两处 ?v= 之间也不一致")
    print(f"\n修复：python 测试工具/check_version_sync.py --fix"
          f"   （会以 {src['file']} 的 {src['value']} 重写上面这些位置）")


def apply_fix(root: Path, result: dict) -> int:
    """用真源重写 index.html 的两处 ?v=。返回改动处数。"""
    source = result["_source"]
    derived = result["_derived"]
    expected = source["value"]
    text = derived["text"]

    # 从后往前替换，避免偏移失效
    changed = 0
    out = text
    for site in sorted(derived["sites"], key=lambda s: s["span"][0], reverse=True):
        if site["value"] == expected:
            continue
        start, end = site["span"]
        out = out[:start] + expected + out[end:]
        changed += 1

    if changed:
        path = derived["path"]
        tmp = path.with_name(path.name + ".version-sync.tmp")
        tmp.write_bytes(out.encode("utf-8"))
        os.replace(tmp, path)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 server.py 的 GAME_VERSION 与 index.html 两处 ?v= 是否一致",
    )
    parser.add_argument("--root", default=None,
                        help="项目根目录（默认：本脚本所在目录的上一级）")
    parser.add_argument("--fix", action="store_true",
                        help="不一致时用真源重写 index.html 的两处 ?v=")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出错误行")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent

    try:
        result = check(root)
    except Structural as exc:
        print(f"[结构问题] {exc}", file=sys.stderr)
        return 2

    if args.fix and not result["in_sync"]:
        changed = apply_fix(root, result)
        if not args.quiet:
            print(f"已用真源 {result['source']['value']} 重写 {changed} 处 ?v=")
        result = check(root)          # 改完复验
        if not args.quiet:
            render(result, args.json)
        return 0 if result["in_sync"] else 1

    if not args.quiet:
        render(result, args.json)
    return 0 if result["in_sync"] else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    raise SystemExit(main())

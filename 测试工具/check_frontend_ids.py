#!/usr/bin/env python3
"""前端 id / 引用静态检查器（阶段 3 前置工具 · 对应审计 3.3，含 B-22 探针）。

用法（在任意目录均可，路径默认按脚本位置推导）：
    python 测试工具\\check_frontend_ids.py                  # 全量：①②③ 都查，命中①② → 退出码 1
    python 测试工具\\check_frontend_ids.py --only dup        # 只看重复 id
    python 测试工具\\check_frontend_ids.py --only dup,missing
    python 测试工具\\check_frontend_ids.py --json            # 机器可读（闸门/CI 用）
    python 测试工具\\check_frontend_ids.py --fail-on-unused  # 把③也当作阻断
    python 测试工具\\check_frontend_ids.py --show-toplevel-bindings       # 顶层 DOM 绑定清单（信息级）
    python 测试工具\\check_frontend_ids.py --show-toplevel-bindings --json  # 同上，机器可读
    python 测试工具\\check_frontend_ids.py --max-toplevel 138             # 顶层绑定棘轮（按“条数”比较）

退出码：
    0 = 本次检查范围内没有 ①重复 id / ②JS 引用但 HTML 不存在
    1 = 命中 ① 或 ②（或**显式开关**下的 ③无人引用 / 顶层绑定超限）
    2 = 读文件失败等内部错误（路径不对/文件缺失）

检查方式（纯静态文本扫描，零第三方依赖，**只读、不改任何文件**）：
    ① 重复 id        static/index.html 里同一 id 出现多次（$() 只会拿到第一个）
    ② 悬空引用       main.js（含 index.html 内联 script 区域）里以
                     getElementById("x") / $("x") / on("x", ...) /
                     querySelector("#x") / querySelectorAll("#x") 引用的 id，
                     在 HTML 里既没有静态声明、也没有在 JS 里被动态声明
    ③ 无人引用       HTML 里声明了、但 JS/CSS/属性都没提到的 id（**仅信息**，默认不影响退出码）
    ④ 顶层 DOM 绑定  main.js 第 1 列（零缩进）的 id 引用清单（`--show-toplevel-bindings`，
                     仅信息；供 M-09 模块化拆 init() 用）

实现约束（有意为之，请勿"优化"掉）：
    * **行解析，不做 XML/XHTML 解析**：读一次文本 → 屏蔽注释 → 逐行判定。
      好处之一是天然不涉及 DOCTYPE/ENTITY/外部实体这类解析器攻击面。
    * **单次读取内完成解析**：行号与"该行原文"取自同一份内存文本，禁止
      "先取行号、之后再读文件"的两段式——文件在两次读取之间变动会让行号整体错位。

关于「动态 id」：main.js 会用模板字符串生成片段（如
`<input id="custom-choice-input">` 配 `row.querySelector("#custom-choice-input")`），
这类 id 在 HTML 里本就不存在。检查器会把 **JS 字符串里出现的 id="..."**
视为「运行时动态声明」，因此不会误报为②。

启发式说明（宁少报不误报）：
    * JS 注释（// 与 /* */）与 HTML 注释（<!-- -->）内的内容一律忽略；
    * 模板插值/变量形态（$(id)、$(containerId)）静态无法解析，单独计数为「不可解析」；
    * ③ 的“被引用”包含：JS 全部引用形态、index.html 内联 script、
      href/for/form/aria-controls/aria-labelledby/aria-describedby 属性、style.css 的 #id 选择器。

顶层 DOM 绑定清单（`--show-toplevel-bindings`，供阶段 3 的 M-09 模块化拆 init() 用）：
    * 「顶层」= **屏蔽注释后**匹配出现在第 1 列（零缩进）的 id 引用；本项目函数体/块内
      代码一律缩进 ≥2 空格，因此第 1 列即模块顶层（与审计「main.js:162-163 这类零缩进
      写法」同一判据）；
    * 覆盖形态：`$("x")`、`on("x", …)`、`getElementById("x")`、`querySelector(All)("#x")`；
      每条给出 **行号 + id + 该行原文**，并标注是否紧跟 `.addEventListener`（=事件绑定）；
    * **信息级**：默认不参与退出码。要让它在闸门里生效必须用显式开关：
      `--fail-on-toplevel`（>0 条即阻断）或 `--max-toplevel N`（超过 N 条即阻断，
      阶段 3 可用「改造前的当前值」作棘轮上限，防止新增顶层绑定）；
    * 口径差异（如实记录，三个口径都给出，不凑数；数字为 2026-10-02 实测）：
      - **行首口径** =「第 1 列就是 `$("`/`on("` 的行数」= `$` 112 + `on` 10 —— 这是审计
        与 t25 报告里的 112 的口径（只统计行首形态）；
      - **行数口径**（`zero_indent_lines`）=「零缩进且**含**引用的行」= 125
        （`$` 120 + `on` 10 − 5 行同时含两者；比行首口径多 8 行，如
        `if ($("cfg-img-provider")) {`、`function cutTarget() { return $("cut-char").value; }`）；
      - **条数口径**（`reference_occurrences`）= 引用个数 = 138（`$` 128 + `on` 10；
        比行数多 8 条，来自 8 行各含两个 `$()`，如
        `$("btn-credits").addEventListener("click", () => $("modal-credits")…)`）。
      关系：行首 ≤ 零缩进行数 ≤ 条数；`--max-toplevel` 棘轮比较的是**条数**。
      三者均在**单次读取**内解析得出（见上文实现约束），口径差异已逐条归因，不并入单一数字。
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:  # Windows 控制台常为 GBK：统一 UTF-8，避免中文 id 打印崩溃
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ID_ATTR_RE = re.compile(r"""\bid\s*=\s*(["'])(?P<v>[^"']+)\1""")
REF_PATTERNS = (
    ("getElementById", re.compile(r"""getElementById\(\s*(["'`])(?P<v>[^"'`]+)\1\s*\)""")),
    ("$", re.compile(r"""(?<![\w$.])\$\(\s*(["'`])(?P<v>[^"'`]+)\1\s*\)""")),
    ("on", re.compile(r"""(?<![\w$.])on\(\s*(["'`])(?P<v>[^"'`]+)\1\s*,""")),
    ("querySelector", re.compile(
        r"""querySelector(?:All)?\(\s*(["'`])#(?P<v>[A-Za-z0-9_:.\-]+)\1\s*\)""")),
)
UNRESOLVABLE_PATTERNS = (
    ("$", re.compile(r"""(?<![\w$.])\$\(\s*(?![ "'`])""")),
    ("getElementById", re.compile(r"""getElementById\(\s*(?![ "'`])""")),
    ("on", re.compile(r"""(?<![\w$.])on\(\s*(?![ "'`])""")),
)
OTHER_REF_ATTR_RE = re.compile(
    r"""\b(?:href|for|form|aria-controls|aria-labelledby|aria-describedby)"""
    r"""\s*=\s*["']#?(?P<v>[^"'\s]+)["']""")
CSS_ID_RE = re.compile(r"#([A-Za-z_][A-Za-z0-9_\-]*)")
SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
                       re.S | re.IGNORECASE)
#: 审计点名的探针（工具改动时不要删：阶段 3 闸门靠它复查 B-22）
AUDIT_PROBES = (
    ("B-22", "index.html", "btn-credits",
     "index.html 重复 id → 页面底部「制作名单」按钮点不动（getElementById 只拿第一个）"),
)


class Finding(dict):
    """一条发现：{file, line, kind, id, detail}。"""


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def mask_html_comments(html: str) -> str:
    """把 <!-- --> 内容替换成同长度空白（保留换行 → 行号不变）。"""
    def _blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    return re.sub(r"<!--.*?-->", _blank, html, flags=re.S)


def mask_js_comments(src: str) -> str:
    """把 JS 注释内容替换成空白（保留换行）；字符串里的 // 不误伤。"""
    out = list(src)
    i, n = 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def split_html_scripts(html: str):
    """返回 (挖空脚本体的标记文本, [(脚本体, 起始行), ...])。"""
    markup = list(html)
    scripts = []
    for m in SCRIPT_RE.finditer(html):
        body = m.group("body")
        if body.strip():
            scripts.append((body, _line_of(html, m.start("body"))))
        start, end = m.start("body"), m.end("body")
        for k in range(start, end):
            if markup[k] != "\n":
                markup[k] = " "
    return "".join(markup), scripts


def js_findings(src: str, fname: str, first_line: int, *, dynamic_only=False):
    """从一个 JS 文本里抽取 动态 id / 引用 / 不可解析计数。"""
    masked = mask_js_comments(src)
    dynamic, refs, unresolvable = [], [], []
    if not dynamic_only:
        for m in ID_ATTR_RE.finditer(masked):
            dynamic.append(Finding(file=fname, line=first_line + _line_of(src, m.start()) - 1,
                                   kind="dynamic-id", id=m.group("v"),
                                   detail="JS 生成的片段里声明的 id（运行时才存在）"))
        for kind, rx in REF_PATTERNS:
            for m in rx.finditer(masked):
                refs.append(Finding(file=fname, line=first_line + _line_of(src, m.start()) - 1,
                                    kind=kind, id=m.group("v"),
                                    detail=f"{kind} 引用"))
        for kind, rx in UNRESOLVABLE_PATTERNS:
            for m in rx.finditer(masked):
                unresolvable.append({"file": fname,
                                     "line": first_line + _line_of(src, m.start()) - 1,
                                     "kind": kind})
    else:
        for m in ID_ATTR_RE.finditer(masked):
            dynamic.append(Finding(file=fname, line=first_line + _line_of(src, m.start()) - 1,
                                   kind="dynamic-id", id=m.group("v"),
                                   detail="JS 生成的片段里声明的 id"))
    return dynamic, refs, unresolvable


def toplevel_bindings(src: str, fname: str = "main.js") -> list[Finding]:
    """列出「顶层（第 1 列，零缩进）」的 DOM id 引用 —— M-09 拆 init() 的核对清单。

    判据：在**屏蔽注释后**的文本里，第 1 列（无前导空白）出现的引用才算顶层；
    函数体/块内（缩进 ≥2 空格）与该行被注释掉的一律不计。
    每条含 `file / line / kind（形态）/ id / source（该行原文）/ binding（是否事件绑定）`。
    """
    masked = mask_js_comments(src)
    out: list[Finding] = []
    for idx, line in enumerate(masked.splitlines(), start=1):
        if not line or line[0].isspace():      # 只认零缩进行
            continue
        for kind, rx in REF_PATTERNS:
            for m in rx.finditer(line):
                raw = src.splitlines()[idx - 1].strip()
                out.append(Finding(
                    file=fname, line=idx, kind=kind, id=m.group("v"),
                    binding=("addEventListener" in line) or kind == "on",
                    source=(raw[:157] + "…") if len(raw) > 158 else raw,
                    detail=("顶层事件绑定" if (("addEventListener" in line) or kind == "on")
                            else "顶层查询（同样会在 id 缺失时中断后续脚本）")))
    out.sort(key=lambda x: (x["line"], x["kind"]))
    return out


def check(static_dir: Path) -> dict:
    html_path = static_dir / "index.html"
    js_path = static_dir / "main.js"
    css_path = static_dir / "style.css"
    for p in (html_path, js_path):
        if not p.is_file():
            raise FileNotFoundError(f"缺少前端文件：{p}")
    html = html_path.read_text(encoding="utf-8", errors="replace")
    js = js_path.read_text(encoding="utf-8", errors="replace")
    css = css_path.read_text(encoding="utf-8", errors="replace") if css_path.is_file() else ""

    markup, scripts = split_html_scripts(mask_html_comments(html))

    # ---- 静态 id（标记里的 id="..."）----
    static_ids = []          # [{file,line,id,detail}]
    for m in ID_ATTR_RE.finditer(markup):
        static_ids.append({"file": "index.html", "line": _line_of(markup, m.start()),
                           "id": m.group("v"), "detail": "HTML 静态声明"})

    # ---- 动态 id + 引用（main.js + index.html 内联 script）----
    dynamic, refs, unresolvable = js_findings(js, "main.js", 1)
    inline_refs = []
    for body, first in scripts:
        d2, r2, u2 = js_findings(body, "index.html", first)
        dynamic += d2
        inline_refs += r2
        unresolvable += u2
    refs += inline_refs

    # ---- ③ 的旁证引用源 ----
    # 说明：③ 是「信息」级别，判定取宽（宁少报不误报）。除结构化引用外，
    # 还接受两种弱证据：CSS 的 #id 选择器，以及 id 字符串在 JS/CSS 里
    # 作为普通字符串出现（例如 renderWhoChips("tsf-who")、拼进 innerHTML）。
    css_ids = sorted(set(CSS_ID_RE.findall(mask_js_comments(css))))
    attr_ids = sorted({m.group("v") for m in OTHER_REF_ATTR_RE.finditer(markup)})
    js_text = mask_js_comments(js) + "\n" + "\n".join(
        mask_js_comments(b) for b, _ in scripts)

    static_set = {x["id"] for x in static_ids}
    dynamic_set = {x["id"] for x in dynamic}
    known = static_set | dynamic_set

    # ---- ① 重复 id ----
    dup = []
    by_id = {}
    for x in static_ids:
        by_id.setdefault(x["id"], []).append(x["line"])
    for i, lines in sorted(by_id.items()):
        if len(lines) > 1:
            dup.append(Finding(file="index.html", kind="duplicate-id", id=i,
                               line=lines[0], lines=lines,
                               detail=f"id 出现 {len(lines)} 次，行 {', '.join(map(str, lines))}"
                                      f"（getElementById/$() 只会拿到第一个）"))

    # ---- ② 悬空引用 ----
    missing = []
    for r in refs:
        if r["id"] not in known:
            missing.append(Finding(file=r["file"], line=r["line"], kind="missing-id",
                                   id=r["id"],
                                   detail=f"{r['detail']}，但 HTML 与 JS 里都没有声明"))
    missing.sort(key=lambda x: (x["file"], x["line"], x["id"]))

    # ---- ③ 无人引用（信息）----
    used = {r["id"] for r in refs} | set(css_ids) | set(attr_ids)
    unused, weak_only = [], []
    for x in static_ids:
        if x["id"] in used:
            continue
        if x["id"] in js_text or x["id"] in css:
            weak_only.append(x)          # 只被「字符串/选择器」弱证据提到
            continue
        unused.append(x)
    unused.sort(key=lambda x: (x["line"], x["id"]))
    weak_only.sort(key=lambda x: (x["line"], x["id"]))

    probes = []
    for code, f, pid, desc in AUDIT_PROBES:
        hit = [x for x in dup if x["id"] == pid and f == "index.html"]
        cur = [x for x in static_ids if x["id"] == pid]
        probes.append({
            "code": code, "id": pid, "file": f, "description": desc,
            "reproduced": bool(hit),
            "occurrences": [x["line"] for x in cur],
            "note": (f"命中：{f}:{', '.join(map(str, [x['line'] for x in cur]))}"
                     if hit else
                     f"未复现：当前 {f} 中 id=\"{pid}\" 出现 {len(cur)} 次"
                     + (f"（行 {', '.join(map(str, [x['line'] for x in cur]))}）" if cur else "（不存在）")),
        })

    return {
        "static_dir": str(static_dir),
        "files": {
            "index.html": {"lines": html.count("\n") + 1,
                           "inline_scripts": len(scripts)},
            "main.js": {"lines": js.count("\n") + 1},
            "style.css": {"lines": css.count("\n") + 1 if css else 0},
        },
        "stats": {
            "html_id_attrs": len(static_ids),
            "html_unique_ids": len(static_set),
            "js_dynamic_ids": len(dynamic_set),
            "refs_total": len(refs),
            "refs_by_kind": {k: sum(1 for r in refs if r["kind"] == k)
                             for k, _ in REF_PATTERNS},
            "unresolvable_refs": len(unresolvable),
            "css_id_selectors": len(css_ids),
            "attr_refs": len(attr_ids),
        },
        "dup": dup,
        "missing": missing,
        "unused": unused,
        "weak_only": weak_only,
        "unresolvable": unresolvable,
        "audit_probes": probes,
        "toplevel": toplevel_bindings(js),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="前端 id / 引用静态检查器（阶段 3 前置 · 审计 3.3 / B-22）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0=干净，1=命中 ①重复id/②悬空引用（或显式开关下的 ③/顶层绑定超限），2=内部错误\n"
            "\n"
            "--show-toplevel-bindings（信息级，供阶段 3 的 M-09 拆 init() 用）：\n"
            "  列出 main.js 第 1 列（零缩进）的 DOM id 引用，每条含 行号 + id + 该行原文。\n"
            "  两个数字口径不同，输出里同时给出，**不要混用**：\n"
            "    · zero_indent_lines（行数）     = 零缩进且含引用的行；与审计「零缩进顶层」\n"
            "      及 t25 报告的 112 同一口径（$(\"…\") 行 = 112）。\n"
            "    · reference_occurrences（条数） = 引用个数，同一行出现多个 $()/on() 各计一条；\n"
            "      --max-toplevel 棘轮比较的就是这个数（当前 138）。\n"
            "  默认不参与退出码；要纳入闸门必须显式开 --fail-on-toplevel 或 --max-toplevel N。\n"
            "  实现说明：本工具走**行解析**（读一次文本 → 屏蔽注释 → 逐行判定），不做 XML/XHTML\n"
            "  解析，因此天然不涉及 DOCTYPE/ENTITY/外部实体问题——请勿为了“更严谨”改成 XML 解析。"))
    ap.add_argument("--root", default="", help="static 目录（默认：脚本所在项目的 static/）")
    ap.add_argument("--only", default="",
                    help="只看某一类：dup,missing,unused（逗号分隔，可多次）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（闸门/CI 用）")
    ap.add_argument("--fail-on-unused", action="store_true", help="把③无人引用也当作阻断")
    ap.add_argument("--show-toplevel-bindings", action="store_true",
                    help="列出 main.js 顶层（零缩进）DOM 绑定/查询（信息级，不参与退出码）")
    ap.add_argument("--fail-on-toplevel", action="store_true",
                    help="把「存在顶层绑定」当作阻断（需显式开启；默认信息级）")
    ap.add_argument("--max-toplevel", type=int, default=None,
                    help="顶层绑定【条数】上限（超过即阻断；阶段 3 可用当前值作棘轮）")
    ap.add_argument("--max-list", type=int, default=40, help="每类最多打印多少条（默认 40）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论行")
    a = ap.parse_args()

    static_dir = Path(a.root).resolve() if a.root else \
        Path(__file__).resolve().parent.parent / "static"
    only = [x.strip() for x in a.only.replace("，", ",").split(",") if x.strip()]
    bad_only = [x for x in only if x not in ("dup", "missing", "unused")]
    if bad_only:
        print(f"[参数错误] --only 只支持 dup/missing/unused，收到：{bad_only}", file=sys.stderr)
        return 2
    show = set(only) if only else {"dup", "missing", "unused"}

    try:
        res = check(static_dir)
    except FileNotFoundError as e:
        print(f"[内部错误] {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"[内部错误] 读取失败：{e}", file=sys.stderr)
        return 2

    blocking = (len(res["dup"]) if "dup" in show else 0) + \
               (len(res["missing"]) if "missing" in show else 0)
    if a.fail_on_unused and "unused" in show:
        blocking += len(res["unused"])
    # 顶层绑定：**默认信息级**（不影响退出码）；只有显式开关才参与判定
    toplevel = res["toplevel"]
    tl_total = len(toplevel)
    tl_bind = sum(1 for x in toplevel if x.get("binding"))
    tl_block = 0
    tl_reason = ""
    if a.fail_on_toplevel and tl_total:
        tl_block = tl_total
        tl_reason = "（--fail-on-toplevel：存在顶层绑定即阻断）"
    if a.max_toplevel is not None and tl_total > a.max_toplevel:
        tl_block = max(tl_block, tl_total - a.max_toplevel)
        tl_reason = f"（--max-toplevel {a.max_toplevel}：超出 {tl_total - a.max_toplevel} 条）"
    blocking += tl_block
    code = 1 if blocking else 0
    want_tl = bool(a.show_toplevel_bindings or a.fail_on_toplevel
                   or a.max_toplevel is not None)

    if a.json:
        out = {"static_dir": res["static_dir"], "files": res["files"],
               "stats": res["stats"],
               "findings": {"dup": res["dup"], "missing": res["missing"],
                            "unused": res["unused"]},
               "unresolvable": res["unresolvable"],
               "audit_probes": res["audit_probes"],
               "checked": sorted(show), "exit_code": code}
        if want_tl:      # 只在显式要求时新增该键，既有 JSON 消费方不受影响
            out["toplevel_bindings"] = {
                # 字段名自解释：「条数」vs「行数」不混用 count/lines 这种模糊名
                "reference_occurrences": tl_total,     # 引用个数（同行多个各计一条）
                "zero_indent_lines": len({x["line"] for x in toplevel}),
                "event_bindings": tl_bind,
                "by_kind_occurrences": {k: sum(1 for x in toplevel if x["kind"] == k)
                                        for k, _ in REF_PATTERNS},
                "by_kind_zero_indent_lines": {
                    k: len({x["line"] for x in toplevel if x["kind"] == k})
                    for k, _ in REF_PATTERNS},
                "blocking": tl_block, "reason": tl_reason,
                "items": toplevel,
            }
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return code

    def _show(items, title, mark):
        print(f"\n[{mark}] {title}：{len(items)} 项")
        for x in items[:a.max_list]:
            loc = f"{x.get('file', 'index.html')}:{x['line']}"
            print(f"    {loc}  id=\"{x['id']}\"  {x.get('detail', '')}")
        if len(items) > a.max_list:
            print(f"    …（还有 {len(items) - a.max_list} 项，用 --max-list 调整）")

    if not a.quiet:
        f = res["files"]
        print("== 前端 id / 引用静态检查（只读）==")
        print(f"目录：{res['static_dir']}")
        print(f"文件：index.html {f['index.html']['lines']} 行"
              f"（内联 script {f['index.html']['inline_scripts']} 处）· "
              f"main.js {f['main.js']['lines']} 行 · style.css {f['style.css']['lines']} 行")
        s = res["stats"]
        print(f"统计：HTML id= {s['html_id_attrs']}（唯一 {s['html_unique_ids']}）· "
              f"JS 动态 id {s['js_dynamic_ids']} · 引用 {s['refs_total']} 处"
              f"（$ {s['refs_by_kind'].get('$', 0)} / on {s['refs_by_kind'].get('on', 0)} / "
              f"getElementById {s['refs_by_kind'].get('getElementById', 0)} / "
              f"querySelector {s['refs_by_kind'].get('querySelector', 0)}）· "
              f"不可静态解析 {s['unresolvable_refs']} 处 · CSS #id {s['css_id_selectors']}")
        if "dup" in show:
            _show(res["dup"], "① 重复 id（阻断）", "①")
        if "missing" in show:
            _show(res["missing"], "② JS 引用但 HTML/JS 中不存在（阻断）", "②")
        if "unused" in show:
            _show(res["unused"], "③ HTML 存在但无人引用（信息，不影响退出码）", "③")
            if res["weak_only"]:
                print(f"    （另有 {len(res['weak_only'])} 个 id 只被弱证据提到——CSS 选择器或 "
                      f"JS 里的字符串（如 renderWhoChips(\"tsf-who\")），未计入③）")
        print("\n审计探针：")
        for p in res["audit_probes"]:
            print(f"    [{p['code']}] {p['id']}（{p['file']}）-> "
                  f"{'复现 ✅' if p['reproduced'] else '未复现'}"
                  f"  {p['note']}")
        if res["unresolvable"]:
            print(f"    不可静态解析的引用 {len(res['unresolvable'])} 处"
                  f"（变量形态，工具不判定），例如："
                  + "、".join(f"{u['file']}:{u['line']} {u['kind']}"
                              for u in res["unresolvable"][:3]))
        if only and not ({"dup", "missing"} & show):
            print("\n提示：--only 未包含 dup/missing，本次未检查①②，退出码不含它们")
        if a.show_toplevel_bindings:
            tl_lines = len({x["line"] for x in toplevel})
            kinds = " / ".join(f"{k} {sum(1 for x in toplevel if x['kind'] == k)}"
                               for k, _ in REF_PATTERNS
                               if any(x["kind"] == k for x in toplevel))
            print(f"\n[顶层 DOM 绑定] main.js 第 1 列（零缩进）的 id 引用："
                  f"{tl_total} 条 / {tl_lines} 行（事件绑定 {tl_bind} 条 · {kinds}）"
                  f"　[信息级，不参与退出码{tl_reason}]")
            for x in toplevel[:a.max_list]:
                print(f"    main.js:{x['line']}  id=\"{x['id']}\"  "
                      f"[{'绑定' if x.get('binding') else '查询'}]  {x['source']}")
            if tl_total > a.max_list:
                print(f"    …（还有 {tl_total - a.max_list} 条，用 --max-list 调整）")
            print(f"    口径：条数 {tl_total} = 引用个数（同行多个各计一条，--max-toplevel 棘轮用它）；"
                  f"行数 {tl_lines} = 零缩进且**含**引用的行；"
                  f"「行首即 $(\"/on(\" 的行数」（审计/t25 的 112 口径）≤ 行数 ≤ 条数，"
                  f"三者含义不同、不互相替代。")

    # 结论行的措辞：**t28 之前的所有调用形态保持逐字不变**；
    # 只有用到 t28 新增开关时才用泛化措辞（因为 blocking 可能来自③/顶层绑定）。
    legacy_modes = not (a.show_toplevel_bindings or a.fail_on_toplevel
                        or a.max_toplevel is not None)
    if legacy_modes:
        verdict = ("OK（本次范围内无 ①② 问题）" if code == 0
                   else f"FAIL（命中 {blocking} 项 ①②）")
    else:
        verdict = "OK（本次范围内无阻断项）" if code == 0 else f"FAIL（命中 {blocking} 项）"
        if code == 0 and want_tl and not a.quiet:
            print(f"（顶层绑定 {tl_total} 条为信息级，未计入退出码）")
    print(f"\n结论：{verdict} → 退出码 {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())

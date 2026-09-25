"""启动进度：控制台进度行 + 桌面 Tk 进度窗 + data/startup.json 阶段记录。

纯标准库实现，不依赖 app 内其它模块——server.py 可以在导入重依赖
（rembg/onnxruntime 等）之前就调用 mark() 显示进度。
tkinter 不可用（headless / 打包异常）时自动降级为控制台进度，不影响启动。
控制台只输出 ASCII（exe 控制台为 GBK，中文会崩）；中文仅用于 GUI 窗口。
"""
import json
import sys
import time
from pathlib import Path

_TOTAL = 8
_idx = 0
_msg_cn = ""
_status_file: Path | None = None
_root = None
_label = None
_bar = None
_tk_ok = False
_initialized = False


def set_status_file(path) -> None:
    """记录阶段状态的 json 路径（data/startup.json），可选。"""
    global _status_file
    _status_file = Path(path) if path else None


def _write_status(done: bool = False) -> None:
    if not _status_file:
        return
    try:
        _status_file.parent.mkdir(parents=True, exist_ok=True)
        _status_file.write_text(
            json.dumps({
                "stage": _idx,
                "total": _TOTAL,
                "msg": _msg_cn,
                "ts": time.time(),
                "done": done,
            }, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def _ensure_window() -> None:
    """懒创建进度窗口（首次 mark 时调用，尽量早显示）。"""
    global _root, _label, _bar, _tk_ok
    if _initialized:
        return
    _initialized = True
    try:
        import tkinter as _tk
        from tkinter import ttk as _ttk
        _root = _tk.Tk()
        _root.title("自动化AI Galgame 正在启动")
        _root.geometry("440x150")
        _root.resizable(False, False)
        _root.attributes("-topmost", True)
        frame = _ttk.Frame(_root, padding=16)
        frame.pack(fill="both", expand=True)
        _label = _ttk.Label(frame, text="启动中…", font=("Microsoft YaHei UI", 10))
        _label.pack(anchor="w", pady=(4, 10))
        _bar = _ttk.Progressbar(frame, maximum=_TOTAL, mode="determinate")
        _bar.pack(fill="x")
        _ttk.Label(frame, text="首次启动会解压运行库，请稍候",
                   foreground="#666").pack(anchor="w", pady=(10, 2))
        try:
            _root.attributes("-topmost", False)   # 避免常驻置顶干扰
        except Exception:
            pass
        _tk_ok = True
    except Exception:
        _root = None
        _tk_ok = False


def mark(idx: int, en_msg: str, cn_msg: str = "") -> None:
    """进度埋点：idx 从 1 开始；控制台打印 ASCII 行，窗口与 json 同步更新。"""
    global _idx, _msg_cn
    _idx = max(1, min(int(idx), _TOTAL))
    _msg_cn = cn_msg or en_msg
    print(f"[STARTUP] {_idx}/{_TOTAL} {en_msg}", flush=True)
    _write_status()
    try:
        _ensure_window()
        if _tk_ok and _root is not None:
            if _label is not None:
                _label.config(text=f"[{_idx}/{_TOTAL}] {_msg_cn}")
            if _bar is not None:
                _bar["value"] = _idx
            _root.update_idletasks()
            _root.update()
    except Exception:
        pass


def finish() -> None:
    """服务监听成功：写入完成状态并关闭进度窗口（幂等）。"""
    global _root
    _write_status(done=True)
    try:
        if _root is not None and _tk_ok:
            _root.destroy()
    except Exception:
        pass
    finally:
        _root = None


if __name__ == "__main__":   # 冒烟测试：python -m app.startup（会闪现一个窗口）
    mark(1, "loading runtime environment...", "加载运行环境…")
    time.sleep(0.3)
    mark(2, "starting web service...", "启动服务…")
    time.sleep(0.3)
    finish()

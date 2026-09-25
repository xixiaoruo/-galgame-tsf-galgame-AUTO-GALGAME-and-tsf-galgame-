"""自动化AI Galgame 启动器（tkinter exe）。

功能：剧本管理（保存/编辑/删除）、一键启动游戏、内容分级调整（剧本/运行中会话）、
插件管理（预设指令包）。全部数据经服务端 API（固定字面量路径，动态数据走请求体）；
服务未运行时先由「启动游戏」拉起。本文件不做任何本地文件读写、不拼接动态 URL。
"""
import ipaddress
import json
import socket
import traceback
import subprocess
import sys
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from tkinter import messagebox, ttk

BASE = "http://127.0.0.1:8765"
ROOT = __import__("os").path.dirname(__import__("os").path.abspath(__file__))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止任何重定向（仅开放本机回环单端口）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    def http_error_303(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    def http_error_307(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    def http_error_308(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def api(path: str, method: str = "GET", body: dict | None = None):
    """调用服务端 API（path 仅为内部字面量）。

    安全校验：协议/主机/端口/凭据硬校验；解析后 IP 必须为环回地址；禁重定向。
    """
    url = BASE + path
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password
            or (parsed.port or 80) != 8765
            or not parsed.path.startswith("/")):
        raise ValueError("非法 URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 80,
                                   proto=socket.IPPROTO_TCP)
        for info in infos:
            if not ipaddress.ip_address(info[4][0]).is_loopback:
                raise ValueError("非回环地址")
    except OSError as e:
        raise ValueError("地址解析失败") from e

    req = urllib.request.Request(url, method=method)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def server_online() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1.5):
            return True
    except OSError:
        return False


def get_storylines() -> list:
    if not server_online():
        return []
    return api("/api/storylines").get("storylines", [])


def get_plugins() -> list:
    if not server_online():
        return []
    return api("/api/plugins").get("plugins", [])


def start_server() -> bool:
    try:
        import os
        cmd = [sys.executable, os.path.join(ROOT, "server.py")]
        creation = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(cmd, creationflags=creation, cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    for _ in range(60):
        time.sleep(0.5)
        if server_online():
            return True
    return False


def _enable_dpi_awareness():
    """Windows 11 高 DPI 支持：声明 DPI 感知，避免界面模糊。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("自动化AI Galgame 启动器")
        self.geometry("520x460")
        self.minsize(480, 420)
        ttk.Style().theme_use("clam")
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self._build_story_tab()
        self._build_launch_tab()
        self._build_rating_tab()
        self._build_plugin_tab()
        self.refresh_all()

    # ---------- 剧本 ----------
    def _build_story_tab(self):
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="📖 剧本")
        self.story_list = tk.Listbox(tab, height=10)
        self.story_list.pack(fill="both", expand=True, padx=8, pady=8)
        row = ttk.Frame(tab)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Button(row, text="新建", command=self.story_new).pack(side="left", padx=3)
        ttk.Button(row, text="编辑", command=self.story_edit).pack(side="left", padx=3)
        ttk.Button(row, text="删除", command=self.story_delete).pack(side="left", padx=3)
        ttk.Button(row, text="刷新", command=self.refresh_story).pack(side="left", padx=3)

    def refresh_story(self):
        self.story_list.delete(0, "end")
        self._story_data = get_storylines()
        for s in self._story_data:
            self.story_list.insert(
                "end", f"{s.get('title', '')} ｜ {s.get('content_rating', 'all')} ｜ "
                       f"{len(s.get('characters', []))}角色")
        if self._story_data:
            self.story_list.selection_set(0)

    def _selected_story(self):
        sel = self.story_list.curselection()
        if not sel or sel[0] >= len(getattr(self, "_story_data", [])):
            return None
        return self._story_data[sel[0]]

    def story_new(self):
        self._story_dialog(None)

    def story_edit(self):
        st = self._selected_story()
        if not st:
            messagebox.showinfo("提示", "请先选择一个剧本")
            return
        self._story_dialog(st)

    def story_delete(self):
        st = self._selected_story()
        if not st:
            messagebox.showinfo("提示", "请先选择一个剧本")
            return
        if not messagebox.askyesno("确认", f"删除剧本「{st.get('title')}」？"):
            return
        try:
            api("/api/storylines/delete", method="POST", body={"id": st["id"]})
        except Exception as e:
            messagebox.showerror("错误", f"删除失败：{e}")
        self.refresh_story()

    def _story_dialog(self, st):
        win = tk.Toplevel(self)
        win.title("编辑剧本" if st else "新建剧本")
        win.geometry("640x560")
        pad = ttk.Frame(win)
        pad.pack(fill="both", expand=True, padx=12, pady=10)
        ttk.Label(pad, text="标题").pack(anchor="w", pady=(8, 2))
        title_w = ttk.Entry(pad)
        title_w.pack(fill="x")
        ttk.Label(pad, text="世界观（剧情设定）").pack(anchor="w", pady=(8, 2))
        world_w = tk.Text(pad, height=5)
        world_w.pack(fill="x")
        ttk.Label(pad, text="角色（每行：名字|外貌|性格，最多4个）").pack(anchor="w", pady=(8, 2))
        chars_w = tk.Text(pad, height=6)
        chars_w.pack(fill="x")
        ttk.Label(pad, text="固定话语（每行一条）").pack(anchor="w", pady=(8, 2))
        catch_w = tk.Text(pad, height=3)
        catch_w.pack(fill="x")
        ttk.Label(pad, text="内容分级").pack(anchor="w", pady=(8, 2))
        rating_w = ttk.Combobox(pad, values=["all", "16", "18"], state="readonly", width=10)
        rating_w.pack(anchor="w")
        rating_w.set(st.get("content_rating", "all") if st else "all")
        r18_var = tk.BooleanVar(value=st.get("r18_enabled", False) if st else False)
        ttk.Checkbutton(pad, text="开启 R18 情节（仅 18+ 档生效）",
                        variable=r18_var).pack(anchor="w", pady=4)

        if st:
            title_w.insert(0, st.get("title", ""))
            world_w.insert("1.0", st.get("world", ""))
            chars_w.insert("1.0", "\n".join(
                f"{c.get('name','')}|{c.get('appearance','')}|{c.get('personality','')}"
                for c in st.get("characters", [])))
            catch_w.insert("1.0", "\n".join(st.get("catchphrases", [])))

        def save():
            world = world_w.get("1.0", "end").strip()
            if not world:
                messagebox.showwarning("提示", "请填写世界观")
                return
            chars = []
            for line in chars_w.get("1.0", "end").splitlines():
                parts = [p.strip() for p in line.split("|")]
                if parts[0]:
                    chars.append({"name": parts[0][:12],
                                  "appearance": parts[1][:300] if len(parts) > 1 else "",
                                  "personality": parts[2][:200] if len(parts) > 2 else ""})
            catch = [c.strip()[:60] for c in
                     catch_w.get("1.0", "end").splitlines() if c.strip()]
            body = {
                "id": st.get("id", "") if st else "",
                "title": title_w.get().strip() or "未命名剧本",
                "world": world,
                "protagonist": {"name": "主角", "anchor": ""},
                "characters": chars[:4],
                "outline": "",
                "lorebook": [],
                "directives": [],
                "catchphrases": catch[:5],
                "content_rating": rating_w.get(),
                "r18_enabled": r18_var.get(),
            }
            try:
                api("/api/storylines", method="POST", body=body)
            except Exception as e:
                messagebox.showerror("错误", f"保存失败：{e}（请先点击「启动游戏」）")
                return
            win.destroy()
            self.refresh_story()

        ttk.Button(win, text="保存", command=save).pack(pady=10)

    # ---------- 启动 ----------
    def _build_launch_tab(self):
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="🚀 启动")
        ttk.Label(tab, text="开局的剧本（留空=手动填设定）").pack(anchor="w", padx=10, pady=(12, 2))
        self.launch_story = ttk.Combobox(tab, state="readonly")
        self.launch_story.pack(fill="x", padx=10)
        self.autostart_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="打开后自动开始游戏（跳过手动确认）",
                        variable=self.autostart_var).pack(anchor="w", padx=10, pady=6)
        ttk.Button(tab, text="▶ 启动游戏", command=self.launch).pack(padx=10, pady=8, fill="x")
        self.launch_status = ttk.Label(tab, text="", foreground="#666")
        self.launch_status.pack(anchor="w", padx=10)

    def refresh_launch(self):
        data = get_storylines()
        names = [""] + [f"{s.get('title', '')}" for s in data]
        self.launch_story["values"] = names
        self.launch_story.current(0)

    def launch(self):
        idx = self.launch_story.current()
        st = None
        if idx > 0:
            data = get_storylines()
            if idx - 1 < len(data):
                st = data[idx - 1]
        if server_online():
            self.launch_status.config(text="服务已在运行，打开浏览器…")
        else:
            self.launch_status.config(text="启动服务（第一次约 3 秒）…")
            self.update()
            if not start_server():
                messagebox.showerror("错误", "启动失败：请确认 Python 与依赖已安装")
                return
        url = BASE + "/"
        if st:
            url += f"?story={st['id']}" + (
                "&autostart=1" if self.autostart_var.get() else "")
        webbrowser.open(url)
        self.launch_status.config(text=f"已打开：{url}")

    # ---------- 分级 ----------
    def _build_rating_tab(self):
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="🎚 分级")
        ttk.Label(tab, text="内容分级（应用到剧本或运行中的会话）").pack(
            anchor="w", padx=10, pady=(12, 4))
        self.rating_w = ttk.Combobox(tab, values=["all", "16", "18"],
                                     state="readonly", width=10)
        self.rating_w.set("all")
        self.rating_w.pack(anchor="w", padx=10)
        self.r18_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="开启 R18 情节（仅 18+ 档生效）",
                        variable=self.r18_var).pack(anchor="w", padx=10, pady=4)
        ttk.Label(tab, text="目标剧本：").pack(anchor="w", padx=10, pady=(8, 0))
        self.rating_story = ttk.Combobox(tab, state="readonly")
        self.rating_story.pack(fill="x", padx=10)
        ttk.Label(tab, text="目标会话（下一幕生效）：").pack(anchor="w", padx=10, pady=(8, 0))
        self.rating_session = ttk.Combobox(tab, state="readonly")
        self.rating_session.pack(fill="x", padx=10)
        row = ttk.Frame(tab)
        row.pack(pady=10)
        ttk.Button(row, text="应用到剧本", command=self.rating_to_story).pack(side="left", padx=5)
        ttk.Button(row, text="应用到会话", command=self.rating_to_session).pack(side="left", padx=5)
        self.rating_status = ttk.Label(tab, text="", foreground="#666")
        self.rating_status.pack(anchor="w", padx=10)

    def refresh_rating(self):
        story_data = get_storylines()
        self.rating_story["values"] = [s.get("title", "") for s in story_data]
        self.rating_session["values"] = []
        if server_online():
            try:
                ses = api("/api/game/sessions").get("sessions", [])
                self.rating_session["values"] = [
                    f"{s.get('title', '')}（{s.get('sid', '')[:6]}）" for s in ses]
            except Exception:
                pass

    def rating_to_story(self):
        idx = self.rating_story.current()
        if idx < 0:
            messagebox.showinfo("提示", "选择目标剧本")
            return
        st = get_storylines()[idx]
        st["content_rating"] = self.rating_w.get()
        st["r18_enabled"] = self.r18_var.get() and self.rating_w.get() == "18"
        try:
            api("/api/storylines", method="POST", body=st)
        except Exception as e:
            messagebox.showerror("错误", str(e))
            return
        self.rating_status.config(text=f"已更新剧本「{st['title']}」分级")
        self.refresh_rating()

    def rating_to_session(self):
        idx = self.rating_session.current()
        if idx < 0:
            messagebox.showinfo("提示", "选择目标会话（无会话则先开局）")
            return
        if not server_online():
            messagebox.showinfo("提示", "服务未运行")
            return
        try:
            ses = api("/api/game/sessions").get("sessions", [])
            target = ses[idx]
            api("/api/game/sessions/policy", method="POST",
                body={"sid": target["sid"],
                      "content_rating": self.rating_w.get(),
                      "r18_enabled": self.r18_var.get()})
        except Exception as e:
            messagebox.showerror("错误", str(e))
            return
        self.rating_status.config(text=f"已应用，下一幕生效（{target.get('title', '')}）")

    # ---------- 插件 ----------
    def _build_plugin_tab(self):
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="🧩 插件")
        self.plugin_list = tk.Listbox(tab, height=8)
        self.plugin_list.pack(fill="both", expand=True, padx=8, pady=6)
        row = ttk.Frame(tab)
        row.pack(fill="x", padx=8)
        ttk.Button(row, text="新增", command=self.plugin_add).pack(side="left", padx=3)
        ttk.Button(row, text="删除", command=self.plugin_delete).pack(side="left", padx=3)
        ttk.Button(row, text="刷新", command=self.refresh_plugin).pack(side="left", padx=3)
        self.plugin_status = ttk.Label(tab, text="", foreground="#666")
        self.plugin_status.pack(anchor="w", padx=8, pady=6)

    def refresh_plugin(self):
        self.plugin_list.delete(0, "end")
        self._plugin_data = get_plugins()
        for p in self._plugin_data:
            self.plugin_list.insert(
                "end", f"{p.get('name', '')}（{len(p.get('directives', []))}条指令）")
        if self._plugin_data:
            self.plugin_list.selection_set(0)

    def plugin_add(self):
        win = tk.Toplevel(self)
        win.title("新增插件（预设指令包）")
        win.geometry("560x420")
        pad = ttk.Frame(win)
        pad.pack(fill="both", expand=True, padx=12, pady=10)
        ttk.Label(pad, text="插件名称").pack(anchor="w")
        name_w = ttk.Entry(pad)
        name_w.pack(fill="x")
        ttk.Label(pad, text="说明（可选）").pack(anchor="w", pady=(8, 0))
        desc_w = ttk.Entry(pad)
        desc_w.pack(fill="x")
        ttk.Label(pad, text="插件类型").pack(anchor="w", pady=(8, 0))
        kind_w = ttk.Combobox(pad, state="readonly", width=22, values=[
            "story｜剧情指令（注入剧情提示词）",
            "image_style｜立绘风格（追加到风格后缀）",
            "image_negative｜立绘负面词（减少错误生成）"])
        kind_w.pack(anchor="w")
        kind_w.current(0)
        ttk.Label(pad, text="指令（每行一条，注入剧情提示词）").pack(anchor="w", pady=(8, 0))
        dir_w = tk.Text(pad, height=10)
        dir_w.pack(fill="both", expand=True)
        ttk.Label(pad, text="示例：剧情节奏放缓；多用比喻；角色互动更主动",
                  foreground="#888").pack(pady=4)

        def save():
            name = name_w.get().strip()
            if not name:
                messagebox.showwarning("提示", "请填写插件名称")
                return
            dirs = [{"text": d.strip()[:200]}
                    for d in dir_w.get("1.0", "end").splitlines() if d.strip()][:8]
            if not dirs:
                messagebox.showwarning("提示", "至少一条指令")
                return
            try:
                api("/api/plugins", method="POST",
                    body={"id": "", "name": name,
                          "description": desc_w.get().strip(),
                          "kind": kind_w.get().split("｜")[0],
                          "directives": dirs})
            except Exception as e:
                messagebox.showerror("错误", str(e))
                return
            win.destroy()
            self.refresh_plugin()

        ttk.Button(win, text="保存", command=save).pack(pady=10)

    def plugin_delete(self):
        sel = self.plugin_list.curselection()
        if not sel or sel[0] >= len(getattr(self, "_plugin_data", [])):
            messagebox.showinfo("提示", "选择插件")
            return
        p = self._plugin_data[sel[0]]
        if not messagebox.askyesno("确认", f"删除插件「{p.get('name')}」？"):
            return
        try:
            api("/api/plugins/delete", method="POST", body={"id": p["id"]})
        except Exception as e:
            messagebox.showerror("错误", str(e))
        self.refresh_plugin()

    def refresh_all(self):
        self.refresh_story()
        self.refresh_launch()
        self.refresh_rating()
        self.refresh_plugin()


if __name__ == "__main__":
    _enable_dpi_awareness()
    try:
        app = Launcher()
        app.mainloop()
    except Exception:
        # 崩溃兜底：弹窗展示完整堆栈（调试用）
        messagebox.showerror("启动器错误",
                             "发生未捕获异常：" + chr(10) + traceback.format_exc())

"""Windows and Linux interface for the bundled redhand and trueup engines."""

from __future__ import annotations

import hashlib
import os
import queue
import sys
import threading
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import engine

PAPER, WHITE, INK, MUTED, TEAL = "#f6f6f0", "#ffffff", "#202c2c", "#566060", "#005b54"


def local_storage() -> Path:
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "File Change Desk"
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "file-change-desk"


class Desk:
    def __init__(self, window: tk.Tk, storage: Path | None = None):
        self.window = window
        self.storage = storage or local_storage()
        self.root: Path | None = None
        self.state: Path | None = None
        self.transcript: Path | None = None
        self.events: list[dict] = []
        self.selected: int | None = None
        self.preview: dict | None = None
        self.audit: dict | None = None
        self.ready = False
        self.reading = False
        self.busy = False
        self.tab = "changes"
        self.message = ""
        self.watch: tuple[threading.Event, threading.Thread] | None = None
        self.work = ThreadPoolExecutor(max_workers=2)
        self.results = queue.SimpleQueue()
        self.closed = False

        window.title("文件改动记录")
        window.configure(background=PAPER)
        window.geometry("1040x680")
        window.minsize(780, 510)
        window.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 7))
        style.configure("Primary.TButton", background=TEAL, foreground=WHITE, borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#11746b"), ("disabled", "#80aaa6")],
                  foreground=[("disabled", WHITE)])
        style.configure("Changes.Treeview", rowheight=47, fieldbackground=PAPER,
                        background=PAPER, foreground=INK, borderwidth=0)
        style.map("Changes.Treeview", background=[("selected", "#dcebe7")],
                  foreground=[("selected", INK)])
        self.body = tk.Frame(window, background=PAPER)
        self.body.pack(fill="both", expand=True)
        self.render()
        self.window.after(50, self.deliver_results)
        self.window.after(1000, self.refresh)

    def label(self, parent, text, *, size=12, color=INK, bg=PAPER, bold=False, **options):
        return tk.Label(parent, text=text, background=bg, foreground=color, anchor="w", justify="left",
                        font=("TkDefaultFont", size, "bold" if bold else "normal"), **options)

    def button(self, parent, title, action, *, primary=False):
        return ttk.Button(parent, text=title, command=action,
                          style="Primary.TButton" if primary else "TButton",
                          state="disabled" if self.busy else "normal")

    def start_task(self, function, callback, on_error=None):
        future = self.work.submit(function)
        future.add_done_callback(lambda completed: self.results.put((completed, callback, on_error)))

    def deliver_results(self):
        while not self.closed:
            try:
                value, callback, on_error = self.results.get_nowait()
            except queue.Empty:
                break
            try:
                result = value.result() if isinstance(value, Future) else value
            except Exception as exc:
                if on_error:
                    on_error(exc)
                else:
                    self.show_error(exc)
            else:
                callback(result)
        if not self.closed:
            self.window.after(50, self.deliver_results)

    def show_error(self, exc):
        self.busy = False
        self.message = "操作未完成"
        self.render()
        messagebox.showerror("操作未完成", str(exc), parent=self.window)

    def reset(self, root: Path, state: Path, transcript: Path | None = None):
        self.stop()
        self.root, self.state, self.transcript = root, state, transcript
        self.events, self.selected, self.preview, self.audit = [], None, None, None
        self.ready = self.reading = False
        self.tab, self.message = "changes", ""

    def choose_folder(self):
        chosen = filedialog.askdirectory(title="选择要记录的文件夹", mustexist=True, parent=self.window)
        if chosen:
            root = Path(chosen).resolve()
            digest = hashlib.sha256(str(root).encode()).hexdigest()
            self.reset(root, self.storage / "folders" / digest)
            self.start()

    def demo(self):
        self.busy = True
        self.render()

        def show(result):
            self.reset(Path(result["root"]), Path(result["state"]), Path(result["transcript"]))
            self.events, self.ready, self.busy = result["events"], True, False
            self.render()
            if self.events:
                self.select(self.events[0]["id"])

        self.start_task(lambda: engine.demo(self.storage), show)

    def start(self):
        if self.root is None or self.state is None or self.watch is not None:
            return
        root, state = self.root, self.state
        stop = threading.Event()

        def finished(error):
            if self.watch and self.watch[0] is stop:
                self.watch = None
                if error:
                    self.show_error(error)
                else:
                    self.render()

        def watch_folder():
            error = None
            try:
                engine.record(root, state, once=False, stop_event=stop)
            except Exception as exc:
                error = exc
            self.results.put((error, finished, None))

        thread = threading.Thread(target=watch_folder, daemon=True)
        self.watch = stop, thread
        thread.start()
        self.render()

    def stop(self):
        if self.watch:
            stop, thread = self.watch
            self.watch = None
            stop.set()
            thread.join(timeout=0.25)

    def toggle_watch(self):
        if self.watch:
            self.stop()
            self.render()
        else:
            self.start()

    def refresh(self):
        if not self.closed and self.root and not self.transcript and not self.reading and not self.busy:
            root, state = self.root, self.state
            self.reading = True

            def update(result):
                if self.root == root:
                    self.reading = False
                    if self.events != result["events"] or self.ready != result["ready"]:
                        self.events, self.ready = result["events"], result["ready"]
                        self.render()

            def retry(_):
                if self.root == root:
                    self.reading = False

            self.start_task(lambda: engine.list_changes(root, state), update, retry)
        if not self.closed:
            self.window.after(1250, self.refresh)

    def select(self, event: int):
        if self.selected == event:
            return
        root, state = self.root, self.state
        self.selected, self.preview = event, None
        self.render_detail()

        def preview():
            with engine.journal_for(root, state, read_only=True) as journal:
                return engine.preview(root, journal, event)

        def show(result):
            if self.root == root and self.selected == event:
                self.preview = result
                self.render_detail()

        def unavailable(exc):
            if self.root == root and self.selected == event:
                self.preview = {"path": "预览", "message": str(exc)}
                self.render_detail()

        self.start_task(preview, show, unavailable)

    def restore(self, confirm=None):
        if not self.preview or not self.preview.get("token") or self.selected is None:
            return
        if confirm is None:
            confirm = lambda: messagebox.askokcancel("恢复到改动前？", "将使用左侧的旧版本替换当前文件。",
                                                    icon="warning", parent=self.window)
        if not confirm():
            return
        root, state, event, token = self.root, self.state, self.selected, self.preview["token"]
        self.busy = True
        self.render()

        def run():
            result = engine.restore(root, state, event, token)
            return result, engine.list_changes(root, state)

        def show(values):
            result, snapshot = values
            self.busy = False
            self.message = "已恢复 " + Path(result["path"]).name
            self.events, self.selected = snapshot["events"], None
            self.render()
            self.select(event)

        self.start_task(run, show)

    def run_audit(self):
        path = self.transcript
        if not path:
            chosen = filedialog.askopenfilename(title="选择 AI 对话日志", parent=self.window,
                                                filetypes=[("对话日志", "*.jsonl *.json *.txt"), ("所有文件", "*")])
            if not chosen:
                return
            path = Path(chosen)
        root, state = self.root, self.state
        self.busy = True
        self.render()

        def show(report):
            self.busy = False
            self.audit = report
            self.render()

        self.start_task(lambda: engine.audit(root, state, path), show)

    def switch_tab(self, tab):
        self.tab = tab
        self.render()

    def render(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.detail = None
        if not self.root:
            hero = tk.Frame(self.body, background=PAPER)
            hero.place(relx=0.5, rely=0.49, anchor="center")
            self.label(hero, "↶", size=39, color=TEAL, bold=True).pack(anchor="w")
            self.label(hero, "文件改动记录", size=31, bold=True).pack(anchor="w", pady=(14, 4))
            self.label(hero, "看看文件被改了什么，需要时恢复到改动前。", size=14, color=MUTED).pack(anchor="w")
            buttons = tk.Frame(hero, background=PAPER)
            buttons.pack(anchor="w", pady=(21, 10))
            self.button(buttons, "试用演示", self.demo, primary=True).pack(side="left", padx=(0, 8))
            self.button(buttons, "选择文件夹…", self.choose_folder).pack(side="left")
            self.label(hero, "准备演示…" if self.busy else "演示使用自带的样例文件。", color=MUTED).pack(anchor="w")
            return

        header = tk.Frame(self.body, background=PAPER)
        header.pack(fill="x", padx=24, pady=15)
        self.label(header, self.root.name, size=17, bold=True).grid(row=0, column=0, sticky="w")
        status = "样例 · 可放心操作" if self.transcript else "正在记录" if self.watch and self.ready else "正在建立基线" if self.watch else "已暂停"
        self.label(header, status, color=MUTED).grid(row=1, column=0, sticky="w")
        header.columnconfigure(0, weight=1)
        if not self.transcript:
            self.button(header, "暂停" if self.watch else "开始记录", self.toggle_watch).grid(row=0, column=1, padx=5)
        self.button(header, "换文件夹…", self.choose_folder).grid(row=0, column=2)
        tabs = tk.Frame(self.body, background=PAPER)
        tabs.pack(fill="x", padx=24, pady=(0, 12))
        for tab, label in (("changes", "文件改动"), ("audit", "核对 AI")):
            self.button(tabs, label, lambda value=tab: self.switch_tab(value), primary=self.tab == tab).pack(side="left", padx=(0, 7))
        self.label(tabs, self.message, color=TEAL).pack(side="right")
        if self.tab == "audit":
            self.render_audit()
            return

        content = tk.Frame(self.body, background=WHITE)
        content.pack(fill="both", expand=True)
        sidebar = tk.Frame(content, background=PAPER, width=235)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        if self.events:
            changes = ttk.Treeview(sidebar, show="tree", selectmode="browse", style="Changes.Treeview")
            changes.column("#0", width=233)
            changes.pack(fill="both", expand=True)
            for event in self.events:
                when = datetime.fromtimestamp(event["ts"]).strftime("%H:%M")
                action = "恢复" if event["isRestore"] else {"created": "新增", "deleted": "删除"}.get(event["event"], "修改")
                changes.insert("", "end", iid=str(event["id"]),
                               text=f'{Path(event["path"]).name}  ·  {action} {when}')
            if self.selected is not None and changes.exists(str(self.selected)):
                changes.selection_set(str(self.selected))
            changes.bind("<<TreeviewSelect>>", lambda _: changes.selection() and self.select(int(changes.selection()[0])))
        else:
            text = "正在读取文件夹…" if self.watch and not self.ready else "还没有记录到文件改动"
            self.label(sidebar, text, color=MUTED, wraplength=195).pack(anchor="nw", padx=18, pady=18)
        self.detail = tk.Frame(content, background=WHITE)
        self.detail.pack(side="left", fill="both", expand=True, padx=24, pady=21)
        self.render_detail()

    def render_detail(self):
        if self.detail is None:
            return
        for child in self.detail.winfo_children():
            child.destroy()
        if not self.preview:
            self.label(self.detail, "选一条改动查看内容" if self.selected is None else "正在读取预览…", bg=WHITE, color=MUTED).pack(anchor="w")
            return
        header = tk.Frame(self.detail, background=WHITE)
        header.pack(fill="x", pady=(0, 15))
        self.label(header, self.preview["path"], bg=WHITE, size=16, bold=True).pack(side="left")
        before, current = self.preview.get("before"), self.preview.get("current")
        if before is None:
            self.label(self.detail, self.preview.get("message", "暂无预览"), bg=WHITE, color=MUTED, wraplength=440).pack(anchor="w")
            return
        if before != current:
            self.button(header, "恢复到改动前…", self.restore, primary=True).pack(side="right")
        else:
            self.label(header, "当前已是此版本", bg=WHITE, color=TEAL).pack(side="right")
        cols = tk.Frame(self.detail, background=WHITE)
        cols.pack(fill="both", expand=True)
        cols.rowconfigure(0, weight=1)
        cols.columnconfigure((0, 1), weight=1, uniform="comparison")
        current = current if current is not None else "文件当前不存在"
        for i, (title, text, other, color) in enumerate((("改动前", before, current, "#e3eeeb"), ("当前文件", current, before, "#f9eadc"))):
            panel = tk.Frame(cols, background=PAPER, highlightbackground="#dce1d9", highlightthickness=1)
            panel.grid(row=0, column=i, sticky="nsew", padx=(0, 8) if i == 0 else (8, 0))
            self.label(panel, title, bold=True).pack(anchor="w", padx=11, pady=10)
            field = tk.Text(panel, width=1, height=1, wrap="word", borderwidth=0, padx=11, pady=6,
                            background=PAPER, foreground=INK, font=("TkFixedFont", 12))
            scroll = ttk.Scrollbar(panel, command=field.yview)
            field.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            field.pack(side="left", fill="both", expand=True)
            field.tag_configure("changed", background=color)
            compare = other.split("\n")
            for j, row in enumerate(text.split("\n")):
                field.insert("end", f"{j+1:>3}  {row}\n", "changed" if j >= len(compare) or row != compare[j] else ())
            field.configure(state="disabled")

    def render_audit(self):
        view = tk.Frame(self.body, background=WHITE)
        view.pack(fill="both", expand=True, ipadx=24, ipady=20)
        self.label(view, "核对 AI 的说法", size=17, bg=WHITE, bold=True).pack(anchor="w", padx=25, pady=(23, 8))
        self.label(view, "样例里的 AI 说它删了旅行计划。" if self.transcript else "选择本地对话日志，核对 AI 提到的文件改动。", bg=WHITE, color=MUTED).pack(anchor="w", padx=25)
        self.button(view, "核对演示" if self.transcript else "选择对话日志…", self.run_audit, primary=True).pack(anchor="w", padx=25, pady=14)
        if not self.audit:
            return
        field = tk.Text(view, wrap="word", borderwidth=0, padx=12, pady=12, background=PAPER, foreground=INK)
        field.pack(fill="both", expand=True, padx=25, pady=(0, 20))
        if not self.audit["claims"]:
            field.insert("end", "这份日志中没有读到可核对的声明。\n")
        for claim in self.audit["claims"]:
            label = {"ok": "已核对", "failed": "存在矛盾"}.get(claim["status"], "证据不足")
            field.insert("end", f'{label} · {claim["subject"]}\n{claim["detail"]}\n\n')
        for finding in self.audit["findings"]:
            field.insert("end", f'{finding["title"]}\n{finding["detail"]}\n\n')
        for warning in self.audit["warnings"]:
            field.insert("end", warning + "\n")
        field.configure(state="disabled")

    def close(self):
        self.closed = True
        self.stop()
        self.work.shutdown(wait=False, cancel_futures=True)
        self.window.destroy()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        from smoke import verify_portable

        verify_portable(Desk, Path(sys.argv[2]))
    else:
        root = tk.Tk()
        Desk(root)
        root.mainloop()

"""Acceptance checks for the packaged runtime and the portable user flow."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path


def verify_engine(engine: Path) -> None:
    def request(*args):
        return json.loads(subprocess.check_output([str(engine), *map(str, args)], encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="file-change-qa-") as folder:
        sample = request("demo", folder)
        root, state, transcript = (sample[key] for key in ("root", "state", "transcript"))
        assert sample["ready"] and len(sample["events"]) == 1, sample
        action = (root, "--state", state, "--event", sample["events"][0]["id"])
        preview = request("preview", *action)
        assert "200 元" in preview["before"] and "950 元" in preview["current"], preview
        audit = request("audit", root, "--state", state, "--transcript", transcript)
        assert audit["claims"][0]["status"] == "failed" and len(audit["findings"]) == 1, audit
        request("restore", *action, "--token", preview["token"])
        assert (Path(root) / preview["path"]).read_bytes() == preview["before"].encode("utf-8")
    print("Packaged change, audit, and restore checks passed.", flush=True)


def verify_portable(desk_type, result_path: Path) -> None:
    import tkinter as tk
    from tkinter import ttk

    def find_button(parent, title):
        for child in parent.winfo_children():
            if isinstance(child, ttk.Button) and child.cget("text") == title:
                return child
            result = find_button(child, title)
            if result is not None:
                return result
        return None

    def wait_for(window, condition):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            window.update()
            if condition():
                return
            time.sleep(0.03)
        raise AssertionError("The desktop action did not complete")

    def click(app, title):
        button = find_button(app.body, title)
        assert button is not None, title
        button.invoke()

    try:
        with tempfile.TemporaryDirectory(prefix="file-change-ui-") as folder:
            working = Path(folder).resolve()
            window = tk.Tk()
            app = desk_type(window, working / "history")
            try:
                wait_for(window, window.winfo_viewable)
                assert app.icon.width() == 64 and app.icon.height() == 64
                click(app, "试用演示")
                wait_for(window, lambda: app.preview is not None)
                assert "200 元" in app.preview["before"] and "950 元" in app.preview["current"], app.preview
                click(app, "核对 AI")
                click(app, "核对演示")
                wait_for(window, lambda: app.audit is not None)
                assert app.audit["claims"][0]["status"] == "failed" and len(app.audit["findings"]) == 1, app.audit
                click(app, "文件改动")
                assert find_button(app.body, "恢复到改动前…") is not None
                app.restore(confirm=lambda: False)
                assert "950 元" in (app.root / app.preview["path"]).read_text(encoding="utf-8")
                app.restore(confirm=lambda: True)
                wait_for(window, lambda: app.preview and app.message.startswith("已恢复"))
                assert app.preview["before"] == app.preview["current"], app.preview
                project = working / "Own folder"
                project.mkdir()
                file = project / "notes.txt"
                file.write_bytes(b"first version\n")
                app.reset(project, working / "own-history")
                app.start()
                wait_for(window, lambda: app.ready)
                file.write_bytes(b"second version\n")
                wait_for(window, lambda: len(app.events) == 1)
                app.select(app.events[0]["id"])
                wait_for(window, lambda: app.preview is not None)
                assert app.preview["before"] == "first version\n", app.preview
                assert app.preview["current"] == "second version\n", app.preview
                click(app, "暂停")
                assert app.watch is None and not (project / ".whodunit").exists()
            finally:
                app.close()
        result_path.write_text("PASS: window, demo, audit, restore, real folder, pause.\n", encoding="utf-8")
    except Exception:
        result_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    verify_engine(Path(sys.argv[1]).resolve())

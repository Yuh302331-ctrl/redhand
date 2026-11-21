"""Robustness tests: specials/symlinks, empty files, cap safety, config
protection, corrupt blobs, nofollow writes, and a real cross-process
restore-while-watch integration."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from redhand.cli import main
from redhand.journal import Journal
from redhand.restore import RestoreError, restore_event
from redhand.scanner import scan_tree
from helpers import seed_file, write


# ---------------------------------------------------------------- specials


def test_fifo_recorded_but_never_opened(tmp_path):
    root = str(tmp_path / "r")
    os.makedirs(root)
    os.mkfifo(os.path.join(root, "pipe"))
    entries = scan_tree(root)
    assert "pipe" in entries and entries["pipe"].special


def test_watch_once_survives_fifo(tmp_path):
    """A FIFO in the tree must not hang the watcher (open() would block forever)."""
    root = str(tmp_path / "r")
    os.makedirs(root)
    os.mkfifo(os.path.join(root, "pipe"))
    write(os.path.join(root, "a.txt"), "ok")
    assert main(["--root", root, "watch", "--once", "--interval", "0.05"]) == 0


def test_symlink_to_huge_target_never_read(tmp_path):
    root = str(tmp_path / "r")
    os.makedirs(root)
    target = tmp_path / "huge.bin"
    target.write_bytes(b"Z" * (2 * 1024 * 1024))  # 2 MB > default blob limit in tests
    os.symlink(str(target), os.path.join(root, "link.txt"))

    from redhand.config import Config
    from redhand.watch import Watcher
    from redhand.util import Palette

    cfg = Config()
    cfg.blob_limit = 64 * 1024
    jj = Journal(root)
    try:
        w = Watcher(root, cfg, jj, palette=Palette(False), once=True, interval=0.01)
        w.run()
        # recorded as an entry, but the target's content was never slurped:
        row = jj.shadow_row("link.txt")
        assert row is not None and row["blob_id"] is None
    finally:
        jj.close()


# ---------------------------------------------------------------- empty files


def test_empty_file_undo_chain(tmp_path):
    root = str(tmp_path / "r")
    os.makedirs(root)
    p = os.path.join(root, "e.txt")
    jj = Journal(root)
    try:
        ev1 = seed_file(jj, root, "e.txt", "")  # created empty
        assert jj.get_blob(jj.shadow_row("e.txt")["blob_id"]) == b""
        write(p, "now full")
        row = jj.shadow_row("e.txt")
        ev2 = jj.record_change(
            ts=time.time(), run_id=None, path="e.txt", event_type="modified",
            actor="manual", confidence="low", old_size=0, new_size=8,
            old_hash=None, new_hash=None, mtime_ns=2,
            pre_blob_id=row["blob_id"], new_content=b"now full",
            blob_cap_total=1024 * 1024,
        )
        # undo the modification: the pre-state was the EMPTY file
        out = restore_event(jj, root, ev2)
        assert out.action == "restored-content"
        assert open(p, "rb").read() == b""
        # undo the creation: the file did not exist before it
        out2 = restore_event(jj, root, ev1)
        assert out2.action == "removed"
        assert not os.path.exists(p)
    finally:
        jj.close()


# ---------------------------------------------------------------- cap safety


def test_failed_store_does_not_destroy_undo_targets(tmp_path):
    """Evict-then-bail is forbidden: a refused store must not evict history."""
    root = str(tmp_path / "r")
    os.makedirs(root)
    jj = Journal(root)
    try:
        seed_file(jj, root, "f.txt", "precious")
        # a modification whose undo target is the pre-change content blob
        row = jj.shadow_row("f.txt")
        ev = jj.record_change(
            ts=time.time(), run_id=None, path="f.txt", event_type="modified",
            actor="manual", confidence="low", old_size=8, new_size=5,
            old_hash=None, new_hash=None, mtime_ns=2,
            pre_blob_id=row["blob_id"], new_content=b"after",
            blob_cap_total=1024 * 1024,
        )
        undo_blob = jj.event_by_id(ev)["blob_id"]
        assert undo_blob is not None
        # refuse an impossible store under a tiny cap
        assert jj.store_blob(b"q" * 100_000, 50_000) is None
        # the undo target survived, and the restore still works
        assert jj.get_blob(undo_blob) == b"precious"
        assert restore_event(jj, root, ev).action == "restored-content"
    finally:
        jj.close()


def test_blob_dedup_identical_content(tmp_path):
    jj = Journal(tmp_path)
    try:
        a = jj.store_blob(b"same-bytes", 1024 * 1024)
        b = jj.store_blob(b"same-bytes", 1024 * 1024)
        assert a == b
        assert jj.blob_count() == 1
    finally:
        jj.close()


# ---------------------------------------------------------------- config protection


def test_protect_never_clobbers_broken_config(tmp_path, capsys):
    root = str(tmp_path / "r")
    os.makedirs(os.path.join(root, ".whodunit"))
    cfg_path = os.path.join(root, ".whodunit", "config.json")
    original = '{"agents": ["my-custom-agent"], "broken'
    with open(cfg_path, "w") as f:
        f.write(original)
    rc = main(["--root", root, "protect", "add", "secrets/"])
    assert rc == 1
    assert "config error" in capsys.readouterr().err  # load_config rejects it up front
    with open(cfg_path) as f:
        assert f.read() == original  # untouched


# ---------------------------------------------------------------- restore hardening


def test_restore_replaces_planted_symlink(tmp_path):
    """A symlink planted over the path must not redirect restore content."""
    root = str(tmp_path / "r")
    os.makedirs(root)
    victim = tmp_path / "victim.txt"
    victim.write_text("victim")

    jj = Journal(root)
    try:
        seed_file(jj, root, "f.txt", "before")
        write(os.path.join(root, "f.txt"), "after")
        row = jj.shadow_row("f.txt")
        ev2 = jj.record_change(
            ts=time.time(), run_id=None, path="f.txt", event_type="modified",
            actor="manual", confidence="low", old_size=6, new_size=5,
            old_hash=None, new_hash=None, mtime_ns=2,
            pre_blob_id=row["blob_id"], new_content=b"after",
            blob_cap_total=1024 * 1024,
        )
        # plant a symlink at the path pointing outside the root
        os.remove(os.path.join(root, "f.txt"))
        os.symlink(str(victim), os.path.join(root, "f.txt"))

        restore_event(jj, root, ev2)
        assert victim.read_text() == "victim"  # target untouched
        p = os.path.join(root, "f.txt")
        assert not os.path.islink(p) and os.path.isfile(p)  # link replaced
        assert open(p).read() == "before"
    finally:
        jj.close()


def test_corrupt_blob_gives_clean_error(tmp_path):
    root = str(tmp_path / "r")
    os.makedirs(root)
    jj = Journal(root)
    try:
        seed_file(jj, root, "f.txt", "data")
        row = jj.shadow_row("f.txt")
        ev = jj.record_change(
            ts=time.time(), run_id=None, path="f.txt", event_type="modified",
            actor="manual", confidence="low", old_size=4, new_size=5,
            old_hash=None, new_hash=None, mtime_ns=2,
            pre_blob_id=row["blob_id"], new_content=b"after",
            blob_cap_total=1024 * 1024,
        )
        blob_id = jj.event_by_id(ev)["blob_id"]  # the pre-change content
        jj.conn.execute("UPDATE blobs SET content=X'00' WHERE id=?", (blob_id,))
        jj.conn.commit()
        with pytest.raises(RestoreError) as e:
            restore_event(jj, root, ev)
        assert "corrupted" in str(e.value)
    finally:
        jj.close()


def test_interval_clamped(tmp_path):
    from redhand.config import Config
    from redhand.watch import Watcher
    from redhand.util import Palette

    w = Watcher(str(tmp_path), Config(), Journal(str(tmp_path)), palette=Palette(False), interval=0)
    assert w.interval >= 0.1


# ---------------------------------------------------------------- integration


def test_restore_while_watch_running(tmp_path):
    """Cross-process: live watcher + CLI restore on the same DB must not
    crash, corrupt, or double-record."""
    root = str(tmp_path / "r")
    os.makedirs(root)
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "redhand", "watch", "--interval", "0.3"],
        cwd=root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(1.2)  # seed
        p = os.path.join(root, "f.txt")
        write(p, "v1")
        time.sleep(1.0)  # created event
        write(p, "v2")
        time.sleep(1.0)  # modified event

        # restore from a second process while the watcher is live
        rc = main(["--root", root, "restore", "--path", "f.txt"])
        assert rc == 0
        assert open(p).read() == "v1"
        time.sleep(1.2)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
    assert b"Traceback" not in err
    jj = Journal(root)
    try:
        events = [e for e in jj.events() if e["path"] == "f.txt"]
        kinds = [e["event"] for e in events]
        assert "created" in kinds and "modified" in kinds
        # content history stays coherent: the restore is recorded exactly once
        assert kinds.count("modified") == 2  # v1->v2 by manual, v2->v1 by redhand
    finally:
        jj.close()

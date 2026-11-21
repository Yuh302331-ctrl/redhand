"""Robustness tests: specials/symlinks, empty files, cap safety, config
protection, corrupt blobs, nofollow writes, and a real cross-process
restore-while-watch integration."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time

import pytest

from redhand.cli import main
from redhand.config import load_config
from redhand.journal import Journal
from redhand.restore import RestoreError, restore_event
from redhand.scanner import scan_tree
from redhand.util import Palette, PatternSet, safe_hash
from helpers import seed_file, write


def make_watcher(jroot, journal, cfg, **kw):
    from redhand.watch import Watcher

    return Watcher(jroot, cfg, journal, palette=Palette(enabled=False), out=io.StringIO(), **kw)


# ---------------------------------------------------------------- specials


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only (mkfifo/symlink)')
def test_fifo_recorded_but_never_opened(tmp_path):
    root = str(tmp_path / "r")
    os.makedirs(root)
    os.mkfifo(os.path.join(root, "pipe"))
    entries = scan_tree(root)
    assert "pipe" in entries and entries["pipe"].special


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only (mkfifo/symlink)')
def test_watch_once_survives_fifo(tmp_path):
    """A FIFO in the tree must not hang the watcher (open() would block forever)."""
    root = str(tmp_path / "r")
    os.makedirs(root)
    os.mkfifo(os.path.join(root, "pipe"))
    write(os.path.join(root, "a.txt"), "ok")
    assert main(["--root", root, "watch", "--once", "--interval", "0.05"]) == 0


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only (mkfifo/symlink)')
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


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only (mkfifo/symlink)')
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


# ---------------------------------------------------------------- round-2 fixes


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only (mkfifo/symlink)')
def test_symlink_to_fifo_never_blocks(tmp_path):
    """symlink -> FIFO: hashing must refuse to open the target (would block)."""
    root = str(tmp_path / "r")
    os.makedirs(root)
    os.mkfifo(os.path.join(root, "pipe"))
    os.symlink("pipe", os.path.join(root, "alias"))  # relative symlink to the FIFO
    # direct helper semantics
    assert safe_hash(os.path.join(root, "alias"), 1024 * 1024) is None
    assert safe_hash(os.path.join(root, "pipe"), 1024 * 1024) is None
    # and the whole watch cycle completes without hanging
    assert main(["--root", root, "watch", "--once", "--interval", "0.05"]) == 0


def test_transient_hash_failure_skips_then_records(tmp_path, monkeypatch):
    """A failing hash read must not downgrade the file or emit a phantom event."""
    import redhand.scanner as S

    root = str(tmp_path / "r")
    os.makedirs(root)
    write(os.path.join(root, "a.txt"), "v1")
    cfg = load_config(root)
    jj = Journal(root)
    try:
        make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01).run()
        assert len(jj.events()) == 0  # seeded silently
        write(os.path.join(root, "a.txt"), "v2")
        # next cycle: hash read fails transiently
        orig = S.safe_hash
        monkeypatch.setattr(S, "safe_hash", lambda p, lim: None if p.endswith("a.txt") else orig(p, lim))
        w = make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01)
        summary = w.run()
        assert summary.events == 0  # nothing recorded this cycle
        assert jj.shadow_row("a.txt")["sha256"] is not None  # not downgraded
        monkeypatch.undo()
        # following cycle: hash works again -> real modification recorded
        w2 = make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01)
        summary2 = w2.run()
        assert summary2.events == 1
        rows = [e for e in jj.events() if e["path"] == "a.txt"]
        assert rows[0]["event"] == "modified"
    finally:
        jj.close()


def test_enforce_survives_corrupt_blob(tmp_path):
    """A corrupted undo blob during --enforce must not kill the watcher."""
    root = str(tmp_path / "r")
    os.makedirs(root)
    write(os.path.join(root, ".env"), "KEEP=1")
    cfg = load_config(root)
    cfg.protect = PatternSet([".env"])
    jj = Journal(root)
    try:
        make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01).run()
    finally:
        jj.close()
    write(os.path.join(root, ".env"), "LEAK=1")
    jj = Journal(root)
    try:
        # corrupt every blob: the pre-change content is unreadable
        jj.conn.execute("UPDATE blobs SET content=X'00'")
        jj.conn.commit()
        w = make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, enforce=True, interval=0.01)
        w.run()  # must not raise
        assert open(os.path.join(root, ".env")).read() == "LEAK=1"  # revert impossible, file intact
    finally:
        jj.close()


def test_actor_kinds_and_restore_run(tmp_path, monkeypatch):
    import time as _t

    import redhand.watch as W

    # no agents running -> manual attribution, through the REAL record path
    monkeypatch.setattr(W._actors, "agents_running", lambda sigs, exclude_pid=None: [])

    root = str(tmp_path / "r")
    os.makedirs(root)
    write(os.path.join(root, "f.txt"), "one")
    cfg = load_config(root)
    jj = Journal(root)
    try:
        make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01).run()
        write(os.path.join(root, "h.txt"), "by hand")
        make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01).run()
        kinds = {a["name"]: a["kind"] for a in jj.actors()}
        assert kinds["manual"] == "human"  # no kind override at the call site
        write(os.path.join(root, "f.txt"), "two")
        # simulate an agent working here: codex records the modification
        row = jj.shadow_row("f.txt")
        ev = jj.record_change(
            ts=_t.time(), run_id=None, path="f.txt", event_type="modified",
            actor="codex", confidence="high", old_size=4, new_size=4,
            old_hash=None, new_hash=None, mtime_ns=2,
            pre_blob_id=row["blob_id"], new_content=b"two",
            blob_cap_total=1024 * 1024,
        )
        jj.get_or_create_actor("codex")  # no override: default kind applies
        restore_event(jj, root, ev, run_id=None)  # registers 'redhand' itself
        kinds = {a["name"]: a["kind"] for a in jj.actors()}
        assert kinds["codex"] == "agent" and kinds["redhand"] == "tool"
        # created-restore metadata: records the CURRENT state, not creation-time
        write(os.path.join(root, "grew.txt"), "12345678")
        ev_grow = jj.record_change(
            ts=_t.time(), run_id=None, path="grew.txt", event_type="created",
            actor="manual", confidence="low", old_size=None, new_size=3,
            old_hash=None, new_hash=None, mtime_ns=3,
            pre_blob_id=None, new_content=b"123", blob_cap_total=1024 * 1024,
        )
        write(os.path.join(root, "grew.txt"), "1234567890")  # grew outside the journal
        out2 = restore_event(jj, root, ev_grow)
        deleted = jj.event_by_id(out2.new_event_id)
        assert deleted["old_size"] == 10  # current size, not the stale 3
    finally:
        jj.close()


def test_restore_creates_run_row(tmp_path, capsys):
    root = str(tmp_path / "r")
    os.makedirs(root)
    write(os.path.join(root, "f.txt"), "one")
    jj = Journal(root)
    seed_file(jj, root, "f.txt", "one")
    write(os.path.join(root, "f.txt"), "two")
    row = jj.shadow_row("f.txt")
    jj.record_change(
        ts=time.time(), run_id=None, path="f.txt", event_type="modified",
        actor="manual", confidence="low", old_size=4, new_size=4,
        old_hash=None, new_hash=None, mtime_ns=2,
        pre_blob_id=row["blob_id"], new_content=b"two",
        blob_cap_total=1024 * 1024,
    )
    jj.close()
    assert main(["--root", root, "restore", "--path", "f.txt"]) == 0
    jj = Journal(root)
    try:
        run = jj.last_run()
        assert run["note"] == "restore" and run["ended_at"] is not None
    finally:
        jj.close()


def test_file_growing_beyond_hash_limit_is_journaled(tmp_path):
    """A file that grows beyond hash_limit after seeding must produce an
    event (size-only from then on) — never a silent skip (P1 regression)."""
    from redhand.config import load_config
    from redhand.journal import Journal

    root = str(tmp_path / "r")
    os.makedirs(root)
    write(os.path.join(root, "grow.bin"), "tiny")
    cfg = load_config(root)
    cfg.hash_limit = 1024  # tiny limit for the test
    jj = Journal(root)
    try:
        make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01).run()
        assert jj.shadow_row("grow.bin")["sha256"] is not None  # seeded small
        # grow far beyond the limit
        write(os.path.join(root, "grow.bin"), "X" * 4096)
        w = make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01)
        summary = w.run()
        assert summary.events == 1  # NOT silently skipped
        rows = [e for e in jj.events() if e["path"] == "grow.bin"]
        assert rows[0]["event"] == "modified"
        assert rows[0]["new_size"] == 4096 and rows[0]["new_hash"] is None
        # shadow switched to size-only tracking (sha NULL) — and the next
        # cycle stays quiet instead of re-detecting forever
        row = jj.shadow_row("grow.bin")
        assert row["sha256"] is None and row["size"] == 4096
        w2 = make_watcher(jroot=root, journal=jj, cfg=cfg, once=True, interval=0.01)
        summary2 = w2.run()
        assert summary2.events == 0
    finally:
        jj.close()


def test_write_file_nofollow_refuses_fifo(tmp_path):
    """Restore/enforce writes must fail cleanly on a planted FIFO, not hang."""
    import errno as _errno

    from redhand.util import write_file_nofollow

    root = str(tmp_path / "r")
    os.makedirs(root)
    pipe = os.path.join(root, "p")
    os.mkfifo(pipe)
    with pytest.raises(OSError) as e:
        write_file_nofollow(pipe, b"data")
    assert e.value.errno == _errno.EINVAL

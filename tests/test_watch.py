"""Watch-engine integration tests: seeding, diff passes, attribution, protect, lock."""

from __future__ import annotations

import io
import os

import pytest

import redhand.watch as W
from redhand import actors as A
from redhand.journal import Journal
from redhand.watch import WatchError, Watcher
from helpers import write


@pytest.fixture()
def jroot(tmp_path):
    d = tmp_path / "wroot"
    d.mkdir()
    return str(d)


def make_watcher(jroot, journal, cfg, **kw):
    import io

    from redhand.util import Palette

    return Watcher(jroot, cfg, journal, palette=Palette(enabled=False), out=io.StringIO(), **kw)


def fake_agents(monkeypatch, agents):
    monkeypatch.setattr(W._actors, "agents_running", lambda sigs, exclude_pid=None: agents)


def test_first_run_seeds_then_catches_changes(jroot, monkeypatch):
    write(os.path.join(jroot, "seed.txt"), "seed")
    from redhand.config import load_config

    cfg = load_config(jroot)
    jj = Journal(jroot)
    fake_agents(monkeypatch, [])
    try:
        w = make_watcher(jroot, jj, cfg, once=True, interval=0.01)
        summary = w.run()
        assert jj.shadow_count()["files"] == 1
        assert len(jj.events()) == 0  # seeding records no events
        assert summary.events == 0
    finally:
        jj.close()

    # change something, run again -> created event
    write(os.path.join(jroot, "new.txt"), "fresh")
    jj = Journal(jroot)
    try:
        w = make_watcher(jroot, jj, cfg, once=True, interval=0.01)
        summary = w.run()
        assert summary.events == 1
        rows = jj.events()
        assert rows[0]["event"] == "created" and rows[0]["path"] == "new.txt"
        assert rows[0]["actor"] == "manual"
        # created events carry no blob themselves; content lives in the shadow
        assert rows[0]["blob_id"] is None
        shadow_blob = jj.shadow_row("new.txt")["blob_id"]
        assert shadow_blob is not None and jj.get_blob(shadow_blob) == b"fresh"
    finally:
        jj.close()


def test_attribution_high_with_agent_cwd(jroot, monkeypatch):
    from redhand.config import load_config

    cfg = load_config(jroot)
    write(os.path.join(jroot, "a.txt"), "1")
    jj = Journal(jroot)
    try:
        w = make_watcher(jroot, jj, cfg, once=True, interval=0.01)
        w.run()  # seed
    finally:
        jj.close()

    agent = A.AgentProc(pid=12345, name="claude", comm="claude", args="claude", cwd=os.path.abspath(jroot))
    fake_agents(monkeypatch, [agent])
    write(os.path.join(jroot, "a.txt"), "2")
    jj = Journal(jroot)
    try:
        w = make_watcher(jroot, jj, cfg, once=True, interval=0.01)
        summary = w.run()
        rows = jj.events()
        assert len(rows) == 1 and rows[0]["event"] == "modified"
        assert rows[0]["actor"] == "claude" and rows[0]["confidence"] == "high"
        assert summary.by_actor == {"claude": 1}
    finally:
        jj.close()


def test_attribution_unverified_and_manual(jroot, monkeypatch):
    from redhand.config import load_config

    cfg = load_config(jroot)
    fake_agents(monkeypatch, [])  # no agents at all
    write(os.path.join(jroot, "a.txt"), "1")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()
    finally:
        jj.close()

    write(os.path.join(jroot, "b.txt"), "2")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()
        assert jj.events()[0]["actor"] == "manual"
    finally:
        jj.close()

    # agent running somewhere unrelated -> unverified-agent
    fake_agents(monkeypatch, [A.AgentProc(pid=1, name="codex", comm="codex", args="", cwd="/elsewhere")])
    write(os.path.join(jroot, "c.txt"), "3")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()
        row = [e for e in jj.events() if e["path"] == "c.txt"][0]
        assert row["actor"] == "unverified-agent" and row["confidence"] == "medium"
    finally:
        jj.close()


def test_deletion_recorded_and_restorable(jroot, monkeypatch):
    from redhand.config import load_config

    cfg = load_config(jroot)
    fake_agents(monkeypatch, [])
    write(os.path.join(jroot, "d.txt"), "data")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()
    finally:
        jj.close()

    os.remove(os.path.join(jroot, "d.txt"))
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()
        row = jj.events()[0]
        assert row["event"] == "deleted" and row["blob_id"] is not None
        assert jj.get_blob(row["blob_id"]) == b"data"
        from redhand.restore import restore_event

        out = restore_event(jj, jroot, row["id"])
        assert out.action == "recreated"
        with open(os.path.join(jroot, "d.txt"), "rb") as f:
            assert f.read() == b"data"
    finally:
        jj.close()


def test_protect_enforce_reverts_created(jroot, monkeypatch):
    from redhand.config import load_config
    from redhand.util import PatternSet

    cfg = load_config(jroot)
    cfg.protect = PatternSet(["secrets/*"])
    fake_agents(monkeypatch, [])
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()  # seed
        # in the same watcher instance, simulate a protected creation
        write(os.path.join(jroot, "secrets", "key.txt"), "leaked")
        w = make_watcher(jroot, jj, cfg, once=True, enforce=True, interval=0.01)
        w.run()
        # key.txt removed again by enforcement
        assert not os.path.exists(os.path.join(jroot, "secrets/key.txt"))
        # two events: the protected creation + the revert (deleted, actor redhand)
        rows = [e for e in jj.events() if e["path"] == "secrets/key.txt"]
        assert [r["event"] for r in rows] == ["created", "deleted"]
        assert rows[1]["actor"] == "redhand" and rows[1]["prev_event"] == rows[0]["id"]
        assert rows[0]["restored"] == 1  # original protected event marked reverted
    finally:
        jj.close()


def test_protect_enforce_reverts_modified(jroot, monkeypatch):
    from redhand.config import load_config
    from redhand.util import PatternSet

    cfg = load_config(jroot)
    cfg.protect = PatternSet([".env"])
    fake_agents(monkeypatch, [])
    write(os.path.join(jroot, ".env"), "TOKEN=keep\n")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()  # seed
    finally:
        jj.close()

    write(os.path.join(jroot, ".env"), "TOKEN=leaked\n")
    jj = Journal(jroot)
    try:
        w = make_watcher(jroot, jj, cfg, once=True, enforce=True, interval=0.01)
        w.run()
        with open(os.path.join(jroot, ".env"), "r", encoding="utf-8") as f:
            assert f.read() == "TOKEN=keep\n"
        # seeding records no events; leak + revert are both 'modified'
        rows = [e for e in jj.events() if e["path"] == ".env"]
        assert [r["event"] for r in rows] == ["modified", "modified"]
        assert rows[1]["actor"] == "redhand"
        assert rows[1]["prev_event"] == rows[0]["id"]
        assert rows[0]["restored"] == 1
    finally:
        jj.close()


def test_protect_enforce_recreates_deleted(jroot, monkeypatch):
    from redhand.config import load_config
    from redhand.util import PatternSet

    cfg = load_config(jroot)
    cfg.protect = PatternSet([".env"])
    fake_agents(monkeypatch, [])
    write(os.path.join(jroot, ".env"), "TOKEN=keep\n")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()  # seed
    finally:
        jj.close()

    os.remove(os.path.join(jroot, ".env"))
    jj = Journal(jroot)
    try:
        w = make_watcher(jroot, jj, cfg, once=True, enforce=True, interval=0.01)
        w.run()
        assert os.path.exists(os.path.join(jroot, ".env"))
        with open(os.path.join(jroot, ".env"), "r", encoding="utf-8") as f:
            assert f.read() == "TOKEN=keep\n"
        rows = [e for e in jj.events() if e["path"] == ".env"]
        assert [r["event"] for r in rows] == ["deleted", "created"]
        assert rows[1]["actor"] == "redhand"
        assert rows[1]["prev_event"] == rows[0]["id"]
    finally:
        jj.close()


def test_watch_lock_single_instance(jroot):
    fcntl = pytest.importorskip("fcntl")  # POSIX-only locking
    from redhand.config import load_config

    cfg = load_config(jroot)
    jj = Journal(jroot)
    try:
        w = make_watcher(jroot, jj, cfg, once=True, interval=0.01)
        # pre-hold the lock ourselves
        lock_path = os.path.join(jroot, ".whodunit", "watch.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WatchError):
            w.run()
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        # now it works
        w.run()
    finally:
        jj.close()


def test_summary_counts(jroot, monkeypatch):
    from redhand.config import load_config

    cfg = load_config(jroot)
    fake_agents(monkeypatch, [])
    write(os.path.join(jroot, "x.txt"), "1")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()
    finally:
        jj.close()

    # pass 1: modify; pass 2: delete (a single poll would only see the final state)
    write(os.path.join(jroot, "x.txt"), "22")
    jj = Journal(jroot)
    try:
        make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()
    finally:
        jj.close()
    os.remove(os.path.join(jroot, "x.txt"))
    jj = Journal(jroot)
    try:
        w = make_watcher(jroot, jj, cfg, once=True, interval=0.01)
        summary = w.run()
        assert summary.events == 1
        assert summary.by_actor == {"manual": 1}
        kinds = [e["event"] for e in jj.events(order="DESC")]
        assert kinds[0] == "deleted" and "modified" in kinds
    finally:
        jj.close()


# the mid-scan dedup race now lives in tests/test_race_dedup.py


# ---------------------------------------------------------------- broad-watch banner


def test_broad_watch_reason(tmp_path):
    from redhand.watch import Watcher
    from redhand.config import Config
    from redhand.journal import Journal
    from redhand.util import Palette
    import io

    jj = Journal(str(tmp_path))  # lives elsewhere; the root below is only lexical
    try:
        deep = os.path.join(os.getcwd(), "a", "b")  # >= 3 path parts
        for path, expect in ((deep, False), ("/", True),
                             (os.path.expanduser("~"), True)):
            w = Watcher(path, Config(), jj, palette=Palette(False), out=io.StringIO())
            assert bool(w._broad_watch_reason()) is expect, path
    finally:
        jj.close()


def test_broad_banner_printed(tmp_path, monkeypatch):
    from redhand.config import load_config
    from redhand.journal import Journal
    from redhand.watch import Watcher
    from redhand.util import Palette

    cfg = load_config(str(tmp_path))
    jj = Journal(str(tmp_path))
    try:
        w = Watcher(str(tmp_path), cfg, jj, palette=Palette(False),
                    out=io.StringIO(), once=True, interval=0.01)
        monkeypatch.setattr(w, "_broad_watch_reason",
                            lambda: "the watched root contains your home directory")
        out = w.out
        w.run()
        assert "broad watch" in out.getvalue()
        assert "attribution can over-credit" in out.getvalue()
    finally:
        jj.close()


def test_seed_progress_printed_for_many_files(tmp_path):
    from redhand.config import load_config
    from redhand.journal import Journal
    from redhand.watch import Watcher
    from redhand.util import Palette

    root = str(tmp_path / "big")
    os.makedirs(root)
    for i in range(2100):
        write(os.path.join(root, f"f{i:05d}.txt"), "x")
    cfg = load_config(root)
    jj = Journal(root)
    try:
        out = io.StringIO()
        w = Watcher(root, cfg, jj, palette=Palette(False), out=out,
                    once=True, interval=0.01)
        w.run()
        text = out.getvalue()
        assert "2100 files found" in text
        assert "2000/2100 files" in text  # progress lines land on multiples of 500
    finally:
        jj.close()

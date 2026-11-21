"""Deterministic replay of the mid-scan race between a watcher and a
concurrent redhand process (restore/enforce from another terminal).

The other process records the change (and moves the shadow) BETWEEN our
shadow_all() snapshot and our record step. The fresh-shadow guard
(_already_recorded) must be what suppresses the duplicate: this test
self-verifies by also running the race with the guard disabled and
asserting the duplicate IS recorded in that case."""

from __future__ import annotations

import os

import pytest

import redhand.watch as W
from redhand.journal import Journal
from redhand.util import sha256_bytes
from helpers import write


@pytest.fixture
def jroot(tmp_path):
    d = tmp_path / "raceroot"
    d.mkdir()
    return str(d)


def _make_watcher(jroot, journal, cfg, **kw):
    import io

    from redhand.util import Palette
    from redhand.watch import Watcher

    return Watcher(jroot, cfg, journal, palette=Palette(enabled=False),
                   out=io.StringIO(), **kw)


def test_midscan_race_guard_suppresses_duplicate(jroot, monkeypatch):
    from redhand.config import load_config

    cfg = load_config(jroot)
    monkeypatch.setattr(W._actors, "agents_running", lambda sigs, exclude_pid=None: [])
    write(os.path.join(jroot, "f.txt"), "one")
    jj = Journal(jroot)
    try:
        _make_watcher(jroot, jj, cfg, once=True, interval=0.01).run()  # seed
        stale_row = jj.shadow_row("f.txt")  # pre-change shadow (sha of b"one")
        write(os.path.join(jroot, "f.txt"), "two")  # the change lands on disk

        def run_race(guard_on: bool) -> int:
            """One watch pass with the race injected deterministically."""
            fired = {"n": 0}
            real_shadow_all = jj.shadow_all

            def racing_shadow_all():
                rows = real_shadow_all()  # STALE snapshot (pre-change state)
                if fired["n"] == 0:
                    fired["n"] += 1
                    import time as _t

                    # what a concurrent restore does: record + move shadow
                    jj.record_change(
                        ts=_t.time(), run_id=None, path="f.txt",
                        event_type="modified", actor="redhand",
                        confidence="high", old_size=3, new_size=3,
                        old_hash=None, new_hash=sha256_bytes(b"two"),
                        mtime_ns=1, pre_blob_id=stale_row["blob_id"],
                        new_content=b"two", blob_cap_total=1024 * 1024,
                    )
                return rows  # hand back the STALE rows

            monkeypatch.setattr(jj, "shadow_all", racing_shadow_all)
            w = _make_watcher(jroot, jj, cfg, once=True, interval=0.01)
            if not guard_on:
                w._already_recorded = lambda change: False
            return w.run()

        # sensitivity: guard disabled -> the duplicate IS recorded
        summary_off = run_race(guard_on=False)
        assert summary_off.events == 1
        assert len([e for e in jj.events() if e["path"] == "f.txt"]) == 2

        # reset exactly to the pre-race state: drop the duplicate event,
        # rewind the shadow to the stale row the race started from
        for e in [e for e in jj.events() if e["path"] == "f.txt" and e["actor"] == "manual"]:
            jj.conn.execute("DELETE FROM events WHERE id=?", (e["id"],))
        jj.conn.execute(
            "UPDATE shadow SET blob_id=?, size=?, mtime_ns=?, sha256=? WHERE path=?",
            (stale_row["blob_id"], stale_row["size"], stale_row["mtime_ns"],
             stale_row["sha256"], "f.txt"))
        jj.conn.commit()

        # real behavior: the guard suppresses the duplicate
        summary = run_race(guard_on=True)
        assert summary.events == 0  # the guard ate the duplicate
        # two races ran, each injecting exactly one concurrent record;
        # NO manual duplicate was ever recorded
        rows = [e for e in jj.events() if e["path"] == "f.txt"]
        assert len(rows) == 2
        assert {e["actor"] for e in rows} == {"redhand"}
    finally:
        jj.close()

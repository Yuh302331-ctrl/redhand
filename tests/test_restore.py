"""End-to-end-ish restore tests over a real seeded journal."""

from __future__ import annotations

import os
import time

import pytest

from redhand.journal import Journal
from redhand.restore import RestoreError, describe_outcome, restore_event, undo_latest
from helpers import seed_file, write


def test_restore_created_removes_file(root):
    jj = Journal(root)
    try:
        ev = seed_file(jj, root, "notes/a.md", "created content")
        assert os.path.exists(os.path.join(root, "notes/a.md"))
        out = restore_event(jj, root, ev, dry_run=True)
        assert out.dry_run and out.action == "removed"
        out = restore_event(jj, root, ev)
        assert out.action == "removed"
        assert not os.path.exists(os.path.join(root, "notes/a.md"))
        assert jj.shadow_row("notes/a.md") is None
        # restore itself is recorded, linked, flagged
        ev_row = jj.event_by_id(ev)
        assert ev_row["restored"] == 1
    finally:
        jj.close()


def test_restore_modified_puts_old_content_back(root):
    jj = Journal(root)
    try:
        seed_file(jj, root, "f.txt", "version one")
        # modify outside the journal, then record the modification manually
        write(os.path.join(root, "f.txt"), "version two")
        row = jj.shadow_row("f.txt")
        ev2 = jj.record_change(
            ts=time.time(), run_id=None, path="f.txt", event_type="modified",
            actor="claude", confidence="high",
            old_size=len(b"version one"), new_size=len(b"version two"),
            old_hash=None, new_hash=None,
            mtime_ns=1, pre_blob_id=row["blob_id"], new_content=b"version two",
            blob_cap_total=1024 * 1024,
        )
        out = restore_event(jj, root, ev2)
        assert out.action == "restored-content"
        with open(os.path.join(root, "f.txt"), "rb") as f:
            assert f.read() == b"version one"
        # undo chain intact: restoring the restore-event redoes the change
        restore_event(jj, root, out.new_event_id)
        with open(os.path.join(root, "f.txt"), "rb") as f:
            assert f.read() == b"version two"
    finally:
        jj.close()


def test_restore_deleted_recreates_file(root):
    jj = Journal(root)
    try:
        seed_file(jj, root, "g.txt", "precious")
        row = jj.shadow_row("g.txt")
        ev2 = jj.record_change(
            ts=time.time(), run_id=None, path="g.txt", event_type="deleted",
            actor="codex", confidence="medium",
            old_size=len(b"precious"), new_size=None,
            old_hash=None, new_hash=None,
            mtime_ns=None, pre_blob_id=row["blob_id"], new_content=None,
            blob_cap_total=1024 * 1024,
        )
        out = restore_event(jj, root, ev2)
        assert out.action == "recreated"
        with open(os.path.join(root, "g.txt"), "rb") as f:
            assert f.read() == b"precious"
        assert jj.shadow_row("g.txt") is not None
    finally:
        jj.close()


def test_undo_latest_chain(root):
    jj = Journal(root)
    try:
        seed_file(jj, root, "chain.txt", "one")
        write(os.path.join(root, "chain.txt"), "two")
        row = jj.shadow_row("chain.txt")
        jj.record_change(
            ts=time.time(), run_id=None, path="chain.txt", event_type="modified",
            actor="manual", confidence="low",
            old_size=3, new_size=3, old_hash=None, new_hash=None,
            mtime_ns=2, pre_blob_id=row["blob_id"], new_content=b"two",
            blob_cap_total=1024 * 1024,
        )
        undo_latest(jj, root, "chain.txt")
        with open(os.path.join(root, "chain.txt"), "rb") as f:
            assert f.read() == b"one"
        # undo again -> redo (restoring the restore event)
        undo_latest(jj, root, "chain.txt")
        with open(os.path.join(root, "chain.txt"), "rb") as f:
            assert f.read() == b"two"
        # and again -> back to "one"
        undo_latest(jj, root, "chain.txt")
        with open(os.path.join(root, "chain.txt"), "rb") as f:
            assert f.read() == b"one"
    finally:
        jj.close()


def test_restore_errors(root):
    jj = Journal(root)
    try:
        with pytest.raises(RestoreError):
            restore_event(jj, root, 99999)
        with pytest.raises(RestoreError):
            undo_latest(jj, root, "never-seen.txt")
        # a modified event without restorable content (unshadowed file) -> error
        ev = jj.record_change(
            ts=time.time(), run_id=None, path="bin.dat", event_type="modified",
            actor="manual", confidence="low", old_size=10, new_size=20,
            old_hash=None, new_hash=None, mtime_ns=1,
            pre_blob_id=None, new_content=None, blob_cap_total=1024 * 1024,
        )
        with pytest.raises(RestoreError):
            restore_event(jj, root, ev)
    finally:
        jj.close()


def test_describe_outcome_text():
    from redhand.restore import RestoreOutcome

    o = RestoreOutcome(1, None, "x.txt", "restored-content", 2048, dry_run=True)
    assert "DRY RUN" in describe_outcome(o)
    o = RestoreOutcome(1, 2, "x.txt", "recreated", 10, dry_run=False)
    assert "recreated" in describe_outcome(o) and "DRY" not in describe_outcome(o)

"""Digest generation tests."""

from __future__ import annotations

import time

from redhand.digest import build_digest
from redhand.journal import Journal
from helpers import seed_file


def _seed_two_actors(root):
    jj = Journal(root)
    seed_file(jj, root, "a.py", "print(1)", actor="claude", confidence="high")
    mod_ev = jj.record_change(
        ts=time.time(), run_id=None, path="a.py", event_type="modified",
        actor="claude", confidence="high", old_size=8, new_size=10,
        old_hash=None, new_hash=None, mtime_ns=2,
        pre_blob_id=jj.shadow_row("a.py")["blob_id"], new_content=b"print(12)",
        blob_cap_total=1024 * 1024,
    )
    seed_file(jj, root, "b.txt", "hi", actor="manual", confidence="low")
    jj.record_change(
        ts=time.time(), run_id=None, path="secret/key.txt", event_type="created",
        actor="codex", confidence="high", old_size=None, new_size=5,
        old_hash=None, new_hash=None, mtime_ns=3,
        pre_blob_id=None, new_content=b"oops!", blob_cap_total=1024 * 1024,
        protected_hit=True,
    )
    return jj, mod_ev


def test_digest_sections(root):
    jj, mod_ev = _seed_two_actors(root)
    try:
        md = build_digest(jj, root)
        assert "# redhand digest" in md
        assert "## Summary" in md
        assert "## Timeline" in md
        assert "## Files by actor" in md
        assert "## Restore" in md
        assert "claude" in md and "manual" in md and "codex" in md
        assert "`a.py`" in md and "`b.txt`" in md
        # protected section
        assert "## ⚠ Protected paths" in md
        assert "secret/key.txt" in md
        # restore hints reference content-bearing (modified) event ids
        assert "redhand restore" in md
        assert str(mod_ev) in md
    finally:
        jj.close()


def test_digest_empty(root):
    jj = Journal(root)
    try:
        md = build_digest(jj, root)
        assert "No recorded changes" in md
    finally:
        jj.close()


def test_digest_actor_filter(root):
    jj, _ = _seed_two_actors(root)
    try:
        md = build_digest(jj, root, actor="claude")
        assert "claude" in md
        assert "`b.txt`" not in md  # manual-only file filtered out
        # an actor with no events yields the empty note with the filter named
        md_none = build_digest(jj, root, actor="nobody")
        assert "(actor filter: nobody)" in md_none
    finally:
        jj.close()


def test_digest_since_window(root):
    jj = Journal(root)
    old = seed_file(jj, root, "old.txt", "x")
    jj.conn.execute("UPDATE events SET ts=? WHERE id=?", (time.time() - 7200, old))
    jj.conn.commit()
    seed_file(jj, root, "new.txt", "y")
    try:
        cutoff = time.time() - 3600
        md = build_digest(jj, root, since=cutoff)
        assert "`old.txt`" not in md
        assert "`new.txt`" in md
    finally:
        jj.close()


def test_digest_footer_storage(root):
    jj, _ = _seed_two_actors(root)
    try:
        md = build_digest(jj, root)
        assert "Journal:" in md
        assert "restorable" in md
    finally:
        jj.close()


def test_digest_actor_filter_summary_table(root):
    """The Summary table must honor the actor filter too."""
    jj, _ = _seed_two_actors(root)
    try:
        md = build_digest(jj, root, actor="claude")
        assert "| codex |" not in md
        assert "| manual |" not in md
        assert "| claude |" in md
    finally:
        jj.close()

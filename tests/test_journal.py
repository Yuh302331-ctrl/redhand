"""Tests for the journal: shadow lifecycle, blobs, events, caps, pruning."""

from __future__ import annotations


# ---------------------------------------------------------------- shadow seeding


def test_seed_and_counts(jj):
    entries = [
        {"path": "a.txt", "size": 5, "mtime_ns": 1, "sha256": "aa"},
        {"path": "b.bin", "size": 9, "mtime_ns": 2, "sha256": "bb"},
    ]
    big_cap = 10 * 1024 * 1024
    stats = jj.seed_shadow(entries, big_cap, content_of=lambda p: b"aaaaa" if p == "a.txt" else None)
    assert stats["files"] == 2
    assert stats["shadowed"] == 1
    assert stats["hash_only"] == 1
    sc = jj.shadow_count()
    assert sc["files"] == 2 and sc["shadowed"] == 1 and sc["bytes"] == 14  # 5 + 9 for all rows
    row = jj.shadow_row("a.txt")
    assert row["blob_id"] is not None
    assert jj.get_blob(row["blob_id"]) == b"aaaaa"


def test_seed_respects_cap(jj):
    # distinct contents per file so blob dedup cannot collapse them
    contents = {f"f{i}.txt": (b"payload-" + str(i).encode()) * 1200 for i in range(20)}
    entries = [
        {"path": f"f{i}.txt", "size": len(contents[f"f{i}.txt"]), "mtime_ns": i, "sha256": f"h{i}"}
        for i in range(20)
    ]
    tiny_cap = 30_000
    stats = jj.seed_shadow(entries, tiny_cap, content_of=lambda p: contents[p])
    assert stats["shadowed"] < 20
    assert stats["skipped_bytes"] > 0
    assert jj.total_blob_bytes() <= tiny_cap


# ---------------------------------------------------------------- record_change chains


def test_created_modified_deleted_chain(jj):
    ts = 1000.0
    # created
    jj.record_change(
        ts=ts, run_id=None, path="src/app.py", event_type="created",
        actor="claude", confidence="high", old_size=None, new_size=10,
        old_hash=None, new_hash="h1", mtime_ns=10, pre_blob_id=None,
        new_content=b"version one", blob_cap_total=1024 * 1024,
    )
    row = jj.shadow_row("src/app.py")
    assert row["sha256"] is not None or row["blob_id"] is not None
    blob1 = row["blob_id"]
    assert jj.get_blob(blob1) == b"version one"

    # modified: event references the pre-change blob; shadow moves on
    e2 = jj.record_change(
        ts=ts + 1, run_id=None, path="src/app.py", event_type="modified",
        actor="manual", confidence="low", old_size=10, new_size=12,
        old_hash="h1", new_hash="h2", mtime_ns=20, pre_blob_id=blob1,
        new_content=b"version two", blob_cap_total=1024 * 1024,
    )
    ev2 = jj.event_by_id(e2)
    assert ev2["blob_id"] == blob1  # undo target = pre-change content
    assert jj.shadow_row("src/app.py")["blob_id"] != blob1

    # deleted: shadow row dropped, event references the last content
    e3 = jj.record_change(
        ts=ts + 2, run_id=None, path="src/app.py", event_type="deleted",
        actor="manual", confidence="low", old_size=12, new_size=None,
        old_hash="h2", new_hash=None, mtime_ns=None,
        pre_blob_id=jj.shadow_row("src/app.py")["blob_id"],
        new_content=None, blob_cap_total=1024 * 1024,
    )
    assert jj.shadow_row("src/app.py") is None
    ev3 = jj.event_by_id(e3)
    assert ev3["event"] == "deleted"
    assert ev3["blob_id"] is not None

    hist = jj.events(path="src/app.py")
    assert [e["event"] for e in hist] == ["created", "modified", "deleted"]


def test_events_filters(jj):
    base = dict(run_id=None, actor="claude", confidence="high", mtime_ns=1,
                pre_blob_id=None, new_content=b"x", blob_cap_total=0)
    jj.record_change(ts=10, path="a/f1.txt", event_type="created",
                     new_size=1, old_size=None, old_hash=None, new_hash="a", **base)
    jj.record_change(ts=20, path="a/f2.txt", event_type="created",
                     new_size=1, old_size=None, old_hash=None, new_hash="b", **base)
    jj.record_change(ts=30, path="b/f3.txt", event_type="created",
                     new_size=1, old_size=None, old_hash=None, new_hash="c", **base)
    jj.record_change(ts=40, path="a/deep/f4.txt", event_type="created",
                     new_size=1, old_size=None, old_hash=None, new_hash="d", **base)

    assert len(jj.events()) == 4
    assert len(jj.events(since=25)) == 2
    assert len(jj.events(until=20)) == 2
    assert len(jj.events(path="a")) == 3  # prefix match
    assert len(jj.events(path="a/f1.txt")) == 1
    # LIKE special chars in path are safe
    jj.record_change(ts=50, path="w/100%.txt", event_type="created",
                     new_size=1, old_size=None, old_hash=None, new_hash="e", **base)
    assert len(jj.events(path="w/100%.txt")) == 1
    assert len(jj.events(path="w")) == 1
    # actor/type filters
    assert len(jj.events(actor="nobody")) == 0
    assert len(jj.events(event_type="created")) == 5


def test_counts_and_stats(jj):
    base = dict(run_id=None, confidence="high", mtime_ns=1, pre_blob_id=None,
                new_content=b"x", blob_cap_total=0, new_size=5, old_size=None,
                old_hash=None, new_hash="h")
    jj.record_change(ts=10, path="a.txt", event_type="created", actor="claude", **base)
    jj.record_change(ts=20, path="a.txt", event_type="modified", actor="manual", **base)
    c = jj.counts()
    assert {(r["actor"], r["event"], r["n"]) for r in c} == {("claude", "created", 1), ("manual", "modified", 1)}
    st = jj.stats()
    assert st["events"] == 2
    assert st["db_bytes"] > 0


# ---------------------------------------------------------------- blobs / caps


def test_blob_eviction_keeps_shadow_backed(jj):
    # shadow-backed blob must survive eviction pressure
    jj.seed_shadow([{"path": "keep.txt", "size": 5, "mtime_ns": 1, "sha256": "k"}],
                   10 * 1024 * 1024, lambda p: b"keepme")
    keep_blob = jj.shadow_row("keep.txt")["blob_id"]
    small_cap = 1000
    # pressure: store blobs under a tiny cap; the shadow blob is not evictable
    for i in range(5):
        jj.record_change(
            ts=i, run_id=None, path=f"tmp{i}.txt", event_type="created",
            actor="manual", confidence="low", old_size=None, new_size=2000,
            old_hash=None, new_hash=None, mtime_ns=i, pre_blob_id=None,
            new_content=b"z" * 2000, blob_cap_total=small_cap,
        )
    assert jj.get_blob(keep_blob) == b"keepme"


def test_store_blob_none_when_cannot_fit(jj):
    assert jj.store_blob(b"z" * 5000, 1000) is None
    # zero-length content is a legitimate state and gets a blob
    empty_id = jj.store_blob(b"", 10 * 1024 * 1024)
    assert empty_id is not None
    assert jj.get_blob(empty_id) == b""


def test_restore_flag_and_prev_event(jj):
    base = dict(run_id=None, actor="manual", confidence="low", old_size=None,
                new_size=3, old_hash=None, new_hash="h", mtime_ns=1,
                blob_cap_total=0)
    e1 = jj.record_change(ts=1, path="f.txt", event_type="created", pre_blob_id=None, new_content=b"abc", **base)
    e2 = jj.record_change(ts=2, path="f.txt", event_type="modified",
                          pre_blob_id=jj.shadow_row("f.txt")["blob_id"], new_content=b"abcd", **base)
    ev = jj.event_by_id(e2)
    assert ev["prev_event"] is None and ev["restored"] == 0
    jj.mark_restored(e1)
    assert jj.event_by_id(e1)["restored"] == 1


# ---------------------------------------------------------------- actors table


def test_actor_kinds(jj):
    jj.get_or_create_actor("claude", "agent")
    jj.get_or_create_actor("manual", "human")
    jj.get_or_create_actor("claude", "agent")  # idempotent
    names = {a["name"] for a in jj.actors()}
    assert names == {"claude", "manual"}


# ---------------------------------------------------------------- runs


def test_runs(jj):
    rid = jj.add_run(100.0, 2.0, note="once")
    jj.end_run(rid, 105.0)
    last = jj.last_run()
    assert last["id"] == rid and last["ended_at"] == 105.0 and last["note"] == "once"


# ---------------------------------------------------------------- prune


def test_prune_removes_old(jj):
    base = dict(run_id=None, actor="manual", confidence="low", mtime_ns=1,
                pre_blob_id=None, new_content=b"x" * 50, blob_cap_total=1024,
                new_size=50, old_size=None, old_hash=None, new_hash="h")
    old = jj.record_change(ts=10, path="old.txt", event_type="created", **base)
    new = jj.record_change(ts=1000, path="new.txt", event_type="created", **base)
    out = jj.prune(before_ts=500)
    assert out["events"] >= 1
    ids = {e["id"] for e in jj.events()}
    assert old not in ids and new in ids

"""Tests for scanner: walk, ignore rules, symlink safety, shadow diff."""

from __future__ import annotations

import os
import sys
import time

import pytest

from redhand.scanner import diff_against_shadow, scan_tree
from redhand.util import PatternSet, sha256_bytes


def w(path, content: str = "hello", mode: str = "w"):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------- scan_tree


def test_scan_tree_basic(root):
    w(os.path.join(root, "src", "a.py"))
    w(os.path.join(root, "b.txt"))
    entries = scan_tree(root)
    assert set(entries) == {"src/a.py", "b.txt"}
    assert all(e.size >= 0 for e in entries.values())


def test_scan_tree_ignores(root):
    w(os.path.join(root, "node_modules", "x.js"))
    w(os.path.join(root, "src", "app.py"))
    w(os.path.join(root, "debug.log"))
    w(os.path.join(root, "secrets", "key.txt"))
    w(os.path.join(root, ".git", "HEAD"))
    ignore = PatternSet([".git/", "node_modules/", "*.log", "/secrets"])
    entries = scan_tree(root, ignore)
    assert set(entries) == {"src/app.py"}


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on windows")
def test_scan_tree_skips_symlink_dirs(root):
    outside = root + "_outside"
    os.makedirs(outside)
    w(os.path.join(outside, "secret.txt"))
    os.symlink(outside, os.path.join(root, "link"))
    entries = scan_tree(root)
    # the link is recorded as an entry, its contents are NOT walked
    assert "link" in entries
    assert "link/secret.txt" not in entries


def test_scan_tree_empty_and_missing(root):
    assert scan_tree(root) == {}
    assert scan_tree(os.path.join(root, "nope")) == {}


# ---------------------------------------------------------------- diff


def _shadow_of(root, entries, hash_limit=1024 * 1024):
    """Build shadow rows from the current filesystem (like seeding does)."""
    rows = []
    for path, e in entries.items():
        sha = None
        if e.size <= hash_limit:
            with open(os.path.join(root, path), "rb") as f:
                sha = sha256_bytes(f.read())
        rows.append({"path": path, "blob_id": None, "size": e.size, "mtime_ns": e.mtime_ns, "sha256": sha})
    return rows


def _read(root):
    """Mimic production read_content: text only (binary -> None, hash-only)."""
    from redhand.util import is_probably_text

    def read(abs_path):
        if not is_probably_text(abs_path):
            return None
        try:
            with open(abs_path, "rb") as f:
                return f.read()
        except OSError:
            return None
    return read


def test_diff_created_and_deleted(root):
    w(os.path.join(root, "a.txt"), "v1")
    entries = scan_tree(root)
    rows = _shadow_of(root, entries)

    # no changes
    changes, touches = diff_against_shadow(root, scan_tree(root), rows, 1024 * 1024,
                                           lambda p, s: True, _read(root))
    assert changes == [] and touches == []

    # create + delete
    w(os.path.join(root, "b.txt"), "new")
    os.remove(os.path.join(root, "a.txt"))
    changes, _ = diff_against_shadow(root, scan_tree(root), rows, 1024 * 1024, lambda p, s: True, _read(root))
    kinds = {(c.path, c.event) for c in changes}
    assert ("b.txt", "created") in kinds
    assert ("a.txt", "deleted") in kinds


def test_diff_modified_and_touch(root):
    w(os.path.join(root, "a.txt"), "v1")
    rows = _shadow_of(root, scan_tree(root))
    time.sleep(0.02)

    # same content, new mtime -> touch, no event
    w(os.path.join(root, "a.txt"), "v1")
    changes, touches = diff_against_shadow(root, scan_tree(root), rows, 1024 * 1024,
                                           lambda p, s: True, _read(root))
    assert changes == [] and len(touches) == 1

    # real content change -> modified
    time.sleep(0.02)
    w(os.path.join(root, "a.txt"), "v2 long content")
    changes, _ = diff_against_shadow(root, scan_tree(root), rows, 1024 * 1024, lambda p, s: True, _read(root))
    assert len(changes) == 1
    c = changes[0]
    assert c.event == "modified" and c.old_hash != c.new_hash and c.new_content == b"v2 long content"


def test_diff_binary_files_hashed(root):
    p = os.path.join(root, "blob.bin")
    with open(p, "wb") as f:
        f.write(b"\x00\x01\x02")
    rows = _shadow_of(root, scan_tree(root))
    time.sleep(0.02)
    with open(p, "wb") as f:
        f.write(b"\x00\x01\x03")
    changes, _ = diff_against_shadow(root, scan_tree(root), rows, 1024 * 1024, lambda p, s: True, _read(root))
    assert len(changes) == 1 and changes[0].event == "modified"
    assert changes[0].new_content is None  # binary: hashed but not shadowed
    assert changes[0].new_hash is not None


def test_diff_big_file_size_only(root):
    p = os.path.join(root, "big.bin")
    with open(p, "wb") as f:
        f.write(b"x" * (2 * 1024 * 1024))  # > hash_limit used below
    rows = _shadow_of(root, scan_tree(root), hash_limit=1024 * 1024)
    assert rows[0]["sha256"] is None
    time.sleep(0.02)
    with open(p, "wb") as f:
        f.write(b"y" * (3 * 1024 * 1024))
    changes, touches = diff_against_shadow(root, scan_tree(root), rows, 1024 * 1024,
                                           lambda p, s: True, _read(root))
    assert len(changes) == 1 and changes[0].event == "modified"
    assert changes[0].new_hash is None

    # mtime-only change on an unhashable file -> silent touch, no event
    rows_after = _shadow_of(root, scan_tree(root), hash_limit=1024 * 1024)
    os.utime(p, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    changes, touches = diff_against_shadow(root, scan_tree(root), rows_after, 1024 * 1024,
                                           lambda p, s: True, _read(root))
    assert changes == [] and len(touches) == 1

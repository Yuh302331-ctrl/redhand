"""Tests for util: sizes, time parsing, hashing, text sniffing, PatternSet."""

from __future__ import annotations

import time

import pytest

from redhand.util import (
    PatternSet,
    age_of,
    human_size,
    is_probably_text,
    parse_since,
    sha256_bytes,
    sha256_file,
)


# ---------------------------------------------------------------- sizes


def test_human_size_units():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"
    assert human_size(None) == "—"


# ---------------------------------------------------------------- time parsing


def test_parse_since_relative():
    now = 1_000_000.0
    assert parse_since("90s", now) == now - 90
    assert parse_since("30m", now) == now - 1800
    assert parse_since("2h", now) == now - 7200
    assert parse_since("7d", now) == now - 7 * 86400
    assert parse_since("2w", now) == now - 14 * 86400
    assert parse_since("1.5h", now) == now - 5400


def test_parse_since_epoch_and_iso():
    now = 1_000_000.0
    assert parse_since("999999", now) == 999999.0
    assert parse_since(12345.5, now) == 12345.5
    # tz-aware ISO is exact regardless of local timezone
    assert parse_since("1970-01-01T00:01:40+00:00") == 100.0
    # naive ISO parses (local tz applied) — just verify it is a finite number
    ts = parse_since("2026-01-01T00:00:00")
    assert isinstance(ts, float) and ts > 0


def test_parse_since_errors():
    with pytest.raises(ValueError):
        parse_since("not-a-time")
    with pytest.raises(ValueError):
        parse_since("")
    with pytest.raises(ValueError):
        parse_since("12x")


def test_age_of():
    now = time.time()
    assert age_of(now - 5, now) == "5s"
    assert age_of(now - 125, now) == "2m05s"
    assert age_of(None) == "—"


# ---------------------------------------------------------------- hashing / text


def test_sha256_file_and_bytes(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    assert sha256_file(str(p)) == sha256_bytes(b"hello")
    assert sha256_file(str(tmp_path / "missing")) is None


def test_is_probably_text(tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("ok 中文 text")
    assert is_probably_text(str(t))
    b = tmp_path / "b.bin"
    b.write_bytes(b"\x00\x01\x02binary")
    assert not is_probably_text(str(b))
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff\xfe\xfa\xfb invalid utf8 \x9c")
    assert not is_probably_text(str(bad))


# ---------------------------------------------------------------- PatternSet


def test_patternset_bare_name_matches_any_component():
    ps = PatternSet(["node_modules", ".git"])
    assert ps.matches_path("a/node_modules/x.js")
    assert ps.matches_path("node_modules")
    assert ps.matches_path("a/b/.git/config")
    assert not ps.matches_path("a/nodemodules/x.js")
    assert not ps.matches_path("src/app.py")


def test_patternset_dir_rule():
    ps = PatternSet(["build/"])
    assert ps.matches_path("build/anything.o")
    assert ps.matches_path("a/build/out.txt")
    assert not ps.matches_path("builder/run.py")  # component must match exactly


def test_patternset_wildcards():
    ps = PatternSet(["*.log", "docs/*.tmp"])
    assert ps.matches_path("errors.log")
    assert ps.matches_path("a/b/errors.log")
    assert ps.matches_path("docs/x.tmp")
    assert ps.matches_path("docs/deep/x.tmp")  # fnmatch '*' crosses '/' by convention
    assert not ps.matches_path("logs")


def test_patternset_anchored():
    ps = PatternSet(["/secrets"])
    assert ps.matches_path("secrets")
    assert ps.matches_path("secrets/key.txt")
    assert not ps.matches_path("a/secrets/key.txt")  # anchored to root only


def test_patternset_component_helper():
    ps = PatternSet(["__pycache__"])
    assert ps.matches_component("__pycache__")
    assert not ps.matches_component("pycache")
    assert not PatternSet(["*.log"]).matches_component("*.log")  # wildcard rules excluded


def test_patternset_empty():
    ps = PatternSet([])
    assert not ps
    assert not ps.matches_path("anything/at/all")

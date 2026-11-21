"""Small shared helpers: sizes, time parsing, hashing, text sniffing, glob patterns, ANSI."""

from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import os
import time
from fnmatch import fnmatch
from typing import Iterable, List, Optional, Union

CHUNK = 65536
_SNIFF_BYTES = 8192


# ---------------------------------------------------------------- sizes


def human_size(n: Optional[int]) -> str:
    """1234567 -> '1.2 MB'. None/0 -> '0 B' / '—'."""
    if n is None:
        return "—"
    n = float(n)
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


# ---------------------------------------------------------------- time


def parse_since(spec: Union[str, float, int], now: Optional[float] = None) -> float:
    """Parse '90s' / '30m' / '2h' / '7d' / '2w' / ISO date / epoch seconds into an epoch float.

    Bare numbers are treated as epoch seconds. ISO strings may be local naive
    datetimes or include an offset.
    """
    now = now if now is not None else time.time()
    if isinstance(spec, (int, float)):
        return float(spec)
    s = spec.strip().lower()
    if not s:
        raise ValueError("empty time specification")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s[-1] in units and s[:-1].replace(".", "", 1).isdigit():
        return now - float(s[:-1]) * units[s[-1]]
    try:
        return float(s)
    except ValueError:
        pass
    iso = spec.strip()
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"  # fromisoformat rejects 'Z' before py3.11
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(f"cannot parse time spec: {spec!r}") from None
    if dt.tzinfo is None:
        return dt.timestamp()
    return dt.timestamp()


def fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_clock(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def age_of(ts: Optional[float], now: Optional[float] = None) -> str:
    """Humanized age like '3m20s ago'."""
    if ts is None:
        return "—"
    now = now if now is not None else time.time()
    d = max(0, int(now - ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m{d % 60:02d}s"
    if d < 86400:
        return f"{d // 3600}h{(d % 3600) // 60:02d}m"
    return f"{d // 86400}d{(d % 86400) // 3600:02d}h"


# ---------------------------------------------------------------- hashing / text sniffing


def sha256_file(path: str) -> Optional[str]:
    """SHA-256 hex digest of a file, or None if unreadable."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_probably_text(path: str) -> bool:
    """Heuristic: no NUL bytes in the head and decodable as UTF-8 (whole file for small files)."""
    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF_BYTES)
            if b"\0" in head:
                return False
            f.seek(0)
            data = f.read()
    except OSError:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def read_file_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def write_file_nofollow(path: str, data: bytes) -> None:
    """Write bytes to `path` without following symlinks.

    A symlink at `path` (planted between journaling and restore) is removed
    and replaced by a regular file — journal content must never be written
    *through* a link to some other file.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except OSError as exc:
        if exc.errno == getattr(errno, "ELOOP", None) or exc.errno == getattr(errno, "EMLINK", None):
            # symlink in the way: replace it with a regular file
            os.remove(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        else:
            raise
    with os.fdopen(fd, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------- pattern matching


class PatternSet:
    """Glob-ish ignore / protect rules with predictable semantics.

    Rule forms (matched against '/'-separated relative paths):

    * ``node_modules``       — matches *any* path component with that name (dirs or files)
    * ``build/``             — matches any *directory* named ``build``; prunes the subtree
    * ``*.log``              — fnmatch against the basename and the full relative path
    * ``docs/*.tmp``         — fnmatch against the full relative path
    * ``/secrets``           — leading slash anchors to the watched root (prefix match)
    """

    def __init__(self, patterns: Optional[Iterable[str]] = None):
        self.patterns: List[str] = [p for p in (patterns or []) if p]

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def __len__(self) -> int:
        return len(self.patterns)

    def matches_component(self, name: str) -> bool:
        """True if any bare-name rule equals/globs this single component."""
        for p in self.patterns:
            if not p.startswith("/") and "*" not in p and p != "" and p.rstrip("/") == name:
                return True
        return False

    def matches_path(self, rel: str, is_dir: bool = False) -> bool:
        rel = rel.replace(os.sep, "/").strip("/")
        parts = rel.split("/")
        for p in self.patterns:
            anchored = p.startswith("/")
            pat = p.lstrip("/")
            if anchored:
                if rel == pat or rel.startswith(pat + "/") or fnmatch(rel, pat):
                    return True
                continue
            if p.endswith("/"):
                name_rule = p[:-1]
                if any(fnmatch(part, name_rule) for part in parts[:-1] if part) or (
                    is_dir and fnmatch(rel, name_rule)
                ):
                    return True
                continue
            if "*" in p:
                if fnmatch(rel, p) or fnmatch(parts[-1], p):
                    return True
                continue
            if p in parts:
                return True
        return False


# ---------------------------------------------------------------- ANSI


class Palette:
    """Tiny ANSI wrapper. Degrades to plain text when disabled."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._c("1", t)

    def dim(self, t: str) -> str:
        return self._c("2", t)

    def red(self, t: str) -> str:
        return self._c("31", t)

    def green(self, t: str) -> str:
        return self._c("32", t)

    def yellow(self, t: str) -> str:
        return self._c("33", t)

    def blue(self, t: str) -> str:
        return self._c("34", t)

    def magenta(self, t: str) -> str:
        return self._c("35", t)

    def cyan(self, t: str) -> str:
        return self._c("36", t)


EVENT_COLOR = {
    "created": "green",
    "modified": "yellow",
    "deleted": "red",
}

CONFIDENCE_MARK = {"high": "✓", "medium": "?", "low": "·"}

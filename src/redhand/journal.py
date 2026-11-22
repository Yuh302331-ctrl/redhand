"""SQLite journal: shadow states, events, blobs, runs, actors.

Core idea — the *shadow* is the last known state of every tracked file and
serves as the diff baseline for the watcher. Every content state is stored
exactly once as a blob:

* on `created`/`modified`, the event references the shadow's previous blob
  (= the state *before* the change), and the shadow moves to a new blob;
* on `deleted`, the event references the shadow's previous blob and the
  shadow row is dropped;
* restores are recorded as ordinary changes (actor 'redhand'), so undo
  chains work; a fresh-shadow check before recording keeps concurrent
  redhand processes from double-logging the same change.

Uniform restore semantic: *restore(event) puts the file back to how it was
just before that event happened*.

Storage discipline: blobs are zlib-compressed; oldest blobs not referenced
by the shadow are evicted first when the global cap is exceeded. Events
whose blob was evicted keep their ledger row but lose restorability.
"""

from __future__ import annotations

import os
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as _config
from .util import sha256_bytes

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at   REAL,
    root       TEXT NOT NULL,
    pid        INTEGER,
    interval   REAL,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS actors (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS blobs (
    id            INTEGER PRIMARY KEY,
    content       BLOB NOT NULL,
    original_size INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow (
    path       TEXT PRIMARY KEY,
    blob_id    INTEGER REFERENCES blobs(id),
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    sha256     TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    ts            REAL NOT NULL,
    run_id        INTEGER,
    path          TEXT NOT NULL,
    event         TEXT NOT NULL,
    new_size      INTEGER,
    old_size      INTEGER,
    new_hash      TEXT,
    old_hash      TEXT,
    actor         TEXT NOT NULL,
    confidence    TEXT NOT NULL DEFAULT 'low',
    blob_id       INTEGER REFERENCES blobs(id) ON DELETE SET NULL,
    prev_event    INTEGER REFERENCES events(id),
    protected_hit INTEGER NOT NULL DEFAULT 0,
    restored      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_path ON events(path);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor);
CREATE INDEX IF NOT EXISTS idx_events_blob ON events(blob_id);
"""

_ACTOR_KIND = {
    "manual": "human",
    "unverified-agent": "agent",
    "redhand": "tool",
}


class JournalError(RuntimeError):
    """Journal-level failures (corrupt DB, missing blobs, ...)."""


class Journal:
    def __init__(self, root: str, *, state_dir: Optional[str] = None, read_only: bool = False):
        self.root = os.path.abspath(root)
        self.state_dir = os.path.abspath(state_dir or os.path.join(self.root, _config.STATE_DIR))
        self.db_path = os.path.join(self.state_dir, _config.DB_NAME)
        if read_only:
            self.conn = sqlite3.connect(Path(self.db_path).as_uri() + "?mode=ro", uri=True, timeout=10.0)
            self.conn.row_factory = sqlite3.Row
            return
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    # ------------------------------------------------------------- meta

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------------- runs

    def add_run(self, started_at: float, interval: float, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, root, pid, interval, note) VALUES(?,?,?,?,?)",
            (started_at, self.root, os.getpid(), interval, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def end_run(self, run_id: int, ended_at: Optional[float] = None) -> None:
        self.conn.execute("UPDATE runs SET ended_at=? WHERE id=?", (ended_at or time.time(), run_id))
        self.conn.commit()

    def last_run(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- actors

    def get_or_create_actor(self, name: str, kind: Optional[str] = None) -> int:
        if kind is None:
            kind = _ACTOR_KIND.get(name, "agent")
        row = self.conn.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone()
        if row:
            return int(row["id"])
        try:
            cur = self.conn.execute("INSERT INTO actors(name, kind) VALUES(?,?)", (name, kind))
            self.conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # another process inserted the same actor between SELECT and INSERT
            self.conn.rollback()
            row = self.conn.execute("SELECT id FROM actors WHERE name=?", (name,)).fetchone()
            if row:
                return int(row["id"])
            raise

    def actors(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT a.name, a.kind, COUNT(e.id) AS n, MAX(e.ts) AS last_ts "
            "FROM actors a LEFT JOIN events e ON e.actor = a.name "
            "GROUP BY a.name ORDER BY n DESC, a.name"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- blobs

    def total_blob_bytes(self) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(original_size), 0) AS t FROM blobs").fetchone()
        return int(row["t"])

    def blob_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()
        return int(row["n"])

    def _evict_blobs_to_fit(self, incoming: int, cap: int) -> None:
        """Free room for `incoming` bytes by deleting oldest blobs that the
        shadow does not reference (FIFO). Never touches shadow-backed states.
        Feasibility is checked BEFORE deleting anything: if even evicting
        every evictable blob would not make room, we keep history intact
        (the caller then refuses to store — evict-then-bail is forbidden)."""
        if cap <= 0:
            return
        total = self.total_blob_bytes()
        if total + incoming <= cap:
            return
        evictable = int(
            self.conn.execute(
                "SELECT COALESCE(SUM(original_size), 0) AS t FROM blobs "
                "WHERE id NOT IN (SELECT blob_id FROM shadow WHERE blob_id IS NOT NULL)"
            ).fetchone()["t"]
        )
        if total + incoming - evictable > cap:
            return  # cannot fit even with full eviction; destroy nothing
        need = total + incoming - cap
        freed = 0
        while freed < need:
            rows = self.conn.execute(
                "SELECT b.id, b.original_size FROM blobs b "
                "WHERE b.id NOT IN (SELECT blob_id FROM shadow WHERE blob_id IS NOT NULL) "
                "ORDER BY b.id ASC LIMIT 64"
            ).fetchall()
            if not rows:
                break  # only shadow-backed blobs remain; stop
            freed += sum(int(r["original_size"]) for r in rows)
            self.conn.execute(
                f"DELETE FROM blobs WHERE id IN ({','.join(str(r['id']) for r in rows)})"
            )

    def store_blob(self, data: bytes, cap_total: int) -> Optional[int]:
        """Compress+store content under the global cap.

        Content-addressed: identical bytes reuse the existing blob (undo/redo
        cycles don't duplicate storage). Zero-length content is a legitimate
        state and gets a blob of its own. Returns None only when nothing
        could be stored (capacity exhausted after safe eviction attempts).
        """
        digest = sha256_bytes(data)
        row = self.conn.execute("SELECT id FROM blobs WHERE sha256=? LIMIT 1", (digest,)).fetchone()
        if row:
            return int(row["id"])
        packed = zlib.compress(data, 6)
        self._evict_blobs_to_fit(len(data), cap_total)
        if cap_total > 0 and self.total_blob_bytes() + len(data) > cap_total:
            return None  # cannot fit even after evicting every evictable blob
        cur = self.conn.execute(
            "INSERT INTO blobs(content, original_size, sha256, created_at) VALUES(?,?,?,?)",
            (sqlite3.Binary(packed), len(data), digest, time.time()),
        )
        return int(cur.lastrowid)

    def get_blob(self, blob_id: Optional[int]) -> Optional[bytes]:
        if blob_id is None:
            return None
        row = self.conn.execute("SELECT content FROM blobs WHERE id=?", (blob_id,)).fetchone()
        if row is None:
            return None
        try:
            return zlib.decompress(bytes(row["content"]))
        except zlib.error as exc:
            raise JournalError(f"blob #{blob_id} is corrupted: {exc}") from None

    # ------------------------------------------------------------- shadow

    def shadow_row(self, path: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM shadow WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def shadow_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM shadow ORDER BY path").fetchall()
        return [dict(r) for r in rows]

    def shadow_count(self) -> Dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS files, "
            "COALESCE(SUM(CASE WHEN blob_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS shadowed, "
            "COALESCE(SUM(size), 0) AS bytes "
            "FROM shadow"
        ).fetchone()
        return {"files": int(row["files"]), "shadowed": int(row["shadowed"]), "bytes": int(row["bytes"])}

    def shadow_touch(self, path: str, size: int, mtime_ns: int, sha256: Optional[str], commit: bool = True) -> None:
        """Silent metadata refresh (e.g. touch without content change)."""
        self.conn.execute(
            "UPDATE shadow SET size=?, mtime_ns=?, sha256=COALESCE(?, sha256), updated_at=? WHERE path=?",
            (size, mtime_ns, sha256, time.time(), path),
        )
        if commit:
            self.conn.commit()

    def _shadow_set(
        self,
        path: str,
        blob_id: Optional[int],
        size: int,
        mtime_ns: int,
        sha256: Optional[str],
    ) -> None:
        self.conn.execute(
            "INSERT INTO shadow(path, blob_id, size, mtime_ns, sha256, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET blob_id=excluded.blob_id, size=excluded.size, "
            "mtime_ns=excluded.mtime_ns, sha256=excluded.sha256, updated_at=excluded.updated_at",
            (path, blob_id, size, mtime_ns, sha256, time.time()),
        )

    def _shadow_drop(self, path: str) -> None:
        self.conn.execute("DELETE FROM shadow WHERE path=?", (path,))

    def seed_shadow(
        self,
        entries: List[Dict[str, Any]],
        blob_cap_total: int,
        content_of,
    ) -> Dict[str, int]:
        """Bulk-initialize the shadow from a full scan.

        entries: list of dicts with path/size/mtime_ns/sha256.
        content_of(path) -> Optional[bytes] supplies text content when a file
        should be restorable; returns None for files we only track by hash.

        Stats returned: {'files', 'shadowed', 'hash_only', 'skipped_bytes'}
        """
        stats = {"files": 0, "shadowed": 0, "hash_only": 0, "skipped_bytes": 0}
        for e in entries:
            stats["files"] += 1
            content = content_of(e["path"])
            blob_id = None
            if content is not None:
                blob_id = self.store_blob(content, blob_cap_total)
                if blob_id is not None:
                    stats["shadowed"] += 1
                else:
                    stats["skipped_bytes"] += int(e.get("size") or 0)
            if blob_id is None and e.get("sha256"):
                stats["hash_only"] += 1
            self._shadow_set(e["path"], blob_id, int(e.get("size") or 0), int(e.get("mtime_ns") or 0), e.get("sha256"))
            if stats["files"] % 200 == 0:
                self.conn.commit()
        self.conn.commit()
        return stats

    # ------------------------------------------------------------- events

    def record_change(
        self,
        ts: float,
        run_id: Optional[int],
        path: str,
        event_type: str,
        actor: str,
        confidence: str,
        old_size: Optional[int],
        new_size: Optional[int],
        old_hash: Optional[str],
        new_hash: Optional[str],
        mtime_ns: Optional[int],
        pre_blob_id: Optional[int],
        new_content: Optional[bytes],
        blob_cap_total: int = 0,
        protected_hit: bool = False,
        prev_event: Optional[int] = None,
        restored_flag: bool = False,
    ) -> int:
        """Insert an event and move the shadow to the new state atomically.

        pre_blob_id: the shadow's current blob (the pre-change state) or None.
        new_content: post-change content to keep (None => not restorable /
        file gone). For 'deleted' the shadow row is removed.

        Ordering invariant: for 'modified', the shadow still references
        pre_blob_id when store_blob() runs, so eviction can never evict the
        undo target of the event being written.
        """
        path = path.replace(os.sep, "/")
        if event_type == "deleted":
            self._shadow_drop(path)
        else:
            new_blob_id = None
            if new_content is not None:
                # empty content is a legitimate state — it gets a blob too
                new_blob_id = self.store_blob(new_content, blob_cap_total)
            self._shadow_set(path, new_blob_id, int(new_size or 0), int(mtime_ns or 0), new_hash)
        cur = self.conn.execute(
            "INSERT INTO events(ts, run_id, path, event, new_size, old_size, new_hash, old_hash, "
            "actor, confidence, blob_id, prev_event, protected_hit, restored) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts,
                run_id,
                path,
                event_type,
                new_size,
                old_size,
                new_hash,
                old_hash,
                actor,
                confidence,
                pre_blob_id,
                prev_event,
                1 if protected_hit else 0,
                1 if restored_flag else 0,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_restored(self, event_id: int) -> None:
        self.conn.execute("UPDATE events SET restored=1 WHERE id=?", (event_id,))
        self.conn.commit()

    _SELECT = (
        "SELECT e.*, b.sha256 AS blob_sha256 "
        "FROM events e LEFT JOIN blobs b ON b.id = e.blob_id "
    )

    def events(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
        actor: Optional[str] = None,
        path: Optional[str] = None,
        event_type: Optional[str] = None,
        restorable_only: bool = False,
        limit: Optional[int] = None,
        order: str = "ASC",
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        args: List[Any] = []
        if since is not None:
            where.append("e.ts >= ?")
            args.append(since)
        if until is not None:
            where.append("e.ts <= ?")
            args.append(until)
        if actor:
            where.append("e.actor = ?")
            args.append(actor)
        if path:
            # stored paths always use '/'; normalize shell tab-completion
            # backslashes (Windows) before matching
            if os.sep != "/":
                path = path.replace(os.sep, "/")
            escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(e.path = ? OR e.path LIKE ? ESCAPE '\\')")
            args.extend([path, escaped + "/%"])
        if event_type:
            where.append("e.event = ?")
            args.append(event_type)
        if restorable_only:
            where.append("e.blob_id IS NOT NULL")
        if order not in ("ASC", "DESC"):  # interpolated into SQL; keep it structural
            raise ValueError(f"invalid order: {order!r}")
        sql = self._SELECT
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += f"ORDER BY e.ts {order}, e.id {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def event_by_id(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(self._SELECT + "WHERE e.id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def counts(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
        actor: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        args: List[Any] = []
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts <= ?")
            args.append(until)
        if actor:
            where.append("actor = ?")
            args.append(actor)
        sql = (
            "SELECT actor, confidence, event, COUNT(*) AS n, "
            "SUM(protected_hit) AS protected_n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
            "FROM events "
        )
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "GROUP BY actor, confidence, event ORDER BY actor, event"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def stats(self) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS events, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM events"
        ).fetchone()
        out = {
            "events": int(row["events"]),
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "db_bytes": self.db_size(),
            "blob_bytes": self.total_blob_bytes(),
            "blob_n": self.blob_count(),
        }
        out.update(self.shadow_count())
        return out

    # ------------------------------------------------------------- maintenance

    def prune(self, before_ts: float) -> Dict[str, int]:
        cur = self.conn.execute("DELETE FROM events WHERE ts < ?", (before_ts,))
        events_n = cur.rowcount
        cur = self.conn.execute(
            "DELETE FROM blobs WHERE id NOT IN ("
            "  SELECT blob_id FROM shadow WHERE blob_id IS NOT NULL"
            "  UNION SELECT blob_id FROM events WHERE blob_id IS NOT NULL)"
        )
        blobs_n = cur.rowcount
        cur = self.conn.execute(
            "DELETE FROM actors WHERE name NOT IN (SELECT DISTINCT actor FROM events)"
        )
        actors_n = cur.rowcount
        cur = self.conn.execute(
            "DELETE FROM runs WHERE ended_at IS NOT NULL AND ended_at < ?", (before_ts,)
        )
        runs_n = cur.rowcount
        self.conn.commit()
        return {"events": events_n, "blobs": blobs_n, "actors": actors_n, "runs": runs_n}

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def db_size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        return total

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

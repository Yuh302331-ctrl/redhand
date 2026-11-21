"""The watch engine: poll-scan -> diff against shadow -> attribute -> record.

Design notes
------------
* The shadow table is the diff baseline, so changes are detected relative to
  the last *known* state (even across watcher restarts). Restores from other
  redhand processes are not double-recorded in practice: a fresh-shadow check
  runs right before each record, closing the scan->record race window.
* Attribution runs only on ticks that actually have changes (one process
  snapshot per tick, not per file).
* Protected paths are recorded first (audit trail), then optionally reverted
  ('--enforce'); each revert is recorded as its own event by actor 'redhand'.
* One watcher per watched root, enforced by an flock on .whodunit/watch.lock.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import stat as _stat
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import fcntl  # POSIX only; redhand targets macOS/Linux
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover
    _HAVE_FCNTL = False

from . import actors as _actors
from . import scanner as _scanner
from .config import Config
from .journal import Journal, JournalError
from .util import (
    CONFIDENCE_MARK,
    EVENT_COLOR,
    Palette,
    fmt_clock,
    human_size,
    is_probably_text,
    read_file_bytes,
    safe_hash,
    write_file_nofollow,
)

LOCK_NAME = "watch.lock"


class WatchError(RuntimeError):
    pass


@dataclass
class WatchSummary:
    run_id: int
    started_at: float
    ended_at: float
    events: int = 0
    touches: int = 0
    protected_events: int = 0
    reverted: int = 0
    seeded: bool = False
    by_actor: Dict[str, int] = field(default_factory=dict)


def session_summary_line(summary: WatchSummary, pal: Palette) -> str:
    dur = max(0.0, summary.ended_at - summary.started_at)
    parts = [f"{dur:.0f}s", f"{summary.events} events", f"{summary.touches} touches"]
    if summary.protected_events:
        parts.append(f"{summary.protected_events} protected")
    if summary.reverted:
        parts.append(f"{summary.reverted} reverted")
    lines = [f"\n{pal.bold('session')}: " + " · ".join(parts)]
    for actor, n in sorted(summary.by_actor.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {actor}: {n}")
    return "\n".join(lines)


class Watcher:
    def __init__(
        self,
        root: str,
        cfg: Config,
        journal: Journal,
        palette: Optional[Palette] = None,
        out: Optional[object] = None,
        once: bool = False,
        enforce: bool = False,
        interval: Optional[float] = None,
    ):
        self.root = os.path.abspath(root)
        self.cfg = cfg
        self.jj = journal
        self.pal = palette or Palette(enabled=False)
        self.out = out if out is not None else sys.stdout
        self.once = once
        self.enforce = enforce
        raw_interval = interval if interval is not None else cfg.interval
        try:
            raw_interval = float(raw_interval)
        except (TypeError, ValueError):
            raw_interval = cfg.interval
        # guard against --interval 0 busy-looping the watcher
        self.interval = max(0.1, raw_interval)
        self._stop = False
        # session counters
        self.n_events = 0
        self.n_touches = 0
        self.n_protected = 0
        self.n_reverted = 0
        self.by_actor: Dict[str, int] = {}

    # ---------------------------------------------------------- plumbing

    def _p(self, text: str = "") -> None:
        try:
            self.out.write(text + "\n")
            self.out.flush()
        except (OSError, ValueError):
            pass

    def _abs(self, rel: str) -> str:
        return os.path.join(self.root, rel.replace("/", os.sep))

    def _shadowable(self, rel_path: str, size: int) -> bool:
        # rel_path unused: gating is size-only here; content type and the
        # symlink-target size check happen later in _read_content
        return size <= self.cfg.blob_limit

    def _read_content(self, abs_path: str) -> Optional[bytes]:
        """Content read with the gate on the *target* file: a symlink's lstat
        size is just the link-path length, so gating on it would happily read
        a multi-GB target into memory. Only regular files within the blob
        limit are ever opened; FIFOs/sockets/devices are never opened."""
        try:
            st = os.lstat(abs_path)
            if _stat.S_ISLNK(st.st_mode):
                try:
                    tst = os.stat(abs_path)  # follow the link
                except OSError:
                    return None
                if not _stat.S_ISREG(tst.st_mode) or tst.st_size > self.cfg.blob_limit:
                    return None
            elif not _stat.S_ISREG(st.st_mode) or st.st_size > self.cfg.blob_limit:
                return None
        except OSError:
            return None
        if not is_probably_text(abs_path):
            return None
        return read_file_bytes(abs_path)

    def _acquire_lock(self):
        if not _HAVE_FCNTL:  # pragma: no cover
            raise WatchError("watch mode requires POSIX file locking (macOS/Linux)")
        lock_path = os.path.join(self.root, ".whodunit", LOCK_NAME)
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            holder = "?"
            try:
                with open(lock_path, "r", encoding="utf-8") as rf:
                    holder = rf.read().strip() or "?"
            except OSError:
                pass
            raise WatchError(
                f"another redhand watch appears active on this root (lock held by pid {holder}); "
                "only one watcher per root is supported"
            ) from None
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        return fh

    # ---------------------------------------------------------- recording

    def _record(
        self,
        change: _scanner.DetectedChange,
        ts: float,
        run_id: int,
        hint: Optional[tuple],
    ) -> int:
        actor, confidence = hint if hint else (_actors.ACTOR_MANUAL, _actors.CONF_LOW)
        self.jj.get_or_create_actor(actor)
        return self.jj.record_change(
            ts=ts,
            run_id=run_id,
            path=change.path,
            event_type=change.event,
            actor=actor,
            confidence=confidence,
            old_size=change.old_size,
            new_size=change.new_size,
            old_hash=change.old_hash,
            new_hash=change.new_hash,
            mtime_ns=change.new_mtime_ns,
            pre_blob_id=change.pre_blob_id,
            new_content=change.new_content,
            blob_cap_total=self.cfg.blob_total,
            protected_hit=bool(self.cfg.protect.matches_path(change.path)),
        )

    # ---------------------------------------------------------- protect enforcement

    def _undo_blob(self, event: Optional[dict]) -> Optional[bytes]:
        """Pre-change content of an event; JournalError (corrupt blob) -> None."""
        if not event:
            return None
        try:
            return self.jj.get_blob(event["blob_id"])
        except JournalError:
            return None

    def _enforce_revert(
        self, change: _scanner.DetectedChange, event_id: int, ts: float, run_id: int
    ) -> bool:
        """Undo a change to a protected path, recording the revert as its own event."""
        self.jj.get_or_create_actor(_actors.ACTOR_SELF)
        abs_path = self._abs(change.path)
        row = self.jj.shadow_row(change.path)
        try:
            if change.event == _scanner.EVENT_CREATED:
                # the file did not exist in the shadow -> remove it again
                if os.path.lexists(abs_path):
                    os.remove(abs_path)
                pre_blob = row["blob_id"] if row else None
                self.jj.record_change(
                    ts=ts, run_id=run_id, path=change.path,
                    event_type=_scanner.EVENT_DELETED,
                    actor=_actors.ACTOR_SELF, confidence=_actors.CONF_HIGH,
                    old_size=change.new_size, new_size=None,
                    old_hash=change.new_hash, new_hash=None,
                    mtime_ns=None, pre_blob_id=pre_blob, new_content=None,
                    blob_cap_total=self.cfg.blob_total,
                    prev_event=event_id, restored_flag=True,
                )
                return True

            if change.event == _scanner.EVENT_DELETED:
                # the shadow row is already gone; the pre-deletion content
                # lives in the deletion event's blob
                event = self.jj.event_by_id(event_id)
                content = self._undo_blob(event)
                if content is None:
                    return False
                os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
                write_file_nofollow(abs_path, content)
                st = os.lstat(abs_path)
                self.jj.record_change(
                    ts=ts, run_id=run_id, path=change.path,
                    event_type=_scanner.EVENT_CREATED,
                    actor=_actors.ACTOR_SELF, confidence=_actors.CONF_HIGH,
                    old_size=None, new_size=len(content),
                    old_hash=None, new_hash=change.old_hash,
                    mtime_ns=st.st_mtime_ns, pre_blob_id=None,
                    new_content=content, blob_cap_total=self.cfg.blob_total,
                    prev_event=event_id, restored_flag=True,
                )
                return True

            # modified: put the pre-change content (referenced by the event) back
            event = self.jj.event_by_id(event_id)
            content = self._undo_blob(event)
            if content is None:
                return False
            write_file_nofollow(abs_path, content)
            st = os.lstat(abs_path)
            pre_blob = row["blob_id"] if row else None
            self.jj.record_change(
                ts=ts, run_id=run_id, path=change.path,
                event_type=_scanner.EVENT_MODIFIED,
                actor=_actors.ACTOR_SELF, confidence=_actors.CONF_HIGH,
                old_size=change.new_size, new_size=len(content),
                old_hash=change.new_hash, new_hash=change.old_hash,
                mtime_ns=st.st_mtime_ns, pre_blob_id=pre_blob,
                new_content=content, blob_cap_total=self.cfg.blob_total,
                prev_event=event_id, restored_flag=True,
            )
            return True
        except OSError:
            return False

    # ---------------------------------------------------------- passes

    def _initial_pass(self) -> bool:
        """Seed the shadow exactly once per root (tracked in journal meta).

        A shadow with zero rows can be a *legitimately empty* root, so the
        seeded-once marker must not be derived from the row count — otherwise
        every restart on an empty dir would silently absorb new files as
        'baseline' instead of recording 'created' events.
        """
        if self.jj.get_meta("seeded"):
            return False
        self._p(self.pal.dim("first run: scanning and seeding shadow…"))
        t0 = time.time()
        fs_entries = _scanner.scan_tree(self.root, self.cfg.ignore)
        entries = []
        for path, e in fs_entries.items():
            # specials (FIFOs etc.) and symlinked non-regular targets are never
            # opened: hashing one would block forever
            sha = None if e.special else safe_hash(self._abs(path), self.cfg.hash_limit)
            entries.append({"path": path, "size": e.size, "mtime_ns": e.mtime_ns, "sha256": sha})
        entries.sort(key=lambda x: x["path"])

        def content_of(p: str) -> Optional[bytes]:
            # _read_content gates on the *target* file (type + size) before reading
            return self._read_content(self._abs(p))

        stats = self.jj.seed_shadow(entries, self.cfg.blob_total, content_of)
        self.jj._set_meta("seeded", "1")
        self.jj.conn.commit()
        dt = time.time() - t0
        skipped = human_size(stats["skipped_bytes"]) if stats["skipped_bytes"] else "0 B"
        self._p(
            self.pal.dim(
                "seeded {} files in {:.1f}s ({} restorable, {} hash-only, {} over cap)".format(
                    stats["files"], dt, stats["shadowed"], stats["hash_only"], skipped
                )
            )
        )
        return True

    def _diff_pass(self, run_id: int) -> List[_scanner.DetectedChange]:
        fs_entries = _scanner.scan_tree(self.root, self.cfg.ignore)
        shadow_rows = self.jj.shadow_all()
        changes, touches = _scanner.diff_against_shadow(
            self.root,
            fs_entries,
            shadow_rows,
            hash_limit=self.cfg.hash_limit,
            shadowable=self._shadowable,
            read_content=self._read_content,
        )
        try:
            for t in touches:
                self.jj.shadow_touch(t.path, t.new_size or 0, t.new_mtime_ns or 0, None, commit=False)
            self.jj.conn.commit()
        except (OSError, sqlite3.Error) as exc:
            self._p(self.pal.red(f"touch refresh failed: {exc}"))
        self.n_touches += len(touches)
        if not changes:
            return []

        try:
            agents = _actors.agents_running(self.cfg.agents, exclude_pid=os.getpid())
        except Exception as exc:  # attribution must never kill the watcher
            self._p(self.pal.red(f"attribution unavailable: {exc}"))
            agents = []
        attr = _actors.attribute(self.root, [c.path for c in changes], agents)
        ts = time.time()
        for change in changes:
            try:
                if self._already_recorded(change):
                    continue  # another redhand process recorded it while we scanned
                self._process_change(change, ts, run_id, attr.get(change.path))
            except (sqlite3.Error, JournalError) as exc:
                self._p(self.pal.red(f"journal error on {change.path}: {exc}"))
        return changes

    def _already_recorded(self, change: _scanner.DetectedChange) -> bool:
        """Dedup guard against a concurrent redhand process (e.g. a restore
        from another terminal landing mid-scan): re-check the shadow row right
        before recording."""
        fresh = self.jj.shadow_row(change.path)
        if change.event == _scanner.EVENT_DELETED:
            return fresh is None  # row already dropped -> deletion recorded
        if fresh is None:
            return False
        if change.new_hash and fresh["sha256"]:
            return fresh["sha256"] == change.new_hash
        return False

    def _process_change(
        self, change: _scanner.DetectedChange, ts: float, run_id: int, hint: Optional[tuple]
    ) -> None:
        event_id = self._record(change, ts, run_id, hint)
        self.n_events += 1
        actor, confidence = hint if hint else (_actors.ACTOR_MANUAL, _actors.CONF_LOW)
        self.by_actor[actor] = self.by_actor.get(actor, 0) + 1
        self._print_change(change, ts, actor, confidence)
        if self.cfg.protect.matches_path(change.path):
            self.n_protected += 1
            suffix = " — enforcing…" if self.enforce else ""
            self._p(
                f"  {self.pal.red('⚠ protected path:')} {self.pal.bold(change.path)}"
                f" ({actor}){suffix}"
            )
            if self.enforce:
                if self._enforce_revert(change, event_id, time.time(), run_id):
                    self.n_reverted += 1
                    self.jj.mark_restored(event_id)
                    self._p(f"  {self.pal.green('✓ reverted')} {change.path}")
                else:
                    self._p(
                        f"  {self.pal.red('✗ could not revert')} {change.path} "
                        f"{self.pal.dim('(no restorable content)')}"
                    )

    def _print_change(self, change: _scanner.DetectedChange, ts: float, actor: str, confidence: str) -> None:
        color_name = EVENT_COLOR.get(change.event, "yellow")
        verb = self.pal.bold(getattr(self.pal, color_name)(f"{change.event:>8}"))
        delta = ""
        if (
            change.event == _scanner.EVENT_MODIFIED
            and change.old_size is not None
            and change.new_size is not None
            and change.old_size != change.new_size
        ):
            d = change.new_size - change.old_size
            delta = self.pal.dim(f" ({'+' if d >= 0 else ''}{human_size(d)})")
        mark = CONFIDENCE_MARK.get(confidence, "·")
        self._p(f"{fmt_clock(ts)}  {verb}  {change.path}{delta}  {self.pal.dim(actor + ' ' + mark)}")

    # ---------------------------------------------------------- main loop

    def run(self) -> WatchSummary:
        started = time.time()
        seeded = False
        lock_fh = self._acquire_lock()
        prev_handlers = {}
        for sig_name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                prev_handlers[sig] = signal.signal(sig, self._sigint_handler)
            except (OSError, ValueError):
                pass  # e.g. not in the main thread
        run_id = self.jj.add_run(started, self.interval, note="once" if self.once else "watch")
        try:
            self._p(
                f"{self.pal.bold('redhand')} watching {self.pal.cyan(self.root)}  "
                + self.pal.dim(f"(interval {self.interval:g}s, ctrl-c to stop)")
            )
            seeded = self._initial_pass()
            while not self._stop:
                self._sleep()
                if self._stop:
                    break
                try:
                    self._diff_pass(run_id)
                except (OSError, sqlite3.Error, JournalError) as exc:
                    self._p(self.pal.red(f"scan error: {exc}"))
                if self.once:
                    break
        finally:
            for sig, prev in prev_handlers.items():
                try:
                    signal.signal(sig, prev)
                except (OSError, ValueError):
                    pass
            self.jj.end_run(run_id)
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except (OSError, NameError, UnboundLocalError):
                pass
        return WatchSummary(
            run_id=run_id,
            started_at=started,
            ended_at=time.time(),
            seeded=seeded,
            events=self.n_events,
            touches=self.n_touches,
            protected_events=self.n_protected,
            reverted=self.n_reverted,
            by_actor=dict(self.by_actor),
        )

    def _sleep(self) -> None:
        end = time.time() + self.interval
        while not self._stop and time.time() < end:
            time.sleep(min(0.2, max(0.0, end - time.time())))

    def _sigint_handler(self, signum, frame) -> None:
        self._stop = True

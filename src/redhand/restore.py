"""Restore logic.

Uniform semantic: *restore(event) puts the file back to how it was just
before that event happened.*

* created  -> the file did not exist: remove it (if still present)
* modified -> write back the pre-change content (event's blob)
* deleted  -> recreate with the pre-deletion content (event's blob)

Every restore is itself recorded as an ordinary change (actor 'redhand'),
which keeps the undo chain intact: undoing the restore-event redoes the
original change.

Writes go through `write_file_nofollow`: a symlink planted over the path
between journaling and restore is replaced by a regular file instead of
redirecting journal content to whatever the link points at. Files that are
not shadow-tracked cannot be restored and are reported as such.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Optional

from . import actors as _actors
from . import scanner as _scanner
from .journal import Journal, JournalError
from .util import human_size, write_file_nofollow


class RestoreError(RuntimeError):
    pass


@dataclass
class RestoreOutcome:
    event_id: int            # the source event that was restored
    new_event_id: Optional[int]
    path: str
    action: str              # 'removed' | 'restored-content' | 'recreated'
    bytes_written: int
    dry_run: bool


def restorable_events(journal: Journal, limit: int = 50) -> List[dict]:
    return journal.events(restorable_only=True, order="DESC", limit=limit)


def restore_event(
    journal: Journal,
    root: str,
    event_id: int,
    dry_run: bool = False,
    run_id: Optional[int] = None,
    blob_cap_total: int = 0,
) -> RestoreOutcome:
    event = journal.event_by_id(int(event_id))
    if event is None:
        raise RestoreError(f"no such event: #{event_id}")
    journal.get_or_create_actor(_actors.ACTOR_SELF)
    path = event["path"]
    abs_path = os.path.join(os.path.abspath(root), path.replace("/", os.sep))
    etype = event["event"]

    if etype == _scanner.EVENT_CREATED:
        # restore to before it existed -> remove
        exists = os.path.lexists(abs_path)
        if dry_run:
            return RestoreOutcome(event["id"], None, path, "removed" if exists else "nothing", 0, dry_run=True)
        if exists:
            # the file may have changed since creation; record ITS state
            try:
                cur_size = os.lstat(abs_path).st_size
            except OSError:
                cur_size = None
            row = journal.shadow_row(path)
            pre_blob = row["blob_id"] if row else None
            try:
                os.remove(abs_path)
            except OSError as exc:
                raise RestoreError(f"cannot remove {path}: {exc}") from None
            new_id = journal.record_change(
                ts=_now(), run_id=run_id, path=path,
                event_type=_scanner.EVENT_DELETED,
                actor=_actors.ACTOR_SELF, confidence=_actors.CONF_HIGH,
                old_size=cur_size, new_size=None,
                old_hash=row["sha256"] if row else None, new_hash=None,
                mtime_ns=None, pre_blob_id=pre_blob, new_content=None,
                blob_cap_total=blob_cap_total,
                prev_event=event["id"], restored_flag=True,
            )
            journal.mark_restored(event["id"])
            return RestoreOutcome(event["id"], new_id, path, "removed", 0, dry_run=False)
        return RestoreOutcome(event["id"], None, path, "nothing", 0, dry_run=False)

    # modified / deleted: need the pre-change content
    try:
        content = journal.get_blob(event["blob_id"])
    except JournalError as exc:
        raise RestoreError(str(exc)) from None
    if content is None:
        raise RestoreError(
            f"event #{event['id']} ({etype} {path}) has no restorable content "
            "(file was not text/within limits, or the blob was evicted)"
        )
    action = "restored-content" if etype == _scanner.EVENT_MODIFIED else "recreated"
    if dry_run:
        return RestoreOutcome(event["id"], None, path, action, len(content), dry_run=True)

    existed = os.path.lexists(abs_path)
    row = journal.shadow_row(path)
    pre_blob = row["blob_id"] if row else None
    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
    try:
        write_file_nofollow(abs_path, content)
    except OSError as exc:
        raise RestoreError(f"cannot write {path}: {exc}") from None
    st = os.lstat(abs_path)
    new_id = journal.record_change(
        ts=_now(), run_id=run_id, path=path,
        event_type=_scanner.EVENT_MODIFIED if existed else _scanner.EVENT_CREATED,
        actor=_actors.ACTOR_SELF, confidence=_actors.CONF_HIGH,
        old_size=event["new_size"] if existed else None,
        new_size=len(content),
        old_hash=event["new_hash"] if existed else None,
        new_hash=event["old_hash"],
        mtime_ns=st.st_mtime_ns,
        pre_blob_id=pre_blob,
        new_content=content,
        blob_cap_total=blob_cap_total,
        prev_event=event["id"],
        restored_flag=True,
    )
    journal.mark_restored(event["id"])
    return RestoreOutcome(event["id"], new_id, path, action, len(content), dry_run=False)


def undo_latest(
    journal: Journal,
    root: str,
    path: str,
    dry_run: bool = False,
    run_id: Optional[int] = None,
    blob_cap_total: int = 0,
) -> RestoreOutcome:
    """Undo the most recent recorded change of `path`."""
    rows = journal.events(path=path, order="DESC", limit=1)
    if not rows:
        raise RestoreError(f"no recorded events for {path}")
    return restore_event(journal, root, rows[0]["id"], dry_run=dry_run,
                         run_id=run_id, blob_cap_total=blob_cap_total)


def describe_outcome(out: RestoreOutcome) -> str:
    head = "[DRY RUN] " if out.dry_run else ""
    detail = {
        "removed": "file removed",
        "recreated": f"recreated ({human_size(out.bytes_written)})",
        "restored-content": f"content restored ({human_size(out.bytes_written)})",
        "nothing": "nothing to do (already in restored state)",
    }[out.action]
    return f"{head}event #{out.event_id} → {out.path}: {detail}"


def _now() -> float:
    return time.time()

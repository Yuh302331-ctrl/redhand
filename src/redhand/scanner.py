"""Directory scanning and shadow-based change detection.

The filesystem scan collects (size, mtime_ns) for every tracked file —
cheap, no content reads. `diff_against_shadow` then compares against the
shadow table (the last known state) and only touches file *content* for
candidates whose size/mtime actually moved:

* content hash equal to the shadow's  -> silent mtime refresh (no event)
* content hash different              -> 'modified' event
* file absent from disk               -> 'deleted' event
* file absent from shadow             -> 'created' event

Files above the hash limit are tracked by size+mtime only; for those, a
size change counts as modified and an mtime-only change is treated as a
touch (cannot be verified without reading — see README limitations).
"""

from __future__ import annotations

import os
import stat as _stat
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .util import PatternSet, safe_hash, sha256_bytes

EVENT_CREATED = "created"
EVENT_MODIFIED = "modified"
EVENT_DELETED = "deleted"

EVENTS = (EVENT_CREATED, EVENT_MODIFIED, EVENT_DELETED)


def _is_junction(path: str) -> bool:
    """Windows directory junctions are reparse points that ntpath.islink
    does not report; descending them can recurse without bound."""
    if os.name != "nt":
        return False
    if hasattr(os.path, "isjunction"):
        try:
            return os.path.isjunction(path)
        except OSError:
            return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0)
                & getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


@dataclass
class FsEntry:
    size: int
    mtime_ns: int
    special: bool = False   # FIFO/socket/device: recorded, never opened or hashed


@dataclass
class DetectedChange:
    path: str                      # '/'-separated, relative to root
    event: str                     # created | modified | deleted
    old_size: Optional[int] = None
    new_size: Optional[int] = None
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    new_mtime_ns: Optional[int] = None
    pre_blob_id: Optional[int] = None   # shadow blob holding the pre-change state
    new_content: Optional[bytes] = None  # post-change content (shadowable files only)
    is_touch: bool = False              # mtime-only refresh, not an event


def scan_tree(root: str, ignore: Optional[PatternSet] = None) -> Dict[str, FsEntry]:
    """Collect size/mtime for regular files (and symlinks recorded as entries).

    Symlinked directories are never followed (cycle-safe). Unreadable
    entries are skipped. Returns {relative_path: FsEntry}.
    """
    entries: Dict[str, FsEntry] = {}
    ignore = ignore or PatternSet()
    root = os.path.abspath(root)

    def on_error(_err: OSError) -> None:  # pragma: no cover - best effort walk
        return None

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

        keep = []
        for d in sorted(dirnames):
            rel_child = f"{rel_dir}/{d}" if rel_dir else d
            full = os.path.join(dirpath, d)
            if os.path.islink(full) or _is_junction(full):
                # record the link/junction itself as an entry; never descend
                # (junctions are Windows reparse points ntpath.islink misses —
                # descending them can walk cycles unboundedly)
                try:
                    st = os.lstat(full)
                    entries[rel_child] = FsEntry(size=st.st_size, mtime_ns=st.st_mtime_ns)
                except OSError:
                    pass
                continue
            if ignore.matches_component(d) or ignore.matches_path(rel_child, is_dir=True):
                continue
            keep.append(d)
        dirnames[:] = keep

        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = os.path.join(dirpath, name)
            if ignore.matches_path(rel):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            # FIFOs/sockets/devices: lstat is safe, but OPENING them can block
            # forever or read hardware state — record metadata, never read.
            special = not (_stat.S_ISREG(st.st_mode) or _stat.S_ISLNK(st.st_mode))
            entries[rel] = FsEntry(size=st.st_size, mtime_ns=st.st_mtime_ns, special=special)
    return entries


def diff_against_shadow(
    root: str,
    fs_entries: Dict[str, FsEntry],
    shadow_rows: List[Dict],
    hash_limit: int,
    shadowable: Callable[[str, int], bool],
    read_content: Callable[[str], Optional[bytes]],
) -> Tuple[List[DetectedChange], List[DetectedChange]]:
    """Compare filesystem state against shadow rows.

    Returns (changes, touches):
      changes — created/modified/deleted records ready for the journal
                (content already read for shadowable files)
      touches — silent mtime/size refreshes (no event)
    """
    root_abs = os.path.abspath(root)
    old_by_path = {r["path"]: r for r in shadow_rows}
    changes: List[DetectedChange] = []
    touches: List[DetectedChange] = []

    for path in sorted(set(fs_entries) | set(old_by_path)):
        fs = fs_entries.get(path)
        old = old_by_path.get(path)
        abs_path = os.path.join(root_abs, path.replace("/", os.sep))

        if fs is not None and old is None:
            change = DetectedChange(path=path, event=EVENT_CREATED, new_size=fs.size, new_mtime_ns=fs.mtime_ns)
            if not fs.special:
                content = read_content(abs_path) if shadowable(path, fs.size) else None
                if content is not None:
                    change.new_hash = sha256_bytes(content)
                    change.new_content = content
            changes.append(change)

        elif fs is None and old is not None:
            changes.append(
                DetectedChange(
                    path=path,
                    event=EVENT_DELETED,
                    old_size=int(old["size"] or 0),
                    old_hash=old["sha256"],
                    pre_blob_id=old["blob_id"],
                )
            )

        elif fs is not None and old is not None:
            same_meta = fs.size == int(old["size"] or 0) and fs.mtime_ns == int(old["mtime_ns"] or 0)
            if same_meta:
                continue
            if fs.special:
                # never open specials; treat any metadata move as modified
                changes.append(
                    DetectedChange(
                        path=path,
                        event=EVENT_MODIFIED,
                        old_size=int(old["size"] or 0),
                        new_size=fs.size,
                        new_mtime_ns=fs.mtime_ns,
                    )
                )
            elif old["sha256"]:
                if fs.size > hash_limit:
                    # the file grew beyond the hash limit after seeding:
                    # record the size change now (content unverifiable), and
                    # the shadow's sha becomes NULL so tracking switches to
                    # size-only from here on — never silently skip it
                    changes.append(
                        DetectedChange(
                            path=path,
                            event=EVENT_MODIFIED,
                            old_size=int(old["size"] or 0),
                            new_size=fs.size,
                            old_hash=old["sha256"],
                            new_mtime_ns=fs.mtime_ns,
                            pre_blob_id=old["blob_id"],
                        )
                    )
                    continue
                # safe_hash resolves symlinks to their target and refuses
                # non-regular targets instead of opening them
                digest = safe_hash(abs_path, hash_limit)
                if digest is None:
                    # transient read failure (EACCES) or the file vanished
                    # between scan and hash: skip this cycle — recording now
                    # would write sha256=NULL into the shadow and permanently
                    # downgrade the file to hash-only tracking. A vanished
                    # file surfaces as 'deleted' on the next poll.
                    continue
                if digest == old["sha256"]:
                    touches.append(
                        DetectedChange(
                            path=path,
                            event=EVENT_MODIFIED,
                            is_touch=True,
                            old_size=fs.size,
                            new_size=fs.size,
                            new_mtime_ns=fs.mtime_ns,
                        )
                    )
                    continue
                change = DetectedChange(
                    path=path,
                    event=EVENT_MODIFIED,
                    old_size=int(old["size"] or 0),
                    new_size=fs.size,
                    old_hash=old["sha256"],
                    new_hash=digest,
                    new_mtime_ns=fs.mtime_ns,
                    pre_blob_id=old["blob_id"],
                )
                content = read_content(abs_path) if shadowable(path, fs.size) else None
                if content is not None:
                    change.new_hash = sha256_bytes(content)
                    change.new_content = content
                changes.append(change)
            else:
                # hash-only tracking: only size changes are detectable events
                if fs.size != int(old["size"] or 0):
                    changes.append(
                        DetectedChange(
                            path=path,
                            event=EVENT_MODIFIED,
                            old_size=int(old["size"] or 0),
                            new_size=fs.size,
                            new_mtime_ns=fs.mtime_ns,
                        )
                    )
                else:
                    touches.append(
                        DetectedChange(
                            path=path,
                            event=EVENT_MODIFIED,
                            is_touch=True,
                            new_size=fs.size,
                            new_mtime_ns=fs.mtime_ns,
                        )
                    )
    return changes, touches

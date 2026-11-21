"""Test helpers: seed journal+filesystem states and write files."""

from __future__ import annotations

import os
import time


def write(path, content: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def seed_file(jj, root, rel_path: str, content: str, actor="manual", confidence="low") -> int:
    """Create a file on disk AND record its 'created' event (like a first watch would)."""
    abs_path = os.path.join(root, rel_path)
    write(abs_path, content)
    data = content.encode("utf-8")
    st = os.lstat(abs_path)
    return jj.record_change(
        ts=time.time(),
        run_id=None,
        path=rel_path.replace(os.sep, "/"),
        event_type="created",
        actor=actor,
        confidence=confidence,
        old_size=None,
        new_size=len(data),
        old_hash=None,
        new_hash=None,
        mtime_ns=st.st_mtime_ns,
        pre_blob_id=None,
        new_content=data,
        blob_cap_total=1024 * 1024,
    )

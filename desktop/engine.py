"""Small JSON bridge between the native desktop app and the two CLI engines."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from agenttrueup.journal_source import JournalSource
from agenttrueup.scan import scan
from redhand.config import DB_NAME, load_config
from redhand.journal import Journal
from redhand.restore import restore_event
from redhand.util import PatternSet
from redhand.watch import Watcher


PREVIEW_LIMIT = 256 * 1024


def journal_for(root: Path, state: Path, *, read_only: bool = False) -> Journal:
    return Journal(str(root), state_dir=str(state), read_only=read_only)


def record(root: Path, state: Path, *, once: bool, stop_event: Event | None = None) -> None:
    cfg = load_config(str(root))
    if state.is_relative_to(root) and state != root:
        cfg.ignore = PatternSet([*cfg.ignore.patterns, f"/{state.relative_to(root).as_posix()}"])
    with journal_for(root, state) as journal, open(os.devnull, "w") as sink:
        Watcher(str(root), cfg, journal, out=sink, once=once, stop_event=stop_event).run()


def list_changes(root: Path, state: Path) -> dict:
    if not (state / DB_NAME).exists():
        return {"events": [], "ready": False}
    with journal_for(root, state, read_only=True) as journal:
        rows = journal.events(order="DESC", limit=300)
        return {"ready": bool(journal.get_meta("seeded")), "events": [
            {"id": row["id"], "path": row["path"], "event": row["event"],
             "ts": row["ts"], "restored": bool(row["restored"]),
             "isRestore": row["actor"] == "redhand" and row["prev_event"] is not None}
            for row in rows
        ]}


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError("这条路径包含链接或已移到文件夹外。")
    return path


def read_current(path: Path) -> bytes | None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > PREVIEW_LIMIT:
            raise ValueError("当前文件无法预览。仅支持 256 KB 以内的普通文本文件。")
        data = stream.read(PREVIEW_LIMIT + 1)
        if len(data) > PREVIEW_LIMIT or b"\x00" in data:
            raise ValueError("当前文件无法作为文本预览。")
        return data


def preview(root: Path, journal: Journal, event_id: int) -> dict:
    event = journal.event_by_id(event_id)
    if not event:
        raise ValueError("找不到这条改动。")
    path = safe_path(root, event["path"])
    if event["event"] not in {"modified", "deleted"}:
        return {"path": event["path"], "message": "这是新增文件，没有改动前的版本。"}
    previous = journal.get_blob(event["blob_id"])
    if previous is None or len(previous) > PREVIEW_LIMIT:
        return {"path": event["path"], "message": "这条记录没有可预览的旧文本。"}
    current = read_current(path)
    try:
        before = previous.decode("utf-8")
        after = current.decode("utf-8") if current is not None else None
    except UnicodeDecodeError:
        return {"path": event["path"], "message": "这条记录包含无法预览的文件格式。"}
    token = hashlib.sha256(current).hexdigest() if current is not None else "missing"
    return {"path": event["path"], "before": before, "current": after, "token": token}


def restore(root: Path, state: Path, event_id: int, token: str) -> dict:
    cfg = load_config(str(root))
    with journal_for(root, state) as journal:
        current = preview(root, journal, event_id)
        if current.get("token") != token:
            raise ValueError("文件在预览后发生了变化。请重新查看再恢复。")
        outcome = restore_event(journal, str(root), event_id, blob_cap_total=cfg.blob_total,
                                hash_limit=cfg.hash_limit)
        return {"path": outcome.path, "new_event_id": outcome.new_event_id}


def audit(root: Path, state: Path, transcript: Path) -> dict:
    db = state / DB_NAME
    source = JournalSource(str(root), str(db)) if db.exists() else None
    result = scan(paths=[str(transcript)], only_paths=True, journal_source=source,
                  use_journal_sources=source is not None, include_local_settings=False)
    return {"claims": [{"subject": c.subject, "kind": c.kind, "status": c.status,
                        "detail": c.detail} for c in result.claims[:50]],
            "findings": [{"severity": f.severity, "title": f.title, "detail": f.detail}
                         for f in result.findings[:50]],
            "sessions": result.stats["sessions"], "warnings": result.stats["warnings"]}


def demo(base: Path) -> dict:
    parent = base / "demos" / uuid.uuid4().hex
    root = parent / "演示文件夹"
    root.mkdir(parents=True, mode=0o700)
    state = parent / "records"
    path = root / "周末旅行.txt"
    path.write_text("周末旅行计划\n周六上午出发\n滑雪场门票预算：200 元\n", encoding="utf-8")
    record(root, state, once=True)
    started = datetime.now(timezone.utc).isoformat()
    path.write_text("周末旅行计划\n周日下午出发\n滑雪场门票预算：950 元\n", encoding="utf-8")
    record(root, state, once=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    transcript = parent / "demo.jsonl"
    rows = [
        ("user", started, "Please review the plan."),
        ("assistant", timestamp, "I deleted `周末旅行.txt`."),
    ]
    with transcript.open("w", encoding="utf-8") as stream:
        for role, stamp, message in rows:
            stream.write(json.dumps({"type": role, "sessionId": "demo", "cwd": str(root),
                                     "timestamp": stamp,
                                     "message": {"role": role, "content": message}},
                                    ensure_ascii=False) + "\n")
    return {"root": str(root), "state": str(state), "transcript": str(transcript),
            **list_changes(root, state)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["watch", "changes", "preview", "restore", "audit", "demo"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--event", type=int)
    parser.add_argument("--token")
    parser.add_argument("--transcript", type=Path)
    args = parser.parse_args()
    root = args.path.resolve()
    try:
        if args.action == "demo":
            result = demo(root)
        else:
            state = args.state
            if args.action == "watch":
                record(root, state, once=False)
                result = {}
            elif args.action == "changes":
                result = list_changes(root, state)
            elif args.action == "preview":
                with journal_for(root, state, read_only=True) as journal:
                    result = preview(root, journal, args.event)
            elif args.action == "restore":
                result = restore(root, state, args.event, args.token)
            else:
                result = audit(root, state, args.transcript)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

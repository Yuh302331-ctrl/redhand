"""Command-line interface.

Exit codes: 0 ok · 1 usage/config error · 2 runtime error.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Optional, Sequence

from . import __version__
from . import actors as _actors
from . import scanner as _scanner
from .config import (
    ConfigError,
    INITIAL_CONFIG_TEMPLATE,
    STATE_DIR,
    config_path,
    load_config,
)
from .digest import build_digest
from .journal import Journal
from .restore import (
    RestoreError,
    describe_outcome,
    restorable_events,
    restore_event,
    undo_latest,
)
from .util import Palette, fmt_ts, human_size, parse_since
from .watch import WatchError, Watcher, session_summary_line

_warned_unknown: set = set()


def _warn_unknown(cfg) -> None:
    keys = cfg.raw.get("_unknown_keys") if isinstance(cfg.raw, dict) else None
    if keys:
        mark = id(keys)
        if mark not in _warned_unknown:
            _warned_unknown.add(mark)
            print(f"note: unknown config keys ignored: {', '.join(keys)}", file=sys.stderr)


def _open(root: str) -> Journal:
    return Journal(root)


# ---------------------------------------------------------------- commands


def cmd_init(args, cfg) -> int:
    path = config_path(args.root)
    if os.path.exists(path):
        print(f"config already exists: {path}")
        print("edit it to change ignore rules, agent signatures, or protect patterns.")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(INITIAL_CONFIG_TEMPLATE, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {path}")
    print("next:")
    print(f"  cd {args.root} && redhand watch          # start journaling")
    print("  redhand protect add secrets/ *.key       # guard sensitive paths")
    return 0


def cmd_watch(args, cfg) -> int:
    pal = Palette(enabled=not args.no_color and sys.stdout.isatty())
    jj = _open(args.root)
    try:
        watcher = Watcher(
            args.root,
            cfg,
            jj,
            palette=pal,
            once=args.once,
            enforce=args.enforce,
            interval=args.interval,
        )
        summary = watcher.run()
        print(session_summary_line(summary, pal))
        return 0
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return 130
    finally:
        jj.close()


def cmd_digest(args, cfg) -> int:
    since = parse_since(args.since) if args.since else None
    jj = _open(args.root)
    try:
        text = build_digest(
            jj,
            args.root,
            since=since,
            actor=args.actor,
            timeline_limit=cfg.digest_timeline,
            per_actor_limit=cfg.digest_per_actor,
        )
    finally:
        jj.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_log(args, cfg) -> int:
    since = parse_since(args.since) if args.since else None
    jj = _open(args.root)
    try:
        rows = jj.events(
            since=since,
            actor=args.actor,
            path=args.path,
            event_type=args.event,
            limit=args.limit,
            order="DESC",
        )
    finally:
        jj.close()
    if not rows:
        print("no events recorded" + (f" (actor {args.actor})" if args.actor else "") + ".")
        return 0
    for e in rows:
        size = e["new_size"] if e["event"] != "deleted" else e["old_size"]
        mark = {"high": "✓", "medium": "?", "low": "·"}.get(e["confidence"], "·")
        restorable = "R" if e["blob_id"] else "-"
        flags = ("P" if e["protected_hit"] else "") + ("r" if e["restored"] else "")
        print(
            f"#{e['id']:<7} {fmt_ts(e['ts'])}  {e['event']:>8}  {e['path']}"
            f"  {human_size(size) if size is not None else '—':>9}  "
            f"{e['actor']} {mark}  [{restorable}{flags}]"
        )
    print(f"\n({len(rows)} events; R=restorable, P=protected path, r=already restored)")
    return 0


def cmd_restore(args, cfg) -> int:
    jj = _open(args.root)
    run_id = None
    try:
        if not args.list:
            # standalone restores get their own run row for the audit trail
            run_id = jj.add_run(time.time(), 0.0, note="restore")
        if args.list:
            rows = restorable_events(jj, limit=args.limit)
            if not rows:
                print("no restorable events.")
                return 0
            for e in rows:
                print(f"#{e['id']:<7} {fmt_ts(e['ts'])}  {e['event']:>8}  {e['path']}  ({e['actor']})")
            print("\nrestore with: redhand restore <id>")
            return 0
        if args.path:
            out = undo_latest(jj, args.root, args.path, dry_run=args.dry_run,
                              run_id=run_id, blob_cap_total=cfg.blob_total)
        elif args.event_id is not None:
            out = restore_event(jj, args.root, args.event_id, dry_run=args.dry_run,
                                run_id=run_id, blob_cap_total=cfg.blob_total)
        else:
            print("nothing to do: pass an event id, --path, or --list (see --help)", file=sys.stderr)
            return 1
        print(describe_outcome(out))
        return 0
    except RestoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if run_id is not None:
            try:
                jj.end_run(run_id)
            except sqlite3.Error:
                pass
        jj.close()


def _edit_config(root: str, mutate) -> dict:
    path = config_path(root)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # never overwrite a config we cannot parse: the user's custom
            # agents/ignore/limits would be silently lost
            raise ConfigError(f"{path} is invalid ({exc}); fix it before editing") from None
    else:
        data = dict(INITIAL_CONFIG_TEMPLATE)
    mutate(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return data


def cmd_protect(args, cfg) -> int:
    action = args.action

    if action is None or action == "list":
        patterns = list(cfg.protect.patterns)
        if patterns:
            print("protected patterns:")
            for p in patterns:
                print(f"  {p}")
        else:
            print("no protected patterns. add some:")
            print("  redhand protect add secrets/ *.key .env")
        return 0

    def mutate(data: dict) -> None:
        cur = list(data.get("protect") or [])
        if action == "add":
            for p in args.patterns:
                if p not in cur:
                    cur.append(p)
        elif action == "remove":
            cur = [p for p in cur if p not in args.patterns]
        data["protect"] = cur

    try:
        data = _edit_config(args.root, mutate)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    print(f"protect patterns: {data['protect'] or '(none)'}")
    print("changes apply on the next watch start.")
    return 0


def cmd_status(args, cfg) -> int:
    jj = _open(args.root)
    try:
        st = jj.stats()
        last = jj.last_run()
    finally:
        jj.close()
    print(f"root          {os.path.abspath(args.root)}")
    print(f"state         {STATE_DIR}/ (journal.db, config.json, watch.lock)")
    print(f"interval      {cfg.interval:.1f}s")
    print(f"agents        {len(cfg.agents)} signatures")
    print(f"protect       {list(cfg.protect.patterns) or '(none)'}")
    print(
        f"tracked       {st['files']} files ({st['shadowed']} restorable, "
        f"{st['files'] - st['shadowed']} hash-only), {human_size(st['bytes'])} on disk"
    )
    print(
        f"events        {st['events']} total"
        + (f", first {fmt_ts(st['first_ts'])}" if st["first_ts"] else "")
        + (f", last {fmt_ts(st['last_ts'])}" if st["last_ts"] else "")
    )
    print(f"storage       {human_size(st['db_bytes'])} db · {human_size(st['blob_bytes'])} content")
    if last:
        ended = f" → ended {fmt_ts(last['ended_at'])}" if last.get("ended_at") else " → running?"
        print(f"last watch    pid {last['pid']} started {fmt_ts(last['started_at'])}{ended}")
    return 0


def cmd_actors(args, cfg) -> int:
    agents = _actors.agents_running(cfg.agents, exclude_pid=os.getpid())
    if not agents:
        print("no AI agent processes detected right now.")
        print("signatures checked: " + ", ".join(cfg.agents))
        return 0
    print(f"{len(agents)} agent-like process(es):")
    for a in agents:
        print(f"  [{a.name}] {a.describe()}")
    return 0


def cmd_forget(args, cfg) -> int:
    before = parse_since(args.before)
    if not args.yes:
        answer = input(f"delete all events before {fmt_ts(before)}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 0
    jj = _open(args.root)
    try:
        counts = jj.prune(before)
        jj.vacuum()
        size = jj.db_size()
    finally:
        jj.close()
    print(
        f"pruned {counts['events']} events, {counts['blobs']} blobs, "
        f"{counts['actors']} actors, {counts['runs']} runs; db now {human_size(size)}"
    )
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    # common flags accepted both before and after the subcommand;
    # SUPPRESS keeps the top-level value when the flag is absent after the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="watched root (default: current directory)")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="disable ANSI colors")

    p = argparse.ArgumentParser(
        prog="redhand",
        description="Catch file changes red-handed: a cross-agent change journal for your folders.",
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"redhand {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sp = sub.add_parser("init", parents=[common], help="write a starter config into <root>/.whodunit/")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("watch", parents=[common], help="journal every change as it happens")
    sp.add_argument("--interval", type=float, default=None, help="poll seconds (default from config)")
    sp.add_argument("--once", action="store_true", help="single diff pass, then exit")
    sp.add_argument("--enforce", action="store_true", help="auto-revert changes to protected paths")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("digest", parents=[common], help="markdown report: who changed what")
    sp.add_argument("--since", default=None, help="window start ('2h', '7d', ISO, epoch)")
    sp.add_argument("--actor", default=None, help="filter by actor")
    sp.add_argument("--out", default=None, help="write markdown to file instead of stdout")
    sp.set_defaults(func=cmd_digest)

    sp = sub.add_parser("log", parents=[common], help="recent events, newest first")
    sp.add_argument("--since", default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--actor", default=None)
    sp.add_argument("--path", default=None)
    sp.add_argument("--event", default=None, choices=list(_scanner.EVENTS))
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("restore", parents=[common], help="put a file back to its pre-event state")
    sp.add_argument("event_id", nargs="?", type=int, default=None)
    sp.add_argument("--path", default=None, help="undo the latest recorded change of this path")
    sp.add_argument("--list", action="store_true", help="list restorable events")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("protect", parents=[common], help="list/add/remove protected path patterns")
    sp.add_argument("action", nargs="?", default=None, choices=["list", "add", "remove"])
    sp.add_argument("patterns", nargs="*", default=[])
    sp.set_defaults(func=cmd_protect)

    sp = sub.add_parser("status", parents=[common], help="root, tracking and storage overview")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("actors", parents=[common], help="probe for running AI agent processes")
    sp.set_defaults(func=cmd_actors)

    sp = sub.add_parser("forget", parents=[common], help="prune the journal before a time point")
    sp.add_argument("--before", required=True, help="prune events older than this ('30d', ISO, epoch)")
    sp.add_argument("--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(func=cmd_forget)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    # defaults live here (not in set_defaults) because subparser flag actions
    # are shared objects whose SUPPRESS defaults must not be clobbered
    if not hasattr(args, "root"):
        args.root = "."
    if not hasattr(args, "no_color"):
        args.no_color = False
    args.root = os.path.abspath(args.root)
    if not os.path.isdir(args.root):
        print(f"error: root is not a directory: {args.root}", file=sys.stderr)
        return 1
    try:
        cfg = load_config(args.root)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    _warn_unknown(cfg)
    try:
        return args.func(args, cfg)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\ninterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""Markdown digest: 'who changed what' for a time window."""

from __future__ import annotations

import os
import time
from typing import Optional

from .journal import Journal
from .util import fmt_ts, human_size


def build_digest(
    journal: Journal,
    root: str,
    since: Optional[float] = None,
    until: Optional[float] = None,
    actor: Optional[str] = None,
    timeline_limit: int = 100,
    per_actor_limit: int = 30,
    now: Optional[float] = None,
) -> str:
    now = now if now is not None else time.time()
    events = journal.events(since=since, until=until, actor=actor, order="ASC")
    counts = journal.counts(since=since, until=until, actor=actor)

    out: list = []
    root_disp = os.path.abspath(root)
    out.append(f"# redhand digest — {root_disp}")
    window = _window_text(since, until, now)
    out.append("")
    out.append(f"*Window:* {window}  ")
    out.append(f"*Generated:* {fmt_ts(now)} · {len(events)} events")
    out.append("")

    if not events:
        out.append(f"> No recorded changes in this window.{'' if actor is None else f' (actor filter: {actor})'}")
        out.append("")
        return "\n".join(out)

    # ---- summary table
    out.append("## Summary")
    out.append("")
    out.append("| actor | conf | created | modified | deleted | protected | first | last |")
    out.append("|---|---|---:|---:|---:|---:|---|---|")
    agg: dict = {}
    for c in counts:
        key = (c["actor"], c["confidence"])
        a = agg.setdefault(key, {"created": 0, "modified": 0, "deleted": 0, "protected": 0,
                                 "first_ts": c["first_ts"], "last_ts": c["last_ts"]})
        a[c["event"]] = a.get(c["event"], 0) + c["n"]
        a["protected"] += c["protected_n"] or 0
        a["first_ts"] = min(a["first_ts"], c["first_ts"])
        a["last_ts"] = max(a["last_ts"], c["last_ts"])

    def _event_total(a: dict) -> int:
        return a["created"] + a["modified"] + a["deleted"]

    for (name, conf), a in sorted(agg.items(), key=lambda kv: (-_event_total(kv[1]), kv[0][0])):
        out.append(
            f"| {name} | {conf} | {a['created']} | {a['modified']} | {a['deleted']} "
            f"| {a['protected'] or ''} | {fmt_ts(a['first_ts'])} | {fmt_ts(a['last_ts'])} |"
        )
    out.append("")

    # ---- protected alerts
    protected = [e for e in events if e["protected_hit"]]
    if protected:
        out.append("## ⚠ Protected paths")
        out.append("")
        for e in protected[:20]:
            out.append(
                f"- `{e['path']}` — {e['event']} by **{e['actor']}** at {fmt_ts(e['ts'])}"
                + (" *(auto-reverted)*" if e["restored"] else " *(not reverted)*")
            )
        if len(protected) > 20:
            out.append(f"- … and {len(protected) - 20} more")
        out.append("")

    # ---- timeline
    out.append("## Timeline")
    out.append("")
    show = events[-timeline_limit:] if len(events) > timeline_limit else events
    if len(events) > timeline_limit:
        out.append(f"*showing last {timeline_limit} of {len(events)}*")
        out.append("")
    for e in show:
        size = e["new_size"] if e["event"] != "deleted" else e["old_size"]
        size_txt = human_size(size) if size is not None else "—"
        mark = {"high": "✓", "medium": "?", "low": "·"}.get(e["confidence"], "·")
        restored = " *(restored)*" if e["restored"] else ""
        out.append(
            f"- `{fmt_ts(e['ts'])}` **{e['event']}** `{e['path']}` ({size_txt}) — {e['actor']} {mark}{restored}"
        )
    out.append("")

    # ---- per-actor files
    out.append("## Files by actor")
    out.append("")
    by_file: dict = {}
    for e in events:
        by_file.setdefault(e["actor"], {})
        slot = by_file[e["actor"]].setdefault(e["path"], {"n": 0})
        slot["n"] += 1
    for name in sorted(by_file, key=lambda n: -sum(s["n"] for s in by_file[n].values())):
        files = by_file[name]
        total = sum(s["n"] for s in files.values())
        out.append(f"### {name} — {total} events across {len(files)} files")
        out.append("")
        top = sorted(files.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:per_actor_limit]
        for path, slot in top:
            out.append(f"- `{path}` ×{slot['n']}")
        if len(files) > per_actor_limit:
            out.append(f"- … and {len(files) - per_actor_limit} more files")
        out.append("")

    # ---- restore hints
    restorable = journal.events(since=since, until=until, restorable_only=True, order="DESC", limit=5)
    n_restorable = sum(1 for e in events if e["blob_id"])
    out.append("## Restore")
    out.append("")
    if n_restorable:
        out.append(f"{n_restorable} events in this window carry restorable content. Examples:")
        out.append("")
        out.append("```")
        for e in restorable:
            out.append(f"redhand restore {e['id']}          # put {e['path']} back to pre-{e['event']} state")
        out.append("```")
        out.append("")
        out.append("`redhand restore --path <path>` rolls back the latest recorded change of a file.")
    else:
        out.append("No restorable content in this window (binary/too-large files are tracked by hash only).")
    out.append("")

    stats = journal.stats()
    out.append("---")
    out.append(
        f"*Journal:* {stats['events']} events total · {human_size(stats['db_bytes'])} db · "
        f"{human_size(stats['blob_bytes'])} content · {stats['shadowed']}/{stats['files']} files restorable"
    )
    return "\n".join(out)


def _window_text(since: Optional[float], until: Optional[float], now: float) -> str:
    if since is None and until is None:
        return "all recorded history"
    left = fmt_ts(since) if since else "beginning"
    right = fmt_ts(until) if until else fmt_ts(now)
    return f"{left} → {right}"

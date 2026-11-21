# The redhand journal — schema contract for external tools

The journal is a plain SQLite database at `<root>/.whodunit/journal.db`,
written with WAL mode. It is the recorded facts of every file change
redhand observed. Any tool may read it read-only:

```python
sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
```

Never write to it while a watcher may be running (one writer is enforced by
redhand; readers are always safe — WAL allows concurrent readers, and an
open read connection never blocks the watcher).

## Tables

| table | meaning |
|---|---|
| `events` | the audit ledger: one row per recorded change |
| `shadow` | last known state per tracked file (diff baseline) |
| `blobs` | zlib-compressed content snapshots (restore targets) |
| `runs` | watcher sessions (started/ended, pid, interval) |
| `actors` | normalized actor names + kind (`agent` / `human` / `tool`) |
| `meta` | `schema_version` = `'2'` as of 0.2.0 |

## `events` columns

| column | meaning |
|---|---|
| `id` | monotonically increasing event id |
| `ts` | unix epoch (float seconds) when redhand recorded it |
| `run_id` | watcher session that recorded it (NULL for standalone restores) |
| `path` | relative to the watched root, **always `/`-separated** |
| `event` | `created` \| `modified` \| `deleted` |
| `new_size` / `old_size` | post/pre-change size in bytes (NULL = unknown/not applicable) |
| `new_hash` / `old_hash` | sha256 when the file was hashable at scan time (NULL for large/binary/special) |
| `actor` | attribution: agent signature name, `unverified-agent`, `manual`, or `redhand` |
| `confidence` | `high` (cwd-verified) · `medium` (agent running, unverified) · `low` (no agents) |
| `blob_id` | the state **before** this event (restore target); NULL = not restorable |
| `prev_event` | for revert/undo events: the event being undone |
| `protected_hit` | 1 when the path matched a protect pattern |
| `restored` | 1 when this event's effect was later reverted |

Restore semantic (for consumers): `events.blob_id` holds the content as it
was *just before* the event — writing it back undoes the event. Undo chains
compose: restoring a revert-event re-applies the original change.

## Contract notes

- `meta.schema_version` — refuse unknown major versions politely; `2` is the
  only guaranteed one today.
- Paths are always normalized to `/` regardless of platform.
- Blobs may be evicted under the storage cap (`blob_total_mb`); `blob_id`
  can go NULL over time (the ledger row remains).
- Timestamps are recording time, not the instant of the change: the watcher
  polls (`interval`, default 2 s), so an event's `ts` may trail the actual
  filesystem mutation by up to one poll window.
- `agenttrueup` consumes this contract in its
  [`journal_source.py`](https://github.com/Yuh302331-ctrl/agenttrueup)
  module — use it as the reference reader.

# Engineering notes & optimization ideas

Living document: records design rationale, known trade-offs, and ideas not yet
built. The README covers usage; this file covers *why it is built this way*
and *where it could go*.

## Design rationale

### Shadow-based diffing (not inotify/fsevents)

v0.1 polls with `os.scandir`-backed walks (default 2 s) and diffs against a
SQLite *shadow* table (last known size/mtime/hash per file). Reasons:

- **Restart-safe**: the baseline lives in the DB, so a watcher that was down
  for an hour still catches everything that changed (one event, final state).
- **Restore-integration**: the shadow doubles as the "current known content"
  store, which is what makes deletions restorable and makes restores from a
  second terminal invisible to the running watcher (fresh-shadow check).
- **Zero deps**: FSEvents needs ctypes bindings or a C extension; polling with
  `os.scandir` (which returns stat data from the directory entry for free) is
  fast enough for project-sized trees (a 10k-file tree scans in ~50 ms on an
  M-series Mac).

Trade-off: sub-second changes collapse to their final state within one window.
The event-stream backend (below) is the planned answer.

### Content stored once, addressed by SHA-256

Every state is one blob; identical content is stored once (undo/redo cycles
reuse blobs). Eviction is FIFO over blobs *not referenced by the shadow* —
the shadow is never evicted, so current states always survive and only the
oldest undo checkpoints age out. Eviction is feasibility-checked *before*
deleting anything: a refused store never destroys history.

### Attribution is correlation, stated honestly

ps snapshot → word-boundary signature match → cwd resolution → ancestry check
against the changed path. `high` = cwd-verified; `medium` = agent running but
unverified; `low` = no agents. We deliberately do NOT claim forensic proof —
an agent running at `$HOME` makes shell edits inside it look like its work.
Fixing that properly needs write-time interception (Endpoint Security
framework / eBPF), which is out of scope for a zero-dependency tool.

## Performance notes (measured)

- 7-file toy tree: full seed < 100 ms; diff pass ~10 ms.
- 10k-file tree (extrapolated): scan ~50–80 ms per tick at 2 s interval ≈ 3–4%
  of one core — acceptable; hashing is the dominant cost and is skipped for
  unchanged files (size/mtime gate).
- SQLite: WAL mode; every record is one short transaction; CLI restore while
  a watcher runs is safe (busy timeout 10 s).

## Optimization backlog (ordered by value/effort)

1. **FSEvents/inotify backend** (high value, medium effort): event-driven
   trigger for the diff pass — keep the poll engine as a fallback and as the
   diff mechanism itself (events only *trigger* a scan; the shadow diff stays
   the source of truth). Removes the collapse-to-final-state limitation.
2. **Skip-hash fast path** (medium): persist dir mtimes to skip unchanged
   subtrees entirely; on macOS use `getattrlistbulk` for batched dirent+stat.
3. **Parallel hashing** (low): hash changed files across a small thread pool
   on big ticks. Only matters for trees with heavy churn.
4. **Incremental digest rendering** (low): digest currently loads all events
   in the window; paginate for pathological windows (100k+ events).
5. **SQLite page size + `PRAGMA synchronous=NORMAL`** (trivial): likely 2×
   write throughput; measure before adopting.

## Compatibility plan

- **Python**: 3.9–3.13 (CI matrix). No stdlib APIs newer than 3.9; `Z`-suffix
  ISO timestamps translated manually because `fromisoformat` gained `Z`
  support only in 3.11.
- **OS**: macOS first (author's platform), Linux CI-equivalent. `lsof` for cwd
  with `/proc/<pid>/cwd` fallback; `O_NOFOLLOW` guarded with `getattr(os, ...)`
  since it is POSIX-only. Windows would need: a watcher without fcntl
  (msvcrt.locking), attribution without ps/lsof (WMI/psutil — a dependency, so
  a separate optional extra), and path-separator audits. Not planned for 0.x.
- **Editors/agents**: signatures are data, not code — new agents are a
  `config.json` edit, no release needed.

## Security notes

- Restores and enforced reverts write with `O_NOFOLLOW`; a symlink planted
  over a path is *replaced*, never written through.
- The journal DB is plain SQLite inside the watched tree (`.whodunit/`); it is
  in the default ignore list so redhand never journals itself. Anyone with
  filesystem access can read it — treat `.whodunit/` as sensitive if your
  tracked files are secrets.
- `--enforce` on a protected path **auto-deletes created files**. Only enable
  it on roots where "everything new in `protect` patterns is hostile" is a
  safe assumption.

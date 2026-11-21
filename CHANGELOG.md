# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).


## [Unreleased]

### Fixed
- **Data loss on `restore` while `watch` was running (Windows/any OS)**:
  restoring `--path` right after a file changed — before the watcher recorded
  it — picked up the stale `created` event and, following the "restore to
  before it existed" semantics, **deleted the file** along with the unrecorded
  content. `restore_event`/`undo_latest` now run a freshness check (disk
  size/mtime/sha256 vs the shadow row) and refuse with
  `RestoreError` ("…unrecorded changes… use --force") when they disagree.

### Changed
- `restore` is now a **dry-run by default**: pass `--yes` to actually write
  to disk (applies to the `created`-undo file removal as well).
- New `restore --force` flag skips the freshness check for intentional
  overrides (e.g. restoring a deliberately-planted symlink in tests).
- `actors._run` decodes process-probe output with `errors="replace"`, so
  non-UTF-8 `tasklist` output (CJK Windows) no longer kills the probe thread
  and spams the watcher's stderr.

### Tests
- `test_restore_while_watch_running` polls the journal (≤5 s) instead of
  sleeping, waits for the watcher's seeding pass via a canary file, passes
  `--yes`, and no longer depends on the developer's machine state; a new
  regression test covers the refuse-on-unrecorded-changes path with the
  default 2 s interval.

## [0.2.0] - 2026-11-21

### Added
- **Windows support**: journal, restore, digest and protect run unchanged.
  Process snapshots use `Get-CimInstance Win32_Process` (name + full command
  line; the 20 agent signatures work as-is). Windows exposes no public API for
  another process's working directory, so attribution caps at `medium`
  (unverified-agent) there — `high` requires cwd verification and stays
  macOS/Linux-only. No agent running -> `low`/manual, same as before.
- Windows one-watcher lock via `msvcrt.locking` (POSIX keeps `flock`).
- ANSI colors enabled for classic Windows consoles (VT sequence enable);
  Windows Terminal unaffected.
- CI matrix extended to windows-latest (Python 3.9 and 3.13 boundary
  versions); README documents a schtasks background setup for Windows.

## [0.1.0] - 2026-11-21

First release.

### Added
- `redhand watch` — poll-scan watcher that journals every created/modified/deleted
  file relative to a shadow baseline (survives restarts; one watcher per root via
  flock).
- Cross-agent attribution: detects AI coding agents (claude, codex, cursor, aider,
  gemini, copilot, … 20 signatures, configurable) via process table + working
  directory verification; honest three-level confidence (high/medium/low).
- Restorable snapshots: text files within per-file and total caps are stored
  (zlib-compressed) with a uniform restore semantic — *restore(event) puts the
  file back to how it was just before that event*. Undo chains work: undoing a
  restore-event redoes the change.
- `redhand protect` — path patterns that alert on every change; `watch --enforce`
  auto-reverts protected changes (created→removed, deleted→recreated,
  modified→restored).
- `redhand digest` — markdown report: summary table, protected-path alerts,
  timeline, files-by-actor, restore hints.
- `redhand log`, `redhand restore`, `redhand status`, `redhand actors`,
  `redhand forget` (prune + vacuum), `redhand init`.
- Storage discipline: per-file blob cap (256 KB default), global cap with FIFO
  eviction (64 MB default), shadow states never evicted, `forget` for retention.
- Zero runtime dependencies (stdlib only); Python 3.9–3.13; macOS + Linux.
- Hardened edge cases: FIFOs/sockets are never opened; symlink content gates
  on the target's real size; restores use O_NOFOLLOW (planted symlinks are
  replaced, not written through); zero-length files participate in undo
  chains; refused stores never evict history (no evict-then-bail); blob
  dedup by SHA-256; SIGTERM/SIGHUP graceful shutdown; sqlite busy-timeout.
- Test suite (119 tests, 90% coverage) and CI across the full matrix.

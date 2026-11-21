# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
- Test suite (114 tests, 90% coverage) and CI across the full matrix.

# Acceptance report — redhand 0.1.0

2026-11-21. This file summarizes the quality gates the project passed before
being handed over. Every claim below was produced by an actual run on the
authoring machine (macOS, Python 3.9.6).

## Automated gates

| gate | result |
|---|---|
| Test suite | **119 tests, all passing** (`pytest tests/`) |
| Coverage | 90% total; lowest modules are error-path-heavy (watch 86%, util 88%) |
| Static lint | `flake8 src/ tests/` — 0 findings (E501 in test data lines excluded) |
| CI matrix | GitHub Actions: 3.9–3.13 × ubuntu/macos (runs on push) |
| Packaging | `pip install .` verified in a clean venv; console script `redhand` works |
| Dependencies | **zero runtime dependencies** (stdlib only) |

## Independent review rounds

1. **Adversarial review #1** (fresh-eyes agent, empirical reproduction): 15
   findings — FIFO hang (P1), symlink→large-file OOM (P1), digest actor filter
   (P1), evict-then-bail, transient-read downgrade, empty-file undo chain,
   restore write-through-symlink, config clobbering, sqlite robustness, blob
   dedup, restore metadata, interval clamp, corrupt blob crash, docstring
   overclaim, actor kinds. **All 15 fixed.**
2. **Verification review #2** (same agent class, probe-driven): verified all
   15 fix sites, found 3 residuals — symlink→FIFO/device hang (both hash
   paths), uncaught JournalError in `--enforce`, digest restore examples
   ignoring the actor filter — plus one vacuous test. All fixed; the vacuous
   dedup-guard test was initially rewritten into the wrong file (a silent
   no-op that was reported as done — caught and corrected by review #3).
3. **Review #3** (third round, post-hardening): verified the earlier fixes,
   found one real P1 regression (files growing beyond `hash_limit` were
   silently never journaled after the safe_hash swap), FIFO/hang and Windows
   gaps (junction descent, write-through, backslash path queries), and the
   still-vacuous dedup test — all fixed; the dedup race now has a
   deterministic, self-verifying test (guard-disabled variant asserted).

## Real-scenario verification (all live runs)

- watch → modify/create/delete → attribution to a *real* running agent
  process (codex, cwd-verified) → digest correct
- protected-path tamper with `--enforce`: auto-reverted, byte-identical content
- CLI restore (`--path`): exact pre-change content back; undo chain round-trips
- FIFO and symlinks in the watched tree: no hang, never opened (the exact
  scenario review #2 reproduced as blocking now passes)
- cross-process restore while a watcher runs: no double-record, no crash
- SIGINT/SIGTERM/SIGHUP: graceful shutdown with session summary
- **60 s soak**: 162 events captured, RSS growth 240 KB total, 196 KB db

## Known limitations (documented, by design)

See README "Known limitations": poll-window collapse, hash-only tracking for
large/binary files, mtime-only undetectability for large files, one watcher
per root, correlation-not-forensics attribution.

## Real-machine integration (2026-11-22, macOS)

Live end-to-end run of the pair on one machine:

1. `redhand watch --enforce` on a scratch project (config: protect `*.key`);
2. real changes land: `report.md` modified twice, `api.key` tampered;
3. the watcher journalled everything — the `api.key` tamper was **reverted by
   protect enforcement** (event flags `[RPr]`: protected, restored);
4. a transcript claiming "I deleted report.md" (a lie — the file was modified,
   never deleted) was audited:
   - **without** the journal: LOW, hedged — "claimed deleted but still exists
     (usually harmless)";
   - **with** the journal: **HIGH — "claim is contradicted by the redhand
     event stream … journal records modified as the last event around the
     claim (actor codex, timestamp)"**.

Platform honesty: this live run was macOS only. The Windows port is
CI-verified (unit suite green on windows-latest × 3.11–3.13) but has **no
real-Windows-machine session verification** — specifically no attribution
sample (cwd resolution is unavailable there by design) and no journal fixture
from a real Windows watcher. That gap is documented rather than papered over.

## Repo state at handover

- 3 commits on `main` (0.1.0 + hardening + docs), clean tree
- ~740 KB total, text only; `.whodunit/` journals never committed
- bilingual docs (README.md / README.zh-CN.md), CHANGELOG, MIT LICENSE,
  docs/OPTIMIZATION.md (design rationale + optimization backlog)

# redhand

[![CI](https://github.com/Yuh302331-ctrl/redhand/actions/workflows/ci.yml/badge.svg)](https://github.com/Yuh302331-ctrl/redhand/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/redhand)](https://pypi.org/project/redhand/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Catch file changes red-handed.** A cross-agent change journal for your folders:
who changed this file, when, what exactly — and put it back if you don't like it.

[中文文档](README.zh-CN.md)

---

## The problem

You run AI coding agents — Claude Code, Codex, Cursor, Aider, Gemini CLI… — often
several at once, sometimes in the same folder. Then one day:

- a file you didn't touch is different. **Who changed it?** The agent's own log
  only covers its own session, `git log` only covers committed work, and plenty
  of folders you care about (notes, sync dirs, exports, dotfiles) aren't git
  repos at all.
- an agent rewrote a file you care about. **How do you put it back?** Time
  Machine is hourly at best; `git checkout` needs a commit that never existed.

redhand answers both questions at the folder level, for every actor, continuously.

## What it does

```console
$ redhand watch
redhand watching /tmp/rhdemo  (interval 2s, ctrl-c to stop)
first run: scanning and seeding shadow…
seeded 4 files in 0.0s (4 restorable, 0 hash-only, 0 B over cap)
17:24:10   modified  app.py (+7 B)  codex ✓
17:24:10    created  feature.py  codex ✓
17:24:10   modified  secrets/key.txt (-2 B)  codex ✓
  ⚠ protected path: secrets/key.txt (codex) — enforcing…
  ✓ reverted secrets/key.txt
17:24:12    deleted  feature.py  codex ✓

session: 5s · 12 events · 1 touches · 1 protected · 1 reverted
```

- **Journal** — every created/modified/deleted file is recorded relative to a
  *shadow* (the last known state), so detection survives restarts and nothing is
  missed between sessions.
- **Attribution** — changes are attributed to the AI agent (or human) that made
  them, by watching the process table and verifying working directories. Three
  honest confidence levels (see [How attribution works](#how-attribution-works)).
- **Restore** — text files up to the size cap are snapshotted. One semantic:
  `restore <event>` puts the file back to *just before* that event happened.
  Undo chains work — undoing a restore-event redoes the change. Restore is a
  dry-run by default (`--yes` writes); a freshness check refuses to touch a
  file whose current state the journal hasn't recorded yet (`--force` overrides).
- **Protect** — patterns like `secrets/` or `*.key`; every change there is
  flagged, and `watch --enforce` reverts it automatically.
- **Digest** — `redhand digest` renders a markdown report of who did what.
- **Storage discipline** — zlib-compressed snapshots, content-addressed (identical
  bytes stored once), per-file and global caps with FIFO eviction, `forget` to
  prune. A typical project journal is tens of KB.

Zero runtime dependencies. Python 3.9+. macOS, Linux and Windows.

## Install

```console
$ pipx install "redhand @ git+https://github.com/Yuh302331-ctrl/redhand.git"
$ uv tool install "redhand @ git+https://github.com/Yuh302331-ctrl/redhand.git"
$ pip install "redhand @ git+https://github.com/Yuh302331-ctrl/redhand.git"
```
(Not on PyPI yet — the name is reserved for this project's next release.)

## Quickstart

```console
$ cd your-project
$ redhand init                # writes .whodunit/config.json
$ redhand watch               # start journaling (ctrl-c to stop)

$ redhand log --limit 20      # what happened recently
$ redhand digest --since 2h   # markdown report, who changed what
$ redhand restore --list      # what can be put back
$ redhand restore 42          # put file back to pre-event-42 state
$ redhand restore --yes --path src/app.py    # undo the latest change (--yes writes)

$ redhand protect add secrets/ *.key .env
$ redhand watch --enforce     # auto-revert changes to protected paths

$ redhand actors              # which agents are running right now
$ redhand status              # tracking + storage overview
$ redhand forget --before 30d # prune the journal
```

## How attribution works

On every tick that has changes, redhand snapshots the process table and matches
your configured agent signatures (20 built in, configurable in `config.json`)
against process names and command lines. It then resolves each matching
process's working directory (`lsof` everywhere on POSIX, with a
`/proc/<pid>/cwd` fallback on Linux when lsof is unavailable)
and checks whether that directory covers the changed file. On Windows,
working directories of other processes are not readable through any public
API, so every agent match caps at `medium` — attribution is a live
"who is running" signal, never a guess at which one wrote the file:

| confidence | meaning |
|---|---|
| `high` ✓ | an agent's working directory is the watched root or an ancestor of the changed file |
| `medium` ? | agent-like processes are running, but none covers this path — attributed to `unverified-agent` |
| `low` · | no agent processes at change time — attributed to `manual` |

**This is correlation, not forensics.** A `high` means "an agent was verifiably
working in this tree at that moment", not cryptographic proof. If you run an
agent over the whole home directory, shell edits you make inside it will also
land near its cwd — treat `high` as "very likely this agent", and use the
digests to spot-check surprising attributions.

## What's restorable

Every event's undo target is kept as a compressed blob, subject to:

- **Per-file cap** — `blob_limit_kb` (default 256 KB): bigger files are tracked
  by hash and size only, not restorable.
- **Total cap** — `blob_total_mb` (default 64 MB): when exceeded, the *oldest*
  blobs are evicted first (FIFO). The shadow — the current state of every
  tracked file — is never evicted, so recent undo chains stay intact.
- **Text only** — files that don't decode as UTF-8 are hashed, not snapshotted.

Known limitations (by design, all visible in `status`):

- poll-based scanning (`interval`, default 2 s; activity-adaptive fast-follow
  narrows it to ~interval/6 during change bursts): two changes inside one
  window are seen as the final state only;
- **attribution on Windows caps at `medium`**: there is no supported way to
  read another process's working directory, so "who changed it" answers "some
  agent was running" there;
- same-size, same-mtime edits of hash-only (large) files are undetectable;
- binary files: no content restore, but their changes are still journalled;
- special files (FIFOs, sockets, devices) are journalled as metadata and never
  opened — opening a named pipe would block forever, so redhand does not;
- symlinks are tracked as the link itself; content snapshots gate on the
  *target's* real size, so a symlink to a huge file is never read into memory;
- one watcher per root (flock on POSIX, msvcrt.locking on Windows); a
  `restore` from a second terminal is
  safe — a fresh-shadow check right before recording prevents double entries.

## Commands

| command | purpose |
|---|---|
| `init` | write a starter `.whodunit/config.json` |
| `watch` | journal changes live (`--once`, `--interval S`, `--enforce`) |
| `digest` | markdown report (`--since 2h`, `--actor claude`, `--out file.md`) |
| `log` | recent events, newest first (`--since`, `--actor`, `--path`, `--event`) |
| `restore` | `--list` / `<event-id>` / `--path p` · dry-run by default, `--yes` writes, `--force` skips freshness check |
| `protect` | list / add / remove protected patterns |
| `status` | root, tracking and storage overview |
| `actors` | probe for running agent processes |
| `forget` | prune events before a time point (`--before 30d`, `--yes`) |

Global flags: `--root DIR` (anywhere in the command), `--no-color`, `--version`.

## Configuration

`.whodunit/config.json` (all keys optional):

```json
{
  "interval": 2.0,
  "ignore": [".git/", "node_modules/", "__pycache__/", ".venv/", "*.pyc", ".DS_Store"],
  "agents": ["claude", "codex", "cursor", "aider", "my-company-agent"],
  "protect": ["secrets/", "*.key", ".env"],
  "hash_limit_mb": 10,
  "blob_limit_kb": 256,
  "blob_total_mb": 64
}
```

Pattern rules: `name` matches any path component · `dir/` prunes a directory ·
`*.log` globs basename or full path · `/path` anchors to the watched root.

## Running in the background

macOS (launchd) — `~/Library/LaunchAgents/com.redhand.watch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.redhand.watch</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/redhand</string>
    <string>watch</string><string>--root</string><string>/Users/you/projects</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/redhand.log</string>
  <key>StandardErrorPath</key><string>/tmp/redhand.log</string>
</dict></plist>
```

```console
$ launchctl load ~/Library/LaunchAgents/com.redhand.watch.plist
```

Linux (systemd user unit) — `~/.config/systemd/user/redhand.service`:

```ini
[Unit]
Description=redhand watcher

[Service]
ExecStart=%h/.local/bin/redhand watch --root %h/projects
Restart=on-failure

[Install]
WantedBy=default.target
```

Windows (schtasks) — run once in an admin prompt:

```console
$ schtasks /create /tn "redhand watch" /sc onlogon /rl limited ^
  /tr \"%LOCALAPPDATA%\Programs\Python\Python313\Scripts\redhand.exe\" watch --root C:\Users\you\projects"
```

Gotcha: keep the log file **outside** the watched root, or redhand will happily
journal its own output.

## How is this different from…

- **git** — git tracks committed work in git repos; redhand tracks every change,
  including never-committed work, in folders that aren't repos at all. They
  complement each other.
- **fswatch / watchman** — event streams for scripts; redhand is a durable,
  queryable journal with attribution and restore on top.
- **Time Machine** — hourly system snapshots, minutes of granularity loss;
  redhand is per-change, per-file, with actor names, in a few KB.

## Development

```console
$ git clone https://github.com/Yuh302331-ctrl/redhand && cd redhand
$ pip install -e ".[dev]"
$ pytest tests/ --cov=redhand          # full suite, ~90% coverage
```

Layout: `src/redhand/` — `scanner.py` (walk + shadow diff) · `actors.py`
(process attribution) · `journal.py` (SQLite: shadow, events, blobs) ·
`watch.py` (engine) · `restore.py` · `digest.py` · `cli.py` · `config.py` ·
`util.py`. Design rationale and the optimization backlog live in
[docs/OPTIMIZATION.md](docs/OPTIMIZATION.md).

## Ecosystem

**trueup** ([agenttrueup](https://github.com/Yuh302331-ctrl/agenttrueup)) is
the audit-side companion: it reads this journal (read-only, by schema
contract — see [docs/JOURNAL.md](docs/JOURNAL.md)) and reconciles what AI
agents *claimed* against the recorded event stream. redhand is the camera;
trueup is the auditor.

## Roadmap

- fsevents/inotify backend for sub-second detection (poll engine stays as fallback)
- per-actor display-name rules (e.g. attribute by git author when in a repo)
- HTML digest with diffs
- ~~Windows support~~ — shipped in 0.2.0 (attribution caps at `medium`; see How attribution works)

## License

[MIT](LICENSE)

"""Tests for attribution: classification, cwd resolution, attribute()."""

from __future__ import annotations

import os
import sys

import pytest

from redhand import actors as A
from redhand.actors import (
    AgentProc,
    Proc,
    attribute,
    classify_agents,
    _is_under,
    _signature_regex,
)


def _proc(pid, comm, args=""):
    return Proc(pid=pid, comm=comm, args=args)


# ---------------------------------------------------------------- classification


def test_classify_by_comm():
    procs = [
        _proc(1, "/usr/sbin/syslogd"),
        _proc(2, "/Users/x/.claude/local/claude", "claude --resume"),
        _proc(3, "node", "/opt/codex/bin/codex exec"),
    ]
    found = classify_agents(procs, ["claude", "codex"], exclude_pid=None)
    names = {f.pid: f.name for f in found}
    assert names == {2: "claude", 3: "codex"}


def test_classify_word_boundaries():
    assert _signature_regex("claude").search("claude") is not None
    assert _signature_regex("claude").search("/opt/claude-code/bin/x") is not None  # '-' is a boundary
    assert _signature_regex("claude").search("claudeium") is None
    assert _signature_regex("codex").search("codexify") is None


def test_classify_args_match_and_exclusion():
    procs = [_proc(9, "python3", "/some/aider --model x"), _proc(10, "zsh", "vim notes.txt")]
    found = classify_agents(procs, ["aider"], exclude_pid=None)
    assert [f.name for f in found] == ["aider"]
    assert found[0].matched_in == "args"
    # self exclusion
    found = classify_agents([_proc(99, "claude", "claude")], ["claude"], exclude_pid=99)
    assert found == []


def test_classify_empty_and_unusable():
    assert classify_agents([], ["claude"]) == []
    procs = [_proc(5, "Terminal", "zsh")]
    assert classify_agents(procs, ["claude"]) == []


# ---------------------------------------------------------------- cwd resolution


@pytest.mark.skipif(sys.platform == "win32", reason="cwd resolution is POSIX-only (returns all-None on windows)")
def test_resolve_cwds_parses_lsof(monkeypatch):
    # real lsof -F0pcn shape: p<pid>\0c<cmd>\0n<path>\0 ...
    out = "p123\0cclaude\0n/Users/x/proj\0p456\0ccodex\0n/Users/y\0"
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=8: out if "lsof" in cmd else "")
    got = A.resolve_cwds([123, 456, 789])  # 789 not in lsof output
    assert got[123] == "/Users/x/proj"
    assert got[456] == "/Users/y"
    assert got[789] is None


def test_resolve_cwds_garbage_output(monkeypatch):
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=8: "not\0nso\0nparsable\0p999\0")
    got = A.resolve_cwds([1, 2, 999])
    assert got[999] is None  # pid field without path stays None, no crash


def test_resolve_cwds_proc_fallback(monkeypatch):
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=8: None)
    real_pid = os.getpid()  # /proc does not exist on macOS; just ensure no crash
    got = A.resolve_cwds([real_pid])
    assert real_pid in got


# ---------------------------------------------------------------- attribution


def _agent(pid, name, cwd):
    return AgentProc(pid=pid, name=name, comm=name, args=name, cwd=cwd)


def test_attribute_high_confidence_by_cwd():
    root = "/tmp/watchroot"
    agents = [_agent(1, "claude", root)]
    got = attribute(root, ["src/app.py", "docs/x.md"], agents)
    assert got["src/app.py"] == ("claude", "high")
    assert got["docs/x.md"] == ("claude", "high")


def test_attribute_agent_cwd_inside_watch(root):
    # agent working in a subdirectory of the watched root
    agents = [_agent(1, "codex", os.path.join(root, "src"))]
    got = attribute(root, ["src/app.py", "docs/x.md"], agents)
    assert got["src/app.py"] == ("codex", "high")
    assert got["docs/x.md"] == ("unverified-agent", "medium")


def test_attribute_most_specific_cwd_wins():
    root = "/tmp/watchroot"
    agents = [_agent(1, "claude", root), _agent(2, "codex", root + "/src")]
    got = attribute(root, ["src/app.py"], agents)
    assert got["src/app.py"] == ("codex", "high")  # deeper cwd is more specific


def test_attribute_medium_when_agents_running_elsewhere():
    root = "/tmp/watchroot"
    agents = [_agent(1, "aider", "/elsewhere")]
    got = attribute(root, ["f.txt"], agents)
    assert got["f.txt"] == ("unverified-agent", "medium")


def test_attribute_manual_when_no_agents():
    root = "/tmp/watchroot"
    got = attribute(root, ["f.txt"], [])
    assert got["f.txt"] == ("manual", "low")
    assert attribute(root, [], [_agent(1, "claude", "/x")]) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="pure-posix lexical paths")
def test_is_under():
    assert _is_under("/a/b/c", "/a/b")
    assert _is_under("/a/b", "/a/b")
    assert not _is_under("/a/bc", "/a/b")
    assert not _is_under("/a", "/a/b")
    assert not _is_under("/x", "/a")  # ValueError -> False


# ---------------------------------------------------------------- snapshot robustness


def test_snapshot_processes_runs():
    """Live ps snapshot: should return some processes on any normal system."""
    procs = A.snapshot_processes()
    assert len(procs) > 0
    assert all(isinstance(p.pid, int) for p in procs)
    # every line's pid present exactly once
    pids = [p.pid for p in procs]
    assert len(pids) == len(set(pids))


# ---------------------------------------------------------------- windows support

# lines as powershell Get-CimInstance emits them: pid\tname\tcommandline
_WIN_SAMPLE = (
    "500\tclaude.exe\tclaude --resume abc\r\n"
    "504\tCode.exe\t\"C:\\app\\Code.exe\" --open D:\\proj\r\n"
    "808\tsvchost.exe\t\r\n"
    "not-a-pid\tbroken.exe\t\r\n"
)


def _force_windows(monkeypatch):
    if not A._IS_WINDOWS:
        monkeypatch.setattr(A, "_IS_WINDOWS", True)


def test_windows_snapshot_parsing(monkeypatch):
    _force_windows(monkeypatch)
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=None: _WIN_SAMPLE)
    procs = A.snapshot_processes()
    by_pid = {p.pid: p for p in procs}
    assert 500 in by_pid and by_pid[500].comm == "claude.exe"
    assert by_pid[500].args.endswith("claude --resume abc")
    # cmdline may be empty (protected system processes) — still snapshotted
    assert 808 in by_pid and by_pid[808].args == "svchost.exe"
    # malformed lines are skipped
    assert all(isinstance(p.pid, int) for p in procs)
    assert len({p.pid for p in procs}) == len(procs)


def test_windows_signature_match_from_args(monkeypatch):
    _force_windows(monkeypatch)
    monkeypatch.setattr(A, "_run", lambda cmd, timeout=None: _WIN_SAMPLE)
    procs = A.snapshot_processes()
    agents = classify_agents(procs, ["claude", "codex"])
    names = {a.name for a in agents}
    assert names == {"claude"}  # Code.exe hosts codex-ish args must NOT match codex


def test_windows_cwds_all_none(monkeypatch):
    _force_windows(monkeypatch)
    assert A.resolve_cwds([500, 504]) == {500: None, 504: None}


def test_attribute_agent_without_cwd_is_medium():
    """The Windows ceiling: agent seen, cwd unknowable -> unverified-agent/medium.
    No agents at all -> manual/low."""
    agent_no_cwd = AgentProc(pid=5, name="claude", comm="claude", args="claude", cwd=None)
    got = attribute(os.path.sep + "root", ["a.txt"], [agent_no_cwd])
    assert got["a.txt"] == (A.ACTOR_UNVERIFIED, A.CONF_MEDIUM)
    got2 = attribute(os.path.sep + "root", ["a.txt"], [])
    assert got2["a.txt"] == (A.ACTOR_MANUAL, A.CONF_LOW)


def test_lock_backend_available():
    """At least one locking backend must exist on any supported platform."""
    from redhand import watch
    assert watch._HAVE_FCNTL or watch._HAVE_MSVCRT

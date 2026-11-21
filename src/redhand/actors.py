"""Attribution: which AI agent (or human) likely made a set of file changes.

Strategy, per change batch:
 1. Snapshot the process table (ps).
 2. Classify agent-like processes using configurable name signatures
    (word-boundary match on the process name and full command line).
 3. Resolve each agent's current working directory (lsof on macOS/Linux,
    /proc/<pid>/cwd on Linux as fallback).
 4. For each changed file: if an agent's cwd is the watched root or an
    ancestor of the file, attribute with HIGH confidence to that agent
    (most specific cwd wins). Otherwise, if any agent is running at all,
    attribute to 'unverified-agent' with MEDIUM confidence. With no agents
    running, attribute to 'manual' with LOW confidence.

Confidence is honest, not forensic: HIGH means "verified by working
directory at the moment of the change", not proof. See README.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

ACTOR_MANUAL = "manual"
ACTOR_UNVERIFIED = "unverified-agent"
ACTOR_SELF = "redhand"

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

_PS_TIMEOUT = 8
_LSOF_TIMEOUT = 8
_LSOF_CHUNK = 100


@dataclass
class Proc:
    pid: int
    comm: str = ""
    args: str = ""


@dataclass
class AgentProc:
    pid: int
    name: str          # matched signature, e.g. 'claude'
    comm: str
    args: str
    cwd: Optional[str] = None
    matched_in: str = "comm"  # 'comm' | 'args'

    def describe(self) -> str:
        cwd = self.cwd or "?"
        cmd = (self.args or self.comm).strip()
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"pid={self.pid} {self.name} cwd={cwd} cmd={cmd}"


def _run(cmd: Sequence[str], timeout: float = _PS_TIMEOUT) -> Optional[str]:
    try:
        out = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def snapshot_processes() -> List[Proc]:
    """pid/comm/args for all processes; comm and args fetched separately to
    survive spaces in executable paths. Returns [] if ps is unusable."""
    comm_out = _run(["ps", "-axo", "pid=,comm="])
    args_out = _run(["ps", "-axo", "pid=,args="])
    if comm_out is None or args_out is None:
        return []
    procs: Dict[int, Proc] = {}

    for line in comm_out.splitlines():
        line = line.strip()
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if m:
            procs[int(m.group(1))] = Proc(pid=int(m.group(1)), comm=m.group(2).strip())
    for line in args_out.splitlines():
        line = line.strip()
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if m:
            pid = int(m.group(1))
            if pid in procs:
                procs[pid].args = m.group(2).strip()
            else:
                procs[pid] = Proc(pid=pid, args=m.group(2).strip())
    return list(procs.values())


def _signature_regex(token: str) -> "re.Pattern[str]":
    """Word-boundary-ish match: 'claude' matches 'claude', '/opt/claude', 'claude-code'
    (the signature list contains variants explicitly), but not 'claudeium'."""
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token.lower()) + r"(?![A-Za-z0-9_])")


def classify_agents(
    procs: Sequence[Proc],
    signatures: Sequence[str],
    exclude_pid: Optional[int] = None,
) -> List[AgentProc]:
    sigs = [(s, _signature_regex(s)) for s in signatures]
    found: List[AgentProc] = []
    seen = set()
    for p in procs:
        if exclude_pid is not None and p.pid == exclude_pid:
            continue
        comm_l = p.comm.lower()
        args_l = p.args.lower()
        for name, rx in sigs:
            if rx.search(comm_l):
                key = (p.pid, name)
                if key not in seen:
                    seen.add(key)
                    found.append(AgentProc(pid=p.pid, name=name, comm=p.comm, args=p.args, matched_in="comm"))
                break
            if rx.search(args_l):
                # a signature inside the args (e.g. editor extension host) counts too
                key = (p.pid, name)
                if key not in seen:
                    seen.add(key)
                    found.append(AgentProc(pid=p.pid, name=name, comm=p.comm, args=p.args, matched_in="args"))
                break
    return found


def resolve_cwds(pids: Sequence[int]) -> Dict[int, Optional[str]]:
    """cwd per pid via lsof -F0pcn; /proc fallback on Linux; None when unknown."""
    result: Dict[int, Optional[str]] = {pid: None for pid in pids}
    pids = [p for p in pids if p > 0]
    if not pids:
        return result

    for i in range(0, len(pids), _LSOF_CHUNK):
        chunk = pids[i : i + _LSOF_CHUNK]
        out = _run(["lsof", "-a", "-d", "cwd", "-w", "-F0pcn", "-p", ",".join(str(p) for p in chunk)], _LSOF_TIMEOUT)
        if out:
            pid: Optional[int] = None
            for field in out.split("\0"):
                if not field:
                    continue
                tag, value = field[0], field[1:]
                if tag == "p":
                    try:
                        pid = int(value)
                    except ValueError:
                        pid = None
                elif tag == "n" and pid is not None:
                    result[pid] = value
        else:
            # lsof missing or failed: try /proc (Linux)
            for p in chunk:
                try:
                    result[p] = os.readlink(f"/proc/{p}/cwd")
                except OSError:
                    pass
    return result


def agents_running(signatures: Sequence[str], exclude_pid: Optional[int] = None) -> List[AgentProc]:
    """Convenience: snapshot + classify + resolve cwd."""
    procs = snapshot_processes()
    agents = classify_agents(procs, signatures, exclude_pid=exclude_pid)
    cwds = resolve_cwds([a.pid for a in agents])
    for a in agents:
        a.cwd = cwds.get(a.pid)
    return agents


def _is_under(path: str, directory: str) -> bool:
    """True if path is directory itself or inside it (lexically, both absolute)."""
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def attribute(
    root: str,
    paths: Sequence[str],
    agents: Sequence[AgentProc],
) -> Dict[str, Tuple[str, str]]:
    """Map relative path -> (actor, confidence) for one change batch.

    root must be absolute; paths are '/'-separated relative paths.
    """
    root_abs = os.path.abspath(root)
    result: Dict[str, Tuple[str, str]] = {}
    if not paths:
        return result

    # pre-resolve agent cwds that matter (inside/at the watched root)
    rooted: List[Tuple[int, str, str]] = []  # (len(cwd), name, cwd)
    for a in agents:
        if a.cwd:
            cwd_abs = os.path.abspath(a.cwd)
            if _is_under(root_abs, cwd_abs) or _is_under(cwd_abs, root_abs):
                rooted.append((len(cwd_abs), a.name, cwd_abs))
    rooted.sort(key=lambda t: t[0], reverse=True)  # most specific first

    any_agent = len(agents) > 0
    for rel in paths:
        abs_path = os.path.join(root_abs, rel.replace("/", os.sep))
        pick: Optional[Tuple[str, str]] = None
        for _, name, cwd_abs in rooted:
            if _is_under(abs_path, cwd_abs) or _is_under(cwd_abs, abs_path):
                pick = (name, CONF_HIGH)
                break
        if pick is None:
            if any_agent:
                pick = (ACTOR_UNVERIFIED, CONF_MEDIUM)
            else:
                pick = (ACTOR_MANUAL, CONF_LOW)
        result[rel] = pick
    return result

"""CLI tests: init, status, log, protect, restore, digest, forget, errors."""

from __future__ import annotations

import json
import os
import time

import pytest

from redhand.cli import main
from redhand.journal import Journal
from helpers import seed_file, write


def run(*args, root="."):
    return main(list(args) + ["--root", root])


# ---------------------------------------------------------------- init / status


def test_init_and_status(tmp_path):
    root = str(tmp_path / "r1")
    os.makedirs(root)
    assert run("init", root=root) == 0
    cfg_path = os.path.join(root, ".whodunit", "config.json")
    assert os.path.exists(cfg_path)
    data = json.load(open(cfg_path))
    assert "protect" in data and "interval" in data
    # idempotent
    assert run("init", root=root) == 0
    # status works after init
    assert run("status", root=root) == 0


def test_status_with_data(tmp_path):
    root = str(tmp_path / "r2")
    os.makedirs(root)
    jj = Journal(root)
    seed_file(jj, root, "a.txt", "data")
    jj.close()
    assert run("status", root=root) == 0


def test_bad_root(tmp_path):
    assert run("status", root=str(tmp_path / "missing")) == 1


def test_bad_config(tmp_path):
    root = str(tmp_path / "r3")
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        f.write("{oops")
    assert run("status", root=root) == 1


def test_unknown_config_key_note(tmp_path, capsys):
    root = str(tmp_path / "r4")
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        json.dump({"interval": 1.0, "frobnicate": True}, f)
    assert run("status", root=root) == 0
    assert "frobnicate" in capsys.readouterr().err


# ---------------------------------------------------------------- log / protect


def test_log_empty_and_rows(tmp_path, capsys):
    root = str(tmp_path / "r5")
    os.makedirs(root)
    assert run("log", root=root) == 0
    assert "no events" in capsys.readouterr().out

    jj = Journal(root)
    seed_file(jj, root, "x.txt", "abc")
    jj.close()
    assert run("log", root=root) == 0
    out = capsys.readouterr().out
    assert "created" in out and "x.txt" in out and "R=restorable" in out


def test_protect_roundtrip(tmp_path, capsys):
    root = str(tmp_path / "r6")
    os.makedirs(root)
    assert run("protect", root=root) == 0  # list (empty)
    assert run("protect", "add", "secrets/", "*.key", root=root) == 0
    data = json.load(open(os.path.join(root, ".whodunit", "config.json")))
    assert data["protect"] == ["secrets/", "*.key"]
    assert run("protect", "remove", "*.key", root=root) == 0
    data = json.load(open(os.path.join(root, ".whodunit", "config.json")))
    assert data["protect"] == ["secrets/"]
    assert run("protect", root=root) == 0
    assert "secrets/" in capsys.readouterr().out


# ---------------------------------------------------------------- restore


def test_restore_via_cli(tmp_path, capsys):
    root = str(tmp_path / "r7")
    os.makedirs(root)
    p = os.path.join(root, "f.txt")
    write(p, "before")

    jj = Journal(root)
    ev = seed_file(jj, root, "f.txt", "before")
    # actually modify the file on disk, then record the modification
    write(p, "modified")
    row = jj.shadow_row("f.txt")
    jj.record_change(
        ts=time.time(), run_id=None, path="f.txt", event_type="modified",
        actor="claude", confidence="high", old_size=6, new_size=8,
        old_hash=None, new_hash=None, mtime_ns=9,
        pre_blob_id=row["blob_id"], new_content=b"modified",
        blob_cap_total=1024 * 1024,
    )
    jj.close()

    # dry run does not change the file
    assert run("restore", "--dry-run", str(ev + 1), root=root) == 0
    assert open(p).read() == "modified"

    assert run("restore", "--yes", str(ev + 1), root=root) == 0
    assert open(p).read() == "before"

    # list shows restorable events
    assert run("restore", "--list", root=root) == 0
    out = capsys.readouterr().out
    assert "f.txt" in out


def test_restore_no_args(tmp_path, capsys):
    root = str(tmp_path / "r8")
    os.makedirs(root)
    assert run("restore", root=root) == 1


def test_restore_missing_event_id(tmp_path, capsys):
    root = str(tmp_path / "r8b")
    os.makedirs(root)
    assert run("restore", "424242", root=root) == 2
    assert "no such event" in capsys.readouterr().err


def test_restore_list_empty(tmp_path, capsys):
    root = str(tmp_path / "r8c")
    os.makedirs(root)
    assert run("restore", "--list", root=root) == 0
    assert "no restorable events" in capsys.readouterr().out


def test_help_and_version(capsys):
    from redhand.cli import build_parser

    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--version"])
    assert e.value.code == 0
    # bare call: no subcommand -> main() prints help and exits 0
    assert not hasattr(build_parser().parse_args([]), "func")


# ---------------------------------------------------------------- digest / forget


def test_digest_to_file(tmp_path):
    root = str(tmp_path / "r9")
    os.makedirs(root)
    out_path = os.path.join(str(tmp_path), "digest.md")
    jj = Journal(root)
    seed_file(jj, root, "a.txt", "data")
    jj.close()
    assert run("digest", "--out", out_path, root=root) == 0
    md = open(out_path).read()
    assert "redhand digest" in md and "a.txt" in md


def test_digest_since_flag(tmp_path):
    root = str(tmp_path / "r10")
    os.makedirs(root)
    jj = Journal(root)
    seed_file(jj, root, "a.txt", "data")
    jj.close()
    assert run("digest", "--since", "1h", root=root) == 0


def test_forget_prunes(tmp_path, capsys):
    root = str(tmp_path / "r11")
    os.makedirs(root)
    jj = Journal(root)
    seed_file(jj, root, "a.txt", "data")
    jj.close()
    # far-future cutoff: everything is older than that
    assert run("forget", "--before", "99999999999", "--yes", root=root) == 0
    jj = Journal(root)
    try:
        assert len(jj.events()) == 0
    finally:
        jj.close()
    assert "pruned" in capsys.readouterr().out


def test_forget_aborts_on_no(tmp_path, capsys, monkeypatch):
    root = str(tmp_path / "r12")
    os.makedirs(root)
    jj = Journal(root)
    seed_file(jj, root, "a.txt", "data")
    jj.close()
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert run("forget", "--before", "1h", root=root) == 0
    assert "aborted" in capsys.readouterr().out


# ---------------------------------------------------------------- actors / watch via CLI


def test_actors_probe(tmp_path, capsys):
    root = str(tmp_path / "r13")
    os.makedirs(root)
    assert run("actors", root=root) == 0
    out = capsys.readouterr().out
    assert ("agent-like process" in out) or ("no AI agent" in out)


def test_watch_once_via_cli(tmp_path, capsys):
    root = str(tmp_path / "r14")
    os.makedirs(root)
    write(os.path.join(root, "s.txt"), "seed")
    assert main(["--root", root, "watch", "--once", "--interval", "0.01"]) == 0
    out = capsys.readouterr().out
    assert "seeded 1 files" in out
    assert "session" in out


# ---------------------------------------------------------------- init gitignore


def test_init_gitignore_created(tmp_path):
    root = str(tmp_path / "g1")
    os.makedirs(root)
    assert run("init", root=root) == 0
    gi = os.path.join(root, ".gitignore")
    assert os.path.exists(gi) and ".whodunit/" in open(gi).read()


def test_init_gitignore_appends_without_dup(tmp_path):
    root = str(tmp_path / "g2")
    os.makedirs(root)
    gi = os.path.join(root, ".gitignore")
    with open(gi, "w") as f:
        f.write("node_modules/\n")  # no trailing newline on purpose
    assert run("init", root=root) == 0
    body = open(gi).read()
    assert body.count(".whodunit/") == 1 and "node_modules/" in body
    assert run("init", root=root) == 0  # idempotent
    assert open(gi).read().count(".whodunit/") == 1

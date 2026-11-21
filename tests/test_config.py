"""Tests for config loading and validation."""

from __future__ import annotations

import json
import os

import pytest

from redhand.config import ConfigError, load_config


def test_defaults_when_no_config(root):
    cfg = load_config(root)
    assert cfg.interval == 2.0
    assert ".git/" in cfg.ignore.patterns
    assert "claude" in cfg.agents
    assert not cfg.protect


def test_merge_and_overrides(root):
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        json.dump({"interval": 0.5, "agents": ["myagent"], "protect": ["secrets/"]}, f)
    cfg = load_config(root)
    assert cfg.interval == 0.5
    assert cfg.agents == ["myagent"]
    assert cfg.protect.patterns == ["secrets/"]
    # ignore falls back to defaults
    assert ".git/" in cfg.ignore.patterns


def test_unknown_keys_surfaced(root):
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        json.dump({"interval": 1.0, "wat": 1}, f)
    cfg = load_config(root)
    assert "wat" in cfg.raw["_unknown_keys"]


def test_invalid_json_rejected(root):
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        f.write("{not json")
    with pytest.raises(ConfigError):
        load_config(root)


def test_non_object_rejected(root):
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        f.write("[1,2]")
    with pytest.raises(ConfigError):
        load_config(root)


@pytest.mark.parametrize(
    "payload",
    [
        {"interval": 0},
        {"interval": -1},
        {"interval": "fast"},
        {"interval": 99999},
        {"ignore": "not-a-list"},
        {"ignore": [1, 2]},
        {"agents": [1]},
        {"protect": 42},
        {"hash_limit_mb": 0},
        {"blob_limit_kb": -5},
        {"blob_total_mb": "big"},
    ],
)
def test_invalid_values_rejected(root, payload):
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        json.dump(payload, f)
    with pytest.raises(ConfigError):
        load_config(root)


def test_numeric_overrides_scale(root):
    os.makedirs(os.path.join(root, ".whodunit"))
    with open(os.path.join(root, ".whodunit", "config.json"), "w") as f:
        json.dump({"hash_limit_mb": 2, "blob_limit_kb": 8, "blob_total_mb": 4}, f)
    cfg = load_config(root)
    assert cfg.hash_limit == 2 * 1024 * 1024
    assert cfg.blob_limit == 8 * 1024
    assert cfg.blob_total == 4 * 1024 * 1024

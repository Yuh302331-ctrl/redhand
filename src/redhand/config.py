"""Configuration: defaults, JSON override file, agent process signatures."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .util import PatternSet

STATE_DIR = ".whodunit"
CONFIG_NAME = "config.json"
DB_NAME = "journal.db"

#: Process-name / command-line fragments treated as AI coding agents.
#: Order matters only for display; matching is case-insensitive on the
#: process comm name AND the full command line, as a whole-word-ish token.
AGENT_SIGNATURES: List[str] = [
    "claude",
    "claude-code",
    "codex",
    "zcode",
    "cursor",
    "cursor-agent",
    "aider",
    "opencode",
    "goose",
    "gemini",
    "copilot",
    "cline",
    "windsurf",
    "continue",
    "crush",
    "amp",
    "kiro",
    "trae",
    "refact",
    "devin",
]

DEFAULT_IGNORE: List[str] = [
    ".git/",
    STATE_DIR + "/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".tox/",
    ".hypothesis/",
    "dist/",
    "build/",
    "*.pyc",
    ".DS_Store",
    "*.swp",
]

DEFAULTS: Dict[str, Any] = {
    "interval": 2.0,
    "ignore": list(DEFAULT_IGNORE),
    "agents": list(AGENT_SIGNATURES),
    "hash_limit_mb": 10,      # files bigger than this are tracked by size/mtime only
    "blob_limit_kb": 256,     # only text files <= this get restorable content snapshots
    "blob_total_mb": 64,      # global cap for stored content; oldest evicted beyond it
    "digest_timeline": 100,   # max timeline rows in digest
    "digest_per_actor": 30,   # max file rows per actor in digest
}


class ConfigError(ValueError):
    """Raised for unreadable/invalid config.json files."""


@dataclass
class Config:
    interval: float = DEFAULTS["interval"]
    ignore: PatternSet = field(default_factory=lambda: PatternSet(DEFAULTS["ignore"]))
    agents: List[str] = field(default_factory=lambda: list(AGENT_SIGNATURES))
    hash_limit: int = DEFAULTS["hash_limit_mb"] * 1024 * 1024
    blob_limit: int = DEFAULTS["blob_limit_kb"] * 1024
    blob_total: int = DEFAULTS["blob_total_mb"] * 1024 * 1024
    digest_timeline: int = DEFAULTS["digest_timeline"]
    digest_per_actor: int = DEFAULTS["digest_per_actor"]
    protect: PatternSet = field(default_factory=lambda: PatternSet([]))
    raw: Dict[str, Any] = field(default_factory=dict)


def config_path(root: str) -> str:
    return os.path.join(root, STATE_DIR, CONFIG_NAME)


def load_config(root: str) -> Config:
    """Defaults merged with <root>/.whodunit/config.json (unknown keys ignored with a warning list)."""
    cfg = Config()
    path = config_path(root)
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    cfg.raw = data

    unknown = sorted(set(data) - set(DEFAULTS) - {"protect"})
    if unknown:
        # keep the warning; CLI layer surfaces it once
        cfg.raw["_unknown_keys"] = unknown

    if "interval" in data:
        v = data["interval"]
        if not isinstance(v, (int, float)) or v <= 0 or v > 3600:
            raise ConfigError(f"{path}: 'interval' must be seconds in (0, 3600]")
        cfg.interval = float(v)
    if "ignore" in data:
        cfg.ignore = PatternSet(_string_list(path, "ignore", data["ignore"]))
    if "agents" in data:
        cfg.agents = _string_list(path, "agents", data["agents"])
    if "protect" in data:
        cfg.protect = PatternSet(_string_list(path, "protect", data["protect"]))
    if "hash_limit_mb" in data:
        cfg.hash_limit = _positive_number(path, "hash_limit_mb", data["hash_limit_mb"]) * 1024 * 1024
    if "blob_limit_kb" in data:
        cfg.blob_limit = int(_positive_number(path, "blob_limit_kb", data["blob_limit_kb"]) * 1024)
    if "blob_total_mb" in data:
        cfg.blob_total = int(_positive_number(path, "blob_total_mb", data["blob_total_mb"]) * 1024 * 1024)
    if "digest_timeline" in data:
        cfg.digest_timeline = int(_positive_number(path, "digest_timeline", data["digest_timeline"]))
    if "digest_per_actor" in data:
        cfg.digest_per_actor = int(_positive_number(path, "digest_per_actor", data["digest_per_actor"]))
    return cfg


def _string_list(path: str, key: str, value: Any) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{path}: '{key}' must be a list of strings")
    return list(value)


def _positive_number(path: str, key: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{path}: '{key}' must be a positive number")
    return float(value)


INITIAL_CONFIG_TEMPLATE: Dict[str, Any] = {
    "interval": 2.0,
    "ignore": list(DEFAULT_IGNORE),
    "agents": list(AGENT_SIGNATURES),
    "protect": [],
    "blob_limit_kb": DEFAULTS["blob_limit_kb"],
    "blob_total_mb": DEFAULTS["blob_total_mb"],
}

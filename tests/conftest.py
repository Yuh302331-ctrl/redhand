"""Shared fixtures: tmp watched roots, journal helpers, CLI runner."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from redhand.config import Config, load_config  # noqa: E402
from redhand.journal import Journal  # noqa: E402
from redhand.util import Palette  # noqa: E402


@pytest.fixture()
def root(tmp_path):
    """An empty watched root directory."""
    d = tmp_path / "proj"
    d.mkdir()
    return str(d)


@pytest.fixture()
def cfg(root):
    return load_config(root)


@pytest.fixture()
def jj(root):
    j = Journal(root)
    yield j
    j.close()


@pytest.fixture()
def pal():
    return Palette(enabled=False)


def touch(path, content: str = "hello\n", mode: str = "w"):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        f.write(content)


def make_cfg(**overrides) -> Config:
    """A Config with tight limits for tests."""
    c = Config()
    c.blob_limit = 64 * 1024
    c.blob_total = 256 * 1024
    c.hash_limit = 1024 * 1024
    for k, v in overrides.items():
        setattr(c, k, v)
    return c

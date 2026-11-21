"""redhand — catch file changes red-handed.

A cross-agent change journal: watches a folder, attributes every change to
the AI agent (or human) that made it, keeps restorable snapshots, and
answers 'who changed this file?' with a plain markdown digest.
"""

try:
    from importlib.metadata import version as _meta_version

    __version__ = _meta_version("redhand")
except Exception:  # not installed (running from a clone): fallback
    __version__ = "0.2.1"

__all__ = ["__version__"]

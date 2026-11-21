"""redhand — catch file changes red-handed.

A cross-agent change journal: watches a folder, attributes every change to
the AI agent (or human) that made it, keeps restorable snapshots, and
answers 'who changed this file?' with a plain markdown digest.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]

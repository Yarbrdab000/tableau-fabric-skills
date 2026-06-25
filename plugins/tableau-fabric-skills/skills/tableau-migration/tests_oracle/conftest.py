"""Put the skill's ``scripts/`` directory on ``sys.path`` for the QUARANTINED oracle tests.

These tests live OUTSIDE ``tests/`` on purpose: the deterministic engine's green gate is run as
``pytest tests`` and must never collect (or be affected by) this advisory, tolerance-banded
fidelity-oracle suite. Run them explicitly with ``pytest tests_oracle``.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

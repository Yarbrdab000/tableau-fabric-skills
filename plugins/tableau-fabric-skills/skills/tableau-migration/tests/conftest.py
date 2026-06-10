"""Put the skill's ``scripts/`` directory on sys.path so tests can import the
ported cores (``calc_to_dax``, ``tmdl_generate``, ``field_resolver``) directly,
mirroring how an agent runs them standalone.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

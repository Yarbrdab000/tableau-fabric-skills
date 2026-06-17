"""Guards the packaging manifests against silent drift.

Copilot CLI reads ``marketplace.json`` from **both** ``.github/plugin/`` and
``.claude-plugin/`` (dual-client support), and the plugin ships ``plugin.json`` in the
same two locations under ``plugins/tableau-fabric-skills/``. Those pairs are maintained
by hand, so they can drift. This test asserts each pair is byte-identical, that every
manifest parses as JSON, and that each marketplace ``source`` (and the skill paths it
lists) resolves to a real directory.

Like the mirror-parity guard, it walks up to the repository root (the directory that
contains BOTH ``skills`` and ``plugins``) and skips when that layout is absent (e.g. an
installed-skill context).
"""

import json
import os

import pytest

_MARKETPLACE_RELS = (
    os.path.join(".claude-plugin", "marketplace.json"),
    os.path.join(".github", "plugin", "marketplace.json"),
)
_PLUGIN_RELS = (
    os.path.join("plugins", "tableau-fabric-skills", ".claude-plugin", "plugin.json"),
    os.path.join("plugins", "tableau-fabric-skills", ".github", "plugin", "plugin.json"),
)


def _find_repo_root():
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(cur, "skills")) and os.path.isdir(
            os.path.join(cur, "plugins")
        ):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _repo_root_or_skip():
    root = _find_repo_root()
    if root is None:
        pytest.skip("repo root with both skills/ and plugins/ not found")
    return root


def _require(root, rels):
    paths = [os.path.join(root, rel) for rel in rels]
    for p in paths:
        if not os.path.isfile(p):
            pytest.skip("manifest not present: %s" % p)
    return paths


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def test_marketplace_manifests_are_byte_identical():
    root = _repo_root_or_skip()
    a, b = _require(root, _MARKETPLACE_RELS)
    assert _read_bytes(a) == _read_bytes(b), (
        "marketplace.json differs between .claude-plugin/ and .github/plugin/"
    )


def test_plugin_manifests_are_byte_identical():
    root = _repo_root_or_skip()
    a, b = _require(root, _PLUGIN_RELS)
    assert _read_bytes(a) == _read_bytes(b), (
        "plugin.json differs between .claude-plugin/ and .github/plugin/"
    )


def test_all_manifests_parse_as_json():
    root = _repo_root_or_skip()
    for path in _require(root, _MARKETPLACE_RELS) + _require(root, _PLUGIN_RELS):
        with open(path, "rb") as fh:
            json.loads(fh.read().decode("utf-8-sig"))


def test_marketplace_sources_and_skills_resolve():
    root = _repo_root_or_skip()
    manifest_path = _require(root, _MARKETPLACE_RELS)[0]
    with open(manifest_path, "rb") as fh:
        data = json.loads(fh.read().decode("utf-8-sig"))

    plugins = data.get("plugins", [])
    assert plugins, "marketplace.json lists no plugins"

    for plugin in plugins:
        source = plugin["source"]
        source_dir = os.path.normpath(os.path.join(root, source))
        assert os.path.isdir(source_dir), (
            "plugin %r source does not resolve: %s" % (plugin.get("name"), source_dir)
        )
        for skill in plugin.get("skills", []):
            skill_dir = os.path.normpath(os.path.join(source_dir, skill))
            assert os.path.isdir(skill_dir), (
                "plugin %r skill does not resolve: %s"
                % (plugin.get("name"), skill_dir)
            )

"""Guards every SKILL.md frontmatter against GitHub Copilot's load-time limits.

GitHub Copilot silently refuses to load a skill whose frontmatter exceeds its
caps -- ``name`` must be <= 60 characters and ``description`` <= 1024 characters.
An over-long ``description`` produces no error: the plugin installs, ``plugin
list`` shows it, but the skill never registers in a session (so the agent ends up
reading the repo and improvising instead of running the skill). This test makes
that failure mode loud at build time.

Like the mirror-parity and manifest-sync guards, it walks up to the repository
root (the directory that contains BOTH ``skills`` and ``plugins``) and skips when
that layout is absent (e.g. an installed-skill context).
"""

import os
import re

import pytest
import yaml

NAME_LIMIT = 60
DESCRIPTION_LIMIT = 1024

_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


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


def _skill_md_paths():
    root = _find_repo_root()
    if root is None:
        return []
    paths = []
    for tree in ("skills", os.path.join("plugins", "tableau-fabric-skills", "skills")):
        base = os.path.join(root, tree)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            md = os.path.join(base, name, "SKILL.md")
            if os.path.isfile(md):
                paths.append(md)
    return paths


def _load_frontmatter(path):
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8-sig")
    match = _FRONTMATTER_RE.match(text)
    assert match, "SKILL.md is missing a leading YAML frontmatter block: %s" % path
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), "frontmatter did not parse to a mapping: %s" % path
    return data


_PATHS = _skill_md_paths()


@pytest.mark.skipif(not _PATHS, reason="repo root with skills/ and plugins/ not found")
@pytest.mark.parametrize("path", _PATHS, ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_skill_frontmatter_within_copilot_limits(path):
    data = _load_frontmatter(path)

    name = data.get("name")
    assert name, "SKILL.md frontmatter has no 'name': %s" % path
    assert len(name) <= NAME_LIMIT, (
        "skill name is %d chars (limit %d) in %s -- Copilot will not load it"
        % (len(name), NAME_LIMIT, path)
    )
    folder = os.path.basename(os.path.dirname(path))
    assert name == folder, (
        "frontmatter name %r does not match folder %r in %s" % (name, folder, path)
    )

    description = data.get("description")
    assert description, "SKILL.md frontmatter has no 'description': %s" % path
    assert len(description) <= DESCRIPTION_LIMIT, (
        "skill description is %d chars (limit %d) in %s -- Copilot silently drops "
        "over-limit descriptions and the skill never loads"
        % (len(description), DESCRIPTION_LIMIT, path)
    )

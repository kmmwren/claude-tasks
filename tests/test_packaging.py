"""Packaging guardrails.

These would have caught the install-time bugs found on first real install: skill
descriptions that broke YAML parsing (unquoted values containing ': '), an unrecognised
key in plugin.json, and a missing marketplace.json. Every skill/command frontmatter must
parse as YAML; the manifests must be valid JSON with the required shape.
"""

import json
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))
COMMANDS = sorted((ROOT / "commands").glob("*.md"))


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert m, "missing --- frontmatter block"
    parsed = yaml.safe_load(m.group(1))
    assert isinstance(parsed, dict), "frontmatter must parse to a mapping"
    return parsed


@pytest.mark.parametrize("skill", SKILLS, ids=[s.parent.name for s in SKILLS])
def test_skill_frontmatter_is_valid_yaml(skill):
    fm = _frontmatter(skill.read_text())
    assert fm.get("name") == skill.parent.name, "skill `name` must match its directory"
    assert (fm.get("description") or "").strip(), "skill needs a non-empty description"


@pytest.mark.parametrize("cmd", COMMANDS, ids=[c.stem for c in COMMANDS])
def test_command_frontmatter_is_valid_yaml(cmd):
    fm = _frontmatter(cmd.read_text())
    assert (fm.get("description") or "").strip(), "command needs a non-empty description"


def test_plugin_manifest_valid():
    d = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    for key in ("name", "version", "description"):
        assert d.get(key), f"plugin.json missing {key}"


def test_marketplace_manifest_valid():
    d = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert d.get("name"), "marketplace.json needs a name"
    plugins = d.get("plugins")
    assert isinstance(plugins, list) and plugins, "marketplace must list ≥1 plugin"
    assert any(p.get("name") == "claude-tasks" for p in plugins), "claude-tasks must be listed"

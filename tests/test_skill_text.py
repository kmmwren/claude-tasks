"""Skill text names tools that exist and the right tool for each job.

A skill step that names no tool, or the wrong one, is followed by improvisation: handoff's
dedup step once said only "sharpen the file", and re-filing through add_task.py created
duplicate briefs because its merge needs a matching title and cited sources.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL_TEXTS = sorted([*(ROOT / "skills").glob("*/SKILL.md"), *(ROOT / "commands").glob("*.md")])
PLUGIN_SCRIPT = re.compile(r"(?<![\w/.~])(?:\$\{CLAUDE_PLUGIN_ROOT\}/)?scripts/(\w+\.py)")
HANDOFF = ROOT / "skills" / "handoff" / "SKILL.md"


def section(text: str, heading: str) -> str:
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


@pytest.mark.parametrize("doc", SKILL_TEXTS, ids=[f"{d.parent.name}/{d.name}" for d in SKILL_TEXTS])
def test_every_plugin_script_a_skill_names_exists(doc):
    named = set(PLUGIN_SCRIPT.findall(doc.read_text()))
    missing = sorted(name for name in named if not (ROOT / "scripts" / name).is_file())
    assert not missing, f"{doc.relative_to(ROOT)} names scripts that do not exist: {missing}"


def test_handoff_dedup_sharpens_through_append_log():
    dedup = section(HANDOFF.read_text(), "Step 3")
    assert "scripts/append_log.py" in dedup
    assert "title and cited sources" in dedup, "Step 3 must say why add_task.py merge is not a sharpen tool"


def test_handoff_dedup_greps_every_folder_append_log_searches():
    dedup = section(HANDOFF.read_text(), "Step 3")
    for folder in ("inbox", "ready", "in-progress", "blocked", "parked", "done"):
        assert folder in dedup.split("```bash", 1)[1].split("```", 1)[0], f"Step 3 grep skips {folder}/"

#!/usr/bin/env python3
"""
readiness.py — what a brief must carry before it leaves inbox/ for ready/ or in-progress/.

add_task.py enforces it when a brief is filed ready; transition.py enforces it when an
inbox file is promoted. One definition keeps the two gates from drifting apart. A brief
filed to inbox/ gets placeholder stubs for these fields, so the stubs count as missing.

Stdlib only.
"""
import re
from collections.abc import Sequence

from deps import fm

READY_FIELDS = ("goal", "criteria", "context")
# What add_task writes into a brief that was given no context.
PLACEHOLDER_CONTEXT = "_none recorded_"
_CRITERION = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$", re.MULTILINE)


def placeholder_criterion(title: str) -> str:
    """The criterion add_task writes into a brief that was given none."""
    return f"{title} is done."


def section(body: str, name: str) -> str:
    """Text under the `## name` heading, up to the next heading; empty when the heading is absent."""
    m = re.search(rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)", body, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def missing_ready_fields(goal: str, criteria: Sequence[str], context: str) -> list[str]:
    """Names from READY_FIELDS that are empty, in READY_FIELDS order."""
    present = {"goal": goal.strip(), "criteria": criteria, "context": context.strip()}
    return [name for name in READY_FIELDS if not present[name]]


def brief_missing_fields(text: str) -> list[str]:
    """READY_FIELDS a brief's text leaves empty or at add_task's placeholder."""
    title = fm(text).get("title", "")
    criteria = [c for c in _CRITERION.findall(section(text, "Success criteria"))
                if c != placeholder_criterion(title)]
    context = section(text, "Context")
    return missing_ready_fields(
        section(text, "Goal"), criteria, "" if context == PLACEHOLDER_CONTEXT else context)

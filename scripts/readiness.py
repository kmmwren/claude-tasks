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

READY_FIELDS = ("goal", "criteria", "context")
# What add_task writes into a brief that was given no context.
PLACEHOLDER_CONTEXT = "_none recorded_"
# The whole `## Execution log` section, heading included, up to the next `##` heading.
EXECUTION_LOG = re.compile(r"^##[ \t]+Execution log\b.*?(?=^##\s|\Z)", re.DOTALL | re.MULTILINE)
_CRITERION = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Templates write these where a field is still to be filled in.
_STUB = re.compile(r"#*\s*(?:todo|tbd)\.?", re.IGNORECASE)


def placeholder_goal(title: str) -> str:
    """The goal add_task writes into a brief that was given none."""
    return title


def placeholder_criterion(title: str) -> str:
    """The criterion add_task writes into a brief that was given none."""
    return f"{title} is done."


def raw_title(text: str) -> str:
    """The `title:` line as add_task wrote it; fm() would cut a title like `Review PR #42` at the `#`."""
    m = re.search(r"^title:[ \t]*(.*?)[ \t]*$", text, re.MULTILINE)
    return m.group(1) if m else ""


def section(body: str, name: str) -> str:
    """Text under the `## name` heading, up to the next heading; empty when the heading is absent."""
    m = re.search(rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)", body, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def missing_ready_fields(goal: str, criteria: Sequence[str], context: str) -> list[str]:
    """Names from READY_FIELDS that are empty, in READY_FIELDS order."""
    present = {"goal": goal.strip(), "criteria": criteria, "context": context.strip()}
    return [name for name in READY_FIELDS if not present[name]]


def brief_missing_fields(text: str) -> list[str]:
    """READY_FIELDS a brief's text leaves empty or at add_task's placeholder.

    HTML comments are prompts to the enricher, so a criterion like `<!-- define when enriching -->` is no criterion;
    a `# TODO` or `TBD` stub is no value either.
    """
    text = _HTML_COMMENT.sub("", text)
    title = raw_title(text)
    criteria = [
        c
        for c in _CRITERION.findall(section(text, "Success criteria"))
        if c != placeholder_criterion(title) and not _STUB.fullmatch(c)
    ]
    goal, context = section(text, "Goal"), section(text, "Context")
    return missing_ready_fields(
        "" if goal == placeholder_goal(title) or _STUB.fullmatch(goal) else goal,
        criteria,
        "" if context == PLACEHOLDER_CONTEXT or _STUB.fullmatch(context) else context,
    )

#!/usr/bin/env python3
"""
triage_collapse.py — fold bulk filings into one parent brief, reversibly.

Briefs that share an id stem (the id without numeric or hash suffixes), their tags and
their title shape are one filing split many ways: a peer session's messages, a retried
capture. The parent lists every child and its context; each child moves to done/ with
`parent:` set and a log line, so moving it back to inbox/ restores it. Nothing is deleted.

Stdlib only.
"""

import collections
import dataclasses
import pathlib
import re
from collections.abc import Sequence

from _concurrency import atomic_write, require_lock
from transition import find_brief, move_brief
from triage_brief import GENERATED_BY, InboxBrief

_SUFFIX = re.compile(r"(?:-(?:\d+|[0-9a-f]{8}))+$")
_VARIANT = re.compile(r"\(?\b(?:\d+|[0-9a-f]{8})\b\)?")
CHILD_CONTEXT_CHARS = 600


def stem(brief_id: str) -> str:
    return _SUFFIX.sub("", brief_id) or brief_id


def title_shape(title: str) -> str:
    """The title with its numbers and hashes removed, so `Message (1a2b3c4d)` and `Message (5e6f7a8b)` match."""
    return " ".join(_VARIANT.sub("", title).split())


@dataclasses.dataclass(frozen=True)
class Collapse:
    children: tuple[InboxBrief, ...]

    @property
    def stem(self) -> str:
        return stem(self.children[0].id)

    @property
    def parent_id(self) -> str:
        return f"{self.stem}-batch"

    def _importance(self) -> str:
        ranks = [
            int(c.fields["importance"])
            for c in self.children
            if c.fields.get("importance", "").isdigit()
        ]
        return str(min(ranks)) if ranks else "3"

    def _child_context(self, child: InboxBrief) -> str:
        body = child.context[:CHILD_CONTEXT_CHARS]
        return f"### {child.id}: {child.title}\n\n{body}"

    def parent_text(self, today: str) -> str:
        first, n = self.children[0], len(self.children)
        return "\n".join(
            (
                "---",
                f"id: {self.parent_id}",
                f"title: {title_shape(first.title)} ({n} filed)",
                f"created: {today}",
                f"updated: {today}",
                "status: inbox",
                "type: todo",
                f"importance: {self._importance()}",
                f"autonomy: {first.fields.get('autonomy', '')}",
                "estimated-effort: ",
                "due: ",
                f"domain: {first.fields.get('domain', '')}",
                f"tags: [{', '.join(first.tags)}]",
                "parent: ",
                "source: claude",
                f"generated-by: {GENERATED_BY}",
                "blockers: []",
                "related: []",
                "---",
                "",
                "## Goal",
                "",
                f"Every one of the {n} briefs filed as `{self.stem}` is done, answered or closed.",
                "",
                "## Context",
                "",
                f"inbox-triage collapsed {n} inbox briefs sharing the id stem `{self.stem}`, their tags and "
                f"their title shape. Each child is in done/ with `parent: {self.parent_id}`; move one back to "
                "inbox/ to restore it.",
                "",
                *(self._child_context(c) + "\n" for c in self.children),
                "## Success criteria",
                "",
                *(f"- [ ] {c.id}: {c.title} is done or answered." for c in self.children),
                "",
                "## Execution log",
                "",
                f"{today}: collapsed {n} inbox briefs into this one (inbox-triage).",
                "",
            )
        )


def _key(brief: InboxBrief) -> tuple[str, tuple[str, ...], str]:
    return stem(brief.id), brief.tags, title_shape(brief.title)


def plan_collapses(
    briefs: Sequence[InboxBrief], root: pathlib.Path, min_size: int = 2
) -> list[Collapse]:
    """Groups of at least `min_size` unparented briefs whose parent id is not already taken."""
    groups: dict[tuple[str, tuple[str, ...], str], list[InboxBrief]] = collections.defaultdict(list)
    for brief in briefs:
        if not brief.fields.get("parent"):
            groups[_key(brief)].append(brief)
    plans = [Collapse(tuple(g)) for g in groups.values() if len(g) >= min_size]
    return [p for p in plans if find_brief(p.parent_id, root) is None]


def apply_collapse(plan: Collapse, root: pathlib.Path, today: str) -> list[str]:
    """Write the parent to inbox/, link and retire each child to done/. Returns the touched ids."""
    with require_lock(root):
        atomic_write(root / "inbox" / f"{plan.parent_id}.md", plan.parent_text(today))
        for child in plan.children:
            atomic_write(child.path, child.with_field("parent", plan.parent_id).text)
    log = f"collapsed into {plan.parent_id} by inbox-triage; move back to inbox/ to restore"
    for child in plan.children:
        move_brief(child.id, "done", root, log=log, today=today)
    return [plan.parent_id, *(c.id for c in plan.children)]

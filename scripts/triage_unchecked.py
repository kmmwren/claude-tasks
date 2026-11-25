#!/usr/bin/env python3
"""
triage_unchecked.py: run the dedup and done checks on briefs a capture filed straight to ready/.

`add_task.py --ready` stays a plain write, so it marks the brief `unchecked`. The next triage run
treats it as it treats a promoted one: a repeat of an earlier brief is merged into it and closed
as a duplicate (the same Move, the same body-kept merge), and the evidence check that already
covers ready/ closes it when it is finished. A brief that survives loses the mark; one whose move
is still pending keeps it, so a capped or refused close is retried on the next run.

Stdlib only.
"""

import pathlib
from collections.abc import Collection, Mapping, Sequence

from _concurrency import atomic_write, require_lock
from triage_brief import UNCHECKED, InboxBrief, load_folder
from triage_classify import repeat_reason
from triage_close import Move, Reason, eligible


def load_unchecked(root: pathlib.Path) -> list[InboxBrief]:
    return [b for b in load_folder(root, "ready") if b.unchecked]


def repeat_moves(
    briefs: Sequence[InboxBrief], repeats: Mapping[str, str], skip: Collection[str] = ()
) -> list[Move]:
    """A duplicate move for each eligible brief in `repeats` that is not already being moved."""
    return [
        Move(b.id, b.title, "ready", Reason.DUPLICATE, repeat_reason(repeats[b.id]), repeats[b.id])
        for b in briefs
        if b.id in repeats and b.id not in skip and eligible(b)
    ]


def _clear(brief: InboxBrief) -> bool:
    """Remove the mark unless the brief changed since it was read; True when it was removed."""
    with require_lock(brief.path.parent.parent):
        if not brief.path.exists() or brief.path.read_text() != brief.text:
            return False
        atomic_write(brief.path, brief.without_field(UNCHECKED).text)
    return True


def clear_checked(briefs: Sequence[InboxBrief], pending: Collection[str]) -> list[str]:
    """Clear the mark of every brief with no move pending; returns the ids cleared."""
    return [b.id for b in briefs if b.id not in pending and _clear(b)]

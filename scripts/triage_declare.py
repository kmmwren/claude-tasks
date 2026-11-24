#!/usr/bin/env python3
"""
triage_declare.py: declare `blocked_by: []` on inbox briefs whose prose waits on nothing.

An inbox brief with no dependency declaration cannot be promoted, and most of them wait on
nothing. The prose-dependency check that flags a ready brief for waiting on a PR, brief or
person decides here too: a brief it finds no wait phrase in is declared through the same
`declare_brief` as `transition.py declare`; one it does find a phrase in is left for the fill
to name its blocker. Nothing is moved.

Stdlib only.
"""

import dataclasses
import pathlib
import sys
from collections.abc import Sequence

from _paths import QueueBriefs
from declaration import declare_brief
from deps import Brief, declares_dependencies, load_queue
from prose_dependency import first_wait
from triage_brief import InboxBrief, load_inbox

LOG = "declared blocked_by: [] by inbox-triage: no prose wait found"


@dataclasses.dataclass(frozen=True)
class Declarations:
    declared: tuple[str, ...] = ()
    held: tuple[str, ...] = ()


def prose_wait(brief: InboxBrief, known: set[str]) -> str | None:
    """The phrase in which the brief says it waits on something, else None."""
    return first_wait(Brief(brief.id, "inbox", brief.fields, body=brief.text), known)


def _declare(brief_id: str, root: pathlib.Path, today: str) -> bool:
    try:
        declare_brief(brief_id, [], root, log=LOG, today=today)
    except (ValueError, RuntimeError) as e:
        print(f"inbox-triage: {brief_id}: not declared: {e}", file=sys.stderr)
        return False
    return True


def declare_empty(root: pathlib.Path, apply: bool, today: str) -> Declarations:
    """Declare each undeclared inbox brief with no prose wait (only counted unless `apply`); say why the others are held."""
    inbox: Sequence[InboxBrief] = load_inbox(root)
    known = {b.id for b in load_queue(QueueBriefs.read(root))} | {b.id for b in inbox}
    undeclared = [b for b in inbox if not declares_dependencies(b.fields)]
    waits = {b.id: prose_wait(b, known) for b in undeclared}
    for bid, phrase in waits.items():
        if phrase:
            print(
                f'inbox-triage: {bid}: not declared, prose says "{phrase}"; left for the fill',
                file=sys.stderr,
            )
    free = [bid for bid, phrase in waits.items() if not phrase]
    done = [bid for bid in free if not apply or _declare(bid, root, today)]
    return Declarations(tuple(done), tuple(bid for bid, phrase in waits.items() if phrase))

#!/usr/bin/env python3
"""
start_prompt.py: print the prompt that starts a session on a slice of the queues.

A slice is one project's queue, or every queue in $CLAUDE_TASKS_ROOTS, narrowed by milestone and
tag. Its ready briefs are ranked by prioritise.py and printed as action-task commands, highest
first. With no slice named it prints the menu: each queue's ready briefs counted by milestone
and tag, beside the command that starts a session on them.

Usage:
  python scripts/start_prompt.py                                   # the menu
  python scripts/start_prompt.py checkout --milestone m1,m2
  python scripts/start_prompt.py sp --tag ux                       # sp: search-platform
  python scripts/start_prompt.py all --tag memory
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import heapq
import pathlib
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar

import prioritise
from _default_branch import DefaultBranchError, default_branch, git_stdout
from _paths import PROJECT_MARKER, is_queue, known_roots, resolve_root, warn
from milestones import MilestonesError, milestone_specs
from scope import AllOfScope, AllScope, MilestoneScope, ProjectScope, Scope, TagScope
from signals import Subject

COMMAND = "/claude-tasks:start"
EVERY = "all"
MENU_TAGS = 6
MENU_TAG_MIN = 2  # a tag on one brief is a label, not a slice worth a session
READY = "ready"
WAITING_DIRS = ("in-progress", "blocked")
GIT_TIMEOUT = 5.0
INDENT = "             "

INSTRUCTIONS = (
    "Work these briefs in this order. Start each with its command, which moves the brief to "
    "in-progress and works it to done; run it against the brief's own queue (its repository, "
    "or pass --root). Re-check live state first (gh pr view, git log): the ranking was taken "
    "when this prompt was printed. A brief flagged blocked or needs-you waits on that, so note "
    "it and take the next one. Recreate the list on your task ledger with metadata.brief set to "
    "each brief's path. When you hand off, run /claude-tasks:handoff, which prints the next prompt."
)
COMBINING = (
    "Combine them: --milestone m1,m2 takes either milestone, --tag ux,memory either tag, "
    "and the two together take briefs matching both."
)

T = TypeVar("T")


def one_of(
    token: str,
    what: str,
    options: Sequence[T],
    label: Callable[[T], str],
    *tests: Callable[[T], bool],
) -> T:
    """The option the first test that matches anything picks; none or several is a usage error."""
    for test in tests:
        hits = [o for o in options if test(o)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise SystemExit(f"{what} {token!r} matches {', '.join(map(label, hits))}: name one")
    choices = ", ".join(map(label, options)) or "(none)"
    raise SystemExit(f"no {what} {token!r}; choose from {choices}")


def split(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def counted(pairs: Iterable[tuple[str, int]]) -> str:
    return " · ".join(f"{key} {n}" for key, n in pairs)


def natural(text: str) -> tuple[int | str, ...]:
    """Sort key putting m2 before m10: digit runs compare as numbers."""
    return tuple(int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text))


@dataclasses.dataclass(frozen=True)
class Milestones:
    """A project's milestone ids in file order, answering to their short forms (m2 for web-m2)."""

    ids: tuple[str, ...]

    @staticmethod
    def _tests(token: str) -> list[Callable[[str], bool]]:
        low = token.lower()
        return [
            lambda i: i == token,
            lambda i: i.lower() == low,
            lambda i: i.lower().endswith(f"-{low}"),
        ]

    def resolve(self, token: str) -> str:
        return one_of(token, "milestone", self.ids, str, *self._tests(token))

    def short(self, milestone: str) -> str:
        """The id's last part when that names it alone, else the whole id."""
        token = milestone.rsplit("-", 1)[-1]
        named = [i for i in self.ids if any(t(i) for t in self._tests(token))]
        return token if named == [milestone] else milestone

    def ordered(self, counts: collections.Counter[str]) -> list[tuple[str, int]]:
        rank = {m: n for n, m in enumerate(self.ids)}
        return sorted(counts.items(), key=lambda kv: (rank.get(kv[0], len(rank)), natural(kv[0])))


@dataclasses.dataclass(frozen=True)
class Queue:
    root: pathlib.Path

    @property
    def name(self) -> str:
        """A project's `.tasks` queue is named for the project folder holding it."""
        return self.root.parent.name if self.root.name == PROJECT_MARKER else self.root.name

    @property
    def aliases(self) -> frozenset[str]:
        """Its name, each word of it and, for a name of several words, its initials (sp)."""
        words = self.name.split("-")
        initials = {"".join(w[:1] for w in words)} if len(words) > 1 else set()
        return frozenset({self.name, *words, *initials})

    def answers_to(self, token: str) -> bool:
        return token in self.aliases or self.name.startswith(token)

    def briefs(self, folder: str) -> list[Subject]:
        files = sorted((self.root / folder).glob("*.md"))
        return [prioritise.subject(prioritise.load_fields(f)) for f in files]

    @property
    def milestones(self) -> Milestones:
        try:
            specs = milestone_specs(self.root)
        except MilestonesError as e:
            warn(str(e))
            specs = None
        return Milestones(tuple(m.id for m in specs or ()))

    @property
    def checkout(self) -> Checkout | None:
        top = git_stdout(self.root, "rev-parse", "--show-toplevel", timeout=GIT_TIMEOUT)
        return Checkout(pathlib.Path(top)) if top else None


@dataclasses.dataclass(frozen=True)
class Checkout:
    """The clone a queue lives in; several queues can share one, so it is checked once."""

    top: pathlib.Path

    def commits_behind(self) -> int:
        """Commits origin's default branch has that this clone lacks, as of its last fetch; 0 when unknown."""
        try:
            ref = f"HEAD..origin/{default_branch(self.top)}"
        except DefaultBranchError:
            return 0
        return int(git_stdout(self.top, "rev-list", "--count", ref, timeout=GIT_TIMEOUT) or 0)


def warn_stale(queues: Iterable[Queue]) -> None:
    """One warning per clone behind origin, naming the queues whose ranking it skews."""
    held: dict[Checkout, list[str]] = collections.defaultdict(list)
    for queue in queues:
        if (checkout := queue.checkout) is not None:
            held[checkout].append(queue.name)
    for checkout, names in held.items():
        if behind := checkout.commits_behind():
            queues_named = ", ".join(names)
            warn(
                f"{checkout.top} is {behind} commits behind origin; pull it to rank {queues_named}"
            )


def by_name(queue: Queue) -> str:
    return queue.name


def discover() -> list[Queue]:
    """Every initialised queue this run may name: the current one, then $CLAUDE_TASKS_ROOTS."""
    return [Queue(r) for r in known_roots(resolve_root()) if is_queue(r)]


@dataclasses.dataclass(frozen=True)
class Pick:
    """One ranked ready brief and the queue it lives in."""

    queue: Queue
    row: dict[str, Any]

    @property
    def score(self) -> float:
        return float(self.row["_score"])

    def line(self) -> str:
        title = self.row.get("title") or self.row["id"]
        where = f"{self.queue.name}{prioritise.flags(self.row)}"
        brief = self.queue.root / READY / self.row["_file"]
        return f"- /claude-tasks:action-task {self.row['id']}  ({title}; {where}; brief {brief})"


@dataclasses.dataclass(frozen=True)
class Slice:
    queues: tuple[Queue, ...]
    milestones: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def scope(self) -> Scope:
        parts = [
            *([MilestoneScope.of(self.milestones)] if self.milestones else []),
            *([TagScope.of(self.tags)] if self.tags else []),
        ]
        return AllOfScope(parts, ProjectScope.name if len(self.queues) == 1 else AllScope.name)

    def describe(self) -> str:
        parts = [
            self.queues[0].name if len(self.queues) == 1 else "every queue",
            f"milestone {' or '.join(self.milestones)}" if self.milestones else "",
            f"tagged {' or '.join(self.tags)}" if self.tags else "",
        ]
        return ", ".join(p for p in parts if p)

    def picks(self) -> list[Pick]:
        """Each queue in its own ranked order, the queues interleaved by score."""
        ranked = (
            [Pick(q, row) for row in prioritise.rank(q.root, scope=self.scope)] for q in self.queues
        )
        return list(heapq.merge(*ranked, key=lambda p: -p.score))

    def waiting(self) -> collections.Counter[str]:
        """In-scope briefs not listed because another session or a blocker holds them."""
        scope = self.scope
        return collections.Counter(
            folder
            for q in self.queues
            for folder in WAITING_DIRS
            for s in q.briefs(folder)
            if scope.admits(s)
        )


def choose(
    queues: Sequence[Queue], project: str, milestones: tuple[str, ...], tags: tuple[str, ...]
) -> Slice:
    tests = (lambda q: q.name == project, lambda q: q.answers_to(project))
    named = project != EVERY
    chosen = (one_of(project, "queue", queues, by_name, *tests),) if named else tuple(queues)
    if milestones and len(chosen) != 1:
        raise SystemExit("a milestone belongs to one project: name the project before --milestone")
    resolved = tuple(chosen[0].milestones.resolve(m) for m in milestones)
    return Slice(chosen, resolved, tags)


def render(chosen: Slice, picks: Sequence[Pick], waiting: collections.Counter[str]) -> str:
    count = f"{len(picks)} ready briefs, highest priority first"
    header = f"Start a session on {chosen.describe()}: {count}."
    empty = f"- (nothing ready in this slice: run {COMMAND} for the menu)"
    body = "\n".join(p.line() for p in picks) or empty
    held = ", ".join(f"{n} {folder}" for folder, n in sorted(waiting.items()))
    note = f"Not listed, held elsewhere: {held}." if held else ""
    return "\n\n".join(s for s in (header, INSTRUCTIONS, body, note) if s) + "\n"


@dataclasses.dataclass(frozen=True)
class Shelf:
    """What the menu offers for one queue, or for every queue together (no milestones then)."""

    handle: str
    ready: tuple[Subject, ...]
    milestones: Milestones | None

    def _milestone_lines(self) -> list[str]:
        counts = collections.Counter(s.milestone for s in self.ready if s.milestone)
        if self.milestones is None or not counts:
            return []
        pairs = self.milestones.ordered(counts)
        example = f"{COMMAND} {self.handle} --milestone {self.milestones.short(pairs[0][0])}"
        return [f"  milestone  {counted(pairs)}", f"{INDENT}{example}"]

    def _tag_lines(self) -> list[str]:
        common = collections.Counter(t for s in self.ready for t in s.tags).most_common(MENU_TAGS)
        pairs = [(tag, n) for tag, n in common if n >= MENU_TAG_MIN]
        if not pairs:
            return []
        return [
            f"  tag        {counted(pairs)}",
            f"{INDENT}{COMMAND} {self.handle} --tag {pairs[0][0]}",
        ]

    def lines(self) -> list[str]:
        head = [f"{self.handle}: {len(self.ready)} ready", f"  {COMMAND} {self.handle}"]
        return head + self._milestone_lines() + self._tag_lines()


def menu(queues: Sequence[Queue]) -> str:
    shelves = [Shelf(q.name, tuple(q.briefs(READY)), q.milestones) for q in queues]
    stocked = [s for s in shelves if s.ready]
    every = Shelf(EVERY, tuple(b for s in stocked for b in s.ready), None)
    blocks = ["Start a session on any of these slices: copy a command into a new session."]
    blocks += ["\n".join(s.lines()) for s in (*stocked, every)]
    empty = [s.handle for s in shelves if not s.ready]
    blocks += [COMBINING] + ([f"Nothing ready in: {', '.join(empty)}."] if empty else [])
    return "\n\n".join(blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Print the prompt that starts a session on a slice of the queues."
    )
    ap.add_argument("project", nargs="?", default="", help="a queue's name or short form, or 'all'")
    ap.add_argument("--milestone", default="", help="milestone ids or short forms, comma separated")
    ap.add_argument(
        "--tag", default="", help="tags, comma separated: a brief with any of them is in"
    )
    args = ap.parse_args(argv)
    queues = discover()
    if not (args.project or args.milestone or args.tag):
        warn_stale(queues)
        print(menu(queues), end="")
        return 0
    chosen = choose(queues, args.project or EVERY, split(args.milestone), split(args.tag))
    warn_stale(chosen.queues)
    print(render(chosen, chosen.picks(), chosen.waiting()), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

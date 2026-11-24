#!/usr/bin/env python3
"""
cascade.py: when a brief goes done, tell every brief that waited on it what landed, then release.

`unblock.py` moves a blocked brief to ready/ once its waits are done. That is not enough: the
brief that was waiting still needs to know what it was waiting for (the sha or PR the done brief
shipped) and that any prose of the form "after X merges" is now stale. Both go in the dependent's
execution log, whether it is released or still waits on something else, and whether it lives in
the done brief's queue or in another queue that names it as `<queue>:<id>`.

Stdlib only; called by `transition.py move <id> done`.
"""

import dataclasses
import datetime
import pathlib
import re
from collections.abc import Sequence

from _concurrency import atomic_write, repo_lock
from _paths import QueueBriefs, warn
from deps import Brief, QueueRef, load_queue, queue_name, resolve_queue, same_queue
from facts import BriefFacts, load_brief_facts
from prose_dependency import LOG_HEADING, plan_line, prose_lines, waits_in_prose
from transition import _append_log, find_brief
from unblock import release_unblocked

LINK = re.compile(
    r"https?://[^\s)>\]]+"
    r"|[\w.-]+/[\w.-]+#\d+"
    r"|(?<![\w/])#\d+\b"
    r"|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"
)
PR_NUMBER = re.compile(r"#(\d+)|/pull/(\d+)")


def _shipped_summary(facts: BriefFacts) -> str:
    """The shipped line, else the evidence rows, else nothing."""
    if facts.shipped:
        return facts.shipped
    return "; ".join(f"{e.caption} {e.link}" for e in facts.evidence)


def _done_log_line(root: pathlib.Path, brief_id: str) -> str:
    """The last line of the done brief's execution log, which is what its closing move recorded."""
    path = find_brief(brief_id, root)
    lines = path.read_text(encoding="utf-8").split(LOG_HEADING)[-1].splitlines() if path else []
    return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")


@dataclasses.dataclass(frozen=True)
class Landed:
    """What a done brief shipped: its summary and the links the summary does not already carry."""

    id: str
    summary: str
    links: tuple[str, ...]

    @classmethod
    def of(cls, root: pathlib.Path, brief_id: str) -> "Landed":
        shipped = _shipped_summary(load_brief_facts(root, brief_id))
        logged = _done_log_line(root, brief_id)
        summary = shipped or logged
        found = dict.fromkeys(LINK.findall(f"{shipped} {logged}"))
        return cls(brief_id, summary, tuple(link for link in found if link not in summary))

    @property
    def pull_requests(self) -> set[str]:
        return {
            n
            for link in (*self.links, self.summary)
            for m in PR_NUMBER.finditer(link)
            for n in m.groups()
            if n
        }

    def cited_in(self, phrase: str, names: frozenset[str]) -> bool:
        """True when `phrase` names the brief (by any spelling in `names`) or one of its PRs."""
        by_name = any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", phrase) for n in names)
        by_pr = any(re.search(rf"(?:#|/pull/){n}\b", phrase) for n in self.pull_requests)
        return by_name or by_pr

    def line(self, label: str) -> str:
        links = f" [{', '.join(self.links)}]" if self.links else ""
        return f"landed: {label} done: {self.summary}{links}".rstrip()


@dataclasses.dataclass(frozen=True)
class Dependent:
    """An open brief whose `blocked_by` names the done brief, and the spellings it uses for it."""

    root: pathlib.Path
    brief: Brief
    names: frozenset[str]

    def stale_wait(self, landed: Landed) -> str | None:
        """The prose phrase that still waits on the done brief, None when the prose says no such thing."""
        known = set(self.names)
        for line in (plan_line(ln) for ln in prose_lines(self.brief.body)):
            phrase = waits_in_prose(line, known, set(), humans=False)
            if phrase and landed.cited_in(phrase, self.names):
                return phrase
        return None

    def messages(self, landed: Landed, done_root: pathlib.Path) -> list[str]:
        label = (
            landed.id
            if same_queue(self.root, done_root)
            else f"{queue_name(done_root)}:{landed.id}"
        )
        stale = self.stale_wait(landed)
        flag = f'stale prose: says "{stale}" but {label} is done; update the plan' if stale else ""
        return [m for m in (landed.line(label), flag) if m]


@dataclasses.dataclass(frozen=True)
class Touched:
    """What the cascade changed in one queue."""

    root: pathlib.Path
    released: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()

    @property
    def ids(self) -> list[str]:
        return list(dict.fromkeys([*self.released, *self.updated]))


def citations(
    brief: Brief,
    home: pathlib.Path,
    target: pathlib.Path,
    target_id: str,
    roots: Sequence[pathlib.Path],
) -> frozenset[str]:
    """The ways `brief`, in queue `home`, names brief `target_id` of queue `target`."""
    remote = {
        str(ref)
        for ref in brief.remote_refs
        if ref.id == target_id and _resolves_to(ref, target, roots)
    }
    local = {target_id} if same_queue(home, target) and target_id in brief.local_ids else set()
    return frozenset(local | remote)


def _resolves_to(ref: QueueRef, target: pathlib.Path, roots: Sequence[pathlib.Path]) -> bool:
    found = resolve_queue(ref.queue, roots)
    return found is not None and same_queue(found, target)


def dependents(
    done_root: pathlib.Path, done_id: str, roots: Sequence[pathlib.Path]
) -> list[Dependent]:
    """Every open brief, in this queue or any known one, that names the done brief."""
    queues = [done_root, *(r for r in roots if not same_queue(r, done_root))]
    return [
        Dependent(root, brief, names)
        for root in queues
        for brief in load_queue(QueueBriefs.read(root))
        if brief.status != "done" and (names := citations(brief, root, done_root, done_id, roots))
    ]


def _log(root: pathlib.Path, brief_id: str, day: str, messages: list[str]) -> None:
    """Append each message not already in the brief's log; a lock timeout skips with a warning."""
    path = find_brief(brief_id, root)
    with repo_lock(root) as acquired:
        if not acquired or path is None:
            warn(f"{brief_id}: could not log what landed (queue busy or brief moved)")
            return
        text = path.read_text(encoding="utf-8")
        for message in (m for m in messages if m not in text):
            text = _append_log(text, day, message)
        atomic_write(path, text)


def cascade_done(
    done_root: pathlib.Path,
    done_id: str,
    roots: Sequence[pathlib.Path],
    today: str | None = None,
) -> list[Touched]:
    """Log what landed on every dependent, then release whatever is now free; the queues that changed."""
    day = today or datetime.date.today().isoformat()
    landed = Landed.of(done_root, done_id)
    found = dependents(done_root, done_id, roots)
    for dependent in found:
        _log(dependent.root, dependent.brief.id, day, dependent.messages(landed, done_root))
    queues = [done_root, *(d.root for d in found if not same_queue(d.root, done_root))]
    touched = [
        Touched(
            root,
            tuple(release_unblocked(root, today, roots=roots)),
            tuple(d.brief.id for d in found if same_queue(d.root, root)),
        )
        for root in {q.resolve(): q for q in queues}.values()
    ]
    return [t for t in touched if t.ids]

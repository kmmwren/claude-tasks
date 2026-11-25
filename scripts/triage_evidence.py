#!/usr/bin/env python3
"""
triage_evidence.py: whether a brief's own evidence says its work is finished.

Every success criterion ticked is evidence. So is a PR named as the deliverable, when none it cites
is open or unreadable, it cites no issue, and nothing looks past the merge. No evidence, no close.
"""

import dataclasses
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from readiness import all_criteria_ticked, section
from triage_brief import InboxBrief
from triage_sources import PrState, PrStateCheck, pr_state

PR_FETCH_JOBS = 8
_PULL_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_ISSUE_URL = re.compile(r"github\.com/[\w.-]+/[\w.-]+/issues/\d+")
_SLUG = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)")
_NUMBER = re.compile(r"(?:pull/|#)(\d+)")
_FOLLOW_ON = re.compile(
    r"follow[- ]?ups?\b|\b(?:after|once|when|until)\b[^.\n]{0,40}\bmerg|post[- ]?merge"
    r"|\bnext (?:step|phase)|\bthen\b|\bafterwards?\b|\bsubsequent",
    re.IGNORECASE,
)
_FINISHED = (PrState.MERGED, PrState.CLOSED)


@dataclasses.dataclass(frozen=True, order=True)
class PullRef:
    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"


class PrStates:
    """A PrStateCheck that reads each pull request once for the whole run."""

    def __init__(self, fetch: PrStateCheck = pr_state) -> None:
        self._fetch, self._seen = fetch, dict[tuple[str, int], PrState | None]()

    @classmethod
    def offline(cls) -> "PrStates":
        """States that are never readable, so no pull request is ever evidence."""
        return cls(lambda repo, number: None)

    def __call__(self, repo: str, number: int) -> PrState | None:
        key = (repo.lower(), number)
        if key not in self._seen:
            self._seen[key] = self._fetch(repo, number)
        return self._seen[key]

    def prefetch(self, refs: Iterable[PullRef], jobs: int = PR_FETCH_JOBS) -> None:
        """Read every ref's state, `jobs` at a time, so the lookups that follow hit the cache."""
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(lambda r: self(r.repo, r.number), sorted(set(refs))))


def cited_pulls(brief: InboxBrief) -> frozenset[PullRef]:
    """Every pull request the brief cites, as a URL or `owner/repo#N`; a bare one may turn out to be an issue."""
    found = set[PullRef]()
    for source in brief.sources:
        if m := _PULL_URL.search(source) or _SLUG.fullmatch(source):
            found.add(PullRef(m[1], int(m[2])))
    return frozenset(found)


def _cites_issue(brief: InboxBrief) -> bool:
    return any(_ISSUE_URL.search(s) for s in brief.sources)


def deliverable_pulls(brief: InboxBrief) -> frozenset[PullRef]:
    """The cited pull requests when the brief plainly is about them, else none."""
    pulls = cited_pulls(brief)
    aim = f"{brief.title}\n{section(brief.text, 'Goal')}"
    named = {int(n) for n in _NUMBER.findall(aim)}
    plain = not _cites_issue(brief) and not _FOLLOW_ON.search(aim)
    return pulls if plain and any(p.number in named for p in pulls) else frozenset()


def _pr_evidence(brief: InboxBrief, states: PrStates) -> str | None:
    pulls = sorted(deliverable_pulls(brief))
    seen = [(p, states(p.repo, p.number)) for p in pulls]
    if not seen or any(state not in _FINISHED for _, state in seen):
        return None
    return ", ".join(f"{p} {state}" for p, state in seen)


def closing_evidence(brief: InboxBrief, states: PrStates) -> str | None:
    """What shows the brief is finished, or None when nothing does."""
    if all_criteria_ticked(brief.text):
        return "every success criterion is ticked"
    return _pr_evidence(brief, states)

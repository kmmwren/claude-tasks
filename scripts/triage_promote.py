#!/usr/bin/env python3
"""
triage_promote.py — move inbox briefs that pass the readiness gate to ready/.

A brief is promoted only when goal, criteria and context are real (not stubs) and its context
cites a source that appears in the brief as it was filed and exists right now. Briefs missing
fields are enriched first; a fill that fails the gate leaves the brief in the inbox untouched.

Stdlib only.
"""

import dataclasses
import pathlib
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor

from _concurrency import atomic_write, require_lock
from transition import move_brief
from triage_brief import InboxBrief
from triage_enrich import Fill, PredictFn, apply_fill
from triage_sources import SourceCheck, cited_sources, grounded

SOURCE_LIST_MAX = 5


@dataclasses.dataclass(frozen=True)
class Promotion:
    original: InboxBrief
    candidate: InboxBrief
    reason: str


def batches(briefs: Sequence[InboxBrief], size: int) -> Iterator[Sequence[InboxBrief]]:
    return (briefs[i : i + size] for i in range(0, len(briefs), size))


@dataclasses.dataclass(frozen=True)
class Gate:
    """The readiness gate from inbox/ to ready/, judging citations with `exists`."""

    exists: SourceCheck

    def live_sources(self, brief: InboxBrief) -> list[str]:
        return [s for s in sorted(brief.sources) if self.exists(s)]

    def with_cited_context(self, brief: InboxBrief) -> InboxBrief:
        """A filed context that names no source gains a line listing the brief's sources that exist."""
        live = self.live_sources(brief)
        if "context" in brief.missing or cited_sources(brief.context) or not live:
            return brief
        listing = ", ".join(live[:SOURCE_LIST_MAX])
        return brief.with_section(
            "Context", f"{brief.context}\n\nSources cited in this brief: {listing}"
        )

    def passes(self, candidate: InboxBrief, original: InboxBrief) -> bool:
        return not candidate.missing and grounded(candidate.context, original.text, self.exists)

    def _promotion(
        self, original: InboxBrief, candidate: InboxBrief, reason: str
    ) -> Promotion | None:
        return Promotion(original, candidate, reason) if self.passes(candidate, original) else None

    def as_filed(self, brief: InboxBrief) -> Promotion | None:
        return self._promotion(brief, self.with_cited_context(brief), "ready as filed")

    def enriched(self, brief: InboxBrief, fill: Fill) -> Promotion | None:
        candidate = apply_fill(self.with_cited_context(brief), fill)
        return self._promotion(brief, candidate, "missing fields filled from the brief")

    def needs_fill(self, brief: InboxBrief) -> bool:
        """Missing fields, but cites a live source a fill could ground its context in."""
        return bool(brief.missing) and bool(self.live_sources(brief))

    def enrich_all(
        self, briefs: Sequence[InboxBrief], predict: PredictFn, size: int, jobs: int
    ) -> list[Promotion]:
        """Fill every brief in batches of `size`, `jobs` batches at a time; keep the fills that pass."""
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            fills = [
                f for batch in pool.map(predict, batches(briefs, size)) for f in batch.items()
            ]
        by_id = {b.id: b for b in briefs}
        found = (self.enriched(by_id[bid], fill) for bid, fill in fills)
        return [p for p in found if p is not None]


def promote(p: Promotion, root: pathlib.Path, today: str) -> bool:
    """Write the candidate and move it to ready/, unless the brief changed since it was read."""
    with require_lock(root):
        if not p.original.path.exists() or p.original.path.read_text() != p.original.text:
            return False
        atomic_write(p.original.path, p.candidate.text)
    move_brief(
        p.original.id,
        "ready",
        root,
        log=f"promoted by inbox-triage: {p.reason}",
        today=today,
        only_if=lambda: p.original.path.read_text() == p.candidate.text,
    )
    return True

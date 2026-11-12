#!/usr/bin/env python3
"""
triage.py — move the inbox towards ready/: collapse bulk filings, then promote what passes the gate.

    triage.py promote                    # dry run: what would collapse and promote, no LLM, no writes
    triage.py promote --llm              # dry run including enrichment of briefs missing fields
    triage.py promote --apply --llm      # do it, then one commit for everything touched

Every inbox brief also gets a class (ready, needs-info, duplicate or drop) and a reason, in the
report; --apply logs them, and logs a correction when the owner has since moved a brief against
its class. Nothing is ever dropped: a brief that does not pass stays in the inbox exactly as it was.

Stdlib only.
"""

import argparse
import collections
import dataclasses
import datetime
import json
import pathlib
import sys
from collections.abc import Collection, Mapping, Sequence

from _paths import resolve_root
from class_log import BriefClass, ClassLog, Verdict
from fingerprint import index
from git_sync import sync_commit
from triage_brief import InboxBrief, load_inbox
from triage_classify import (
    EXAMPLE_LIMIT,
    Call,
    ClassifyFn,
    classify,
    claude_classify,
    duplicate_map,
    fixed_calls,
    reconcile,
)
from triage_collapse import apply_collapse, plan_collapses
from triage_enrich import Fill, PredictFn, claude_predict
from triage_promote import Gate, Promotion, promote
from triage_sources import LiveSources


@dataclasses.dataclass
class Report:
    inbox_before: int = 0
    ready_before: int = 0
    collapsed_groups: int = 0
    collapsed_children: int = 0
    promoted_as_filed: int = 0
    enrich_candidates: int = 0
    promoted_enriched: int = 0
    changed_underfoot: int = 0
    duplicates: int = 0
    corrections_logged: int = 0
    classes: dict[str, int] = dataclasses.field(default_factory=dict)
    verdicts: list[dict[str, str]] = dataclasses.field(default_factory=list)
    inbox_after: int = 0
    ready_after: int = 0
    committed: bool = False


@dataclasses.dataclass(frozen=True)
class Options:
    root: pathlib.Path
    apply: bool
    gate: Gate
    predict: PredictFn | None
    batch_size: int
    jobs: int
    limit: int | None
    today: str
    classify: ClassifyFn | None = None
    log: ClassLog | None = None


def _count(root: pathlib.Path, folder: str) -> int:
    return sum(1 for _ in (root / folder).glob("*.md"))


def _collapse(opts: Options, report: Report) -> list[str]:
    plans = plan_collapses(load_inbox(opts.root), opts.root)
    report.collapsed_groups = len(plans)
    report.collapsed_children = sum(len(p.children) for p in plans)
    return (
        [i for p in plans for i in apply_collapse(p, opts.root, opts.today)] if opts.apply else []
    )


def _candidates(
    opts: Options, report: Report, skip: Collection[str]
) -> tuple[list[Promotion], list[Promotion]]:
    inbox = [b for b in load_inbox(opts.root) if b.id not in skip]
    filed = [p for p in map(opts.gate.as_filed, inbox) if p is not None]
    to_fill = [b for b in inbox if opts.gate.needs_fill(b)][: opts.limit]
    report.enrich_candidates = len(to_fill)
    filled = (
        opts.gate.enrich_all(to_fill, opts.predict, opts.batch_size, opts.jobs)
        if opts.predict
        else []
    )
    report.promoted_as_filed, report.promoted_enriched = len(filed), len(filled)
    return filed, filled


def _promote_all(
    promotions: Sequence[Promotion], opts: Options, report: Report
) -> list[Promotion]:
    moved = [p for p in promotions if _promote_one(p, opts)]
    report.changed_underfoot = len(promotions) - len(moved)
    return moved


def _promote_one(p: Promotion, opts: Options) -> bool:
    try:
        return promote(p, opts.root, opts.today)
    except ValueError:
        return False


def _classify(
    opts: Options, report: Report, log: ClassLog, fixed: Mapping[str, Call], found: Sequence[Promotion]
) -> list[Verdict]:
    promoted = {p.original.id: p.reason for p in found}
    verdicts = classify(
        load_inbox(opts.root),
        fixed,
        promoted,
        opts.classify,
        log.examples(EXAMPLE_LIMIT),
        opts.batch_size,
        opts.jobs,
    )
    report.classes = dict(collections.Counter(v.cls.value for v in verdicts))
    report.verdicts = [{"id": v.brief_id, "class": v.cls.value, "reason": v.reason} for v in verdicts]
    return verdicts


def _record(log: ClassLog, verdicts: Sequence[Verdict], moved: Collection[str]) -> None:
    """Log each verdict; a ready one only once its brief has actually moved."""
    for v in verdicts:
        if v.cls is not BriefClass.READY or v.brief_id in moved:
            log.record_verdict(v)


def run(opts: Options) -> Report:
    log = opts.log or ClassLog(opts.root)
    report = Report(_count(opts.root, "inbox"), _count(opts.root, "ready"))
    report.corrections_logged = len(reconcile(opts.root, log)) if opts.apply else 0
    touched = _collapse(opts, report)
    dupes = duplicate_map(index(opts.root))
    fixed = fixed_calls(dupes, log.sent_back())
    report.duplicates = len(dupes)
    filed, filled = _candidates(opts, report, fixed)
    verdicts = _classify(opts, report, log, fixed, [*filed, *filled])
    moved = _promote_all([*filed, *filled], opts, report) if opts.apply else []
    if opts.apply:
        _record(log, verdicts, {p.original.id for p in moved})
    touched += [p.original.id for p in moved]
    report.committed = bool(touched) and sync_commit(
        f"triage: collapse {report.collapsed_children} into {report.collapsed_groups}, "
        f"promote {report.promoted_as_filed + report.promoted_enriched} inbox briefs to ready",
        opts.root,
        ids=touched,
    )
    report.inbox_after, report.ready_after = _count(opts.root, "inbox"), _count(opts.root, "ready")
    return report


def _progress(predict: PredictFn) -> PredictFn:
    def logged(briefs: Sequence[InboxBrief]) -> Mapping[str, Fill]:
        fills = predict(briefs)
        print(
            f"batch {briefs[0].id}..: {len(fills)}/{len(briefs)} filled",
            file=sys.stderr,
            flush=True,
        )
        return fills

    return logged


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Collapse bulk inbox filings and promote briefs that pass the gate."
    )
    ap.add_argument("command", choices=["promote"])
    ap.add_argument("--root", type=pathlib.Path)
    ap.add_argument(
        "--apply", action="store_true", help="write changes and commit (default: dry run)"
    )
    ap.add_argument("--llm", action="store_true", help="fill missing fields with claude -p")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, help="enrich at most this many briefs")
    args = ap.parse_args(argv)
    predict = _progress(claude_predict(args.model)) if args.llm else None
    classifier = claude_classify(args.model) if args.llm else None
    root = args.root or resolve_root()
    opts = Options(
        root,
        args.apply,
        Gate(LiveSources(root)),
        predict,
        args.batch_size,
        args.jobs,
        args.limit,
        datetime.date.today().isoformat(),
        classifier,
    )
    print(json.dumps(dataclasses.asdict(run(opts)), indent=2))


if __name__ == "__main__":
    main()  # pragma: no cover

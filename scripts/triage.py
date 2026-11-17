#!/usr/bin/env python3
"""
triage.py — keep the queue clean: collapse bulk filings, promote what passes the gate, close what is done.

    triage.py promote                    # dry run: what would collapse and promote, no LLM, no writes
    triage.py promote --llm              # dry run including enrichment of briefs missing fields
    triage.py promote --dry-run          # the planned closes grouped by reason, nothing written
    triage.py promote --apply --llm      # do it, then one commit for everything touched
    triage.py reopen <id>                # put a brief triage closed back where it came from

Every inbox brief also gets a class (ready, needs-info, duplicate or drop) and a reason, in the
report; --apply logs them, and logs a correction when the owner has since moved a brief against
its class. --apply then acts on evidence only (see triage_close): finished briefs in inbox/ and
ready/ go to done/, and a duplicate goes to done/ after its body is appended to the survivor.
Judgement never moves a brief: stale needs-info briefs and model-called drops stay where they are
and raise one aggregated alert per run (see triage_alerts). Nothing is ever deleted: every move is
logged in closed.jsonl and undone with `reopen`.

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
from triage_alerts import (
    ALERT_STATE,
    Alert,
    AlertLedger,
    Notifier,
    alert_text,
    alerts_plan,
    session_alert_notifier,
    undelivered,
    verdict_alerts,
)
from triage_brief import InboxBrief, load_folder, load_inbox
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
from triage_close import (
    MOVE_LOG,
    CloseRules,
    Move,
    apply_moves,
    capped,
    evidence_moves,
    plan_text,
    reopen,
    touched_ids,
    verdict_moves,
)
from triage_collapse import Collapse, apply_collapse, plan_collapses
from triage_enrich import Fill, PredictFn, claude_predict
from triage_evidence import PrStates, cited_pulls
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
    moves: list[Move] = dataclasses.field(default_factory=list)
    closed: dict[str, int] = dataclasses.field(default_factory=dict)
    alerts: list[Alert] = dataclasses.field(default_factory=list)
    alerts_held: int = 0
    alerts_delivered: bool = False
    closes_deferred: int = 0
    closes_changed_underfoot: int = 0
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
    rules: CloseRules = CloseRules()
    states: PrStates = dataclasses.field(default_factory=PrStates.offline)
    notify: Notifier = undelivered


def _count(root: pathlib.Path, folder: str) -> int:
    return sum(1 for _ in (root / folder).glob("*.md"))


def _plan_collapses(opts: Options, report: Report) -> list[Collapse]:
    plans = plan_collapses(load_inbox(opts.root), opts.root)
    report.collapsed_groups = len(plans)
    report.collapsed_children = sum(len(p.children) for p in plans)
    return plans


def _evidence(opts: Options, retired: Collection[str]) -> list[Move]:
    """Moves for briefs in inbox/ and ready/ whose evidence says they are finished."""
    folders = {
        "inbox": [b for b in load_inbox(opts.root) if b.id not in retired],
        "ready": load_folder(opts.root, "ready"),
    }
    opts.states.prefetch(p for briefs in folders.values() for b in briefs for p in cited_pulls(b))
    return [
        m for folder, briefs in folders.items() for m in evidence_moves(briefs, folder, opts.states)
    ]


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


def _promote_all(promotions: Sequence[Promotion], opts: Options, report: Report) -> list[Promotion]:
    moved = [p for p in promotions if _promote_one(p, opts)]
    report.changed_underfoot = len(promotions) - len(moved)
    return moved


def _promote_one(p: Promotion, opts: Options) -> bool:
    try:
        return promote(p, opts.root, opts.today)
    except ValueError:
        return False


def _classify(
    opts: Options,
    report: Report,
    log: ClassLog,
    fixed: Mapping[str, Call],
    found: Sequence[Promotion],
    inbox: Sequence[InboxBrief],
) -> list[Verdict]:
    promoted = {p.original.id: p.reason for p in found}
    verdicts = classify(
        inbox,
        fixed,
        promoted,
        opts.classify,
        log.examples(EXAMPLE_LIMIT),
        opts.batch_size,
        opts.jobs,
    )
    report.classes = dict(collections.Counter(v.cls.value for v in verdicts))
    report.verdicts = [
        {"id": v.brief_id, "class": v.cls.value, "reason": v.reason} for v in verdicts
    ]
    return verdicts


def _record(log: ClassLog, verdicts: Sequence[Verdict], moved: Collection[str]) -> None:
    """Log each verdict; a ready one only once its brief has actually moved."""
    for v in verdicts:
        if v.cls is not BriefClass.READY or v.brief_id in moved:
            log.record_verdict(v)


def _close(opts: Options, report: Report, planned: Sequence[Move]) -> list[Move]:
    """Make up to the cap's worth of `planned` moves and return those made; a dry run makes none."""
    selected, report.closes_deferred = capped(planned, opts.rules.max_moves)
    made = apply_moves(selected, opts.root, opts.today) if opts.apply else []
    shown = made if opts.apply else selected
    report.moves = list(shown)
    report.closed = dict(collections.Counter(str(m.reason) for m in shown))
    report.closes_changed_underfoot = len(selected) - len(shown)
    return made


def _alert(opts: Options, report: Report, flagged: Sequence[Alert]) -> bool:
    """Raise one alert for the flagged briefs not alerted lately; True when the ledger was written."""
    today = datetime.date.fromisoformat(opts.today)
    ledger = AlertLedger(opts.root)
    due, held = ledger.due(flagged, today, opts.rules.stale_days)
    report.alerts, report.alerts_held = due, len(held)
    if not (opts.apply and due):
        return False
    report.alerts_delivered = opts.notify(alert_text(due, opts.rules.stale_days))
    if report.alerts_delivered:
        ledger.save(flagged, {a.brief_id for a in due}, today)
    return report.alerts_delivered


def run(opts: Options) -> Report:
    log = opts.log or ClassLog(opts.root)
    report = Report(_count(opts.root, "inbox"), _count(opts.root, "ready"))
    report.corrections_logged = len(reconcile(opts.root, log)) if opts.apply else 0
    plans = _plan_collapses(opts, report)
    retired = {c.id for p in plans for c in p.children}
    touched = (
        [i for p in plans for i in apply_collapse(p, opts.root, opts.today)] if opts.apply else []
    )
    finished = _evidence(opts, retired)
    finished_inbox = {m.brief_id for m in finished if m.source == "inbox"}
    dupes = duplicate_map(index(opts.root))
    fixed = fixed_calls(dupes, log.sent_back())
    report.duplicates = len(dupes)
    filed, filled = _candidates(opts, report, {*fixed, *finished_inbox})
    inbox = [b for b in load_inbox(opts.root) if b.id not in finished_inbox]
    verdicts = _classify(opts, report, log, fixed, [*filed, *filled], inbox)
    moved = _promote_all([*filed, *filled], opts, report) if opts.apply else []
    if opts.apply:
        _record(log, verdicts, {p.original.id for p in moved})
    touched += [p.original.id for p in moved]
    standing = [v for v in verdicts if v.brief_id not in retired]
    by_id = {b.id: b for b in inbox}
    closed = _close(opts, report, [*finished, *verdict_moves(standing, by_id, dupes)])
    alerted = _alert(
        opts,
        report,
        verdict_alerts(standing, by_id, opts.rules, datetime.date.fromisoformat(opts.today)),
    )
    touched += touched_ids(closed)
    report.committed = (bool(touched) or alerted) and sync_commit(
        f"triage: collapse {report.collapsed_children} into {report.collapsed_groups}, "
        f"promote {report.promoted_as_filed + report.promoted_enriched} inbox briefs to ready, "
        f"close {len(closed)}, alert {len(report.alerts) if alerted else 0}",
        opts.root,
        ids=touched,
        paths=[*([MOVE_LOG] if closed else []), *([ALERT_STATE] if alerted else [])],
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


def _reopen(ap: argparse.ArgumentParser, brief_id: str | None, root: pathlib.Path) -> None:
    if not brief_id:
        ap.error("reopen needs a brief id")
    try:
        folder = reopen(brief_id, root, datetime.date.today().isoformat())
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
    sync_commit(f"triage: reopen {brief_id}", root, ids=[brief_id], paths=[MOVE_LOG])
    print(f"{brief_id} -> {folder}/")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Collapse bulk inbox filings and promote briefs that pass the gate."
    )
    ap.add_argument("command", choices=["promote", "reopen"])
    ap.add_argument("brief_id", nargs="?", help="the brief `reopen` puts back")
    ap.add_argument("--root", type=pathlib.Path)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="write changes and commit (default: dry run)"
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned closes grouped by reason instead of the JSON report; writes nothing",
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
    if args.command == "reopen":
        _reopen(ap, args.brief_id, root)
        return
    states = PrStates()
    opts = Options(
        root,
        args.apply,
        Gate(LiveSources(root, prs=states)),
        predict,
        args.batch_size,
        args.jobs,
        args.limit,
        datetime.date.today().isoformat(),
        classifier,
        rules=CloseRules.load(root),
        states=states,
        notify=session_alert_notifier(),
    )
    report = run(opts)
    if args.dry_run:
        print(plan_text(report.moves, report.closes_deferred, opts.rules.max_moves))
        print(alerts_plan(report.alerts, report.alerts_held))
    else:
        print(json.dumps(dataclasses.asdict(report), indent=2))


if __name__ == "__main__":
    main()  # pragma: no cover

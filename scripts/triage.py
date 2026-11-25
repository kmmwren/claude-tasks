#!/usr/bin/env python3
"""
triage.py: keep the queue clean by collapsing bulk filings, promoting what passes the gate and closing what is done.

triage.py: keep the queue clean by collapsing bulk filings, promoting what passes the gate and closing what is done.

    triage.py promote [--llm] [--dry-run | --apply]    # default: dry run, no writes
    triage.py reopen <id>                              # put a brief triage closed back

Every inbox brief also gets a class (ready, needs-info, duplicate or drop) and a reason, in the
report; --apply logs them, and logs a correction when the owner has since moved a brief against
its class; a brief a model was asked about and left unanswered gets none and is counted as
unclassified. A brief that does not pass stays in the inbox exactly as it was; only evidence or a
fingerprinted duplicate closes one, and judgement raises an alert instead.

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
from git_sync import SyncResult, sync_commit
from headless import TALLY, configured_launch, no_capacity, reset_capacity
from triage_alerts import (
    ALERT_STATE,
    Alert,
    AlertLedger,
    Notifier,
    alert_text,
    alerts_plan,
    refusal_alerts,
    session_alert_notifier,
    undelivered,
    undelivered_text,
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
    Applied,
    CloseRules,
    Move,
    capped,
    evidence_moves,
    plan_text,
    reopen,
    settle_moves,
    verdict_moves,
)
from triage_collapse import Collapse, apply_collapse, plan_collapses
from triage_declare import declare_empty
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
    unclassified: int = 0
    classes: dict[str, int] = dataclasses.field(default_factory=dict)
    verdicts: list[dict[str, str]] = dataclasses.field(default_factory=list)
    moves: list[Move] = dataclasses.field(default_factory=list)
    closed: dict[str, int] = dataclasses.field(default_factory=dict)
    alerts: list[Alert] = dataclasses.field(default_factory=list)
    alerts_held: int = 0
    alerts_delivered: bool = False
    alert_notice: str = ""
    closes_deferred: int = 0
    closes_changed_underfoot: int = 0
    closes_refused: int = 0
    inbox_after: int = 0
    ready_after: int = 0
    committed: bool = False
    declared_empty: int = 0
    held_for_fill: int = 0
    llm_calls: int = 0
    llm_failed: int = 0
    llm_skipped: int = 0


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
        log.latest(),
    )
    report.unclassified = len(inbox) - len(verdicts)
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


def _close(opts: Options, report: Report, planned: Sequence[Move]) -> Applied:
    """Make up to the cap's worth of `planned` moves and return the attempts; a dry run makes none."""
    selected, report.closes_deferred = capped(planned, opts.rules.max_moves)
    applied = (
        settle_moves(selected, opts.root, opts.today, opts.states) if opts.apply else Applied(())
    )
    shown = applied.landed if opts.apply else selected
    report.moves = list(shown)
    report.closed = dict(collections.Counter(str(m.reason) for m in shown))
    report.closes_changed_underfoot = len(selected) - len(shown)
    report.closes_refused = len(applied.refused)
    return applied


def _alert(opts: Options, report: Report, flagged: Sequence[Alert]) -> bool:
    """Raise one alert for the flagged briefs not alerted lately; True when the ledger was written."""
    today = datetime.date.fromisoformat(opts.today)
    ledger = AlertLedger(opts.root)
    due, held = ledger.due(flagged, today, opts.rules.stale_days)
    report.alerts, report.alerts_held = due, len(held)
    if not (opts.apply and due):
        return False
    report.alerts_delivered = opts.notify(alert_text(due, opts.rules.stale_days))
    if not report.alerts_delivered:
        report.alert_notice = undelivered_text(due)
        return False
    ledger.save(flagged, {a.brief_id for a in due}, today)
    return True


@dataclasses.dataclass(frozen=True)
class Standing:
    """What is left to act on once collapses and evidence closes are set aside."""

    verdicts: list[Verdict]
    inbox: dict[str, InboxBrief]
    dupes: Mapping[str, str]
    promoted: list[str]


def _declare(opts: Options, report: Report) -> list[str]:
    """Declare the briefs with no blockers; returns those written (none on a dry run)."""
    declarations = declare_empty(opts.root, opts.apply, opts.today)
    report.declared_empty, report.held_for_fill = len(declarations.declared), len(declarations.held)
    return list(declarations.declared) if opts.apply else []


def _collapse(opts: Options, report: Report) -> tuple[list[str], set[str]]:
    """Collapse bulk filings; returns the briefs written and the child ids now retired."""
    plans = _plan_collapses(opts, report)
    retired = {c.id for p in plans for c in p.children}
    plans_to_apply = plans if opts.apply else []
    written = [i for p in plans_to_apply for i in apply_collapse(p, opts.root, opts.today)]
    return written, retired


def _classify_and_promote(
    opts: Options, report: Report, log: ClassLog, retired: Collection[str], finished: Sequence[Move]
) -> Standing:
    finished_inbox = {m.brief_id for m in finished if m.source == "inbox"}
    dupes = duplicate_map(index(opts.root))
    fixed = fixed_calls(dupes, log.sent_back())
    report.duplicates = len(dupes)
    filed, filled = _candidates(opts, report, {*fixed, *finished_inbox})
    inbox = [b for b in load_inbox(opts.root) if b.id not in finished_inbox]
    verdicts = _classify(opts, report, log, fixed, [*filed, *filled], inbox)
    promoted = _promote_and_record(opts, report, log, verdicts, [*filed, *filled])
    standing = [v for v in verdicts if v.brief_id not in retired]
    return Standing(standing, {b.id: b for b in inbox}, dupes, promoted)


def _promote_and_record(
    opts: Options,
    report: Report,
    log: ClassLog,
    verdicts: Sequence[Verdict],
    candidates: Sequence[Promotion],
) -> list[str]:
    """Promote the candidates, then log the verdicts; a dry run writes and logs nothing."""
    if not opts.apply:
        return []
    promoted = [p.original.id for p in _promote_all(candidates, opts, report)]
    _record(log, verdicts, promoted)
    return promoted


def _alert_and_commit(
    opts: Options, report: Report, touched: list[str], applied: Applied, standing: Standing
) -> None:
    today = datetime.date.fromisoformat(opts.today)
    flagged = verdict_alerts(standing.verdicts, standing.inbox, opts.rules, today)
    flagged += refusal_alerts(applied.refused)
    alerted = _alert(opts, report, flagged)
    report.committed = (bool(touched) or alerted) and (
        sync_commit(
            f"triage: collapse {report.collapsed_children} into {report.collapsed_groups}, "
            f"promote {report.promoted_as_filed + report.promoted_enriched} inbox briefs to ready, "
            f"close {len(applied.landed)}, alert {len(report.alerts) if alerted else 0}",
            opts.root,
            ids=sorted(set(touched)),
            paths=[*([MOVE_LOG] if applied.landed else []), *([ALERT_STATE] if alerted else [])],
        )
        is not SyncResult.DEFERRED
    )


def run(opts: Options) -> Report:
    log = opts.log or ClassLog(opts.root)
    report = Report(_count(opts.root, "inbox"), _count(opts.root, "ready"))
    report.corrections_logged = len(reconcile(opts.root, log)) if opts.apply else 0
    touched, retired = _collapse(opts, report)
    touched += _declare(opts, report)
    finished = _evidence(opts, retired)
    standing = _classify_and_promote(opts, report, log, retired, finished)
    touched += standing.promoted
    dupe_moves = verdict_moves(standing.verdicts, standing.inbox, standing.dupes)
    applied = _close(opts, report, [*finished, *dupe_moves])
    touched += applied.touched
    _alert_and_commit(opts, report, touched, applied, standing)
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


def _add_mode(ap: argparse.ArgumentParser) -> None:
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write and commit (default: dry run)")
    mode.add_argument(
        "--dry-run", action="store_true", help="print the planned closes and alerts; writes nothing"
    )


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Collapse, promote and close inbox briefs.")
    ap.add_argument("command", choices=["promote", "reopen"])
    ap.add_argument("brief_id", nargs="?", help="the brief `reopen` puts back")
    ap.add_argument("--root", type=pathlib.Path)
    _add_mode(ap)
    ap.add_argument("--llm", action="store_true", help="fill missing fields with claude -p")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, help="enrich at most this many briefs")
    return ap


def _llm_calls(
    args: argparse.Namespace, root: pathlib.Path
) -> tuple[PredictFn | None, ClassifyFn | None]:
    """The fill and classify calls when `--llm` is set, both on the queue's configured launcher."""
    if not args.llm:
        return None, None
    launch = configured_launch(root)
    return (
        _progress(claude_predict(args.model, launch=launch)),
        claude_classify(args.model, launch=launch),
    )


def _options(args: argparse.Namespace, root: pathlib.Path) -> Options:
    states = PrStates()
    gate = Gate(LiveSources(root, prs=states))
    predict, classifier = _llm_calls(args, root)
    today = datetime.date.today().isoformat()
    base = Options(root, args.apply, gate, predict, args.batch_size, args.jobs, args.limit, today)
    return dataclasses.replace(
        base,
        classify=classifier,
        rules=CloseRules.load(root),
        states=states,
        notify=session_alert_notifier(),
    )


def _tally(report: Report) -> None:
    report.llm_calls, report.llm_failed, report.llm_skipped = (
        TALLY.calls,
        TALLY.failed,
        TALLY.skipped,
    )
    if no_capacity():
        print(f"inbox-triage: no headless capacity; skipped {TALLY.skipped} calls", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    ap = _parser()
    args = ap.parse_args(argv)
    root = args.root or resolve_root()
    if args.command == "reopen":
        return _reopen(ap, args.brief_id, root)
    reset_capacity()  # a latch lasts the process, so queues run in it share one
    opts = _options(args, root)
    report = run(opts)
    _tally(report)
    if args.dry_run:
        print(plan_text(report.moves, report.closes_deferred, opts.rules.max_moves))
        print(alerts_plan(report.alerts, report.alerts_held))
    else:
        print(json.dumps(dataclasses.asdict(report), indent=2))
    if TALLY.all_failed:
        # A scheduled run that fills nothing still looked healthy; failing lets the watchdog see it.
        sys.exit(f"inbox-triage: all {TALLY.calls} headless claude calls failed")


if __name__ == "__main__":
    main()  # pragma: no cover

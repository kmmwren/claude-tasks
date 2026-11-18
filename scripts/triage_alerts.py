#!/usr/bin/env python3
"""
triage_alerts.py: tell the owner about briefs that need a human call, and leave them where they are.

A stale needs-info brief or a model-called drop is flagged, never moved. One aggregated alert goes
out per run; <queue>/.triage-alerts.json holds when each brief was last alerted.
"""

import dataclasses
import datetime
import enum
import importlib.util
import json
import pathlib
import sys
from collections.abc import Callable, Collection, Mapping, Sequence

from _concurrency import atomic_write
from class_log import BriefClass, Verdict
from triage_brief import InboxBrief
from triage_close import CloseRules, eligible, idle_days

ALERT_STATE = ".triage-alerts.json"
CHANNEL = "triage"
PROJECT = "claude-tasks"
KIND = "triage-needs-decision"
SCRIPTS_DIR = pathlib.Path.home() / ".claude" / "scripts"

Notifier = Callable[[str], bool]


class AlertReason(enum.StrEnum):
    STALE = "stale needs-info"
    DROP = "model-suggested drop"


@dataclasses.dataclass(frozen=True)
class Alert:
    brief_id: str
    title: str
    reason: AlertReason
    detail: str


def _flag(v: Verdict, brief: InboxBrief, rules: CloseRules, today: datetime.date) -> Alert | None:
    if not eligible(brief):
        return None
    if v.cls is BriefClass.DROP:
        return Alert(brief.id, brief.title, AlertReason.DROP, v.reason)
    idle = idle_days(brief, today) if v.cls is BriefClass.NEEDS_INFO else None
    if idle is not None and idle > rules.stale_days:
        return Alert(brief.id, brief.title, AlertReason.STALE, f"untouched for {idle} days")
    return None


def verdict_alerts(
    verdicts: Sequence[Verdict],
    inbox: Mapping[str, InboxBrief],
    rules: CloseRules,
    today: datetime.date,
) -> list[Alert]:
    """The briefs this run's verdicts want a human to look at: stale needs-info and drops."""
    flags = (_flag(v, inbox[v.brief_id], rules, today) for v in verdicts)
    return [a for a in flags if a]


class AlertLedger:
    """When each flagged brief was last alerted, so a standing flag is not raised every run."""

    def __init__(self, root: pathlib.Path) -> None:
        self.path = root / ALERT_STATE
        self.raised = self._load()

    def _load(self) -> dict[str, datetime.date]:
        try:
            stored = json.loads(self.path.read_text())
            return {k: datetime.date.fromisoformat(v) for k, v in stored.items()}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def due(
        self, flagged: Sequence[Alert], today: datetime.date, quiet_days: int
    ) -> tuple[list[Alert], list[Alert]]:
        """Alerts to raise now, and alerts held back because one went out within `quiet_days`."""
        due: list[Alert] = []
        held: list[Alert] = []
        for alert in flagged:
            last = self.raised.get(alert.brief_id)
            quiet = last is not None and (today - last).days <= quiet_days
            (held if quiet else due).append(alert)
        return due, held

    def save(
        self, flagged: Sequence[Alert], delivered: Collection[str], today: datetime.date
    ) -> None:
        """Record `delivered` as alerted today; keep earlier dates for the rest, forget unflagged ids."""
        ids = {a.brief_id for a in flagged}
        kept = {
            i: today if i in delivered else self.raised[i]
            for i in sorted(ids)
            if i in delivered or i in self.raised
        }
        atomic_write(
            self.path, json.dumps({i: d.isoformat() for i, d in kept.items()}, indent=2) + "\n"
        )
        self.raised = kept


def alert_text(alerts: Sequence[Alert], stale_days: int) -> str:
    """One message for the whole run: a count, then each brief by reason with its id and title."""
    lines = [
        f"Tell Katherine, she decides (she may be away, so keep it on the ledger until she answers): "
        f"inbox triage found {len(alerts)} briefs that need her call. Each is left where it is; "
        f"none was moved. Raised again after {stale_days} more days."
    ]
    for reason in AlertReason:
        group = [a for a in alerts if a.reason is reason]
        if group:
            lines.append(f"{reason} ({len(group)}):")
            lines += [f"- {a.brief_id}: {a.title} ({a.detail})" for a in group]
    return "\n".join(lines)


def alerts_plan(due: Sequence[Alert], held: int) -> str:
    """The alerts a dry run would raise, grouped by reason, and how many are held back."""
    lines = [f"{len(due)} alerts to raise (briefs stay where they are)"]
    for reason in AlertReason:
        group = [a for a in due if a.reason is reason]
        if group:
            lines += [f"{reason}: {len(group)}"] + [f"  {a.brief_id}: {a.detail}" for a in group]
    if held:
        lines.append(f"{held} more already alerted recently")
    return "\n".join(lines)


def _warn(why: str) -> bool:
    print(f"triage: alert not delivered, {why}", file=sys.stderr)
    return False


def session_alert_notifier(scripts_dir: pathlib.Path = SCRIPTS_DIR) -> Notifier:
    """Post to the session-alerts channel; warn on stderr and report False when it cannot."""

    def notify(text: str) -> bool:
        script = scripts_dir / "session_alerts.py"
        if not script.exists():
            return _warn(f"{script} is absent")
        try:
            spec = importlib.util.spec_from_file_location("session_alerts", script)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.post(CHANNEL, PROJECT, KIND, text)
        except (
            Exception,
            SystemExit,
        ) as err:  # a broken alert script never stops triage or loses a close
            return _warn(f"{script.name} failed: {err}")
        return True

    return notify


def undelivered_text(alerts: Sequence[Alert]) -> str:
    """The run-report line for alerts that went nowhere, so a headless run shows them too."""
    ids = ", ".join(a.brief_id for a in alerts)
    return f"{len(alerts)} alerts NOT delivered and not recorded, raised again next run: {ids}"


def undelivered(text: str) -> bool:
    """The notifier used when none is configured: it warns and delivers nothing."""
    return _warn("no notifier configured")

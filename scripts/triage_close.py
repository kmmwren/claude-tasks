#!/usr/bin/env python3
"""
triage_close.py: act on triage's evidence by moving briefs to done/, never deleting one and never
closing on judgement alone.

A brief closes when its own evidence says it is finished (`closed_reason: evidence`, see
triage_evidence) or when it repeats another brief (`closed_reason: duplicate`); the duplicate's body
is appended to the survivor first, so no content lives only in the closed file. What takes judgement
(a needs-info brief gone stale, a model-called drop) is never moved: triage_alerts tells the owner.
A brief carrying a closing reason is never closed again, a claimed brief is left alone, and each run
closes at most `[triage] max_moves`, evidence first. Every move is one line in <queue>/closed.jsonl,
and `reopen` puts a brief back with one command, clearing its closing fields.

Stdlib only.
"""

import dataclasses
import datetime
import enum
import fcntl
import json
import pathlib
import re
from collections.abc import Mapping, Sequence

from _concurrency import atomic_write, require_lock
from class_log import BriefClass, Verdict
from config import load_config
from readiness import EXECUTION_LOG
from transition import _append_log, find_brief, move_brief
from triage_brief import InboxBrief
from triage_evidence import PrStates, closing_evidence

MOVE_LOG = "closed.jsonl"
STALE_DAYS = 21
MAX_MOVES = 200
DONE = "done"
REOPENED = "reopened"
REOPENED_FIELD = "reopened"
CLOSING_FIELDS = ("closed_reason", "closed_evidence", "duplicate_of")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)


class Reason(enum.StrEnum):
    """Why a brief moves; the declaration order is the order the per-run cap spends its budget in."""

    EVIDENCE = "evidence"
    DUPLICATE = "duplicate"


@dataclasses.dataclass(frozen=True)
class Move:
    brief_id: str
    title: str
    source: str
    reason: Reason
    detail: str
    survivor: str = ""

    @property
    def fields(self) -> dict[str, str]:
        """The frontmatter the move records, so the brief says why it left."""
        match self.reason:
            case Reason.DUPLICATE:
                return {"closed_reason": "duplicate", "duplicate_of": self.survivor}
            case Reason.EVIDENCE:
                return {"closed_reason": "evidence", "closed_evidence": self.detail}

    @property
    def log(self) -> str:
        return (
            f"closed by inbox-triage ({self.reason}): {self.detail}; "
            f"`triage.py reopen {self.brief_id}` puts it back in {self.source}/"
        )


@dataclasses.dataclass(frozen=True)
class CloseRules:
    stale_days: int = STALE_DAYS
    max_moves: int = MAX_MOVES

    @classmethod
    def load(cls, root: pathlib.Path) -> "CloseRules":
        """The `[triage]` table of tasks.toml; a missing or malformed value falls back to its default."""
        table = load_config(root).get("triage")
        configured = table if isinstance(table, dict) else {}
        return cls(
            _whole(configured.get("stale_days"), STALE_DAYS, 1),
            _whole(configured.get("max_moves"), MAX_MOVES, 0),
        )


def _whole(value: object, default: int, minimum: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    return default


class MoveLog:
    """Append-only JSONL of every move and reopen, in the queue so it travels with the briefs."""

    def __init__(self, root: pathlib.Path) -> None:
        self.path = root / MOVE_LOG

    def append(self, move: Move) -> None:
        self._write(move.brief_id, move.source, DONE, str(move.reason), move.detail)

    def append_reopen(self, brief_id: str, was_in: str, back_to: str) -> None:
        self._write(brief_id, was_in, back_to, REOPENED, f"back in {back_to}/")

    def last(self, brief_id: str) -> dict[str, str] | None:
        """The newest row for `brief_id`, or None when triage never moved it."""
        rows = (
            (json.loads(line) for line in self.path.read_text().splitlines())
            if self.path.exists()
            else ()
        )
        found = [r for r in rows if r["id"] == brief_id]
        return found[-1] if found else None

    def _write(self, brief_id: str, source: str, target: str, reason: str, detail: str) -> None:
        row = {
            "id": brief_id,
            "from": source,
            "to": target,
            "reason": reason,
            "evidence": detail,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        with self.path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def eligible(brief: InboxBrief) -> bool:
    """Not claimed, not closed, and not put back by the owner: a reopened brief stays open."""
    fields = brief.fields
    return not any(fields.get(k) for k in ("assignee", "closed_reason", REOPENED_FIELD))


def evidence_moves(briefs: Sequence[InboxBrief], folder: str, states: PrStates) -> list[Move]:
    """A move for each eligible brief in `folder` whose own evidence says it is finished."""
    found = ((b, closing_evidence(b, states)) for b in briefs if eligible(b))
    return [Move(b.id, b.title, folder, Reason.EVIDENCE, why) for b, why in found if why]


def idle_days(brief: InboxBrief, today: datetime.date) -> int | None:
    """Days since `updated`, else `created`; None when neither is a date."""
    stamp = brief.fields.get("updated") or brief.fields.get("created", "")
    try:
        return (today - datetime.date.fromisoformat(stamp)).days
    except ValueError:
        return None


def verdict_moves(
    verdicts: Sequence[Verdict], inbox: Mapping[str, InboxBrief], dupes: Mapping[str, str]
) -> list[Move]:
    """A move for each eligible brief the fingerprint shows repeats another; the model alone never closes one."""
    calls = [
        v
        for v in verdicts
        if v.cls is BriefClass.DUPLICATE and v.brief_id in dupes and eligible(inbox[v.brief_id])
    ]
    return [
        Move(
            v.brief_id,
            inbox[v.brief_id].title,
            "inbox",
            Reason.DUPLICATE,
            v.reason,
            dupes[v.brief_id],
        )
        for v in calls
    ]


def capped(moves: Sequence[Move], limit: int) -> tuple[list[Move], int]:
    """At most `limit` moves, evidence first, and how many were left for a later run."""
    order = list(Reason)
    ordered = sorted(moves, key=lambda m: (order.index(m.reason), m.brief_id))
    return ordered[:limit], max(len(ordered) - limit, 0)


def _body(text: str) -> str:
    """A brief's text without its frontmatter or execution log (the log stays in the closed file)."""
    return EXECUTION_LOG.sub("", _FRONTMATTER.sub("", text, count=1)).strip()


def _demoted(body: str) -> str:
    """Headings pushed below the `## Merged from` heading, so they never read as the survivor's own."""
    return re.sub(r"(?m)^#(?=\s)", "###", re.sub(r"(?m)^(#{2,5})(?=\s)", r"\1#", body))


def _with_merged(survivor: str, source_id: str, source: str) -> str:
    """`survivor` with `source` appended under `## Merged from <id>`, ahead of the execution log."""
    heading = f"## Merged from {source_id}"
    if heading in survivor:
        return survivor
    block = f"{heading}\n\n{_demoted(_body(source))}\n\n"
    log = EXECUTION_LOG.search(survivor)
    if log is None:
        return survivor.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"
    return survivor[: log.start()] + block + survivor[log.start() :]


def _merge_into_survivor(move: Move, root: pathlib.Path, today: str) -> bool:
    """Copy the duplicate's body into the survivor and log it; False when either brief is gone."""
    with require_lock(root):
        survivor = find_brief(move.survivor, root)
        duplicate = root / move.source / f"{move.brief_id}.md"
        if survivor is None or not duplicate.exists():
            return False
        merged = _with_merged(survivor.read_text(), move.brief_id, duplicate.read_text())
        line = f"merged {move.brief_id}: {move.title}"
        atomic_write(survivor, _append_log(merged, today, line))
    return True


def _apply(move: Move, root: pathlib.Path, today: str) -> bool:
    if move.survivor and not _merge_into_survivor(move, root, today):
        return False
    try:
        move_brief(
            move.brief_id,
            DONE,
            root,
            log=move.log,
            today=today,
            fields=move.fields,
            only_if=lambda: (root / move.source / f"{move.brief_id}.md").exists(),
        )
    except ValueError:
        return False
    return True


def apply_moves(moves: Sequence[Move], root: pathlib.Path, today: str) -> list[Move]:
    """Make each move and log it; a brief that left its folder since it was read, or whose
    survivor is gone, is skipped."""
    log = MoveLog(root)
    done = [m for m in moves if _apply(m, root, today)]
    for move in done:
        log.append(move)
    return done


def reopen(brief_id: str, root: pathlib.Path, today: str) -> str:
    """Put a brief triage closed back where it came from; returns that folder.

    Its closing fields are cleared and `reopened: <date>` is set, so triage never closes it again.
    """
    row = MoveLog(root).last(brief_id)
    if row is None or row["reason"] == REOPENED:
        raise ValueError(f"triage has not closed {brief_id!r}, or it is already reopened")
    path = find_brief(brief_id, root)
    if path is None or path.parent.name != row["to"]:
        raise ValueError(f"{brief_id!r} is no longer in {row['to']}/; move it by hand")
    move_brief(
        brief_id,
        row["from"],
        root,
        log=f"reopened by its owner; triage had closed it ({row['reason']})",
        today=today,
        fields={**dict.fromkeys(CLOSING_FIELDS, ""), REOPENED_FIELD: today},
    )
    MoveLog(root).append_reopen(brief_id, row["to"], row["from"])
    return row["from"]


def touched_ids(moves: Sequence[Move]) -> list[str]:
    """Every brief a set of moves wrote: the ones moved and the survivors that gained a body."""
    return [*(m.brief_id for m in moves), *(m.survivor for m in moves if m.survivor)]


def plan_text(moves: Sequence[Move], deferred: int, limit: int) -> str:
    """The moves grouped by reason with counts, and a line when the cap held some back."""
    lines = [f"{len(moves)} planned moves"]
    for reason in Reason:
        group = [m for m in moves if m.reason is reason]
        if group:
            lines += [f"{reason}: {len(group)} -> {DONE}/"]
            lines += [f"  {m.brief_id} (from {m.source}): {m.detail}" for m in group]
    if deferred:
        lines.append(f"capped at {limit} moves per run: {deferred} more left for the next run")
    return "\n".join(lines)

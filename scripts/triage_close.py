#!/usr/bin/env python3
"""
triage_close.py: close briefs on evidence or as duplicates by moving them to done/, never deleting.

Judgement (a stale needs-info brief, a model-called drop) never moves anything: triage_alerts tells
the owner. Every move is logged and `reopen` undoes it; the closed brief also records its origin.
"""

import dataclasses
import datetime
import enum
import fcntl
import json
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence

from _concurrency import atomic_write, require_lock
from class_log import BriefClass, Verdict
from config import load_config
from deps import fm
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
CLOSED_REASON = "closed_reason"
CLOSED_EVIDENCE = "closed_evidence"
DUPLICATE_OF = "duplicate_of"
CLOSED_FROM = "closed_from"
CLOSING_FIELDS = (CLOSED_REASON, CLOSED_EVIDENCE, DUPLICATE_OF, CLOSED_FROM)
OPEN_FOLDERS = ("inbox", "ready")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)
_FENCE = re.compile(r"\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,5})(?=\s|$)")


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
        """The frontmatter the move records, so the brief alone says why it left and where from."""
        evidence = self.detail if self.reason is Reason.EVIDENCE else ""
        values = {
            CLOSED_REASON: str(self.reason),
            CLOSED_EVIDENCE: evidence,
            DUPLICATE_OF: self.survivor,
            CLOSED_FROM: self.source,
        }
        return {k: values[k] for k in CLOSING_FIELDS if values[k]}

    @property
    def log(self) -> str:
        return (
            f"closed by inbox-triage ({self.reason}): {self.detail}; "
            f"`triage.py reopen {self.brief_id}` puts it back in {self.source}/"
        )


@dataclasses.dataclass(frozen=True)
class MoveRecord:
    """One row of the move log: a brief that went from `source` to `target`, and why."""

    brief_id: str
    source: str
    target: str
    reason: str
    evidence: str


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

    def last(self, brief_id: str) -> MoveRecord | None:
        """The newest row for `brief_id`, or None when triage never logged a move of it."""
        rows = (
            (json.loads(line) for line in self.path.read_text().splitlines())
            if self.path.exists()
            else ()
        )
        found = [r for r in rows if r["id"] == brief_id]
        if not found:
            return None
        row = found[-1]
        return MoveRecord(brief_id, row["from"], row["to"], row["reason"], row["evidence"])

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
    return not any(fields.get(k) for k in ("assignee", CLOSED_REASON, REOPENED_FIELD))


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


def _demote(line: str) -> str:
    """One heading line pushed down; a lone `#` goes to `###` so it sits below `## Merged from`."""
    return _HEADING.sub(lambda m: "###" if m[1] == "#" else m[1] + "#", line, count=1)


def _demoted(body: str) -> str:
    """Headings pushed below the `## Merged from` heading; fenced code, where `#` is a comment, is kept."""
    lines, fenced = [], False
    for line in body.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            lines.append(line)
        else:
            lines.append(line if fenced else _demote(line))
    return "\n".join(lines)


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


def _open_survivor(move: Move, root: pathlib.Path) -> pathlib.Path | None:
    """The survivor when it is still open and unclaimed; a closed or claimed one takes no merge."""
    path = find_brief(move.survivor, root)
    if path is None or path.parent.name not in OPEN_FOLDERS:
        return None
    return path if eligible(InboxBrief(path, path.read_text())) else None


def _merge_into_survivor(move: Move, root: pathlib.Path, today: str, states: PrStates) -> bool:
    """Copy the duplicate's body into an open survivor and log it; False when it cannot be merged."""
    with require_lock(root):
        survivor = _open_survivor(move, root)
        duplicate = root / move.source / f"{move.brief_id}.md"
        if survivor is None or not _closable(move, root, states):
            return False
        merged = _with_merged(survivor.read_text(), move.brief_id, duplicate.read_text())
        line = f"merged {move.brief_id}: {move.title}"
        atomic_write(survivor, _append_log(merged, today, line))
    return True


def _closable(move: Move, root: pathlib.Path, states: PrStates) -> bool:
    """Whether the brief, as it reads now, is still unclaimed and (for evidence) still finished."""
    path = root / move.source / f"{move.brief_id}.md"
    if not path.exists():
        return False
    brief = InboxBrief(path, path.read_text())
    return eligible(brief) and (
        move.reason is Reason.DUPLICATE or bool(closing_evidence(brief, states))
    )


def _skip(move: Move, err: Exception) -> bool:
    print(f"triage: {move.brief_id} not closed, left in {move.source}/: {err}", file=sys.stderr)
    return False


def _apply(move: Move, root: pathlib.Path, today: str, states: PrStates, log: MoveLog) -> bool:
    """Make one move and log it at once, so a later failure never leaves a move unlogged."""
    try:
        if move.survivor and not _merge_into_survivor(move, root, today, states):
            return False
        move_brief(
            move.brief_id,
            DONE,
            root,
            log=move.log,
            today=today,
            fields=move.fields,
            only_if=lambda: _closable(move, root, states),
        )
    except ValueError:
        return False
    except (RuntimeError, TimeoutError) as err:
        return _skip(move, err)
    log.append(move)
    return True


def apply_moves(
    moves: Sequence[Move], root: pathlib.Path, today: str, states: PrStates | None = None
) -> list[Move]:
    """Make each move, logging each as it lands; a move that cannot be made safely is skipped."""
    log, states = MoveLog(root), states or PrStates.offline()
    return [m for m in moves if _apply(m, root, today, states, log)]


def _recorded_on_brief(brief_id: str, root: pathlib.Path) -> MoveRecord | None:
    """The closure as the brief itself records it, for a move whose log row was never written."""
    path = find_brief(brief_id, root)
    fields = fm(path.read_text()) if path else {}
    if not (path and fields.get(CLOSED_REASON) and fields.get(CLOSED_FROM)):
        return None
    detail = fields.get(CLOSED_EVIDENCE) or fields.get(DUPLICATE_OF, "")
    return MoveRecord(
        brief_id, fields[CLOSED_FROM], path.parent.name, fields[CLOSED_REASON], detail
    )


def _closure(brief_id: str, root: pathlib.Path) -> MoveRecord:
    row = MoveLog(root).last(brief_id) or _recorded_on_brief(brief_id, root)
    if row is None or row.reason == REOPENED:
        raise ValueError(f"triage has not closed {brief_id!r}, or it is already reopened")
    return row


def reopen(brief_id: str, root: pathlib.Path, today: str) -> str:
    """Put a brief triage closed back where it came from; returns that folder.

    Its closing fields are cleared and `reopened: <date>` is set, so triage never closes it again.
    """
    row = _closure(brief_id, root)
    path = find_brief(brief_id, root)
    if path is None or path.parent.name != row.target:
        raise ValueError(f"{brief_id!r} is no longer in {row.target}/; move it by hand")
    cleared = {**dict.fromkeys(CLOSING_FIELDS, ""), REOPENED_FIELD: today}
    log = f"reopened by its owner; triage had closed it ({row.reason})"
    move_brief(brief_id, row.source, root, log=log, today=today, fields=cleared)
    MoveLog(root).append_reopen(brief_id, row.target, row.source)
    return row.source


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

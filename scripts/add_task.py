#!/usr/bin/env python3
"""
add_task.py — let any Claude session add a well-formed brief to the queue.

Fills out a brief from flags so Claude can self-capture follow-ups, discovered work, and
improvements without hand-writing the file. Defaults to inbox/ (raw); a ready/ brief
requires --goal, --criteria and --context so it never carries placeholder stubs.

Every brief declares its dependencies: --no-deps writes `blocked_by: []`; --blocked-by ID (a brief
id, or external:<reason> with a --probe 'N: <command>') files the brief to blocked/ until they clear.
A ready/ or blocked/ brief is refused without one of them; an inbox/ brief may leave it to triage.

A capture whose fingerprint (normalised title plus the sources it cites) matches a brief already
in the queue is a repeat: --on-duplicate merge (default) adds it to that brief as a log line and,
when it carries new context, a note; refuse files nothing and exits 1; file captures it anyway.
Either way the output says which brief it matched. Stdlib only.

Examples:
  python scripts/add_task.py --title "Rotate the leaked API key" \
     --goal "The leaked key is rotated and the app still works." \
     --context "Key leaked in PR 123; rotate in the vault." --criteria "Old key rejected by the API" \
     --domain security --importance 1 --autonomy needs-input --due 2026-06-05 --ready --no-deps --commit

  # waits on another brief, or on something outside the queue (probe exits 0 once it is true)
  python scripts/add_task.py --title "Ship the importer" --goal "..." --context "..." --criteria "..." \
     --blocked-by 2026-11-04-fix-flaky-fixture --blocked-by "external:vendor PR merged" \
     --probe "2: gh pr view 12 --json merged --jq .merged | grep -q true"

  # work found mid-task: queue it with the brief it blocks, so the prioritiser can rank it
  python scripts/add_task.py --title "Fix flaky fixture" --blocks 2026-11-04-ship-the-importer

  echo "idea: a hook that blocks committing secrets" | \
     python scripts/add_task.py --stdin --title "Pre-commit secret scanner"
"""

import argparse
import dataclasses
import datetime
import pathlib
import re
import sys

from _concurrency import atomic_write, require_lock, reserve_name
from _paths import COMMENT_START, TASK_DIRS, require_queue, resolve_root
from deps import (
    CONFIRMED_KEY,
    PROBE_KEY,
    date_of,
    is_date,
    is_external,
    load_queue,
    probe_gaps,
)
from fingerprint import Fingerprint, Located, find_duplicate
from git_sync import sync_commit as _sync_commit
from readiness import (
    PLACEHOLDER_CONTEXT,
    missing_ready_fields,
    placeholder_criterion,
    placeholder_goal,
)

# The CLI flag that supplies each field a ready brief needs.
FLAGS = {"goal": "--goal", "criteria": "--criteria/--success", "context": "--context"}
NOTES = re.compile(r"(## Notes / open questions\n\n)(?:_none_\n*)?")
ON_DUPLICATE = ("merge", "refuse", "file")
SOURCES = ("claude", "user", "intake")


@dataclasses.dataclass(frozen=True)
class Provenance:
    """Who filed a brief and, for an intake brief, the page or file it was extracted from."""

    source: str
    ref: str = ""

    def __str__(self) -> str:
        return f"{self.source} from {self.ref}" if self.ref else self.source


def as_scalar(value: str) -> str:
    """`value` as one frontmatter scalar that reads back whole: any whitespace run, line breaks
    included, becomes one space, and a `#` the parser would take as a comment is joined to its word."""
    one_line = " ".join(value.split())
    return COMMENT_START.sub(lambda m: "#" if m.start() else "", one_line).strip()


def scalar(value: str) -> str:
    """Free text for a frontmatter flag, normalised rather than rejected so callers passing page or PR text keep working."""
    clean = as_scalar(value)
    if clean != value.strip():
        print(f"add_task: normalised {value!r} to {clean!r} for the frontmatter", file=sys.stderr)
    return clean


def frontmatter_value(value: str) -> str:
    """A ref is cited verbatim, so one that would not read back unchanged is refused, not rewritten."""
    if as_scalar(value) != value.strip():
        raise argparse.ArgumentTypeError(
            f"{value!r} must be one line with no ' #' (it would not read back)"
        )
    return value.strip()


def slug(s: str, maxwords: int = 6) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", s.lower())
    words = [w for w in re.split(r"[\s-]+", s) if w]
    return "-".join(words[:maxwords]) or "task"


def list_items(values: list[str]) -> list[str]:
    """Comma-separated flag values as flow-list items that cannot close the list or split into further keys."""
    parts = (part.strip() for value in values for part in value.split(","))
    return [item for part in parts if (item := scalar(part.replace("[", "-").replace("]", "-")))]


def flow_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def dependency_items(values: list[str]) -> list[str]:
    """`--blocked-by` values: brief ids or `external:<reason>`, comma-separated or repeated."""
    return [scalar(part) for value in values for part in value.split(",") if part.strip()]


@dataclasses.dataclass(frozen=True)
class Dependencies:
    """What a new brief waits on, or an explicit "nothing"; silence is no declaration."""

    blocked_by: list[str]
    probes: list[str]
    confirmed: list[str]
    none: bool

    @property
    def declared(self) -> bool:
        return self.none or bool(self.blocked_by)

    def frontmatter(self) -> str:
        if not self.declared:
            return ""
        lines = [f"blocked_by: {flow_list(self.blocked_by)}"]
        lines += [f"{PROBE_KEY}: {p}" for p in self.probes]
        lines += [f"{CONFIRMED_KEY}: {c}" for c in self.confirmed]
        return "\n".join(lines) + "\n"

    def problem(self, status: str) -> str | None:
        """Why filing to `status` with this declaration is refused, else None."""
        if self.none and self.blocked_by:
            return "--blocked-by and --no-deps contradict each other"
        if status == "ready" and self.blocked_by:
            return "a brief that waits on something files to blocked/; drop --ready"
        if status == "ready" and not self.declared:
            return "a ready brief needs --blocked-by <id|external:reason>, or --no-deps when it depends on nothing"
        if status == "blocked" and not self.blocked_by:
            return "a blocked brief needs --blocked-by <id|external:reason>"
        bad_dates = [b for b in self.blocked_by if is_date(b) and date_of(b) is None]
        if bad_dates:
            return f"malformed date blocker(s) {bad_dates}; write date:YYYY-MM-DD"
        gaps = probe_gaps(self.blocked_by, {PROBE_KEY: self.probes, CONFIRMED_KEY: self.confirmed})
        if status == "blocked" and gaps:
            return f"blocker(s) {gaps} are external with no probe; add --probe 'N: <command>' (or 'N: human' with --confirmed)"
        return None

    def unknown_ids(self, root: pathlib.Path) -> list[str]:
        known = {b.id for b in load_queue(root)}
        return [b for b in self.blocked_by if not (is_external(b) or is_date(b)) and b not in known]


def filing_status(a: argparse.Namespace) -> str:
    """The explicit status; otherwise a brief that waits files to blocked/ and anything else to inbox/."""
    explicit = a.status or ("ready" if a.ready else None)
    return explicit or ("blocked" if a.deps.blocked_by else "inbox")


def render_brief(nid: str, a: argparse.Namespace, status: str, context: str, today: str) -> str:
    tags = flow_list(a.tags)
    reqs = flow_list(a.requirements)
    crit = (
        "\n".join(f"- [ ] {c}" for c in a.criteria)
        if a.criteria
        else f"- [ ] {placeholder_criterion(a.title)}"
    )
    return f"""---
id: {nid}
title: {a.title}
created: {today}
updated: {today}
status: {status}
type: {a.type}
importance: {a.importance}
autonomy: {a.autonomy}
estimated-effort: {a.effort}
due: {a.due}
domain: {a.domain}
tags: {tags}
parent: {a.parent}
source: {a.provenance.source}
source_ref: {a.provenance.ref}
blockers: []
{a.deps.frontmatter()}blocks: {flow_list(a.blocks)}
related: []
requirements: {reqs}
---

## Goal

{a.goal or placeholder_goal(a.title)}

## Context

{context or PLACEHOLDER_CONTEXT}

## Success criteria

{crit}

## Constraints

# none recorded

## Notes / open questions

_none_

## Execution log

{today}: captured by {a.provenance} via add_task.py.
"""


def where(found: Located) -> str:
    return f"{found.folder}/{found.id}.md"


def add_note(text: str, note: str) -> str:
    """`note` first under Notes / open questions, replacing the `_none_` placeholder."""
    if not NOTES.search(text):
        return f"{text.rstrip()}\n\n## Notes / open questions\n\n{note}\n"
    return NOTES.sub(lambda m: f"{m[1]}{note}\n\n", text, count=1)


def merged_text(text: str, provenance: Provenance, context: str, today: str) -> str:
    noted = add_note(text, context) if context and context not in text else text
    return f"{noted.rstrip()}\n{today}: repeat capture by {provenance} merged here.\n"


def merge_into(
    root: pathlib.Path, found: Located, provenance: Provenance, context: str, today: str
) -> None:
    with require_lock(root):
        atomic_write(found.path, merged_text(found.path.read_text(), provenance, context, today))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Add a brief to the tasks queue")
    ap.add_argument("--title", required=True, type=scalar)
    ap.add_argument("--goal", default="")
    ap.add_argument("--context", default="")
    ap.add_argument(
        "--criteria",
        "--success",
        dest="criteria",
        action="append",
        default=[],
        help="repeatable success criterion (alias --success)",
    )
    ap.add_argument("--type", default="todo", choices=["todo", "project", "research", "decision"])
    ap.add_argument("--importance", default="3", choices=["1", "2", "3", "4"])
    ap.add_argument("--autonomy", default="needs-input", choices=["full", "needs-input", "blocked"])
    ap.add_argument("--effort", default="", type=scalar)
    ap.add_argument("--due", default="", type=scalar)
    ap.add_argument("--domain", default="", type=scalar)
    ap.add_argument("--tags", default="")
    ap.add_argument("--parent", default="", type=scalar)
    ap.add_argument(
        "--requirements",
        default="",
        help="comma-separated requirement ids from requirements.md this brief covers",
    )
    ap.add_argument(
        "--blocks",
        action="append",
        default=[],
        metavar="ID",
        help="repeatable id of a brief this one blocks (the inverse of that brief's blockers:)",
    )
    ap.add_argument(
        "--blocked-by",
        action="append",
        default=[],
        metavar="ID",
        help="repeatable brief id or external:<reason> this brief waits on; files it to blocked/",
    )
    ap.add_argument(
        "--probe",
        action="append",
        default=[],
        help="`N: <command>` probe for external blocker N (or `N: human` with --confirmed)",
    )
    ap.add_argument(
        "--confirmed",
        action="append",
        default=[],
        help="`N: <YYYY-MM-DD> <who>` confirming a `human` probe for blocker N",
    )
    ap.add_argument(
        "--no-deps", action="store_true", help="declare that the brief depends on nothing"
    )
    ap.add_argument("--source", default="claude", choices=SOURCES)
    ap.add_argument(
        "--source-ref",
        default="",
        type=frontmatter_value,
        metavar="URL",
        help="the page, message or file the brief was extracted from (provenance for intake briefs)",
    )
    ap.add_argument(
        "--status",
        choices=["inbox", "ready", "blocked", "parked"],
        default=None,
        help="lifecycle state to file the brief in (default: inbox)",
    )
    ap.add_argument("--ready", action="store_true", help="alias for --status ready")
    ap.add_argument(
        "--on-duplicate",
        choices=ON_DUPLICATE,
        default="merge",
        help="a capture matching an existing brief: merge into it (default), refuse, or file anyway",
    )
    ap.add_argument("--stdin", action="store_true", help="append stdin to Context")
    ap.add_argument(
        "--commit", action="store_true", help="accepted for old callers: committing is the default"
    )
    ap.add_argument("--no-sync", action="store_true", help="skip the git commit/push")
    ap.add_argument("--root", default=None, help="queue root (default: resolved per _paths)")
    a = ap.parse_args(argv)
    if a.source == "intake" and not a.source_ref:
        ap.error("--source intake needs --source-ref: an intake brief must say where it came from")
    a.provenance = Provenance(a.source, a.source_ref)

    a.deps = Dependencies(
        dependency_items(a.blocked_by),
        [scalar(p) for p in a.probe],
        [scalar(c) for c in a.confirmed],
        a.no_deps,
    )
    status = filing_status(a)
    refused = a.deps.problem(status)
    if refused:
        ap.error(refused)
    a.criteria = [c.strip() for c in a.criteria if c.strip()]
    a.tags, a.requirements, a.blocks = (
        list_items([a.tags]),
        list_items([a.requirements]),
        list_items(a.blocks),
    )
    context = a.context.strip()
    if a.stdin and not sys.stdin.isatty():
        context = (context + "\n\n" + sys.stdin.read().strip()).strip()
    if status in ("ready", "blocked"):
        missing = [FLAGS[f] for f in missing_ready_fields(a.goal, a.criteria, context)]
        if missing:
            ap.error(
                f"a {status} brief needs {', '.join(missing)}; a brief must not carry placeholder stubs "
                "(file it with --status inbox instead)"
            )
    root = require_queue(a.root if a.root else resolve_root())
    unknown = a.deps.unknown_ids(root)
    if unknown:
        ap.error(f"unknown blocker id(s) {unknown}; use a brief id or external:<reason>")
    today = datetime.date.today().isoformat()
    repeat = (
        None
        if a.on_duplicate == "file"
        else find_duplicate(
            root, Fingerprint.of_text(render_brief("pending", a, status, context, today))
        )
    )
    if repeat and a.on_duplicate == "refuse":
        sys.exit(f"duplicate of {where(repeat)}")
    if repeat:
        merge_into(root, repeat, a.provenance, context, today)
        print(f"merged into {where(repeat)}")
        if not a.no_sync:
            _sync_commit(
                f"brief({repeat.id}): repeat capture merged ({a.source})", root, ids=[repeat.id]
            )
        return
    # Atomically claim a unique filename BEFORE writing, so two sessions capturing the
    # same title at the same instant can't overwrite each other (O_CREAT|O_EXCL).
    nid = reserve_name(root / status, slug(a.title), [root / d for d in TASK_DIRS])
    fm = render_brief(nid, a, status, context, today)
    # Fill the reserved (empty) file atomically: a concurrent committer never sees a
    # half-written brief, it sees either nothing-yet or the complete file.
    path = root / status / f"{nid}.md"
    atomic_write(path, fm)
    print(f"{status}/{nid}.md")

    if not a.no_sync:
        _sync_commit(f"brief({nid}): captured ({a.source})", root, ids=[nid])


if __name__ == "__main__":
    main()  # pragma: no cover

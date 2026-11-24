#!/usr/bin/env python3
"""
deps.py — the queue's dependency model, shared by prioritise, unblock and check_deps.

A brief depends on other briefs through three frontmatter spellings, all of which mean
"not runnable until that brief is done":

  blocked_by: [id, id, external:<reason>]   the first-class form; set by `transition.py move
                                            <id> blocked --blocked-by ...`
  blockers:   free text or ids              the original form, kept working
  tags:       [depends:<id>]                the original form, kept working

An `external:<reason>` entry is a blocker no brief can complete (a login, a decision, a
vendor). It never resolves by itself; `unblock.py resolve` clears it explicitly.

A `date:YYYY-MM-DD` entry means "leave this until that day": unmet before the day and met on it,
so the daily release sweep returns the brief to ready/ like any other finished dependency.
"""

import dataclasses
import datetime
import pathlib
import re
from collections.abc import Sequence

from _paths import CONFIG_FILE, PROJECT_MARKER, TASK_DIRS, strip_comment

EXTERNAL_PREFIX = "external:"
BRIEF_DIRS = tuple(d for d in TASK_DIRS if d != "inbox")


def fm(text: str) -> dict[str, str]:
    """Parse the YAML-ish frontmatter block into a flat dict."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    d: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                d[k.strip()] = strip_comment(v)
    return d


PROBE_KEY = "blocked_probe"
CONFIRMED_KEY = "blocked_confirmed"
HUMAN_PROBE = "human"


def fm_repeated(text: str) -> dict[str, list[str]]:
    """Every frontmatter key with all its values, in order, for keys that repeat (`blocked_probe:`)."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    out: dict[str, list[str]] = {}
    for line in m.group(1).splitlines() if m else []:
        if ":" in line:
            k, _, v = line.partition(":")
            out.setdefault(k.strip(), []).append(strip_comment(v))
    return out


def blocker_number(value: str) -> int | None:
    """The blocker number N of an `N: <text>` value (1-based, the position in `blocked_by`), else None."""
    n = value.partition(":")[0].strip()
    return int(n) if n.isdigit() else None


def indexed(values: list[str]) -> dict[int, str]:
    """`N: <text>` values keyed by blocker number N."""
    pairs = ((blocker_number(v), v.partition(":")[2].strip()) for v in values)
    return {n: text for n, text in pairs if n is not None and text}


def probe_gaps(blocked_by: list[str], repeated: dict[str, list[str]]) -> list[int]:
    """Numbers of the `external:` blockers with no probe, or a `human` probe nobody has confirmed."""
    probes, confirmed = (
        indexed(repeated.get(PROBE_KEY, [])),
        indexed(repeated.get(CONFIRMED_KEY, [])),
    )
    return [
        i
        for i, b in enumerate(blocked_by, 1)
        if is_external(b) and (i not in probes or (probes[i] == HUMAN_PROBE and i not in confirmed))
    ]


def brief_id(fields: dict[str, str], stem: str) -> str:
    """A brief's `id:`, or its file stem when the field is absent or blank."""
    return fields.get("id") or stem


def parse_list(raw: str) -> list[str]:
    """Items of a `[a, b, c]` frontmatter list, in order. Reasons may not contain commas."""
    return [t.strip() for t in (raw or "").strip().strip("[]").split(",") if t.strip()]


def is_external(item: str) -> bool:
    return item.startswith(EXTERNAL_PREFIX)


DATE_PREFIX = "date:"
# Written by a deliberate declaration (`declare`, `--no-deps`), unlike a bulk `blocked_by: []`.
DECLARED_FIELD = "deps_declared"


def is_date(item: str) -> bool:
    return item.startswith(DATE_PREFIX)


def date_of(item: str) -> datetime.date | None:
    """The day a `date:YYYY-MM-DD` entry names; None when malformed, so it never counts as reached."""
    try:
        return datetime.date.fromisoformat(item[len(DATE_PREFIX) :])
    except ValueError:
        return None


def date_reached(item: str, today: datetime.date) -> bool:
    day = date_of(item)
    return day is not None and day <= today


PR_HOLD = re.compile(r"^(?:[\w.-]+/[\w.-]+#\d+|split)$")


def is_pr_hold(item: str) -> bool:
    """An `owner/repo#n` or `split` entry written by the PR queue sync, which drops it when the PR merges."""
    return bool(PR_HOLD.match(item))


QUALIFIER = ":"


@dataclasses.dataclass(frozen=True)
class QueueRef:
    """A `<queue>:<id>` blocker: a brief in another queue, the queue named by path or short name."""

    queue: str
    id: str

    @classmethod
    def parse(cls, item: str) -> "QueueRef | None":
        """The reference an item spells, None for a plain id, an `external:` reason or a PR hold."""
        if is_external(item) or is_date(item) or is_pr_hold(item):
            return None
        queue, sep, bid = item.rpartition(QUALIFIER)
        return cls(queue, bid) if sep and queue and bid else None

    def __str__(self) -> str:
        return f"{self.queue}{QUALIFIER}{self.id}"


def queue_name(root: pathlib.Path) -> str:
    """A queue's short name: its folder, or the project folder holding a `.tasks`."""
    return root.parent.name if root.name == PROJECT_MARKER else root.name


def same_queue(a: pathlib.Path, b: pathlib.Path) -> bool:
    return a.resolve() == b.resolve()


def resolve_queue(name: str, roots: Sequence[pathlib.Path]) -> pathlib.Path | None:
    """The queue root `name` means: a path to a queue, else the short name of exactly one known root."""
    if "/" in name or name.startswith(("~", ".")):
        path = pathlib.Path(name).expanduser()
        return path if (path / CONFIG_FILE).is_file() else None
    matches = [r for r in roots if queue_name(r) == name]
    return matches[0] if len(matches) == 1 else None


def ref_problem(ref: QueueRef, roots: Sequence[pathlib.Path]) -> str | None:
    """Why `ref` names nothing (unknown or ambiguous queue, no such brief there), None when it resolves."""
    target = resolve_queue(ref.queue, roots)
    if target is None:
        return f"{ref}: no single known queue {ref.queue!r}"
    if not any(b.id == ref.id for b in load_queue(target)):
        return f"{ref}: no brief {ref.id!r} in {ref.queue}"
    return None


@dataclasses.dataclass(frozen=True)
class Brief:
    """One brief as the dependency tools see it: its folder (status) and parsed frontmatter."""

    id: str
    status: str
    fields: dict[str, str]
    repeated: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    body: str = ""

    @property
    def probe_gaps(self) -> list[int]:
        return probe_gaps(self.blocked_by, self.repeated)

    @property
    def blocked_by(self) -> list[str]:
        return parse_list(self.fields.get("blocked_by", ""))

    @property
    def milestone(self) -> str:
        return self.fields.get("milestone", "")

    @property
    def architecture(self) -> str | None:
        """The architecture the brief belongs to, None when it names none."""
        return self.fields.get("architecture") or None

    @property
    def evidence_required(self) -> bool:
        """True only for `evidence: required`; the brief then needs recorded evidence to be done."""
        return self.fields.get("evidence") == "required"

    @property
    def depends_tags(self) -> set[str]:
        return {
            t[len("depends:") :]
            for t in parse_list(self.fields.get("tags", ""))
            if t.startswith("depends:")
        }

    @property
    def blocks(self) -> set[str]:
        """Ids of the briefs this one blocks: the inverse of their `blockers:`/`blocked_by:`."""
        return set(parse_list(self.fields.get("blocks", "")))

    @property
    def external_blockers(self) -> list[str]:
        return [b for b in self.blocked_by if is_external(b)]

    @property
    def reviewed_declaration(self) -> bool:
        """True once someone judged the brief's waits and declared them, so its prose is history."""
        return bool(self.fields.get(DECLARED_FIELD))

    @property
    def date_blockers(self) -> list[str]:
        return [b for b in self.blocked_by if is_date(b)]

    @property
    def declared_ids(self) -> set[str]:
        """Ids this brief names as dependencies in the strict forms (blocked_by, depends:)."""
        return {
            b for b in self.blocked_by if not (is_external(b) or is_date(b) or is_pr_hold(b))
        } | self.depends_tags

    @property
    def remote_refs(self) -> list[QueueRef]:
        """The `<queue>:<id>` entries of `blocked_by`, in order."""
        return [ref for item in self.blocked_by if (ref := QueueRef.parse(item))]

    @property
    def local_ids(self) -> set[str]:
        """`declared_ids` that name a brief in this brief's own queue."""
        return {i for i in self.declared_ids if QueueRef.parse(i) is None}

    @property
    def legacy_tokens(self) -> set[str]:
        """Every word of the free-text `blockers:` field; some are ids, some are prose."""
        return set(re.findall(r"[\w-]+", self.fields.get("blockers", "") or ""))

    @property
    def declares_dependencies(self) -> bool:
        return declares_dependencies(self.fields)


def declares_dependencies(fields: dict[str, str]) -> bool:
    """A `blocked_by:` line, even `[]` for "depends on nothing", or a non-empty legacy form; silence is no declaration."""
    depends = [t for t in parse_list(fields.get("tags", "")) if t.startswith("depends:")]
    return "blocked_by" in fields or bool(parse_list(fields.get("blockers", "")) or depends)


def load_queue(root: pathlib.Path) -> list[Brief]:
    """Every brief across the lifecycle folders. A folder that does not exist is empty."""
    out: list[Brief] = []
    for d in BRIEF_DIRS:
        for f in sorted((root / d).glob("*.md")):
            text = f.read_text()
            fields = fm(text)
            out.append(Brief(brief_id(fields, f.stem), d, fields, fm_repeated(text), text))
    return out


def done_ids(root: pathlib.Path, roots: Sequence[pathlib.Path] = ()) -> set[str]:
    """Ids of `root`'s done briefs, plus the spelling of every `<queue>:<id>` blocker whose brief is done."""
    queue = load_queue(root)
    done = {b.id for b in queue if b.status == "done"}
    refs = {ref for b in queue for ref in b.remote_refs}
    return done | {str(ref) for ref in refs if _done_elsewhere(ref, roots)}


def _done_elsewhere(ref: QueueRef, roots: Sequence[pathlib.Path]) -> bool:
    target = resolve_queue(ref.queue, roots)
    return target is not None and any(
        b.id == ref.id and b.status == "done" for b in load_queue(target)
    )


def unmet(brief: Brief, done_ids: set[str], today: datetime.date | None = None) -> list[str]:
    """Dependencies that block `brief`: external reasons always, ids until done, dates until reached."""
    day = today or datetime.date.today()
    ids = brief.declared_ids | brief.legacy_tokens
    dates = {d for d in brief.date_blockers if not date_reached(d, day)}
    return sorted({d for d in ids if d not in done_ids} | set(brief.external_blockers) | dates)


def awaits_release(brief: Brief, done_ids: set[str], today: datetime.date | None = None) -> bool:
    """A blocked brief that names something to wait for, all of it now satisfied."""
    named = brief.blocked_by or brief.declared_ids or brief.legacy_tokens
    return bool(named) and not unmet(brief, done_ids, today)


def dependency_edges(brief: Brief, known_ids: set[str]) -> set[str]:
    """Brief ids `brief` waits on: the strict forms, plus legacy words that are real ids.

    Legacy `blockers:` prose contains ordinary words, so only tokens naming an existing
    brief are treated as edges; a strict-form id that names nothing is a dangling dependency.
    """
    return brief.declared_ids | (brief.legacy_tokens & known_ids)

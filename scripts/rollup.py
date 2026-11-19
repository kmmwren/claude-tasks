#!/usr/bin/env python3
"""
rollup.py - project roll-ups for a product manager (pm) or a CTO (cto), over any number of queues.
Only titles, states, dates, the owner's own health, ask and outcome, and the text of the automatic
checks leave a queue; see skills/rollup.

  python scripts/rollup.py json|markdown|html [--level pm|cto] [--source LABEL=PATH ...]
  python scripts/rollup.py sources [--source LABEL=PATH ...]
  python scripts/rollup.py write --out OUT_DIR [--source LABEL=PATH ...]
  python scripts/rollup.py serve [--port 8788] [--source LABEL=PATH ...]
"""

import argparse
import dataclasses
import datetime as dt
import enum
import html
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import tomllib
import zoneinfo
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any, ClassVar

import project_view as pv
from _concurrency import atomic_write
from _paths import PROJECT_MARKER, is_queue, resolve_root, warn
from config import load_config
from deps import load_queue, unmet
from milestones import Milestone

SCHEMA = "claude-tasks.rollup/v1"
CONFIG_ENV = "CLAUDE_TASKS_ROLLUP_CONFIG"
DEFAULT_CONFIG = "~/.config/claude-tasks/rollup.toml"
DEFAULT_LABEL = "this repository"
SEARCH_DEPTH = 4
SKIP_DIRS = frozenset({"node_modules", "venv", "__pycache__", "dist", "build"})
SHIPPED_DAYS = 14
GIT_TIMEOUT_SECONDS = 5
BEHIND_TTL_SECONDS = 30
WHAT_A_PROJECT_IS = "a queue with a project.toml"
NO_PROJECTS = f"No projects found. A project is {WHAT_A_PROJECT_IS}."
NO_PROJECTS_IN_SOURCE = f"no projects (a project is {WHAT_A_PROJECT_IS})"
SHOWN_UNDER_ANOTHER_SOURCE = "every project it finds is already shown under another source"
WORD = {**pv.LABEL, "": "Not set by owner"}
CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1)(:\d{1,5})?", re.IGNORECASE)
GENERATED = re.compile(r'(?m)^\*Generated \S+|^\s*"generated": "[^"]*"')


class RollupError(Exception):
    """A source or config the user named cannot be used; the message says which and why."""


class Stage(enum.Enum):
    NOW = "Now"
    NEXT = "Next"
    DONE = "Done"

    @property
    def key(self) -> str:
        return self.name.lower()

    @classmethod
    def of(cls, status: str) -> "Stage":
        return _STAGE_OF_STATUS[status]


_STAGE_OF_STATUS = {
    "done": Stage.DONE,
    "active": Stage.NOW,
    "not-started": Stage.NEXT,
    "unmapped": Stage.NEXT,
}


class Format(enum.Enum):
    JSON = ("application/json", ".json")
    MARKDOWN = ("text/markdown; charset=utf-8", ".md")
    HTML = ("text/html; charset=utf-8", "")

    def __init__(self, content_type: str, suffix: str) -> None:
        self.content_type = content_type
        self.suffix = suffix


SERVED_BY_SUFFIX = {f.suffix: f for f in (Format.HTML, Format.JSON)}


# --- text a project owner typed -------------------------------------------------------------

_ESCAPABLE = "\\*_[]<>`|"
# GitHub links a bare `http://`, `https://`, `www.` or email on its own. A backslash before the
# `:` or `.` keeps a URL plain, but GitHub rejoins escaped text before matching emails, so an
# invisible word joiner after the `@` is what breaks those.
_SPECIAL = re.compile(f"[{re.escape(_ESCAPABLE)}]|(?<=www)\\.|:(?=//)", re.IGNORECASE)
WORD_JOINER = "⁠"


def escape_md(text: str) -> str:
    """One line of literal text: whitespace collapsed so it cannot start a block, markup escaped."""
    escaped = _SPECIAL.sub(r"\\\g<0>", " ".join(text.split()))
    return escaped.replace("@", "@" + WORD_JOINER)


# Every string from a queue reaches HTML through these, so none of it can become markup.
def _el(tag: str, inner: str) -> str:
    return f"<{tag}>{inner}</{tag}>"


def _text_el(tag: str, text: str) -> str:
    return _el(tag, html.escape(text))


def _ul(items: Iterable[str]) -> str:
    """A list whose items are already HTML."""
    return _el("ul", "".join(_el("li", item) for item in items))


def _ul_text(items: Iterable[str]) -> str:
    return _ul(html.escape(item) for item in items)


def _group_html(heading: str, items: list[str]) -> str:
    return _text_el("h3", heading) + _ul_text(items) if items else ""


def _group_markdown(heading: str, items: list[str]) -> list[str]:
    bullets = [f"- {escape_md(x)}" for x in items]
    return [f"**{heading}**", "", *bullets, ""] if items else []


# --- sources -------------------------------------------------------------------------------


def expand(text: str) -> pathlib.Path:
    """`~` and `~user` expanded; a home that cannot be found is a usage error, not a crash."""
    try:
        return pathlib.Path(text).expanduser()
    except RuntimeError as e:
        raise RollupError(f"cannot expand {text!r}: {e}") from e


def find_queues(path: pathlib.Path, depth: int = SEARCH_DEPTH) -> list[pathlib.Path]:
    """Queues at or under `path`, never descending into a queue or a hidden or build folder."""
    if is_queue(path):
        return [path]
    if depth == 0:
        return []
    return [q for child in _children(path) for q in find_queues(child, depth - 1)]


def _children(path: pathlib.Path) -> list[pathlib.Path]:
    """The folders worth searching below `path`; one that cannot be listed has none."""
    try:
        return [c for c in sorted(path.iterdir()) if _walkable(c)]
    except OSError:
        return []


def _walkable(p: pathlib.Path) -> bool:
    # A symlink can lead out of the tree or back into it, so it is never followed.
    hidden = p.name.startswith(".") and p.name != PROJECT_MARKER
    return not p.is_symlink() and p.is_dir() and not hidden and p.name not in SKIP_DIRS


def commits_behind(path: pathlib.Path) -> int | None:
    """Commits `path`'s work tree lacks from its upstream, without fetching; None when unknown."""
    command = ["git", "-C", str(path), "rev-list", "--count", "HEAD..@{u}"]
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # git missing, or too slow to wait for
        return None
    count = done.stdout.strip()
    return int(count) if done.returncode == 0 and count.isdigit() else None


BehindCount = Callable[[pathlib.Path], int | None]


class BehindCounts:
    """Commits-behind per path, kept briefly so a busy server does not run git on every request."""

    def __init__(
        self,
        count: BehindCount = commits_behind,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = BEHIND_TTL_SECONDS,
    ) -> None:
        self._count, self._clock, self._ttl = count, clock, ttl
        self._seen: dict[pathlib.Path, tuple[float, int | None]] = {}
        self._lock = threading.Lock()

    def __call__(self, path: pathlib.Path) -> int | None:
        with self._lock:
            now = self._clock()
            seen = self._seen.get(path)
            if seen is None or now - seen[0] >= self._ttl:
                seen = self._seen[path] = (now, self._count(path))
            return seen[1]


@dataclasses.dataclass(frozen=True)
class SourceWarning:
    label: str
    behind: int

    @property
    def message(self) -> str:
        noun = "commit" if self.behind == 1 else "commits"
        return f"{self.label} is {self.behind} {noun} behind its upstream; pull for current status"


@dataclasses.dataclass(frozen=True)
class Source:
    label: str
    path: pathlib.Path

    @classmethod
    def of(cls, label: str, path: str) -> "Source":
        return cls(label.strip(), expand(path.strip()))

    @classmethod
    def parse(cls, spec: str) -> "Source":
        label, _, path = spec.partition("=")
        if not (label.strip() and path.strip()):
            raise RollupError(f"--source wants LABEL=PATH, got {spec!r}")
        return cls.of(label, path)

    @classmethod
    def from_row(cls, row: object, number: int) -> "Source":
        fields = row if isinstance(row, Mapping) else {}
        label, path = (str(fields.get(key) or "") for key in ("label", "path"))
        if not (label.strip() and path.strip()):
            raise RollupError(f"[[source]] row {number} needs a label and a path")
        return cls.of(label, path)

    def projects(self) -> list[pathlib.Path]:
        return [q for q in find_queues(self.path) if (q / "project.toml").is_file()]

    def warning(self, count: BehindCount = commits_behind) -> SourceWarning | None:
        behind = count(self.path)
        return SourceWarning(self.label, behind) if behind else None


@dataclasses.dataclass(frozen=True)
class ConfigFile:
    path: pathlib.Path
    named: bool  # the user asked for this file, so a fault in it is an error


def config_file(explicit: str | None, env: Mapping[str, str]) -> ConfigFile:
    named = explicit or env.get(CONFIG_ENV)
    return ConfigFile(expand(named or DEFAULT_CONFIG), bool(named))


def load_sources(
    specs: list[str],
    config: Callable[[], ConfigFile],
    here: Callable[[], pathlib.Path] = resolve_root,
) -> list[Source]:
    """`--source` flags win; the config is looked up only when there are none."""
    if not specs:
        return _from_config_or_here(config(), here)
    found = [Source.parse(s) for s in specs]
    _reject_repeated_labels(found)
    return found


def _from_config_or_here(config: ConfigFile, here: Callable[[], pathlib.Path]) -> list[Source]:
    """A fault in the default config must not stop a run the current repository can serve."""
    if not config.named and not config.path.is_file():
        return [_current(here())]
    try:
        return _configured(config)
    except RollupError as e:
        if config.named:
            raise
        warn(f"{e}; using the current repository instead")
    return [_current(here())]


def _current(queue: pathlib.Path) -> Source:
    """The queue in use, or its whole repository when it is a project's `.tasks` folder."""
    queue = queue.resolve()
    # A `.tasks` queue directly in $HOME would make the whole home folder the source.
    home = pathlib.Path.home().resolve()
    inside_repo = queue.name == PROJECT_MARKER and is_queue(queue) and queue.parent != home
    return Source(DEFAULT_LABEL, queue.parent if inside_repo else queue)


def _configured(config: ConfigFile) -> list[Source]:
    if not config.path.is_file():
        raise RollupError(f"config file {config.path} does not exist")
    rows = _rows(config.path)
    found = [s for number, row in enumerate(rows, 1) if (s := _usable(row, number, config.path))]
    if not found:
        raise RollupError(f"{config.path} has no usable [[source]] rows")
    return _first_of_each_label(found, config.path)


def _rows(config: pathlib.Path) -> list[object]:
    try:
        with config.open("rb") as fh:
            rows = tomllib.load(fh).get("source", [])
    except (OSError, ValueError) as e:
        raise RollupError(f"cannot read {config}: {e}") from e
    if not isinstance(rows, list):
        raise RollupError(f"{config}: sources are [[source]] tables with label and path")
    return rows


def _usable(row: object, number: int, config: pathlib.Path) -> Source | None:
    """One bad row is skipped and named, so the rows after it still count."""
    try:
        return Source.from_row(row, number)
    except RollupError as e:
        warn(f"{config}: {e}; that row is skipped")
        return None


def _first_of_each_label(found: list[Source], config: pathlib.Path) -> list[Source]:
    """A label that repeats keeps its first row; each later one is named and skipped."""
    kept: dict[str, Source] = {}
    for s in found:
        if s.label in kept:
            warn(f"{config}: label {s.label!r} is used more than once; that row is skipped")
        kept.setdefault(s.label, s)
    return list(kept.values())


def _reject_repeated_labels(found: list[Source]) -> None:
    repeated = sorted(label for label, n in Counter(s.label for s in found).items() if n > 1)
    if repeated:
        raise RollupError(
            f"source labels must be unique; used more than once: {', '.join(repeated)}"
        )


# --- model ---------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BriefSummary:
    id: str
    title: str
    state: str
    waiting: bool
    updated: dt.date | None

    @classmethod
    def of(cls, b: dict[str, Any], waiting: bool) -> "BriefSummary":
        return cls(b["id"], b["title"], pv.brief_state(b), waiting, pv.parse_date(b["updated"]))

    @property
    def updated_iso(self) -> str:
        return self.updated.isoformat() if self.updated else ""

    def shipped_line(self) -> str:
        return f"{self.title} ({self.updated_iso})"


@dataclasses.dataclass(frozen=True)
class MilestoneSummary:
    milestone: Milestone
    stage: Stage
    due: str
    missed: bool
    percent_done: int  # from ticked criteria, as the project page shows it
    briefs: tuple[BriefSummary, ...]

    @classmethod
    def of(cls, row: dict[str, Any], briefs: Mapping[str, BriefSummary]) -> "MilestoneSummary":
        mine = tuple(briefs[i] for i in row["briefs"] if i in briefs)
        title = pv.require_text(row["reader_title"], f"milestone {row['id']} plain or title")
        counts = dict(Counter(b.state for b in mine))
        return cls(
            Milestone(row["id"], title, counts),
            Stage.of(row["status"]),
            row["due"],
            row["missed"],
            row["percent_done"],
            mine,
        )

    @property
    def id(self) -> str:
        return self.milestone.id

    @property
    def title(self) -> str:
        return self.milestone.title

    @property
    def waiting_count(self) -> int:
        return sum(b.waiting for b in self.briefs)

    def line(self) -> str:
        notes = self._notes()
        return self.title + (f" ({', '.join(notes)})" if notes else "")

    def _notes(self) -> list[str]:
        if self.stage is Stage.DONE:
            return self._waiting_note()
        due = [f"due {self.due}" + (", missed" if self.missed else "")] if self.due else []
        return [f"{self.percent_done}%", *due, *self._waiting_note()]

    def _waiting_note(self) -> list[str]:
        return [f"{self.waiting_count} waiting"] if self.waiting_count else []


@dataclasses.dataclass(frozen=True)
class Health:
    owner: str
    reason: str
    set_on: str
    effective: str
    signals: tuple[str, ...]

    @classmethod
    def of(cls, cfg: dict[str, Any], found: list[dict[str, str]], now: dt.datetime) -> "Health":
        details = tuple(s["detail"] for s in found)
        owned = pv.owner_health(cfg, found, now)
        if owned is None:
            return cls("", "", "", _worst(s["floor"] for s in found), details)
        return cls(owned["owner"], owned["reason"], owned["set_on"], owned["effective"], details)

    @property
    def explained(self) -> bool:
        return bool(self.owner or self.signals)

    @property
    def colour_word(self) -> str:
        return WORD[self.effective]

    def detail(self) -> str:
        """What follows the colour word, as plain text."""
        checks = "; ".join(self.signals)
        return f"{self._lead()}." + (f" Automatic checks: {checks}." if checks else "")

    def markdown(self) -> str:
        return f"**{self.colour_word}**{escape_md(self.detail())}"

    def html(self) -> str:
        return _text_el("strong", self.colour_word) + html.escape(self.detail())

    def _lead(self) -> str:
        reason = self.reason.rstrip(".")
        if not self.owner:
            return ", not set by owner" if self.effective else ""
        if self.effective != self.owner:
            dated = f" on {self.set_on}" if self.set_on else ""
            return f", capped by automatic checks (owner said {WORD[self.owner]}{dated}: {reason})"
        return f", set {self.set_on or 'by owner'}: {reason}"


def _worst(colours: Iterable[str]) -> str:
    return max(colours, key=pv.RANK.__getitem__, default="")


@dataclasses.dataclass(frozen=True)
class Ask:
    question: str
    deadline: str

    @classmethod
    def of(cls, raw: dict[str, Any] | None) -> "Ask | None":
        if not raw or not raw.get("question"):
            return None
        return cls(str(raw["question"]), str(raw.get("deadline", "")))

    def text(self) -> str:
        return self.question + (f" (by {self.deadline})" if self.deadline else "")


@dataclasses.dataclass(frozen=True)
class Project:
    source: str
    slug: str
    title: str
    outcome: str
    health: Health
    ask: Ask | None
    milestones: tuple[MilestoneSummary, ...]
    shipped: tuple[BriefSummary, ...]

    def stage(self, stage: Stage) -> tuple[MilestoneSummary, ...]:
        return tuple(m for m in self.milestones if m.stage is stage)


@dataclasses.dataclass(frozen=True)
class Skipped:
    source: str
    queue: str
    reason: str

    def line(self) -> str:
        where = f"{self.source} / {self.queue}" if self.queue else self.source
        return f"{where}: {self.reason}"


@dataclasses.dataclass(frozen=True)
class Footnote:
    """A closing section of a roll-up: what was left out, or what a reader should know."""

    heading: str
    lines: tuple[str, ...]

    def markdown(self) -> list[str]:
        return ["", f"## {self.heading}", "", *(f"- {escape_md(x)}" for x in self.lines)]

    def html(self) -> str:
        return _text_el("h2", self.heading) + _ul_text(self.lines)


@dataclasses.dataclass(frozen=True)
class Rollup:
    generated: str
    sources: tuple[str, ...]
    projects: tuple[Project, ...]
    skipped: tuple[Skipped, ...]
    warnings: tuple[SourceWarning, ...]

    def project_counts(self) -> dict[str, int]:
        counted = Counter(p.source for p in self.projects)
        return {label: counted[label] for label in self.sources}

    def footnotes(self) -> tuple[Footnote, ...]:
        notes = (
            Footnote("Not shown", tuple(s.line() for s in self.skipped)),
            Footnote("Source warnings", tuple(w.message for w in self.warnings)),
        )
        return tuple(n for n in notes if n.lines)


def _zone(queue: pathlib.Path) -> zoneinfo.ZoneInfo:
    """The queue's own `timezone`, UTC when unset or unusable."""
    try:
        name = str(load_config(queue)["timezone"])
    except (OSError, ValueError):  # project_view already warned that tasks.toml is unreadable
        return zoneinfo.ZoneInfo("UTC")
    try:
        return zoneinfo.ZoneInfo(name)
    except (KeyError, ValueError):  # an unknown zone name is a KeyError subclass
        warn(f"{queue / 'tasks.toml'}: timezone {name!r} is not a known zone; using UTC")
        return zoneinfo.ZoneInfo("UTC")


def _waiting_ids(queue: pathlib.Path) -> frozenset[str]:
    """Open briefs with a blocker still unmet, by the rule the rest of the plugin uses."""
    briefs = load_queue(queue)
    done = {b.id for b in briefs if b.status == "done"}
    return frozenset(b.id for b in briefs if b.status != "done" and unmet(b, done))


def project_of(label: str, queue: pathlib.Path, now: dt.datetime) -> Project:
    cfg = pv.read_project(queue)
    signals = pv.Signals(now=now.astimezone(_zone(queue)))
    briefs = pv.project_briefs(queue)
    today = signals.now.date()
    rows = pv.milestone_rows(pv.declared_milestones(queue, cfg), briefs, today)
    waiting = _waiting_ids(queue)
    summaries = {b["id"]: BriefSummary.of(b, b["id"] in waiting) for b in briefs}
    health = Health.of(cfg, pv.computed_floor(rows, briefs, signals), signals.now)
    return Project(
        label,
        _slug(cfg["project"], queue),
        cfg["project"]["title"],
        cfg["project"]["outcome"],
        health,
        Ask.of(cfg.get("ask")),
        _milestones(label, queue, rows, summaries),
        _shipped(summaries.values(), today),
    )


def _slug(table: dict[str, Any], queue: pathlib.Path) -> str:
    """The project's own slug when it is text; otherwise the folder name, saying so."""
    slug = table.get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug
    if slug is not None:
        warn(f"{queue / 'project.toml'}: [project] slug must be text; using the folder name")
    return queue.parent.name


def _milestones(
    label: str, queue: pathlib.Path, rows: list[dict[str, Any]], briefs: Mapping[str, BriefSummary]
) -> tuple[MilestoneSummary, ...]:
    """One milestone with a title that is not text is left out; the project still shows."""
    kept = []
    for row in rows:
        try:
            kept.append(MilestoneSummary.of(row, briefs))
        except pv.ProjectConfigError as e:
            warn(f"{label} / {queue.parent.name}: {e}; that milestone is left out")
    return tuple(kept)


def _shipped(briefs: Iterable[BriefSummary], today: dt.date) -> tuple[BriefSummary, ...]:
    since = today - dt.timedelta(days=SHIPPED_DAYS)
    done = [b for b in briefs if b.state == "done" and b.updated and b.updated >= since]
    return tuple(sorted(done, key=lambda b: b.updated_iso, reverse=True))


def project_or_skipped(source: Source, queue: pathlib.Path, now: dt.datetime) -> Project | Skipped:
    try:
        return project_of(source.label, queue, now)
    except (pv.ProjectConfigError, tomllib.TOMLDecodeError) as e:
        return Skipped(source.label, queue.parent.name, str(e))
    except OSError as e:
        return Skipped(source.label, queue.parent.name, _os_reason(e, queue))
    except (KeyError, TypeError, ValueError) as e:  # ValueError covers UnicodeDecodeError
        return Skipped(source.label, queue.parent.name, f"{type(e).__name__}: {e}")


def _os_reason(error: OSError, queue: pathlib.Path) -> str:
    """Skipped rows are shared, so the failing file is named relative to its queue, never absolute."""
    name = pathlib.Path(str(error.filename or ""))
    inside = name.relative_to(queue) if name.is_relative_to(queue) else pathlib.Path(name.name)
    reason = f"{type(error).__name__}: {error.strerror or 'cannot be read'}"
    return f"{reason} ({inside})" if error.filename else reason


@dataclasses.dataclass(frozen=True)
class Contribution:
    """What one source adds: the queues it finds, and those no earlier source already has."""

    source: Source
    found: tuple[pathlib.Path, ...]
    new: tuple[pathlib.Path, ...]

    def nothing_added(self) -> Skipped | None:
        if self.new or not self.source.path.is_dir():
            return None
        reason = SHOWN_UNDER_ANOTHER_SOURCE if self.found else NO_PROJECTS_IN_SOURCE
        return Skipped(self.source.label, "", reason)


def contributions(sources: list[Source]) -> list[Contribution]:
    """Each queue once, under the first source that reaches it, however many paths lead there."""
    seen: set[pathlib.Path] = set()
    out = []
    for s in sources:
        found = s.projects()
        new = [q for q in found if q.resolve() not in seen]
        seen.update(q.resolve() for q in new)
        out.append(Contribution(s, tuple(found), tuple(new)))
    return out


def build(sources: list[Source], now: dt.datetime, behind: BehindCount = commits_behind) -> Rollup:
    added = contributions(sources)
    outcomes = [project_or_skipped(c.source, q, now) for c in added for q in c.new]
    missing = [
        Skipped(s.label, s.path.name, "not a folder that exists")
        for s in sources
        if not s.path.is_dir()
    ]
    # A source that adds nothing must say so, or the header names it and the reader assumes it did.
    empty = [note for c in added if (note := c.nothing_added())]
    return Rollup(
        now.isoformat(timespec="seconds"),
        tuple(s.label for s in sources),
        tuple(o for o in outcomes if isinstance(o, Project)),
        tuple(missing) + tuple(empty) + tuple(o for o in outcomes if isinstance(o, Skipped)),
        tuple(w for s in sources if (w := s.warning(behind))),
    )


# --- levels --------------------------------------------------------------------------------

STYLE = """:root{--bg:#fff;--fg:#1d2433;--muted:#5b6475;--line:#d9dee7}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e6e9ef;--muted:#9aa3b2;--line:#2c323d}}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;max-width:60rem;margin:0 auto;padding:1rem}
em{color:var(--muted)}table{border-collapse:collapse;display:block;overflow-x:auto}
td,th{border:1px solid var(--line);padding:.35rem .6rem;text-align:left}a{color:inherit}"""


def page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


class Level(ABC):
    name: ClassVar[str]
    title: ClassVar[str]

    @abstractmethod
    def project(self, p: Project) -> dict[str, Any]: ...

    @abstractmethod
    def markdown_body(self, r: Rollup) -> list[str]: ...

    @abstractmethod
    def html_body(self, r: Rollup) -> str: ...

    def data(self, r: Rollup) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "level": self.name,
            "generated": r.generated,
            "sources": list(r.sources),
            "source_projects": [
                {"source": label, "projects": n} for label, n in r.project_counts().items()
            ],
            "projects": [self.project(p) for p in r.projects],
            "skipped": [dataclasses.asdict(s) for s in r.skipped],
            "warnings": [
                {"source": w.label, "behind": w.behind, "message": w.message} for w in r.warnings
            ],
        }

    def json_text(self, r: Rollup) -> str:
        return json.dumps(self.data(r), indent=2)

    def markdown(self, r: Rollup) -> str:
        sources = ", ".join(escape_md(s) for s in r.sources)
        head = [f"# {self.title}", "", f"*Generated {r.generated} from {sources}.*", ""]
        body = self.markdown_body(r) if r.projects else [NO_PROJECTS, ""]
        notes = [line for note in r.footnotes() for line in note.markdown()]
        return "\n".join(head + body + notes + [""] * bool(notes))

    def html(self, r: Rollup) -> str:
        generated = f"Generated {r.generated} from {', '.join(r.sources)}."
        body = self.html_body(r) if r.projects else _text_el("p", NO_PROJECTS)
        notes = "".join(note.html() for note in r.footnotes())
        head = _text_el("h1", self.title) + _el("p", _text_el("em", generated))
        return page(self.title, head + body + notes)

    def render(self, fmt: Format, r: Rollup) -> str:
        views: dict[Format, Callable[[Rollup], str]] = {
            Format.JSON: self.json_text,
            Format.MARKDOWN: self.markdown,
            Format.HTML: self.html,
        }
        return views[fmt](r)


class Pm(Level):
    name, title = "pm", "Product roll-up"

    def project(self, p: Project) -> dict[str, Any]:
        return {
            "source": p.source,
            "slug": p.slug,
            "title": p.title,
            "outcome": p.outcome,
            "health": dataclasses.asdict(p.health),
            "ask": dataclasses.asdict(p.ask) if p.ask else None,
            **{s.key: [self._milestone(m) for m in p.stage(s)] for s in Stage},
            "shipped": [
                {"id": b.id, "title": b.title, "updated": b.updated_iso} for b in p.shipped
            ],
        }

    @staticmethod
    def _milestone(m: MilestoneSummary) -> dict[str, Any]:
        return {
            "id": m.id,
            "title": m.title,
            "percent_done": m.percent_done,
            "due": m.due,
            "missed": m.missed,
            "waiting": m.waiting_count,
        }

    def markdown_body(self, r: Rollup) -> list[str]:
        return [line for p in r.projects for line in self._markdown_section(p)]

    def html_body(self, r: Rollup) -> str:
        return "".join(self._html_section(p) for p in r.projects)

    @staticmethod
    def _groups(p: Project) -> list[tuple[str, list[str]]]:
        """The labelled lists under a project, each as plain text; both views render these."""
        shipped = f"Shipped in the last {SHIPPED_DAYS} days"
        return [
            ("Ask", [p.ask.text()] if p.ask else []),
            *((s.value, [m.line() for m in p.stage(s)]) for s in Stage),
            (shipped, [b.shipped_line() for b in p.shipped]),
        ]

    def _markdown_section(self, p: Project) -> list[str]:
        head = [
            f"## {escape_md(p.title)}",
            "",
            f"*{escape_md(p.source)}.* {escape_md(p.outcome)}",
            "",
            f"**Health:** {p.health.markdown()}",
            "",
        ]
        return head + [line for g in self._groups(p) for line in _group_markdown(*g)]

    def _html_section(self, p: Project) -> str:
        source = _text_el("em", f"{p.source}.")
        return "".join(
            [
                _text_el("h2", p.title),
                _el("p", f"{source} {html.escape(p.outcome)}"),
                _el("p", f"{_text_el('strong', 'Health:')} {p.health.html()}"),
                *(_group_html(*g) for g in self._groups(p)),
            ]
        )


class Cto(Level):
    name, title = "cto", "Portfolio roll-up"
    HEADINGS = ("Project", "Health", "Milestones", "Next due", "Missed")

    def project(self, p: Project) -> dict[str, Any]:
        soonest = self._next_due(p)
        return {
            "source": p.source,
            "slug": p.slug,
            "title": p.title,
            "outcome": p.outcome,
            "health": {
                "effective": p.health.effective,
                "owner": p.health.owner,
                "reason": p.health.reason,
            },
            "milestones": {"done": len(p.stage(Stage.DONE)), "total": len(p.milestones)},
            "next_due": (
                {"id": soonest.id, "title": soonest.title, "due": soonest.due} if soonest else None
            ),
            "missed": [
                {"id": m.id, "title": m.title, "due": m.due} for m in p.milestones if m.missed
            ],
        }

    @staticmethod
    def _next_due(p: Project) -> MilestoneSummary | None:
        dated = sorted(
            (m for m in p.milestones if m.due and m.stage is not Stage.DONE), key=lambda m: m.due
        )
        return dated[0] if dated else None

    def _cells(self, p: Project) -> list[str]:
        soonest = self._next_due(p)
        return [
            f"{p.title} ({p.source})",
            p.health.colour_word,
            f"{len(p.stage(Stage.DONE))} of {len(p.milestones)}",
            f"{soonest.title} ({soonest.due})" if soonest else "",
            "; ".join(m.title for m in p.milestones if m.missed),
        ]

    @staticmethod
    def _explained(r: Rollup) -> list[Project]:
        return [p for p in r.projects if p.health.explained]

    def markdown_body(self, r: Rollup) -> list[str]:
        head = [f"| {' | '.join(self.HEADINGS)} |", "|---|---|---|---|---|"]
        rows = [f"| {' | '.join(escape_md(c) for c in self._cells(p))} |" for p in r.projects]
        why = [f"- **{escape_md(p.title)}:** {p.health.markdown()}" for p in self._explained(r)]
        return head + rows + [""] + why + [""]

    def html_body(self, r: Rollup) -> str:
        head = _el("tr", "".join(_text_el("th", h) for h in self.HEADINGS))
        rows = [_el("tr", "".join(_text_el("td", c) for c in self._cells(p))) for p in r.projects]
        why = [f"{_text_el('strong', p.title + ':')} {p.health.html()}" for p in self._explained(r)]
        table = _el("table", _el("thead", head) + _el("tbody", "".join(rows)))
        return table + (_ul(why) if why else "")


LEVELS: dict[str, Level] = {lv.name: lv for lv in (Pm(), Cto())}


# --- the files a roll-up is published as ------------------------------------------------------

GUIDE = f"""## What these files are

`pm.md` and `cto.md` are read-only summaries of project status, written by `rollup.py` from project
queues. `pm.md` shows each project's health, ask and milestones; `cto.md` is one table across every
project. `pm.json` and `cto.json` hold the same data for tools.

## Read them on GitHub

Open `pm.md` or `cto.md` in this folder; GitHub shows them as formatted pages.

## Ask Claude

Install the claude-tasks plugin, then ask "how are my projects going?". In Claude Desktop or
claude.ai chat, attach `pm.md` with **+ > Add from GitHub** first, then ask the same question.

## Make your own roll-up

Point the generator at your own queues; repeat `--source` for each repository:

    rollup.py write --out <dir> --source Label=path

## The JSON is an API

`pm.json` and `cto.json` use the schema `{SCHEMA}`. Keys keep their names and meaning within a
schema version, so tools can rely on them; new keys may be added.
"""


def _linked(lv: Level, fmt: Format) -> str:
    return f"{lv.name}{fmt.suffix}"


def readme_markdown(r: Rollup) -> str:
    """The index of the files `write` produces, with how someone else gets their own view."""
    links = [
        f"- [{lv.title}]({_linked(lv, Format.MARKDOWN)}) ([JSON]({_linked(lv, Format.JSON)}))"
        for lv in LEVELS.values()
    ]
    head = ["# Project roll-ups", "", f"*Generated {r.generated}.*", "", *links, ""]
    return "\n".join(head) + "\n" + GUIDE


def _link(text: str, target: str) -> str:
    return f'<a href="{html.escape(target)}">{html.escape(text)}</a>'


def index_html(r: Rollup) -> str:
    """The server's front page: each level by route, never a project's own text as a target."""
    items = [
        f"{_link(lv.title, _linked(lv, Format.HTML))} ({_link('JSON', _linked(lv, Format.JSON))})"
        for lv in LEVELS.values()
    ]
    head = _text_el("h1", "Project roll-ups") + _el(
        "p", _text_el("em", f"Generated {r.generated}.")
    )
    return page("Project roll-ups", head + _ul(items))


def _files(r: Rollup) -> dict[str, str]:
    files = {"README.md": readme_markdown(r)}
    for lv in LEVELS.values():
        for fmt in (Format.JSON, Format.MARKDOWN):
            files[_linked(lv, fmt)] = lv.render(fmt, r).rstrip("\n") + "\n"
    return files


def _holds_rollup(out: pathlib.Path) -> bool:
    """True when `out` already carries a roll-up of ours: a pm.json with our schema."""
    try:
        data = json.loads((out / "pm.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("schema") == SCHEMA


def _check_destination(out: pathlib.Path) -> None:
    empty = out.is_dir() and not any(out.iterdir())
    if out.exists() and not (empty or _holds_rollup(out)):
        raise RollupError(
            f"{out} is not an empty folder and holds no roll-up (no pm.json with "
            f"schema {SCHEMA}); name a new or empty folder so nothing else is overwritten"
        )


def write_if_changed(path: pathlib.Path, text: str) -> bool:
    """Write `path` unless it only differs in its generated-on line; True when it wrote."""
    if _same_but_for_date(path, text):
        return False
    atomic_write(path, text)
    return True


def _same_but_for_date(path: pathlib.Path, text: str) -> bool:
    """A file we cannot decode is not ours to keep, so it counts as different."""
    if not path.is_file():
        return False
    try:
        current = path.read_text()
    except UnicodeDecodeError:
        return False
    return GENERATED.sub("", current) == GENERATED.sub("", text)


def write(out: pathlib.Path, r: Rollup) -> list[pathlib.Path]:
    """Write the roll-up files that changed into `out` and return them."""
    try:
        _check_destination(out)
        return [
            out / name for name, text in _files(r).items() if write_if_changed(out / name, text)
        ]
    except OSError as e:
        raise RollupError(f"cannot write {out}: {e.strerror or type(e).__name__}") from e


def route(path: str, r: Rollup) -> tuple[str, str] | None:
    """(content type, body) for a localhost path, or None for a path the server does not have."""
    target = path.strip("/")
    if not target:
        return Format.HTML.content_type, index_html(r)
    name, dot, ext = target.partition(".")
    level, fmt = LEVELS.get(name), SERVED_BY_SUFFIX.get(dot + ext)
    if level is None or fmt is None:
        return None
    return fmt.content_type, level.render(fmt, r)


# --- commands ------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Run(ABC):
    sources: list[Source]
    now: Callable[[], dt.datetime]

    def rollup(self) -> Rollup:
        return build(self.sources, self.now())

    @abstractmethod
    def execute(self) -> None: ...


@dataclasses.dataclass(frozen=True)
class ListSources(Run):
    def execute(self) -> None:
        for s in self.sources:
            found = [q.parent.name for q in s.projects()]
            print(f"{s.label}: {s.path}\n  " + (", ".join(found) or "no projects found"))
            if warning := s.warning():
                print(f"  warning: {warning.message}")


@dataclasses.dataclass(frozen=True)
class Show(Run):
    level: Level
    fmt: Format

    def execute(self) -> None:
        print(self.level.render(self.fmt, self.rollup()))


@dataclasses.dataclass(frozen=True)
class Write(Run):
    out: pathlib.Path

    def execute(self) -> None:
        written = write(self.out, self.rollup())
        print("\n".join(map(str, written)) if written else f"{self.out} is up to date")


@dataclasses.dataclass(frozen=True)
class Serve(Run):
    port: int
    behind: BehindCount = dataclasses.field(default_factory=BehindCounts)

    def rollup(self) -> Rollup:
        return build(self.sources, self.now(), self.behind)

    def server(self) -> http.server.ThreadingHTTPServer:
        """Localhost only, rebuilt on every request so the page is never older than the queue."""
        run = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                # A rebinding page reaches this loopback port under its own name; only ours pass.
                if not LOCAL_HOST.fullmatch(self.headers.get("Host", "")):
                    return self._send(403, "text/plain; charset=utf-8", "forbidden host")
                got = route(self.path.split("?")[0], run.rollup())
                ctype, body = got or ("text/plain; charset=utf-8", "not found")
                self._send(200 if got else 404, ctype, body)

            def _send(self, status: int, ctype: str, body: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)

    def execute(
        self, block: Callable[[http.server.ThreadingHTTPServer], None] | None = None
    ) -> None:
        try:
            srv = self.server()
        except (OSError, OverflowError) as e:  # in use, or outside 0-65535
            raise RollupError(f"cannot listen on port {self.port}: {e}") from e
        print(f"roll-ups at http://localhost:{srv.server_address[1]}/ (Ctrl-C to stop)", flush=True)
        (block or serve_until_interrupted)(srv)


def serve_until_interrupted(
    srv: http.server.ThreadingHTTPServer,
) -> None:  # pragma: no cover - blocks until killed
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


MakeRun = Callable[[argparse.Namespace, list[Source], Callable[[], dt.datetime]], Run]


def _command(
    commands: "argparse._SubParsersAction[argparse.ArgumentParser]",
    name: str,
    summary: str,
    make: MakeRun,
) -> argparse.ArgumentParser:
    sub = commands.add_parser(name, help=summary, description=summary)
    sub.add_argument("--source", action="append", default=[], metavar="LABEL=PATH")
    sub.add_argument("--config", help=f"sources file (default ${CONFIG_ENV} or {DEFAULT_CONFIG})")
    sub.set_defaults(make_run=make)
    return sub


def parse(argv: list[str], env: Mapping[str, str], now: Callable[[], dt.datetime] = _now) -> Run:
    ap = argparse.ArgumentParser(description="Project roll-ups for a product manager or a CTO.")
    commands = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for fmt in (Format.JSON, Format.MARKDOWN, Format.HTML):
        word = fmt.name.lower()
        shown = _command(commands, word, f"print the roll-up as {word}", _show)
        shown.add_argument("--level", choices=list(LEVELS), default="pm")
        shown.set_defaults(fmt=fmt)
    _command(commands, "sources", "list the projects each source finds", _list_sources)
    _command(commands, "write", "write the roll-up files into OUT_DIR", _write).add_argument(
        "--out",
        required=True,
        metavar="OUT_DIR",
        help="a new or empty folder, or an earlier roll-up",
    )
    _command(commands, "serve", "serve the roll-ups on localhost", _serve).add_argument(
        "--port", type=int, default=8788
    )
    a = ap.parse_args(argv)
    if a.source and a.config:
        warn("--config is ignored because --source was given")
    sources = load_sources(a.source, lambda: config_file(a.config, env))
    return a.make_run(a, sources, now)  # type: ignore[no-any-return]


def _show(a: argparse.Namespace, sources: list[Source], now: Callable[[], dt.datetime]) -> Run:
    return Show(sources, now, LEVELS[a.level], a.fmt)


def _list_sources(
    a: argparse.Namespace, sources: list[Source], now: Callable[[], dt.datetime]
) -> Run:
    return ListSources(sources, now)


def _write(a: argparse.Namespace, sources: list[Source], now: Callable[[], dt.datetime]) -> Run:
    return Write(sources, now, expand(a.out))


def _serve(a: argparse.Namespace, sources: list[Source], now: Callable[[], dt.datetime]) -> Run:
    return Serve(sources, now, a.port)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        parse(args, os.environ if env is None else env).execute()
    except RollupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

"""
facts.py - the project facts a queue records beside its briefs, read into typed rows.

Every fact is a small file that is only ever added, so two people writing at once never conflict:

  history/health/<YYYY-MM-DD>-<8 hex>.toml        colour, reason, set_by, set_on
  history/due/<milestone>-<YYYY-MM-DD>-<8 hex>.toml   milestone, due, set_on, set_by, reason
  history/ci/<YYYY-MM-DD>-<8 hex>.toml            state, link, set_on
  facts/<brief-id>.toml                           [[flag]], [[evidence]], shipped, shipped_on, release

`project.toml` keeps the baseline: `[[milestones]].due` is the first date promised, and its
`[health]` table is read as the oldest health row. It may also name an `initiative`, a `[links]`
table of https URLs and `[[releases]]` rows (id, date). The current health, the current due date and the
slip are derived here on every read and never stored beside their own history.

A missing file or key is "not recorded", never an error; a row that cannot be read is skipped with a
warning on stderr that names its file.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import datetime as dt
import pathlib
import tomllib
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from _dates import parse_date
from _paths import warn

PROJECT_FILE = "project.toml"
HEALTH_DIR = pathlib.PurePosixPath("history/health")
DUE_DIR = pathlib.PurePosixPath("history/due")
CI_DIR = pathlib.PurePosixPath("history/ci")
FACTS_DIR = pathlib.PurePosixPath("facts")

COLOURS = ("green", "amber", "red")
EVIDENCE_KINDS = ("screenshot", "video")
EVIDENCE_ACCESS = ("public", "org", "private")
CI_STATES = ("green", "red")
HTTPS = "https://"

T = TypeVar("T")


class RowError(ValueError):
    """A recorded row that cannot be read; the message says which field."""


def _text(table: Mapping[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RowError(f"{key} must be non-empty text")
    return value.strip()


def _choice(table: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = _text(table, key)
    if value not in allowed:
        raise RowError(f"{key} must be one of {', '.join(allowed)}")
    return value


def _https(table: Mapping[str, Any], key: str) -> str:
    value = _text(table, key)
    if not value.lower().startswith(HTTPS) or len(value) == len(HTTPS):
        raise RowError(f"{key} must be an https link")
    return value


def _day(table: Mapping[str, Any], key: str) -> dt.date:
    day = parse_date(table.get(key))
    if day is None:
        raise RowError(f"{key} must be a YYYY-MM-DD date")
    return day


def _optional_text(table: Mapping[str, Any], key: str) -> str | None:
    value = table.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _rows(
    path: pathlib.Path, table: Mapping[str, Any], key: str, parse: Callable[[Any], T]
) -> list[T]:
    """The `[[key]]` rows of a facts file that parse; each bad row is named and left out."""
    raw = table.get(key, [])
    if not isinstance(raw, list):
        warn(f"{path}: {key} must be [[{key}]] tables; they are left out")
        return []
    out: list[T] = []
    for number, row in enumerate(raw, 1):
        try:
            out.append(parse(row))
        except RowError as e:
            warn(f"{path}: {key} row {number}: {e}; that row is left out")
    return out


def _read(path: pathlib.Path) -> dict[str, Any] | None:
    """A TOML file's table; None, with a warning, when it cannot be read as TOML."""
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        warn(f"{path}: cannot be read ({e}); it is left out")
        return None


def _history_files(queue: pathlib.Path, folder: pathlib.PurePosixPath) -> list[pathlib.Path]:
    return sorted((queue / folder).glob("*.toml"), key=lambda p: p.name)


# --- health ------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HealthRow:
    """One owner statement of project health; `source` is the file it came from."""

    colour: str
    reason: str
    set_by: str
    set_on: dt.date | None
    source: str

    @classmethod
    def parse(cls, table: Any, source: str, dated: bool) -> "HealthRow":
        """A health table; history rows must carry `set_on`, the baseline `[health]` may omit it."""
        if not isinstance(table, Mapping):
            raise RowError("not a table")
        has_date = dated or table.get("set_on") is not None
        return cls(
            _choice(table, "colour", COLOURS),
            _text(table, "reason"),
            str(table.get("set_by") or ""),
            _day(table, "set_on") if has_date else None,
            source,
        )

    @property
    def order(self) -> tuple[dt.date, bool, str]:
        """Newest sorts last: by date, then recorded rows over the baseline, then file name."""
        return (self.set_on or dt.date.min, self.source != PROJECT_FILE, self.source)

    def table(self) -> dict[str, Any]:
        """The file body that parses back to this row."""
        row: dict[str, Any] = {"colour": self.colour, "reason": self.reason, "set_by": self.set_by}
        return row if self.set_on is None else {**row, "set_on": self.set_on}


def load_health(queue: pathlib.Path) -> list[HealthRow]:
    """Every health row, oldest first; `project.toml`'s `[health]` counts as the oldest."""
    rows: list[HealthRow] = []
    project = _read(queue / PROJECT_FILE) or {}
    if "health" in project:
        rows += _parsed_health(queue / PROJECT_FILE, project["health"], PROJECT_FILE, False)
    for path in _history_files(queue, HEALTH_DIR):
        rows += _parsed_health(path, _read(path), path.name, True)
    return sorted(rows, key=lambda r: r.order)


def _parsed_health(path: pathlib.Path, table: Any, source: str, dated: bool) -> list[HealthRow]:
    if table is None:
        return []
    try:
        return [HealthRow.parse(table, source, dated)]
    except RowError as e:
        warn(f"{path}: health: {e}; that row is left out")
        return []


def current_health(queue: pathlib.Path) -> HealthRow | None:
    """The newest health row, None when none is recorded."""
    rows = load_health(queue)
    return rows[-1] if rows else None


# --- due dates ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DueRow:
    """One re-forecast of a milestone's due date."""

    milestone: str
    due: dt.date
    set_on: dt.date
    set_by: str
    reason: str
    source: str

    @classmethod
    def parse(cls, table: Any, source: str) -> "DueRow":
        if not isinstance(table, Mapping):
            raise RowError("not a table")
        return cls(
            _text(table, "milestone"),
            _day(table, "due"),
            _day(table, "set_on"),
            str(table.get("set_by") or ""),
            _text(table, "reason"),
            source,
        )

    @property
    def order(self) -> tuple[dt.date, str]:
        return (self.set_on, self.source)

    def table(self) -> dict[str, Any]:
        """The file body that parses back to this row."""
        return {
            "milestone": self.milestone,
            "due": self.due,
            "set_on": self.set_on,
            "set_by": self.set_by,
            "reason": self.reason,
        }


@dataclasses.dataclass(frozen=True)
class Schedule:
    """A milestone's promised date and every later forecast of it."""

    milestone: str
    baseline: dt.date | None
    rows: tuple[DueRow, ...] = ()

    @property
    def current(self) -> dt.date | None:
        """The newest forecast, else the baseline; None when no date was ever recorded."""
        return self.rows[-1].due if self.rows else self.baseline

    @property
    def slip_days(self) -> int | None:
        """Days the current date is past the baseline (0 when equal); None without a baseline."""
        if self.baseline is None or self.current is None:
            return None
        return (self.current - self.baseline).days


def load_due_rows(queue: pathlib.Path) -> dict[str, list[DueRow]]:
    """Every due-date row by milestone, oldest first."""
    found: dict[str, list[DueRow]] = {}
    for path in _history_files(queue, DUE_DIR):
        table = _read(path)
        if table is None:
            continue
        try:
            row = DueRow.parse(table, path.name)
        except RowError as e:
            warn(f"{path}: due: {e}; that row is left out")
            continue
        found.setdefault(row.milestone, []).append(row)
    return {m: sorted(rows, key=lambda r: r.order) for m, rows in found.items()}


def baseline_dates(queue: pathlib.Path) -> dict[str, dt.date | None]:
    """Each `[[milestones]]` id in `project.toml` with the baseline `due` it promised, None if none."""
    project = _read(queue / PROJECT_FILE) or {}
    raw = project.get("milestones", [])
    listed = [m for m in raw if isinstance(m, Mapping)] if isinstance(raw, list) else []
    return {m["id"]: parse_date(m.get("due")) for m in listed if isinstance(m.get("id"), str)}


def load_schedules(queue: pathlib.Path) -> dict[str, Schedule]:
    """One schedule per milestone in `project.toml`, plus any milestone only the history names."""
    baselines, rows = baseline_dates(queue), load_due_rows(queue)
    return {m: Schedule(m, baselines.get(m), tuple(rows.get(m, ()))) for m in (*baselines, *rows)}


# --- per-brief facts ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Flag:
    """A feature flag's state in one environment, as recorded on `changed_on`."""

    name: str
    environment: str
    state: str
    changed_on: dt.date

    @classmethod
    def parse(cls, table: Any) -> "Flag":
        if not isinstance(table, Mapping):
            raise RowError("not a table")
        return cls(
            _text(table, "name"),
            _text(table, "environment"),
            _text(table, "state"),
            _day(table, "changed_on"),
        )

    def table(self) -> dict[str, Any]:
        """The `[[flag]]` row that parses back to this flag."""
        return {
            "name": self.name,
            "environment": self.environment,
            "state": self.state,
            "changed_on": self.changed_on,
        }


@dataclasses.dataclass(frozen=True)
class Evidence:
    """A screenshot or recording of the work; `access` says who can open the link."""

    kind: str
    link: str
    caption: str
    access: str
    recorded_on: dt.date

    @classmethod
    def parse(cls, table: Any) -> "Evidence":
        if not isinstance(table, Mapping):
            raise RowError("not a table")
        return cls(
            _choice(table, "kind", EVIDENCE_KINDS),
            _https(table, "link"),
            _text(table, "caption"),
            _choice(table, "access", EVIDENCE_ACCESS),
            _day(table, "recorded_on"),
        )

    def table(self) -> dict[str, Any]:
        """The `[[evidence]]` row that parses back to this evidence."""
        return {
            "kind": self.kind,
            "link": self.link,
            "caption": self.caption,
            "access": self.access,
            "recorded_on": self.recorded_on,
        }


@dataclasses.dataclass(frozen=True)
class BriefFacts:
    """What is recorded about one brief; every part may be empty."""

    brief: str
    flags: tuple[Flag, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    shipped: str | None = None
    shipped_on: dt.date | None = None
    release: str | None = None

    @property
    def current_flags(self) -> tuple[Flag, ...]:
        """The newest row per (name, environment); a later row of the same date wins."""
        newest: dict[tuple[str, str], tuple[tuple[dt.date, int], Flag]] = {}
        for position, flag in enumerate(self.flags):
            key, order = (flag.name, flag.environment), (flag.changed_on, position)
            if key not in newest or order >= newest[key][0]:
                newest[key] = (order, flag)
        return tuple(flag for _, flag in newest.values())

    @classmethod
    def parse(cls, brief: str, path: pathlib.Path, table: Mapping[str, Any]) -> "BriefFacts":
        return cls(
            brief,
            tuple(_rows(path, table, "flag", Flag.parse)),
            tuple(_rows(path, table, "evidence", Evidence.parse)),
            _optional_text(table, "shipped"),
            parse_date(table.get("shipped_on")),
            _optional_text(table, "release"),
        )


def facts_path(queue: pathlib.Path, brief_id: str) -> pathlib.Path:
    return queue / FACTS_DIR / f"{brief_id}.toml"


def facts_files(queue: pathlib.Path) -> list[pathlib.Path]:
    """Every facts file, by name."""
    return sorted((queue / FACTS_DIR).glob("*.toml"), key=lambda p: p.name)


def declared_brief(path: pathlib.Path) -> str | None:
    """The `brief` a facts file names, None if it names none. Raises on a file that is not TOML."""
    declared = tomllib.loads(path.read_text()).get("brief")
    return declared if isinstance(declared, str) else None


def load_brief_facts(queue: pathlib.Path, brief_id: str) -> BriefFacts:
    """The facts recorded for `brief_id`; empty when none are recorded or the file cannot be used."""
    path = facts_path(queue, brief_id)
    table = _read(path)
    if table is None:
        return BriefFacts(brief_id)
    if table.get("brief") != brief_id:
        warn(f"{path}: `brief` must be {brief_id!r}; the file is left out")
        return BriefFacts(brief_id)
    return BriefFacts.parse(brief_id, path, table)


def load_facts(queue: pathlib.Path) -> dict[str, BriefFacts]:
    """The facts of every brief that has a usable file, by brief id."""
    return {p.stem: load_brief_facts(queue, p.stem) for p in facts_files(queue)}


def lacks_evidence(queue: pathlib.Path, brief_id: str) -> bool:
    """True when nothing is recorded as evidence for `brief_id`."""
    return not load_brief_facts(queue, brief_id).evidence


# --- continuous integration --------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CiRow:
    """One reading of the project's latest build: green or red, with the run it came from."""

    state: str
    link: str
    set_on: dt.date
    source: str

    @classmethod
    def parse(cls, table: Any, source: str) -> "CiRow":
        if not isinstance(table, Mapping):
            raise RowError("not a table")
        return cls(
            _choice(table, "state", CI_STATES),
            _https(table, "link"),
            _day(table, "set_on"),
            source,
        )

    @property
    def order(self) -> tuple[dt.date, str]:
        return (self.set_on, self.source)

    def table(self) -> dict[str, Any]:
        """The file body that parses back to this row."""
        return {"state": self.state, "link": self.link, "set_on": self.set_on}


def load_ci(queue: pathlib.Path) -> list[CiRow]:
    """Every CI reading, oldest first."""
    rows: list[CiRow] = []
    for path in _history_files(queue, CI_DIR):
        table = _read(path)
        if table is None:
            continue
        try:
            rows.append(CiRow.parse(table, path.name))
        except RowError as e:
            warn(f"{path}: ci: {e}; that row is left out")
    return sorted(rows, key=lambda r: r.order)


def current_ci(queue: pathlib.Path) -> CiRow | None:
    """The newest CI reading, None when none is recorded."""
    rows = load_ci(queue)
    return rows[-1] if rows else None


# --- project meta ------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Release:
    """A named release and the day it ships; briefs name it in their `release`."""

    id: str
    date: dt.date

    @classmethod
    def parse(cls, table: Any) -> "Release":
        if not isinstance(table, Mapping):
            raise RowError("not a table")
        return cls(_text(table, "id"), _day(table, "date"))


@dataclasses.dataclass(frozen=True)
class ProjectMeta:
    """The optional descriptive keys of `project.toml`; each is empty when not recorded."""

    initiative: str | None = None
    links: tuple[tuple[str, str], ...] = ()
    releases: tuple[Release, ...] = ()


def _links(path: pathlib.Path, raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, Mapping):
        warn(f"{path}: links must be a [links] table; it is left out")
        return ()
    found: list[tuple[str, str]] = []
    for name, url in raw.items():
        try:
            found.append((name, _https({"url": url}, "url")))
        except RowError as e:
            warn(f"{path}: link {name!r}: {e}; that link is left out")
    return tuple(found)


def load_project_meta(queue: pathlib.Path) -> ProjectMeta:
    """`initiative`, `[links]` and `[[releases]]` from `project.toml`, whatever is usable."""
    path = queue / PROJECT_FILE
    table = _read(path) or {}
    return ProjectMeta(
        _optional_text(table, "initiative"),
        _links(path, table["links"]) if "links" in table else (),
        tuple(_rows(path, table, "releases", Release.parse)),
    )

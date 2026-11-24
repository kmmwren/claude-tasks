"""Roll-up views: work, project and portfolio readings of the same projects, as JSON, Markdown or HTML.

A View turns `Entry` objects (a model Project plus the queue facts the model leaves out) into a small
document tree. A Markup format renders that tree, so no view knows a format and no format knows a
view. Every string from a queue reaches Markdown or HTML through these classes, so none becomes markup.
"""

import dataclasses
import datetime as dt
import enum
import html
import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from itertools import chain
from typing import Any, ClassVar

import facts
import prioritise
import project_view as pv
import rollup_model as rm
from _dates import parse_date
from _paths import warn_undecodable
from queue_snapshot import QueueSnapshot
from requirements_register import parse_register, register_path
from rollup_model import plural

SCHEMA = "claude-tasks.rollup/v1"
DUE_SOON_DAYS = 14
BUG = "bug"
BACKLOG_STATES = ("queued", "parked")
NO_MILESTONE = "No milestone"
NO_INITIATIVE = "No initiative"
NO_RELEASE = f"No release named, last {rm.SHIPPED_DAYS} days"
NO_PROJECTS = "No projects found. A project is a queue with a project.toml."
NO_READY = "No ready briefs."
NO_BUGS = "No open bugs."
NO_UNCOVERED = "No uncovered requirements."
NO_BACKLOG = "No backlog."
NO_MILESTONES = "No milestones defined."
NO_NEXT = "no open milestone has a date."
NO_ASK = "none."
NO_BRIEFS = "No briefs"
NO_DATE = "No date"
NO_BASELINE = "No first date"
NO_SLIP = "No slip"
NO_COUNT = "Not counted"
DETAIL_SUMMARY = "Show detail"
BADGE = "Roll-up"
CHEVRON = (
    '<svg class="sg-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>'
)
CALENDAR = (
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" '
    'aria-hidden="true"><rect x="2.5" y="4" width="15" height="13" rx="2"/>'
    '<path d="M2.5 8.5h15M6.5 2v4M13.5 2v4"/></svg>'
)
NO_SHIPPED = f"Nothing shipped in the last {rm.SHIPPED_DAYS} days."
HTML_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src https:; media-src https:; "
    "frame-ancestors 'none'"
)

# --- what the model leaves out ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RankedBrief:
    """A ready brief with its place in the queue's own ranking and how close its due date is."""

    rank: int
    id: str
    title: str
    kind: str
    effort: str
    due: dt.date | None
    days_to_due: int | None
    waiting_on: tuple[str, ...]

    @classmethod
    def of(
        cls, rank: int, raw: dict[str, Any], today: dt.date, waiting_on: tuple[str, ...]
    ) -> "RankedBrief":
        due = parse_date(raw.get("due"))
        return cls(
            rank,
            raw["id"],
            raw.get("title", raw["id"]),
            raw.get("type", ""),
            raw.get("estimated-effort", ""),
            due,
            (due - today).days if due else None,
            waiting_on,
        )

    @property
    def is_bug(self) -> bool:
        return self.kind == BUG

    @property
    def overdue(self) -> bool:
        return self.days_to_due is not None and self.days_to_due < 0

    @property
    def due_soon(self) -> bool:
        return self.days_to_due is not None and self.days_to_due <= DUE_SOON_DAYS

    def urgency(self) -> tuple[int, int]:
        """Sorts the briefs due within the fortnight (overdue first) ahead of the rest."""
        return (0, self.days_to_due or 0) if self.due_soon else (1, 0)


@dataclasses.dataclass(frozen=True)
class Gap:
    """A requirement in the register that no brief cites."""

    id: str
    words: str


@dataclasses.dataclass(frozen=True)
class BacklogGroup:
    """Open work not yet started; `milestone` is empty for the group with no milestone."""

    milestone: str
    title: str
    briefs: tuple[rm.BriefSummary, ...]


def _ready(read: rm.QueueRead) -> tuple[RankedBrief, ...]:
    today, waiting = read.today, read.blockers
    ranked = prioritise.rank(read.files, today=today, config=read.settings)
    items = [
        RankedBrief.of(place, raw, today, waiting.get(raw["id"], ()))
        for place, raw in enumerate(ranked, 1)
    ]
    return tuple(sorted(items, key=RankedBrief.urgency))


def _gaps(read: rm.QueueRead) -> tuple[Gap, ...]:
    """The register's requirements no brief cites; a register that cannot be decoded gives none."""
    register = register_path(read.queue)
    if not register.is_file():
        return ()
    try:
        text = register.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        warn_undecodable(f"{read.label}/{register.name}", "no requirements are read from it")
        return ()
    cited = {r for b in read.briefs for r in b["requirements"]}
    return tuple(
        Gap(r.id, r.quote or r.title)
        for r in parse_register(text)
        if not r.withdrawn and r.id not in cited
    )


def _not_started(briefs: Iterable[rm.BriefSummary]) -> tuple[rm.BriefSummary, ...]:
    return tuple(b for b in briefs if b.state in BACKLOG_STATES)


def _backlog(
    project: rm.Project, briefs: list[dict[str, Any]], waiting: dict[str, tuple[str, ...]]
) -> tuple[BacklogGroup, ...]:
    """One group per milestone that has work waiting, then the briefs no milestone holds."""
    groups = [
        BacklogGroup(m.id, m.title, queued)
        for m in project.milestones
        if (queued := _not_started(m.briefs))
    ]
    placed = {b.id for m in project.milestones for b in m.briefs}
    loose = _not_started(
        rm.BriefSummary.of(b, waiting.get(b["id"], ()), project.as_at)
        for b in briefs
        if b["id"] not in placed
    )
    return tuple(groups + ([BacklogGroup("", NO_MILESTONE, loose)] if loose else []))


@dataclasses.dataclass(frozen=True)
class QueueBoard:
    """The work-level facts about one queue: what is ready, what is uncovered, what waits."""

    as_at: rm.AsAt
    ready: tuple[RankedBrief, ...]
    gaps: tuple[Gap, ...]
    backlog: tuple[BacklogGroup, ...]

    @classmethod
    def read(cls, read: rm.QueueRead, project: rm.Project) -> "QueueBoard":
        return cls(
            project.as_at,
            _ready(read),
            _gaps(read),
            _backlog(project, read.briefs, read.blockers),
        )

    @property
    def bugs(self) -> tuple[RankedBrief, ...]:
        return tuple(b for b in self.ready if b.is_bug)

    @property
    def work(self) -> tuple[RankedBrief, ...]:
        return tuple(b for b in self.ready if not b.is_bug)


@dataclasses.dataclass(frozen=True)
class Entry:
    project: rm.Project
    board: QueueBoard

    @classmethod
    def read(cls, label: str, snapshot: QueueSnapshot, now: dt.datetime) -> "Entry":
        """One queue's project and board, from a single read of its snapshot."""
        read = rm.QueueRead.load(label, snapshot, now)
        project = rm.project_of(read)
        return cls(project, QueueBoard.read(read, project))

    @property
    def title(self) -> str:
        return self.project.title


@dataclasses.dataclass(frozen=True)
class Omission:
    """A source that gave no project, and why."""

    source: str
    queue: str
    reason: str

    def line(self) -> str:
        where = f"{self.source} / {self.queue}" if self.queue else self.source
        return f"{where}: {self.reason}"


@dataclasses.dataclass(frozen=True)
class SourceNote:
    """A warning about one source, such as a cached copy shown while it was unreachable."""

    source: str
    message: str


@dataclasses.dataclass(frozen=True)
class Report:
    generated: dt.datetime
    sources: tuple[str, ...]
    entries: tuple[Entry, ...]
    omitted: tuple[Omission, ...] = ()
    notes: tuple[SourceNote, ...] = ()

    @property
    def stamp(self) -> str:
        return self.generated.isoformat(timespec="seconds")

    def project_counts(self) -> dict[str, int]:
        counted = Counter(e.project.source for e in self.entries)
        return {label: counted[label] for label in self.sources}


@dataclasses.dataclass(frozen=True)
class InitiativeGroup:
    name: str
    entries: tuple[Entry, ...]

    @property
    def title(self) -> str:
        return self.name or NO_INITIATIVE


def initiatives(entries: Iterable[Entry]) -> tuple[InitiativeGroup, ...]:
    """Entries grouped by their project's initiative in order of first appearance; none last."""
    found: dict[str, list[Entry]] = {}
    for e in entries:
        found.setdefault(e.project.initiative, []).append(e)
    named = [InitiativeGroup(k, tuple(v)) for k, v in found.items() if k]
    return tuple(named + ([InitiativeGroup("", tuple(found[""]))] if "" in found else []))


# --- the document tree -----------------------------------------------------------------------

_ESCAPABLE = "\\*_[]<>`|"
# GitHub links a bare `http://`, `https://`, `www.` or email on its own. A backslash before the
# `:` or `.` keeps a URL plain, but GitHub rejoins escaped text before matching emails, so an
# invisible word joiner after the `@` is what breaks those.
_SPECIAL = re.compile(f"[{re.escape(_ESCAPABLE)}]|(?<=www)\\.|:(?=//)", re.IGNORECASE)
_MENTION = re.compile(r"@(?=\S)")
_SAFE_LINK = re.compile(r"https://[^\s<>\"'()\\]+")


def escape_md(text: str) -> str:
    """One line of literal text: whitespace collapsed so it cannot start a block, markup escaped."""
    escaped = _SPECIAL.sub(r"\\\g<0>", " ".join(text.split()))
    return _MENTION.sub("@\N{WORD JOINER}", escaped)


def is_https(url: str) -> bool:
    """A link both formats can carry without it breaking out of its attribute or its brackets."""
    return _SAFE_LINK.fullmatch(url) is not None


class Tone(enum.Enum):
    GOOD = "good"
    WARN = "warn"
    BAD = "bad"
    ACTIVE = "active"
    NEUTRAL = "neutral"


HEALTH_TONE = {pv.GREEN: Tone.GOOD, pv.AMBER: Tone.WARN, pv.RED: Tone.BAD}
BUILD_TONE = {"green": Tone.GOOD, "red": Tone.BAD}
STAGE_TONE = {rm.Stage.DONE: Tone.GOOD, rm.Stage.NOW: Tone.ACTIVE, rm.Stage.NEXT: Tone.NEUTRAL}


class Inline(ABC):
    @abstractmethod
    def render(self, m: "Markup") -> str: ...


@dataclasses.dataclass(frozen=True)
class Text(Inline):
    text: str

    def render(self, m: "Markup") -> str:
        return m.text(self.text)


@dataclasses.dataclass(frozen=True)
class Strong(Inline):
    text: str

    def render(self, m: "Markup") -> str:
        return m.strong(self.text)


@dataclasses.dataclass(frozen=True)
class Chip(Inline):
    """A short status word; the word carries the meaning, the tone only the colour."""

    label: str
    tone: Tone = Tone.NEUTRAL

    def render(self, m: "Markup") -> str:
        return m.chip(self.label, self.tone)


@dataclasses.dataclass(frozen=True)
class Link(Inline):
    """A link when its address is a plain https one; otherwise just its words."""

    text: str
    url: str

    def render(self, m: "Markup") -> str:
        return m.link(self.text, self.url) if is_https(self.url) else m.text(self.text)


@dataclasses.dataclass(frozen=True)
class Space(Inline):
    def render(self, m: "Markup") -> str:
        return " "


@dataclasses.dataclass(frozen=True)
class Code(Inline):
    text: str

    def render(self, m: "Markup") -> str:
        return m.code(self.text)


@dataclasses.dataclass(frozen=True)
class Page(Inline):
    """A link to another file of the same roll-up; its target is built from view and format names."""

    text: str
    target: str

    def render(self, m: "Markup") -> str:
        return m.link(self.text, self.target)


@dataclasses.dataclass(frozen=True)
class Image(Inline):
    caption: str
    url: str

    def render(self, m: "Markup") -> str:
        return m.image(self.caption, self.url)


@dataclasses.dataclass(frozen=True)
class Video(Inline):
    caption: str
    url: str

    def render(self, m: "Markup") -> str:
        return m.video(self.caption, self.url)


_PLAYERS = {"screenshot": Image, "video": Video}


def show(e: facts.Evidence) -> Inline:
    """Evidence anyone can open is shown where it stands; the rest is a link that says who can."""
    if e.access == "public" and is_https(e.link):
        return _PLAYERS[e.kind](e.caption, e.link)
    return Link(f"{e.caption} ({e.kind}, {e.access} access)", e.link)


@dataclasses.dataclass(frozen=True)
class Row:
    parts: tuple[Inline, ...]
    gap: str = " "

    def render(self, m: "Markup") -> str:
        return self.gap.join(p.render(m) for p in self.parts)


def row(*parts: Inline | str) -> Row:
    return Row(tuple(Text(p) if isinstance(p, str) else p for p in parts))


class Block(ABC):
    @abstractmethod
    def render(self, m: "Markup", depth: int) -> str: ...


@dataclasses.dataclass(frozen=True)
class Paragraph(Block):
    line: Row

    def render(self, m: "Markup", depth: int) -> str:
        return m.paragraph(self.line.render(m))


@dataclasses.dataclass(frozen=True)
class Item:
    head: Row
    details: tuple[Row, ...] = ()


@dataclasses.dataclass(frozen=True)
class Bullets(Block):
    items: tuple[Item, ...]

    def render(self, m: "Markup", depth: int) -> str:
        return self.draw(
            m, [(i.head.render(m), [d.render(m) for d in i.details]) for i in self.items]
        )

    def draw(self, m: "Markup", shown: list[tuple[str, list[str]]]) -> str:
        return m.bullets(shown)


class NumberedBullets(Bullets):
    """Items a reader refers to by position, such as a ranking."""

    def draw(self, m: "Markup", shown: list[tuple[str, list[str]]]) -> str:
        return m.numbered(shown)


@dataclasses.dataclass(frozen=True)
class Section(Block):
    """A headed part of a page."""

    heading: str
    blocks: tuple[Block, ...] = ()

    def render(self, m: "Markup", depth: int) -> str:
        return self.frame(m, depth, [b.render(m, depth + 1) for b in self.blocks])

    def frame(self, m: "Markup", depth: int, body: list[str]) -> str:
        return m.section(self.heading, depth, body)


class TileSection(Section):
    """One project's summary, which a format may style apart from the sections around it."""

    def frame(self, m: "Markup", depth: int, body: list[str]) -> str:
        return m.tile(self.heading, depth, body)


@dataclasses.dataclass(frozen=True)
class Details(Block):
    """Blocks a reader opens on demand; `summary` is fixed wording, never queue text."""

    summary: str
    blocks: tuple[Block, ...]

    def render(self, m: "Markup", depth: int) -> str:
        return m.details(self.summary, [b.render(m, depth) for b in self.blocks])


@dataclasses.dataclass(frozen=True)
class Target:
    """The date a row aims at; `late` once it has passed with the work not done."""

    iso: str = ""
    late: bool = False


@dataclasses.dataclass(frozen=True)
class Progress:
    done: int
    total: int

    @classmethod
    def of(cls, milestones: Iterable[rm.MilestoneSummary]) -> "Progress":
        stages = [m.stage for m in milestones]
        return cls(stages.count(rm.Stage.DONE), len(stages))

    @property
    def percent(self) -> int:
        return round(100 * self.done / self.total) if self.total else 0

    @property
    def text(self) -> str:
        return f"{self.done}/{self.total}"

    def tone(self, standing: Tone) -> Tone:
        """A finished count reads as done whatever the row's standing; otherwise it shares it."""
        return Tone.GOOD if self.total and self.done == self.total else standing


@dataclasses.dataclass(frozen=True)
class StatusRow:
    """One line of a status grid; `progress` is None where nothing is counted."""

    title: str
    description: str
    target: Target
    progress: Progress | None
    status: Chip
    note: str
    children: tuple["StatusRow", ...] = ()


@dataclasses.dataclass(frozen=True)
class StatusGrid(Block):
    """Rows read down by status, each opening onto the rows it is made of."""

    noun: str
    child_noun: str
    rows: tuple[StatusRow, ...]

    def render(self, m: "Markup", depth: int) -> str:
        return m.status_grid(self)

    def numbered(self) -> Iterable[tuple[str, StatusRow]]:
        """Every row with its place: `2` for a top row, `2.3` for the third row under it."""
        for n, top in enumerate(self.rows, 1):
            yield str(n), top
            yield from ((f"{n}.{k}", child) for k, child in enumerate(top.children, 1))

    def tally(self) -> tuple[tuple[Chip, int], ...]:
        """How many rows stand at each status, in order of first appearance."""
        return tuple(Counter(r.status for _, r in self.numbered()).items())

    def totals(self) -> str:
        children = sum(len(r.children) for r in self.rows)
        counts = [(len(self.rows), self.noun), (children, self.child_noun)]
        return ", ".join(plural(n, noun.lower()) for n, noun in counts)


@dataclasses.dataclass(frozen=True)
class Sibling(Inline):
    """Another view of the same roll-up, linked in whichever format the page is drawn in."""

    view: "View"
    current: bool = False

    def render(self, m: "Markup") -> str:
        return m.sibling(self.view.title, m.filename(self.view), self.current)


@dataclasses.dataclass(frozen=True)
class Document:
    title: str
    generated: str
    sources: tuple[str, ...]
    blocks: tuple[Block, ...]
    nav: tuple[Inline, ...] = ()

    def render(self, m: "Markup") -> str:
        body = [b.render(m, 2) for b in self.blocks]
        nav = [n.render(m) for n in self.nav]
        return m.document(self.title, self.generated, self.sources, nav, body)


def paragraph(*parts: Inline | str) -> Paragraph:
    return Paragraph(row(*parts))


def bullets(items: Iterable[Item], gap: str) -> Block:
    """The items as a list, or `gap` saying what is missing, so a section is never blank."""
    return _listed(Bullets, items, gap)


def _listed(block: type[Bullets], items: Iterable[Item], gap: str) -> Block:
    listed = tuple(items)
    return block(listed) if listed else paragraph(gap)


def numbered(items: Iterable[Item], gap: str) -> Block:
    """The items as a numbered list, in order, or `gap` saying what is missing."""
    return _listed(NumberedBullets, items, gap)


def lines(rows: Iterable[Row], gap: str) -> Block:
    return bullets((Item(r) for r in rows), gap)


# --- formats ---------------------------------------------------------------------------------


class Format(ABC):
    name: ClassVar[str]
    content_type: ClassVar[str]
    suffix: ClassVar[str]

    @abstractmethod
    def render(self, view: "View", report: Report) -> str: ...

    def filename(self, view: "View") -> str:
        """The file a view has in this format, and the route the server answers it on."""
        return f"{view.name}{self.suffix}"


class JsonFormat(Format):
    name, content_type, suffix = "json", "application/json", ".json"

    def render(self, view: "View", report: Report) -> str:
        return json.dumps(view.data(report), indent=2)


class Markup(Format, ABC):
    """A format drawn from the document tree; each method returns one piece of finished text."""

    def render(self, view: "View", report: Report) -> str:
        return view.document(report).render(self)

    @abstractmethod
    def text(self, text: str) -> str: ...

    @abstractmethod
    def strong(self, text: str) -> str: ...

    @abstractmethod
    def chip(self, label: str, tone: Tone) -> str: ...

    @abstractmethod
    def code(self, text: str) -> str: ...

    @abstractmethod
    def link(self, text: str, url: str) -> str: ...

    @abstractmethod
    def image(self, caption: str, url: str) -> str: ...

    @abstractmethod
    def video(self, caption: str, url: str) -> str: ...

    @abstractmethod
    def paragraph(self, line: str) -> str: ...

    @abstractmethod
    def bullets(self, items: list[tuple[str, list[str]]]) -> str: ...

    @abstractmethod
    def numbered(self, items: list[tuple[str, list[str]]]) -> str: ...

    @abstractmethod
    def section(self, heading: str, depth: int, body: list[str]) -> str: ...

    def tile(self, heading: str, depth: int, body: list[str]) -> str:
        return self.section(heading, depth, body)

    @abstractmethod
    def details(self, summary: str, body: list[str]) -> str: ...

    @abstractmethod
    def status_grid(self, grid: StatusGrid) -> str: ...

    def sibling(self, title: str, target: str, current: bool) -> str:
        return self.strong(title) if current else self.link(title, target)

    @abstractmethod
    def document(
        self,
        title: str,
        generated: str,
        sources: tuple[str, ...],
        nav: list[str],
        body: list[str],
    ) -> str: ...


def moment(as_at: rm.AsAt) -> str:
    return f"{as_at.committed:%Y-%m-%d %H:%M %Z}".strip()


class MarkdownFormat(Markup):
    name, content_type, suffix = "markdown", "text/markdown; charset=utf-8", ".md"

    def text(self, text: str) -> str:
        return escape_md(text)

    def strong(self, text: str) -> str:
        return f"**{escape_md(text)}**"

    def chip(self, label: str, tone: Tone) -> str:
        tidy = label.replace("`", "'")
        return f"`{tidy}`"

    def code(self, text: str) -> str:
        tidy = text.replace("`", "'")
        return f"`{tidy}`"

    def link(self, text: str, url: str) -> str:
        return f"[{escape_md(text)}]({url})"

    def image(self, caption: str, url: str) -> str:
        return f"![{escape_md(caption)}]({url})"

    def video(self, caption: str, url: str) -> str:
        return self.link(f"{caption} (video)", url)

    def paragraph(self, line: str) -> str:
        return line

    def bullets(self, items: list[tuple[str, list[str]]]) -> str:
        return self._list(items, lambda number: "- ")

    def numbered(self, items: list[tuple[str, list[str]]]) -> str:
        return self._list(items, lambda number: f"{number}. ")

    def _list(self, items: list[tuple[str, list[str]]], marker: Callable[[int], str]) -> str:
        shown = enumerate(items, 1)
        return "\n".join(self._item(marker(n), head, rest) for n, (head, rest) in shown)

    @staticmethod
    def _item(marker: str, head: str, rest: list[str]) -> str:
        indent = " " * len(marker)
        return f"{marker}{head}" + "".join(f"\n{indent}- {d}" for d in rest)

    def table(self, headings: list[str], rows: list[list[str]]) -> str:
        shown = [f"| {' | '.join(escape_md(h) for h in headings)} |", "|" + "---|" * len(headings)]
        return "\n".join([*shown, *(f"| {' | '.join(cells)} |" for cells in rows)])

    def section(self, heading: str, depth: int, body: list[str]) -> str:
        return "\n\n".join([f"{'#' * min(depth, 6)} {escape_md(heading)}", *body])

    def details(self, summary: str, body: list[str]) -> str:
        opening = f"<details>\n<summary>{html.escape(summary)}</summary>"
        return "\n\n".join([opening, *body, "</details>"])

    def status_grid(self, grid: StatusGrid) -> str:
        """A table whose child rows are numbered under their parent and marked with an arrow."""
        headings = ["#", grid.noun, "Due", "Progress", "Status", "Note"]
        rows = [self._grid_cells(n, r) for n, r in grid.numbered()]
        key = ", ".join(f"{self.chip(c.label, c.tone)} {n}" for c, n in grid.tally())
        return "\n\n".join([self.table(headings, rows), f"{escape_md(grid.totals())}. Key: {key}"])

    def _grid_cells(self, number: str, r: StatusRow) -> list[str]:
        arrow = "↳ " if "." in number else ""
        return [
            number,
            f"{arrow}{self.strong(r.title)} {escape_md(r.description)}".rstrip(),
            self._target(r.target),
            r.progress.text if r.progress else NO_COUNT,
            self.chip(r.status.label, r.status.tone),
            escape_md(r.note),
        ]

    def _target(self, t: Target) -> str:
        return (t.iso + (" `late`" if t.late else "")) if t.iso else NO_DATE

    def document(
        self,
        title: str,
        generated: str,
        sources: tuple[str, ...],
        nav: list[str],
        body: list[str],
    ) -> str:
        origin = ", ".join(escape_md(s) for s in sources)
        head = [f"# {escape_md(title)}", f"*Generated {generated} from {origin}.*"]
        links = [" · ".join(nav)] if nav else []
        return "\n\n".join([*head, *links, *body]) + "\n"


STYLE = """:root{--page:#F5F6FA;--card:#fff;--ink:#1F2950;--text:#2C3E50;--muted:#8A93AD;--slate:#5B6B8C;
--line:#E4E5EE;--soft:#EEF0F5;--kid:#FBFCFE;--link:#1F5FBF;--date:#EFF1F7;--date-fg:#3D4670;
--late:#F8C7CD;--late-fg:#8A0E12;--empty:#D3D8E5;--total:#3E5A8F;
--good:#16A34A;--good-bg:#D9F4E3;--good-bar:#1DB45A;--warn:#B7791F;--warn-bg:#FFF0D4;--warn-bar:#FFB800;
--bad:#E5303A;--bad-bg:#FDDDDF;--bad-bar:#F0313C;--active:#2B5FD9;--active-bg:#E3ECFF;--active-bar:#4C7DF0;
--neutral:#3A4466;--neutral-bg:#EEF0F5;--neutral-bar:#C9D3E6}
@media (prefers-color-scheme:dark){:root{--page:#0B1020;--card:#141A2E;--ink:#E6E9F2;--text:#D5DAE6;
--slate:#A3AEC8;--line:#262E4A;--soft:#1D2440;--kid:#11172A;--link:#8AB4FF;--date:#1F2742;
--date-fg:#C9D0E4;--late:#4A1A20;--late-fg:#FFB3B8;--empty:#39425F;--total:#9DB2DA;
--good:#6EE19A;--good-bg:#12351F;--warn:#F5C76A;--warn-bg:#3A2C0C;--bad:#FF9AA0;--bad-bg:#40161B;
--active:#9DBBFF;--active-bg:#18264A;--neutral:#C5CCDE;--neutral-bg:#232B45;--neutral-bar:#3A4466}}
*{box-sizing:border-box}
body{background:var(--page);color:var(--text);font:14px/1.5 Roboto,Arial,system-ui,sans-serif;margin:0}
.wrap,main{max-width:72rem;margin:0 auto;padding:0 1.5rem}
main{padding-bottom:3rem}
.cover{background:linear-gradient(to right,#001039 0%,#0B2260 55%,#163D80 100%);color:#fff;padding:1.6rem 0 1.4rem}
.badge{display:inline-block;border:1px solid rgba(250,203,50,.45);border-radius:20px;padding:3px 13px;
font-size:.7rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#FACB32;
background:rgba(100,160,255,.08)}
.cover h1{font-size:2rem;font-weight:700;margin:.5rem 0 .4rem;color:#fff}
.meta{display:flex;flex-wrap:wrap;gap:.4rem 1.4rem;margin:0;font-size:.8rem;color:rgba(255,255,255,.65)}
.meta b{color:#fff;margin-right:.35rem}
nav.views{background:#001039}
nav.views .wrap{display:flex;flex-wrap:wrap;gap:.2rem 1.6rem;padding-top:.7rem;padding-bottom:.7rem}
nav.views a{color:rgba(255,255,255,.85);font-size:.75rem;font-weight:600;letter-spacing:.8px;
text-transform:uppercase;text-decoration:none;padding-bottom:.2rem;border-bottom:2px solid transparent}
nav.views a:hover{color:#fff}nav.views a[aria-current=page]{color:#fff;border-bottom-color:#FACB32}
h2{display:flex;align-items:center;gap:.5rem;font-size:.8rem;font-weight:700;letter-spacing:1.4px;
text-transform:uppercase;color:var(--ink);margin:2.2rem 0 .9rem}
h2::before{content:"";width:4px;height:15px;background:var(--ink);flex:none}
h3{font-size:1.05rem;color:var(--ink);margin:1.4rem 0 .3rem}
h4,h5,h6{font-size:.95rem;color:var(--ink);margin:1rem 0 .2rem}
p{margin:.4rem 0}a{color:var(--link)}code{font-size:.9em}
section{overflow-wrap:anywhere}
.chip{display:inline-block;border-radius:999px;padding:.05rem .65rem;font-size:.78rem;font-weight:600;
white-space:nowrap;line-height:1.5}
.chip-good{color:var(--good);background:var(--good-bg)}.chip-warn{color:var(--warn);background:var(--warn-bg)}
.chip-bad{color:var(--bad);background:var(--bad-bg)}.chip-active{color:var(--active);background:var(--active-bg)}
.chip-neutral{color:var(--neutral);background:var(--neutral-bg)}
ul.items,ul.details{margin:.2rem 0;padding-left:1.2rem}ul.items>li{margin:.35rem 0}
ul.details{color:var(--slate)}.head{color:var(--text)}
ol.items{margin:.2rem 0;padding-left:1.6rem}ol.items>li{margin:.35rem 0}
section.tile{background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:.1rem 1rem .7rem;margin:.8rem 0;box-shadow:0 1px 2px rgba(16,24,64,.05)}
time{white-space:nowrap}
details{margin:.4rem 0}summary{cursor:pointer;color:var(--slate)}
figure.evidence{margin:.4rem 0;max-width:100%}figure.evidence img,figure.evidence video{max-width:100%;
height:auto;border:1px solid var(--line);border-radius:.3rem;background:var(--card)}
figcaption{font-size:.85rem;color:var(--muted)}
.sg{--cols:2.8rem minmax(0,1fr) 8.4rem 8.6rem 8rem minmax(0,15rem);background:var(--card);
border:1px solid var(--line);border-radius:6px;overflow:hidden;box-shadow:0 1px 2px rgba(16,24,64,.05)}
.sg-head,.sg-row{display:grid;grid-template-columns:var(--cols);align-items:center}
.sg-head{background:#1F2950;color:#fff;font-size:.68rem;font-weight:500;letter-spacing:.4px;text-transform:uppercase}
.sg-head span{padding:0 .7rem;border-left:1px solid rgba(255,255,255,.14);line-height:2.4rem}
.sg-head span:first-child{border-left:0;text-align:center}
.sg-item{margin:0;border-top:1px solid var(--line)}
.sg-row{position:relative;min-height:3.2rem;padding:.45rem 0}
.sg-row>span{padding:0 .7rem;min-width:0}
.sg-row::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2.5px;background:var(--neutral-bar)}
.tone-good::before{background:var(--good-bar)}.tone-warn::before{background:var(--warn-bar)}
.tone-bad::before{background:var(--bad-bar)}.tone-active::before{background:var(--active-bar)}
summary.sg-row{cursor:pointer;color:inherit;list-style:none}summary.sg-row::-webkit-details-marker{display:none}
summary.sg-row:hover{background:var(--kid)}
.sg-n{display:flex;justify-content:center}
.sg-num{display:inline-flex;align-items:center;justify-content:center;min-width:1.45rem;height:1.45rem;
padding:0 .25rem;border-radius:3px;background:#1F2950;color:#fff;font-size:.72rem;font-weight:600}
.sg-name{display:flex;gap:.45rem;align-items:flex-start}
.sg-chev{flex:none;width:.7rem;height:.7rem;margin-top:.4rem;color:var(--muted);transition:transform .15s}
details[open]>summary .sg-chev{transform:rotate(90deg)}
.sg-words{min-width:0}
.sg-title{display:block;color:var(--ink);font-weight:500}
.sg-desc{display:block;color:var(--muted);font-size:.8rem;font-style:italic;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.sg-date{display:inline-flex;align-items:center;gap:.3rem;background:var(--date);color:var(--date-fg);
font:700 .74rem/1 "Roboto Mono",ui-monospace,SFMono-Regular,Menlo,monospace;padding:.32rem .45rem;
border-radius:3px;white-space:nowrap}
.sg-date svg{width:.75rem;height:.75rem;flex:none}
.sg-late{background:var(--late);color:var(--late-fg)}
.sg-undated{background:transparent;border:1px dashed var(--empty);color:var(--muted);font-family:inherit;font-weight:500}
.sg-prog{display:flex;align-items:center;gap:.5rem}
.sg-bar{flex:1;max-width:5.5rem;height:.5rem;border-radius:2px;background:var(--soft);
box-shadow:inset 0 0 0 1px var(--line);overflow:hidden}
.sg-fill{display:block;height:100%}
.fill-good{background:var(--good-bar)}.fill-warn{background:var(--warn-bar)}.fill-bad{background:var(--bad-bar)}
.fill-active{background:var(--active-bar)}.fill-neutral{background:var(--neutral-bar)}
.sg-pc{font-size:.75rem;color:var(--slate);font-variant-numeric:tabular-nums}
.sg-none{font-size:.75rem;color:var(--muted)}
.sg-note{color:var(--slate);font-size:.8rem;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sg-kids{background:var(--kid);border-top:1px solid var(--soft);padding-left:1.7rem}
.sg-kids .sg-row{min-height:2.6rem;border-bottom:1px solid var(--soft)}
.sg-kids .sg-row:last-child{border-bottom:0}
.sg-kids .sg-num{background:transparent;color:var(--slate);box-shadow:inset 0 0 0 1px var(--empty)}
.sg-kids .sg-title{font-weight:400}
.sg-foot{display:flex;flex-wrap:wrap;justify-content:space-between;gap:.5rem 1.5rem;margin:.6rem .1rem 0;
font-size:.8rem;color:var(--total)}
.sg-key{display:flex;flex-wrap:wrap;gap:.4rem .8rem;align-items:center}
@media (max-width:760px){.sg{--cols:2.4rem minmax(0,1fr) auto}
.sg-head span:nth-child(n+4),.sg-row>.sg-p,.sg-row>.sg-o{display:none}
.sg-row>.sg-s{grid-column:2/-1;padding-top:.3rem}.sg-kids{padding-left:.6rem}.cover h1{font-size:1.5rem}}"""


class HtmlFormat(Markup):
    name, content_type, suffix = "html", "text/html; charset=utf-8", ""

    def text(self, text: str) -> str:
        return html.escape(text)

    def strong(self, text: str) -> str:
        return f"<strong>{html.escape(text)}</strong>"

    def chip(self, label: str, tone: Tone) -> str:
        return f'<span class="chip chip-{tone.value}">{html.escape(label)}</span>'

    def code(self, text: str) -> str:
        return f"<code>{html.escape(text)}</code>"

    def day(self, iso: str) -> str:
        return f'<time datetime="{html.escape(iso)}">{html.escape(iso)}</time>'

    def link(self, text: str, url: str) -> str:
        return f'<a href="{html.escape(url)}" rel="noopener noreferrer">{html.escape(text)}</a>'

    def image(self, caption: str, url: str) -> str:
        picture = f'<img src="{html.escape(url)}" alt="{html.escape(caption)}" loading="lazy">'
        return self._figure(f'<a href="{html.escape(url)}">{picture}</a>', caption)

    def video(self, caption: str, url: str) -> str:
        player = f'<video src="{html.escape(url)}" controls preload="none"></video>'
        return self._figure(player, caption)

    @staticmethod
    def _figure(media: str, caption: str) -> str:
        shown = f"<figcaption>{html.escape(caption)}</figcaption>"
        return f'<figure class="evidence">{media}{shown}</figure>'

    def paragraph(self, line: str) -> str:
        return f"<p>{line}</p>"

    def bullets(self, items: list[tuple[str, list[str]]]) -> str:
        return self._list("ul", items)

    def numbered(self, items: list[tuple[str, list[str]]]) -> str:
        return self._list("ol", items)

    def _list(self, tag: str, items: list[tuple[str, list[str]]]) -> str:
        return f'<{tag} class="items">' + "".join(self._item(*i) for i in items) + f"</{tag}>"

    @staticmethod
    def _item(head: str, details: list[str]) -> str:
        rest = "".join(f"<li>{d}</li>" for d in details)
        return f'<li><div class="head">{head}</div>' + (
            f'<ul class="details">{rest}</ul></li>' if rest else "</li>"
        )

    def section(self, heading: str, depth: int, body: list[str]) -> str:
        return self._section(f"depth-{depth}", heading, depth, body)

    def tile(self, heading: str, depth: int, body: list[str]) -> str:
        return self._section(f"depth-{depth} tile", heading, depth, body)

    @staticmethod
    def _section(classes: str, heading: str, depth: int, body: list[str]) -> str:
        level = min(depth, 6)
        title = f"<h{level}>{html.escape(heading)}</h{level}>"
        return f'<section class="{classes}">{title}{"".join(body)}</section>'

    def details(self, summary: str, body: list[str]) -> str:
        return f"<details><summary>{html.escape(summary)}</summary>{''.join(body)}</details>"

    def status_grid(self, grid: StatusGrid) -> str:
        """Each row with children opens as a `<details>`, so expanding needs no script."""
        headings = ("#", grid.noun, "Due", "Progress", "Status", "Note")
        cells = "".join(f"<span>{html.escape(h)}</span>" for h in headings)
        rows = "".join(self._grid_item(str(n), r) for n, r in enumerate(grid.rows, 1))
        head = f'<div class="sg-head">{cells}</div>'
        return f'<div class="sg">{head}{rows}</div>{self._grid_foot(grid)}'

    def _grid_item(self, number: str, r: StatusRow) -> str:
        if not r.children:
            return f'<div class="sg-item">{self._grid_row("div", number, r, "")}</div>'
        kids = "".join(self._grid_row("div", str(k), c, "") for k, c in enumerate(r.children, 1))
        summary = self._grid_row("summary", number, r, CHEVRON)
        return f'<details class="sg-item">{summary}<div class="sg-kids">{kids}</div></details>'

    def _grid_row(self, tag: str, number: str, r: StatusRow, chevron: str) -> str:
        cells = (
            ("sg-n", f'<span class="sg-num">{number}</span>'),
            ("sg-name", f"{chevron}{self._words(r)}"),
            ("sg-d", self._target(r.target)),
            ("sg-p", self._progress(r)),
            ("sg-s", r.status.render(self)),
            ("sg-o", f'<span class="sg-note">{html.escape(r.note)}</span>'),
        )
        shown = "".join(f'<span class="{cls}">{body}</span>' for cls, body in cells)
        return f'<{tag} class="sg-row tone-{r.status.tone.value}">{shown}</{tag}>'

    @staticmethod
    def _words(r: StatusRow) -> str:
        title = f'<span class="sg-title">{html.escape(r.title)}</span>'
        desc = f'<span class="sg-desc">{html.escape(r.description)}</span>' if r.description else ""
        return f'<span class="sg-words">{title}{desc}</span>'

    def _target(self, t: Target) -> str:
        if not t.iso:
            return f'<span class="sg-date sg-undated">{NO_DATE}</span>'
        late = " sg-late" if t.late else ""
        return f'<span class="sg-date{late}">{CALENDAR}{self.day(t.iso)}</span>'

    @staticmethod
    def _progress(r: StatusRow) -> str:
        p = r.progress
        if p is None:
            return f'<span class="sg-none">{NO_COUNT}</span>'
        fill = f'<span class="sg-fill fill-{p.tone(r.status.tone).value}" style="width:{p.percent}%"></span>'
        bar = (
            f'<span class="sg-bar" role="img" aria-label="{p.done} of {p.total} done">{fill}</span>'
        )
        return f'<span class="sg-prog">{bar}<span class="sg-pc">{p.text}</span></span>'

    def _grid_foot(self, grid: StatusGrid) -> str:
        key = "".join(f"<span>{c.render(self)} {n}</span>" for c, n in grid.tally())
        totals = f'<span class="sg-totals">{html.escape(grid.totals())}</span>'
        return f'<p class="sg-foot">{totals}<span class="sg-key">{key}</span></p>'

    def sibling(self, title: str, target: str, current: bool) -> str:
        mark = ' aria-current="page"' if current else ""
        return f'<a href="{html.escape(target)}"{mark}>{html.escape(title)}</a>'

    def document(
        self,
        title: str,
        generated: str,
        sources: tuple[str, ...],
        nav: list[str],
        body: list[str],
    ) -> str:
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{html.escape(HTML_CSP)}">'
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
            f"{self._cover(title, generated, sources)}{self._nav(nav)}"
            f"<main>{''.join(body)}</main></body></html>\n"
        )

    def _cover(self, title: str, generated: str, sources: tuple[str, ...]) -> str:
        stamp = f"<span><b>Generated</b>{self.day(generated)}</span>"
        origin = f"<span><b>Sources</b>{html.escape(', '.join(sources))}</span>"
        return (
            f'<header class="cover"><div class="wrap"><span class="badge">{BADGE}</span>'
            f'<h1>{html.escape(title)}</h1><p class="meta">{stamp}{origin}</p></div></header>'
        )

    @staticmethod
    def _nav(nav: list[str]) -> str:
        links = "".join(nav)
        return (
            f'<nav class="views" aria-label="Views"><div class="wrap">{links}</div></nav>'
            if nav
            else ""
        )


JSON, MARKDOWN, HTML = JsonFormat(), MarkdownFormat(), HtmlFormat()
FORMATS: dict[str, Format] = {f.name: f for f in (JSON, MARKDOWN, HTML)}


# --- pieces both the page and the data are built from ----------------------------------------


def date_text(day: dt.date | None) -> str:
    return day.isoformat() if day else ""


def capped(h: rm.Health) -> bool:
    return bool(h.owner) and h.effective != h.owner


def health_chips(h: rm.Health) -> tuple[Chip, ...]:
    """The colour, then the reasons it should not be taken at the owner's word."""
    stale = [Chip(f"stale, set {plural(h.age_days or 0, 'day')} ago", Tone.WARN)] if h.stale else []
    cap = [Chip("capped by automatic checks", Tone.WARN)] if capped(h) else []
    colour = Chip(h.colour_word, HEALTH_TONE.get(h.effective, Tone.NEUTRAL))
    return (colour, *stale, *cap)


def owner_line(h: rm.Health) -> Row:
    if not h.owner:
        return row("The owner has not set a health in project.toml.")
    when = h.set_on or "a date that was not recorded"
    return row(f"Owner said {rm.WORD[h.owner]} on {when}: {h.reason.rstrip('.')}.")


def build_chips(tile: rm.Tile) -> tuple[Chip, ...]:
    b = tile.build
    return (Chip(f"build {b.state}", BUILD_TONE[b.state]),) if b else ()


def tile_links(tile: rm.Tile) -> tuple[Link, ...]:
    b = tile.build
    build = (Link(f"Latest build, {b.state} on {b.set_on}", b.link),) if b else ()
    return (*(Link(label, url) for label, url in tile.links), *build)


def links_row(tile: rm.Tile) -> tuple[Row, ...]:
    links = tile_links(tile)
    return (Row((Strong("Links:"), *links)),) if links else ()


def source_line(p: rm.Project) -> Paragraph:
    """Where and when a project's facts were read, said once, in its tile."""
    at = p.as_at
    cached = (Chip("cached copy", Tone.WARN),) if at.stale else ()
    where = Text(f"{p.source} @ {at.sha[:7]}, {moment(at)}")
    return Paragraph(Row((Strong("Source:"), where, *cached)))


def health_detail(tile: rm.Tile) -> tuple[Section, ...]:
    """The signals behind a cap and the links, for the reader who opens the detail."""
    signals = tuple(row(s) for s in tile.health.signals)
    checks = (Bullets((Item(Row((Strong("Automatic checks:"),)), signals),)),) if signals else ()
    shown = (*checks, *(Paragraph(r) for r in links_row(tile)))
    return (Section("Health detail", shown),) if shown else ()


def tile_line(tile: rm.Tile) -> Block:
    return Paragraph(Row((*health_chips(tile.health), *build_chips(tile), *tile_links(tile))))


def brief_parts(b: rm.BriefSummary) -> tuple[Inline | str, ...]:
    parked = [Chip("parked")] if b.state == "parked" else []
    effort = [f"effort {b.effort}"] if b.effort else []
    due = [f"due {b.due.isoformat()}"] if b.due else []
    return (f"{b.title} ({b.id})", *parked, *effort, *due, *waiting_parts(b.blockers))


def waiting_parts(names: Sequence[str]) -> tuple[Inline | str, ...]:
    return (Chip("waiting", Tone.WARN), f"on {', '.join(names)}") if names else ()


def due_parts(b: RankedBrief) -> tuple[Inline | str, ...]:
    if b.due is None or b.days_to_due is None:
        return ()
    if b.overdue:
        return (Chip(f"overdue {plural(-b.days_to_due, 'day')}", Tone.BAD), f"was due {b.due}")
    if b.due_soon:
        return (
            Chip(
                "due today" if not b.days_to_due else f"due in {plural(b.days_to_due, 'day')}",
                Tone.WARN,
            ),
            str(b.due),
        )
    return (f"due {b.due}",)


def ranked_item(b: RankedBrief) -> Item:
    effort = [f"effort {b.effort}"] if b.effort else []
    head = (f"{b.title} ({b.id})", *due_parts(b), *effort, *waiting_parts(b.waiting_on))
    return Item(row(*head))


# --- status grid rows ------------------------------------------------------------------------

MISSED = Chip("Missed", Tone.BAD)
READY = Chip("Ready")
BRIEF_STANDING: tuple[tuple[Callable[[RankedBrief], bool], Chip], ...] = (
    (lambda b: b.overdue, Chip("Overdue", Tone.BAD)),
    (lambda b: bool(b.waiting_on), Chip("Waiting", Tone.WARN)),
    (lambda b: b.due_soon, Chip("Due soon", Tone.WARN)),
)


def sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def stage_chip(stage: rm.Stage) -> Chip:
    return Chip(stage.value, STAGE_TONE[stage])


def milestone_status(m: rm.MilestoneSummary) -> Chip:
    return MISSED if m.missed else stage_chip(m.stage)


def target_of(m: rm.MilestoneSummary | None) -> Target:
    return Target(m.due, m.missed) if m else Target()


def project_row(e: Entry, note: str, children: tuple[StatusRow, ...] = ()) -> StatusRow:
    """A project's health, its next dated milestone and how many milestones are done."""
    p = e.project
    standing = health_chips(p.health)[0]
    progress = Progress.of(p.milestones)
    return StatusRow(
        p.title, p.outcome, target_of(p.next_due()), progress, standing, note, children
    )


def milestone_row(m: rm.MilestoneSummary) -> StatusRow:
    counts = m.milestone.counts
    progress = Progress(counts.get("done", 0), sum(counts.values()))
    waits = (f"{b.brief} waits on {', '.join(b.waiting_on)}" for b in m.blockers)
    note = "; ".join([sentence(slip_text(m)), *waits])
    return StatusRow(
        m.title, counts_text(m), Target(m.due, m.missed), progress, milestone_status(m), note
    )


def brief_note(b: RankedBrief) -> str:
    if b.waiting_on:
        return f"Waits on {', '.join(b.waiting_on)}"
    return f"Effort {b.effort}" if b.effort else "Not estimated"


def brief_row(b: RankedBrief) -> StatusRow:
    standing = next((chip for applies, chip in BRIEF_STANDING if applies(b)), READY)
    target = Target(date_text(b.due), b.overdue)
    return StatusRow(b.title, b.id, target, None, standing, brief_note(b))


def initiative_row(g: InitiativeGroup) -> StatusRow:
    """An initiative stands at its worst project and aims at its soonest open milestone."""
    milestones = [m for e in g.entries for m in e.project.milestones]
    soonest = rm.next_due(milestones)
    standing = health_chips(min(g.entries, key=attention).project.health)[0]
    projects = tuple(project_row(e, sentence(next_line(e.project))) for e in g.entries)
    described = plural(len(g.entries), "project")
    note = sentence(due_line(soonest))
    target, progress = target_of(soonest), Progress.of(milestones)
    return StatusRow(g.title, described, target, progress, standing, note, projects)


# --- JSON tables of the model's own objects --------------------------------------------------


def evidence_table(item: rm.BriefEvidence) -> dict[str, Any]:
    e = item.evidence
    shown = isinstance(show(e), (Image, Video))
    return {
        "brief": item.brief,
        "kind": e.kind,
        "link": e.link,
        "caption": e.caption,
        "access": e.access,
        "recorded_on": date_text(e.recorded_on),
        "inline": shown,
    }


def flags_table(env: rm.EnvironmentFlags) -> dict[str, Any]:
    flags = [
        {"brief": f.brief, "name": f.name, "state": f.state, "changed_on": date_text(f.changed_on)}
        for f in env.flags
    ]
    return {"environment": env.environment, "flags": flags}


def milestone_table(m: rm.MilestoneSummary) -> dict[str, Any]:
    """The keys earlier readers use come first; the rest is additive."""
    return {
        "id": m.id,
        "title": m.title,
        "percent_done": m.percent_done,
        "due": m.due,
        "missed": m.missed,
        "waiting": m.waiting_count,
        "stage": m.stage.key,
        "release": m.milestone.release,
        "baseline": m.baseline,
        "slip_days": m.slip_days,
        "late_days": m.late_days,
        "effort_days": m.effort_days,
        "unestimated": m.unestimated,
        "counts": dict(m.milestone.counts),
        "blockers": [{"brief": b.brief, "waiting_on": list(b.waiting_on)} for b in m.blockers],
        "flags": [flags_table(f) for f in m.flags],
        "evidence": [evidence_table(e) for e in m.evidence],
        "as_at": m.as_at.table(),
    }


def build_table(tile: rm.Tile) -> dict[str, Any] | None:
    b = tile.build
    return {"state": b.state, "link": b.link, "set_on": date_text(b.set_on)} if b else None


def tile_table(tile: rm.Tile) -> dict[str, Any]:
    return {
        "as_at": tile.as_at.table(),
        "health": dataclasses.asdict(tile.health),
        "build": build_table(tile),
        "links": [{"label": label, "url": url} for label, url in tile.links],
    }


def board_table(board: rm.BoardSentence) -> dict[str, Any]:
    return {"as_at": board.as_at.table(), "text": board.text}


def brief_table(b: rm.BriefSummary) -> dict[str, Any]:
    return {
        "id": b.id,
        "title": b.title,
        "state": b.state,
        "effort": b.effort,
        "due": date_text(b.due),
        "waiting_on": list(b.blockers),
        "as_at": b.as_at.table(),
    }


def ranked_table(b: RankedBrief, as_at: rm.AsAt) -> dict[str, Any]:
    return {
        "rank": b.rank,
        "id": b.id,
        "title": b.title,
        "kind": b.kind,
        "effort": b.effort,
        "due": date_text(b.due),
        "days_to_due": b.days_to_due,
        "overdue": b.overdue,
        "waiting_on": list(b.waiting_on),
        "as_at": as_at.table(),
    }


def release_table(r: rm.ShippedRelease) -> dict[str, Any]:
    shipped = [
        {"brief": s.brief, "title": s.title, "note": s.note, "shipped_on": date_text(s.shipped_on)}
        for s in r.lines
    ]
    return {
        "release": r.release,
        "date": date_text(r.date),
        "lines": shipped,
        "as_at": r.as_at.table(),
    }


# --- views -----------------------------------------------------------------------------------


class View(ABC):
    name: ClassVar[str]
    title: ClassVar[str]

    @abstractmethod
    def project_table(self, e: Entry) -> dict[str, Any]: ...

    @abstractmethod
    def sections(self, report: Report) -> tuple[Block, ...]: ...

    def extras(self, report: Report) -> dict[str, Any]:
        return {}

    @staticmethod
    def head(p: rm.Project) -> dict[str, Any]:
        """What identifies a project and the commit its facts were read at."""
        return {
            "source": p.source,
            "slug": p.slug,
            "title": p.title,
            "outcome": p.outcome,
            "initiative": p.initiative,
            "as_at": p.as_at.table(),
        }

    def data(self, r: Report) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "level": self.name,
            "generated": r.stamp,
            "sources": list(r.sources),
            "source_projects": [
                {"source": label, "projects": n} for label, n in r.project_counts().items()
            ],
            "projects": [self.project_table(e) for e in r.entries],
            **self.extras(r),
            "skipped": [dataclasses.asdict(o) for o in r.omitted],
            # `behind` stays, always empty, so readers of the earlier schema still find the key.
            "warnings": [{"source": n.source, "behind": {}, "message": n.message} for n in r.notes],
        }

    def footnotes(self, r: Report) -> tuple[Block, ...]:
        shown = [
            ("Not shown", [o.line() for o in r.omitted]),
            ("Source warnings", [n.message for n in r.notes]),
        ]
        return tuple(
            Section(heading, (Bullets(tuple(Item(row(t)) for t in text)),))
            for heading, text in shown
            if text
        )

    def document(self, r: Report) -> Document:
        body = self.sections(r) if r.entries else (paragraph(NO_PROJECTS),)
        nav = tuple(Sibling(v, v is self) for v in VIEWS.values())
        return Document(self.title, r.stamp, r.sources, (*body, *self.footnotes(r)), nav)


class WorkView(View):
    name, title = "work", "Work roll-up"

    def project_table(self, e: Entry) -> dict[str, Any]:
        b = e.board
        return {
            **self.head(e.project),
            "ready": [ranked_table(x, b.as_at) for x in b.work],
            "bugs": [ranked_table(x, b.as_at) for x in b.bugs],
            "uncovered_requirements": [
                {"id": g.id, "words": g.words, "as_at": b.as_at.table()} for g in b.gaps
            ],
        }

    def sections(self, report: Report) -> tuple[Block, ...]:
        rows = tuple(self.row(e) for e in report.entries)
        ready = Section("Ready, most urgent first", (StatusGrid("Project", "ready brief", rows),))
        return (ready, *(self.project(e) for e in report.entries))

    @staticmethod
    def row(e: Entry) -> StatusRow:
        b = e.board
        counted = f"{plural(len(b.work), 'ready brief')}, {plural(len(b.bugs), 'open bug')}"
        return project_row(e, counted if b.work else NO_READY, tuple(brief_row(x) for x in b.work))

    @staticmethod
    def project(e: Entry) -> Section:
        b = e.board
        uncovered = (row(f"{g.id}: {g.words}") for g in b.gaps)
        return Section(
            e.title,
            (
                source_line(e.project),
                Section("Bugs", (numbered((ranked_item(x) for x in b.bugs), NO_BUGS),)),
                Section("Requirements with no covering brief", (lines(uncovered, NO_UNCOVERED),)),
            ),
        )


TIERS = {pv.RED: 0, pv.AMBER: 1}


def attention(e: Entry) -> tuple[int, int]:
    """Red, then amber, then green that is stale or unset, then the rest; a bigger slip first."""
    h = e.project.health
    unsure = 2 if h.stale or not h.owner else 3
    open_ = (m for m in e.project.milestones if m.stage is not rm.Stage.DONE)
    return TIERS.get(h.effective, unsure), -max((m.slip_days or 0 for m in open_), default=0)


def slip_phrase(days: int) -> str:
    return f"slipped {plural(days, 'day')}" if days > 0 else f"pulled in {plural(-days, 'day')}"


def stage_line(p: rm.Project) -> str:
    if not p.milestones:
        return NO_MILESTONES
    done, now, nxt = (len(p.stage(s)) for s in (rm.Stage.DONE, rm.Stage.NOW, rm.Stage.NEXT))
    missed = [f"{n} missed"] if (n := sum(m.missed for m in p.milestones)) else []
    counts = [
        f"{done} of {plural(len(p.milestones), 'milestone')} done",
        f"{now} now",
        f"{nxt} next",
    ]
    return ", ".join([*counts, *missed]) + "."


def next_line(p: rm.Project) -> str:
    return due_line(p.next_due())


def due_line(soonest: rm.MilestoneSummary | None) -> str:
    if soonest is None:
        return NO_NEXT
    slip = (
        f" ({slip_phrase(soonest.slip_days)} from {soonest.baseline})" if soonest.slip_days else ""
    )
    return f"{soonest.title} due {soonest.due}{slip}"


def latest_shipped(p: rm.Project) -> str:
    """`shipped` is newest first, so its head is the latest line."""
    return p.shipped[0].shipped_line() if p.shipped else NO_SHIPPED


def tile(e: Entry) -> Section:
    """Layer 1: everything a reader needs about a project without opening anything."""
    p = e.project
    standing = Row((*health_chips(p.health), *build_chips(p.tile), *owner_line(p.health).parts))
    return TileSection(
        p.title,
        (
            Paragraph(standing),
            paragraph(Strong("Outcome:"), p.outcome),
            paragraph(Strong("Milestones:"), stage_line(p)),
            paragraph(Strong("Next:"), next_line(p)),
            paragraph(Strong("Ask:"), p.ask.text() if p.ask else NO_ASK),
            paragraph(Strong("Latest shipped:"), latest_shipped(p)),
            source_line(p),
        ),
    )


def counts_text(m: rm.MilestoneSummary) -> str:
    counts = ", ".join(f"{n} {state}" for state, n in sorted(m.milestone.counts.items()))
    return counts or NO_BRIEFS


def slip_text(m: rm.MilestoneSummary) -> str:
    if m.slip_days is None:
        return NO_BASELINE
    return slip_phrase(m.slip_days) if m.slip_days else NO_SLIP


def status_parts(m: rm.MilestoneSummary) -> tuple[Inline | str, ...]:
    waiting = [Chip(f"{m.waiting_count} waiting", Tone.WARN)] if m.waiting_count else []
    missed = [Chip("missed", Tone.BAD)] if m.missed else []
    return (stage_chip(m.stage), *missed, *waiting, f"{m.percent_done}% done")


def stage_sections(p: rm.Project) -> tuple[Section, ...]:
    return tuple(
        Section(s.value, (Bullets(tuple(milestone_item(m) for m in found)),))
        for s in rm.Stage
        if (found := p.stage(s))
    )


def shipped_sections(p: rm.Project) -> tuple[Section, ...]:
    """Releases first, then what shipped with no release named; absent when nothing did."""
    released = {line.brief for r in p.shipped_by_release for line in r.lines}
    unnamed = tuple(b for b in p.shipped if b.id not in released)
    groups = [released_section(r) for r in p.shipped_by_release]
    if unnamed:
        shipped = lines((row(b.shipped_line()) for b in unnamed), NO_SHIPPED)
        groups.append(Section(NO_RELEASE, (shipped,)))
    return (Section("Shipped", tuple(groups)),) if groups else ()


def backlog_section(e: Entry) -> Section:
    groups = tuple(
        Section(g.title, (Bullets(tuple(Item(row(*brief_parts(b))) for b in g.briefs)),))
        for g in e.board.backlog
    )
    return Section("Backlog", groups or (paragraph(NO_BACKLOG),))


def detail_blocks(e: Entry) -> tuple[Block, ...]:
    """Layer 3; a subsection whose gap the tile or table already states is left out."""
    p = e.project
    return (*health_detail(p.tile), *stage_sections(p), *shipped_sections(p), backlog_section(e))


def per_project(heading: str, entries: Iterable[Entry], make: Callable[[Entry], Block]) -> Section:
    return Section(heading, tuple(make(e) for e in entries))


def detail_of(e: Entry) -> Section:
    return Section(e.title, (Details(DETAIL_SUMMARY, detail_blocks(e)),))


class ProjectView(View):
    name, title = "project", "Project roll-up"

    def project_table(self, e: Entry) -> dict[str, Any]:
        p, b = e.project, e.board
        return {
            **self.head(p),
            "health": dataclasses.asdict(p.health),
            "ask": dataclasses.asdict(p.ask) if p.ask else None,
            "board": board_table(p.board),
            "tile": tile_table(p.tile),
            **{s.key: [milestone_table(m) for m in p.stage(s)] for s in rm.Stage},
            "shipped": [
                {"id": s.id, "title": s.title, "updated": s.updated_iso} for s in p.shipped
            ],
            "shipped_by_release": [release_table(r) for r in p.shipped_by_release],
            "backlog": [
                {
                    "milestone": g.milestone,
                    "title": g.title,
                    "briefs": [brief_table(x) for x in g.briefs],
                }
                for g in b.backlog
            ],
        }

    def sections(self, report: Report) -> tuple[Block, ...]:
        ordered = sorted(report.entries, key=attention)
        rows = tuple(self.row(e) for e in ordered)
        return (
            Section("Status", (StatusGrid("Project", "milestone", rows),)),
            per_project("Projects", ordered, tile),
            per_project("Detail", ordered, detail_of),
        )

    @staticmethod
    def row(e: Entry) -> StatusRow:
        milestones = tuple(milestone_row(m) for m in e.project.milestones)
        return project_row(e, sentence(next_line(e.project)), milestones)


def released_section(r: rm.ShippedRelease) -> Section:
    heading = f"Release {r.release}" + (f" ({r.date})" if r.date else "")
    shipped = (
        row(s.title + (f": {s.note}" if s.note else "") + f" ({date_text(s.shipped_on)})")
        for s in r.lines
    )
    return Section(heading, (lines(shipped, NO_SHIPPED),))


def schedule_text(m: rm.MilestoneSummary) -> str:
    """Due, the first promise and what the gap between them is, in the words a reader would use."""
    if not m.due:
        return "No date recorded."
    parts = [f"Due {m.due}"]
    if m.baseline:
        parts.append(f"first promised {m.baseline}")
    if m.slip_days:
        parts.append(slip_phrase(m.slip_days))
    if m.late_days:
        parts.append(f"{plural(m.late_days, 'day')} past the first date")
    return ", ".join(parts) + "."


def days_text(days: float) -> str:
    return f"{days:g} {'day' if days == 1 else 'days'}"


def effort_text(m: rm.MilestoneSummary) -> str:
    sized = f"Open effort about {days_text(m.effort_days)}" if m.effort_days else ""
    unsized = f"{plural(m.unestimated, 'brief')} not estimated" if m.unestimated else ""
    return ", ".join(filter(None, [sized, unsized])) + "."


def milestone_details(m: rm.MilestoneSummary) -> tuple[Row, ...]:
    release = [row(f"Release {m.milestone.release}.")] if m.milestone.release else []
    effort = [row(effort_text(m))] if m.effort_days or m.unestimated else []
    return (
        row(schedule_text(m)),
        *release,
        *effort,
        *(row(f"{b.brief} waits on {', '.join(b.waiting_on)}.") for b in m.blockers),
        *(flag_row(env) for env in m.flags),
        *(row(f"{item.brief}:", show(item.evidence)) for item in m.evidence),
    )


def flag_row(env: rm.EnvironmentFlags) -> Row:
    states = "; ".join(f"{f.name} {f.state} since {f.changed_on}" for f in env.flags)
    return row(Strong(f"Flags in {env.environment}:"), states)


def milestone_item(m: rm.MilestoneSummary) -> Item:
    return Item(row(m.title, *status_parts(m)), milestone_details(m))


class PortfolioView(View):
    name, title = "portfolio", "Portfolio roll-up"

    def project_table(self, e: Entry) -> dict[str, Any]:
        p = e.project
        soonest = p.next_due()
        return {
            **self.head(p),
            "health": {
                "effective": p.health.effective,
                "owner": p.health.owner,
                "reason": p.health.reason,
            },
            "board": board_table(p.board),
            "tile": tile_table(p.tile),
            "milestones": {"done": len(p.stage(rm.Stage.DONE)), "total": len(p.milestones)},
            "next_due": (
                {"id": soonest.id, "title": soonest.title, "due": soonest.due} if soonest else None
            ),
            "missed": [
                {"id": m.id, "title": m.title, "due": m.due} for m in p.milestones if m.missed
            ],
            "milestone_lines": [m.line() for m in p.milestones],
        }

    def extras(self, report: Report) -> dict[str, Any]:
        members = [
            {
                "initiative": g.name,
                "projects": [
                    {"source": e.project.source, "slug": e.project.slug} for e in g.entries
                ],
            }
            for g in initiatives(report.entries)
        ]
        return {"initiatives": members}

    def sections(self, report: Report) -> tuple[Block, ...]:
        groups = initiatives(report.entries)
        grid = StatusGrid("Initiative", "project", tuple(initiative_row(g) for g in groups))
        detail = (Section(g.title, tuple(self.project(e) for e in g.entries)) for g in groups)
        return (Section("Initiatives", (grid,)), *detail)

    @staticmethod
    def project(e: Entry) -> Section:
        p = e.project
        one_liners = (Item(row(m.line(), stage_chip(m.stage))) for m in p.milestones)
        return Section(
            p.title,
            (
                paragraph(p.board.text),
                tile_line(p.tile),
                source_line(p),
                bullets(one_liners, NO_MILESTONES),
            ),
        )


VIEWS: dict[str, View] = {v.name: v for v in (WorkView(), ProjectView(), PortfolioView())}


# --- the front page: what the files are and where each view is ---------------------------------

GUIDE = (
    (
        "What these files are",
        (
            "`work.md`, `project.md` and `portfolio.md` are read-only summaries of project status, "
            "written by `rollup.py` from project queues. `work.md` ranks the ready work, the bugs and "
            "the requirements no brief covers; `project.md` shows each project as a tile with its "
            "milestones and the detail behind them; `portfolio.md` groups projects by initiative. "
            "`work.json`, `project.json` and `portfolio.json` hold the same data for tools.",
        ),
    ),
    (
        "Read them on GitHub",
        (
            "Open `work.md`, `project.md` or `portfolio.md` in this folder; GitHub shows them as "
            "formatted pages.",
        ),
    ),
    (
        "Ask Claude",
        (
            'Install the claude-tasks plugin, then ask "how are my projects going?". In Claude '
            "Desktop or claude.ai chat, attach `project.md` with `+ > Add from GitHub` first, then ask "
            "the same question.",
        ),
    ),
    (
        "Make your own roll-up",
        (
            "Point the generator at your own queues, as GitHub holds them; repeat `--source` for "
            "each repository (a URL, or a path to a clone, with `#folder` when the queue is not "
            "`.tasks`):",
            "`rollup.py write --out <dir> --source Label=repo`",
        ),
    ),
    (
        "The JSON is an API",
        (
            f"The `.json` files use the schema `{SCHEMA}`. Keys keep their names and meaning within "
            "a schema version, so tools can rely on them; new keys may be added.",
        ),
    ),
)


def stretch(text: str) -> tuple[Inline, ...]:
    """Plain text with its edge spaces kept, because `Text` flattens whitespace away."""
    lead = (Space(),) if text[:1].isspace() else ()
    tail = (Space(),) if len(text) > 1 and text[-1:].isspace() else ()
    return (*lead, *((Text(text.strip()),) if text.strip() else ()), *tail)


def prose(text: str) -> Paragraph:
    """A paragraph in which text between backticks is code."""
    parts = ((Code(t),) if n % 2 else stretch(t) for n, t in enumerate(text.split("`")))
    return Paragraph(Row(tuple(chain.from_iterable(parts)), ""))


def guide() -> tuple[Section, ...]:
    return tuple(Section(heading, tuple(prose(t) for t in text)) for heading, text in GUIDE)


def listing(pages: "Markup") -> Section:
    """Each view as a link in the format `pages`, with its data file beside it."""
    items = (
        Item(Row((Page(v.title, pages.filename(v)), Page("JSON", JSON.filename(v)))))
        for v in VIEWS.values()
    )
    return Section("Roll-ups", (Bullets(tuple(items)),))


class Index:
    """The page that lists the views and links each one: the front page of a served roll-up."""

    def blocks(self, pages: Markup) -> tuple[Block, ...]:
        return (listing(pages),)

    def render(self, pages: Markup, report: Report) -> str:
        document = Document("Project roll-ups", report.stamp, report.sources, self.blocks(pages))
        return document.render(pages)


class Readme(Index):
    """The index with the guide under it, for the folder of written files."""

    def blocks(self, pages: Markup) -> tuple[Block, ...]:
        return (*super().blocks(pages), *guide())

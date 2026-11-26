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
NO_FLAGS = "None"
NO_ROLLOUT = "None"
NO_DATE = "No date"
NO_BASELINE = "No first date"
NO_SLIP = "No slip"
DETAIL_SUMMARY = "Show detail"
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
    ranked = prioritise.rank(read.queue, today=today, config=read.settings)
    items = [
        RankedBrief.of(place, raw, today, waiting.get(raw["id"], ()))
        for place, raw in enumerate(ranked, 1)
    ]
    return tuple(sorted(items, key=RankedBrief.urgency))


def _gaps(root: Any, briefs: list[dict[str, Any]]) -> tuple[Gap, ...]:
    register = register_path(root)
    if not register.is_file():
        return ()
    cited = {r for b in briefs for r in b["requirements"]}
    return tuple(
        Gap(r.id, r.quote or r.title)
        for r in parse_register(register.read_text())
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
            _gaps(read.queue, read.briefs),
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
    NEUTRAL = "neutral"


HEALTH_TONE = {pv.GREEN: Tone.GOOD, pv.AMBER: Tone.WARN, pv.RED: Tone.BAD}
BUILD_TONE = {"green": Tone.GOOD, "red": Tone.BAD}


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
class Day(Inline):
    """A calendar date, kept on one line."""

    iso: str

    def render(self, m: "Markup") -> str:
        return m.day(self.iso)


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
    ordered: bool = False

    def render(self, m: "Markup", depth: int) -> str:
        shown = [(i.head.render(m), [d.render(m) for d in i.details]) for i in self.items]
        return m.bullets(shown, self.ordered)


@dataclasses.dataclass(frozen=True)
class Table(Block):
    """Cells are rows of inline parts, so a cell can carry a chip but never a block."""

    headings: tuple[str, ...]
    rows: tuple[tuple[Row, ...], ...]

    def render(self, m: "Markup", depth: int) -> str:
        return m.table(list(self.headings), [[c.render(m) for c in r] for r in self.rows])


@dataclasses.dataclass(frozen=True)
class Section(Block):
    """A headed part of a page; `kind` only styles it, as a tile for one project's summary."""

    heading: str
    blocks: tuple[Block, ...] = ()
    kind: str = ""

    def render(self, m: "Markup", depth: int) -> str:
        body = [b.render(m, depth + 1) for b in self.blocks]
        return m.section(self.heading, depth, self.kind, body)


@dataclasses.dataclass(frozen=True)
class Details(Block):
    """Blocks a reader opens on demand; `summary` is fixed wording, never queue text."""

    summary: str
    blocks: tuple[Block, ...]

    def render(self, m: "Markup", depth: int) -> str:
        return m.details(self.summary, [b.render(m, depth) for b in self.blocks])


@dataclasses.dataclass(frozen=True)
class Document:
    title: str
    generated: str
    sources: tuple[str, ...]
    blocks: tuple[Block, ...]

    def render(self, m: "Markup") -> str:
        body = [b.render(m, 2) for b in self.blocks]
        return m.document(self.title, self.generated, self.sources, body)


def paragraph(*parts: Inline | str) -> Paragraph:
    return Paragraph(row(*parts))


def bullets(items: Iterable[Item], gap: str, ordered: bool = False) -> Block:
    """The items as a list, or `gap` saying what is missing, so a section is never blank."""
    listed = tuple(items)
    return Bullets(listed, ordered) if listed else paragraph(gap)


def lines(rows: Iterable[Row], gap: str) -> Block:
    return bullets((Item(r) for r in rows), gap)


# --- formats ---------------------------------------------------------------------------------


class Format(ABC):
    name: ClassVar[str]
    content_type: ClassVar[str]
    suffix: ClassVar[str]

    @abstractmethod
    def render(self, view: "View", report: Report) -> str: ...


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

    def day(self, iso: str) -> str:
        return self.text(iso)

    @abstractmethod
    def link(self, text: str, url: str) -> str: ...

    @abstractmethod
    def image(self, caption: str, url: str) -> str: ...

    @abstractmethod
    def video(self, caption: str, url: str) -> str: ...

    @abstractmethod
    def paragraph(self, line: str) -> str: ...

    @abstractmethod
    def bullets(self, items: list[tuple[str, list[str]]], ordered: bool) -> str: ...

    @abstractmethod
    def table(self, headings: list[str], rows: list[list[str]]) -> str: ...

    @abstractmethod
    def section(self, heading: str, depth: int, kind: str, body: list[str]) -> str: ...

    @abstractmethod
    def details(self, summary: str, body: list[str]) -> str: ...

    @abstractmethod
    def document(
        self, title: str, generated: str, sources: tuple[str, ...], body: list[str]
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

    def bullets(self, items: list[tuple[str, list[str]]], ordered: bool) -> str:
        return "\n".join(
            self._item(n, head, rest, ordered) for n, (head, rest) in enumerate(items, 1)
        )

    @staticmethod
    def _item(number: int, head: str, rest: list[str], ordered: bool) -> str:
        marker = f"{number}. " if ordered else "- "
        indent = " " * len(marker)
        return f"{marker}{head}" + "".join(f"\n{indent}- {d}" for d in rest)

    def table(self, headings: list[str], rows: list[list[str]]) -> str:
        shown = [f"| {' | '.join(escape_md(h) for h in headings)} |", "|" + "---|" * len(headings)]
        return "\n".join([*shown, *(f"| {' | '.join(cells)} |" for cells in rows)])

    def section(self, heading: str, depth: int, kind: str, body: list[str]) -> str:
        return "\n\n".join([f"{'#' * min(depth, 6)} {escape_md(heading)}", *body])

    def details(self, summary: str, body: list[str]) -> str:
        opening = f"<details>\n<summary>{html.escape(summary)}</summary>"
        return "\n\n".join([opening, *body, "</details>"])

    def document(
        self, title: str, generated: str, sources: tuple[str, ...], body: list[str]
    ) -> str:
        origin = ", ".join(escape_md(s) for s in sources)
        head = [f"# {escape_md(title)}", f"*Generated {generated} from {origin}.*"]
        return "\n\n".join([*head, *body]) + "\n"


STYLE = """:root{--bg:#fff;--fg:#1d2433;--muted:#556070;--line:#d9dee7;--card:#f6f8fb;
--good:#17653a;--good-bg:#e2f3e8;--warn:#85500a;--warn-bg:#fdf0d5;--bad:#a1241d;--bad-bg:#fce5e3;
--neutral:#394354;--neutral-bg:#e9edf3;--link:#1457a8}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e6e9ef;--muted:#a0a9b8;--line:#2c323d;
--card:#1b1f27;--good:#86d9a1;--good-bg:#12301f;--warn:#f1c473;--warn-bg:#3a2a0d;--bad:#ff9f98;
--bad-bg:#3e1613;--neutral:#c5ccd8;--neutral-bg:#2a303b;--link:#8ab8f5}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;margin:0}
main{max-width:62rem;margin:0 auto;padding:1rem}
h1{font-size:1.6rem;margin:.2rem 0}h2{font-size:1.3rem;margin:2rem 0 .3rem}
h3{font-size:1.1rem;margin:1.2rem 0 .2rem}h4,h5,h6{font-size:1rem;margin:1rem 0 .2rem}
p{margin:.4rem 0}a{color:var(--link)}code{font-size:.9em}
section{overflow-wrap:anywhere}
section.depth-2{border-top:1px solid var(--line);padding-top:.2rem}
.stamp{color:var(--muted);font-size:.85rem;margin:0 0 .4rem}
.chip{display:inline-block;border-radius:.6rem;padding:0 .55rem;font-size:.85rem;font-weight:600;
white-space:nowrap}
.chip-good{color:var(--good);background:var(--good-bg)}.chip-warn{color:var(--warn);background:var(--warn-bg)}
.chip-bad{color:var(--bad);background:var(--bad-bg)}.chip-neutral{color:var(--neutral);background:var(--neutral-bg)}
ul.items,ul.details{margin:.2rem 0;padding-left:1.2rem}ul.items>li{margin:.35rem 0}
ul.details{color:var(--muted)}.head{color:var(--fg)}
ol.items{margin:.2rem 0;padding-left:1.6rem}ol.items>li{margin:.35rem 0}
section.tile{background:var(--card);border:1px solid var(--line);border-radius:.5rem;
padding:.1rem .9rem .6rem;margin:.8rem 0}
table{border-collapse:collapse;display:block;overflow-x:auto;margin:.4rem 0}
th,td{border:1px solid var(--line);padding:.3rem .6rem;text-align:left;vertical-align:top}
th,time{white-space:nowrap}
details{margin:.4rem 0}summary{cursor:pointer;color:var(--muted)}
figure.evidence{margin:.4rem 0;max-width:100%}figure.evidence img,figure.evidence video{max-width:100%;
height:auto;border:1px solid var(--line);border-radius:.3rem;background:var(--card)}
figcaption{font-size:.85rem;color:var(--muted)}"""


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

    def bullets(self, items: list[tuple[str, list[str]]], ordered: bool) -> str:
        tag = "ol" if ordered else "ul"
        return f'<{tag} class="items">' + "".join(self._item(*i) for i in items) + f"</{tag}>"

    @staticmethod
    def _item(head: str, details: list[str]) -> str:
        rest = "".join(f"<li>{d}</li>" for d in details)
        return f'<li><div class="head">{head}</div>' + (
            f'<ul class="details">{rest}</ul></li>' if rest else "</li>"
        )

    def table(self, headings: list[str], rows: list[list[str]]) -> str:
        head = "".join(f"<th>{html.escape(h)}</th>" for h in headings)
        body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>" for cells in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    def section(self, heading: str, depth: int, kind: str, body: list[str]) -> str:
        level = min(depth, 6)
        title = f"<h{level}>{html.escape(heading)}</h{level}>"
        classes = " ".join(filter(None, [f"depth-{depth}", kind]))
        return f'<section class="{classes}">{title}{"".join(body)}</section>'

    def details(self, summary: str, body: list[str]) -> str:
        return f"<details><summary>{html.escape(summary)}</summary>{''.join(body)}</details>"

    def document(
        self, title: str, generated: str, sources: tuple[str, ...], body: list[str]
    ) -> str:
        origin = html.escape(", ".join(sources))
        stamp = f'<p class="stamp">Generated <time datetime="{html.escape(generated)}">{html.escape(generated)}</time> from {origin}.</p>'
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{html.escape(HTML_CSP)}">'
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>"
            f"<header><h1>{html.escape(title)}</h1>{stamp}</header>{''.join(body)}"
            "</main></body></html>\n"
        )


FORMATS: dict[str, Format] = {f.name: f for f in (JsonFormat(), MarkdownFormat(), HtmlFormat())}


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


def rollout_table(r: facts.RolloutSnapshot) -> dict[str, Any]:
    return {
        "key": r.key,
        "on": r.on_count,
        "released": r.released_count,
        "total": r.total,
        "spread": r.spread.value,
        "missing": list(r.missing),
        "recorded_on": date_text(r.recorded_on),
    }


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
        **({"rollouts": [rollout_table(r) for r in m.rollouts]} if m.rollouts else {}),
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


def next_due(p: rm.Project) -> rm.MilestoneSummary | None:
    dated = sorted(
        (m for m in p.milestones if m.due and m.stage is not rm.Stage.DONE), key=lambda m: m.due
    )
    return dated[0] if dated else None


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
        return Document(self.title, r.stamp, r.sources, (*body, *self.footnotes(r)))


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
        return tuple(self.project(e) for e in report.entries)

    @staticmethod
    def project(e: Entry) -> Section:
        b = e.board
        uncovered = (row(f"{g.id}: {g.words}") for g in b.gaps)
        return Section(
            e.title,
            (
                source_line(e.project),
                Section(
                    "Ready, most urgent first",
                    (bullets((ranked_item(x) for x in b.work), NO_READY, ordered=True),),
                ),
                Section(
                    "Bugs", (bullets((ranked_item(x) for x in b.bugs), NO_BUGS, ordered=True),)
                ),
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
    soonest = next_due(p)
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
    return Section(
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
        "tile",
    )


def counts_text(m: rm.MilestoneSummary) -> str:
    counts = ", ".join(f"{n} {state}" for state, n in sorted(m.milestone.counts.items()))
    return counts or NO_BRIEFS


def flags_text(m: rm.MilestoneSummary) -> str:
    per_environment = (
        f"{env.environment}: " + ", ".join(f"{f.name} {f.state}" for f in env.flags)
        for env in m.flags
    )
    return "; ".join(per_environment) or NO_FLAGS


def slip_text(m: rm.MilestoneSummary) -> str:
    if m.slip_days is None:
        return NO_BASELINE
    return slip_phrase(m.slip_days) if m.slip_days else NO_SLIP


def status_parts(m: rm.MilestoneSummary) -> tuple[Inline | str, ...]:
    waiting = [Chip(f"{m.waiting_count} waiting", Tone.WARN)] if m.waiting_count else []
    missed = [Chip("missed", Tone.BAD)] if m.missed else []
    return (Chip(m.stage.value), *missed, *waiting, f"{m.percent_done}% done")


MILESTONE_HEADINGS = ("Milestone", "Status", "Due", "Slip", "Briefs", "Evidence", "Flags")
ROLLOUT_HEADING = "Rollout"


def rollout_text(m: rm.MilestoneSummary) -> str:
    return "; ".join(r.line() for r in m.rollouts) or NO_ROLLOUT


def has_rollouts(p: rm.Project) -> bool:
    return any(m.rollouts for m in p.milestones)


def milestone_cells(m: rm.MilestoneSummary, with_rollout: bool = False) -> tuple[Row, ...]:
    """The Rollout cell is there only for a project that records a rollout, so other tables stay as they were."""
    cells = (
        row(m.title),
        row(*status_parts(m)),
        row(Day(m.due) if m.due else NO_DATE),
        row(slip_text(m)),
        row(counts_text(m)),
        row(str(len(m.evidence))),
        row(flags_text(m)),
    )
    return (*cells, row(rollout_text(m))) if with_rollout else cells


def milestone_grid(p: rm.Project) -> Block:
    """Layer 2: one row per milestone, so projects can be read down a column."""
    if not p.milestones:
        return paragraph(NO_MILESTONES)
    wide = has_rollouts(p)
    headings = (*MILESTONE_HEADINGS, ROLLOUT_HEADING) if wide else MILESTONE_HEADINGS
    return Table(headings, tuple(milestone_cells(m, wide) for m in p.milestones))


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


def milestones_of(e: Entry) -> Section:
    return Section(e.title, (milestone_grid(e.project),))


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
        return (
            per_project("Projects", ordered, tile),
            per_project("Milestones", ordered, milestones_of),
            per_project("Detail", ordered, detail_of),
        )


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
        *(missing_row(r) for r in m.rollouts if r.missing),
        *(row(f"{item.brief}:", show(item.evidence)) for item in m.evidence),
    )


def missing_row(r: facts.RolloutSnapshot) -> Row:
    return row(Strong(f"{r.key} not on:"), ", ".join(r.missing))


def flag_row(env: rm.EnvironmentFlags) -> Row:
    states = "; ".join(f"{f.name} {f.state} since {f.changed_on}" for f in env.flags)
    return row(Strong(f"Flags in {env.environment}:"), states)


def milestone_item(m: rm.MilestoneSummary) -> Item:
    return Item(row(m.title, *status_parts(m)), milestone_details(m))


class PortfolioView(View):
    name, title = "portfolio", "Portfolio roll-up"

    def project_table(self, e: Entry) -> dict[str, Any]:
        p = e.project
        soonest = next_due(p)
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
        return tuple(
            Section(g.title, tuple(self.project(e) for e in g.entries))
            for g in initiatives(report.entries)
        )

    @staticmethod
    def project(e: Entry) -> Section:
        p = e.project
        one_liners = (Item(row(m.line(), Chip(m.stage.value))) for m in p.milestones)
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


def render(view: str, fmt: str, report: Report) -> str:
    return FORMATS[fmt].render(VIEWS[view], report)


def view_names() -> Sequence[str]:
    return tuple(VIEWS)


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
    data = FORMATS["json"].suffix
    items = (
        Item(Row((Page(v.title, v.name + pages.suffix), Page("JSON", v.name + data))))
        for v in VIEWS.values()
    )
    return Section("Roll-ups", (Bullets(tuple(items)),))


MARKUPS: dict[str, Markup] = {f.name: f for f in FORMATS.values() if isinstance(f, Markup)}


def render_contents(fmt: str, report: Report, with_guide: bool = False) -> str:
    """The page that lists the views: the README beside written files, the index when served."""
    pages = MARKUPS[fmt]
    blocks = (listing(pages), *(guide() if with_guide else ()))
    return Document("Project roll-ups", report.stamp, report.sources, blocks).render(pages)

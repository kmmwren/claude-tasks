#!/usr/bin/env python3
"""
route_inbox.py: move generic briefs into the project queue they belong to, on evidence only.

Evidence, strongest first: an explicit `project:` field or tag; a Jira key or `requires-repo:`
one project owns; a project keyword in the id, title or tags. No match, several projects, or an
unlisted project name leaves the brief in place and reported. Moves are logged in
`route-inbox.jsonl`; `reverse <id>` undoes one; `--dry-run` writes nothing. Stdlib only.
"""

import argparse
import dataclasses
import datetime
import enum
import json
import pathlib
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Sequence
from typing import Any

from _paths import resolve_root
from git_sync import sync_commit
from route_registry import JIRA_KEY, Project, load_projects, registry_path
from transition import holding_folder, relocate_brief
from triage_brief import InboxBrief

LOG_NAME = "route-inbox.jsonl"
SOURCE_FOLDERS = ("inbox", "ready")
DEST_FOLDER = "inbox"
ACTOR = "route_inbox"


class Tier(enum.IntEnum):
    KEYWORD = 1
    KEY = 2
    EXPLICIT = 3


class Outcome(enum.StrEnum):
    ROUTED = "routed"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"
    UNKNOWN_PROJECT = "unknown_project"
    NOT_ACCEPTING = "not_accepting"
    ALREADY_ROUTED = "already_routed"
    HELD_PR_MODE = "held_pr_mode"
    HELD_NO_QUEUE = "held_no_queue"
    HELD_CONFLICT = "held_conflict"


@dataclasses.dataclass(frozen=True)
class Evidence:
    slug: str
    tier: Tier
    term: str


@dataclasses.dataclass(frozen=True)
class Match:
    outcome: Outcome
    slug: str = ""
    terms: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Decision:
    brief_id: str
    folder: str
    outcome: Outcome
    slug: str = ""
    terms: tuple[str, ...] = ()

    def row(self) -> dict[str, Any]:
        return {
            "id": self.brief_id,
            "folder": self.folder,
            "outcome": self.outcome.value,
            "project": self.slug,
            "terms": list(self.terms),
        }


def _headline(brief: InboxBrief) -> str:
    return f"{brief.id} {brief.title} {' '.join(brief.tags)}"


def _word(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class Matcher:
    """Scores a brief against the registry; `projects` lists every project, takers or not."""

    projects: tuple[Project, ...]

    @property
    def takers(self) -> tuple[Project, ...]:
        return tuple(p for p in self.projects if p.takes_briefs)

    def by_slug(self, slug: str) -> Project:
        return next(p for p in self.projects if p.slug == slug)

    def declared(self, brief: InboxBrief) -> list[str]:
        """Project names the brief states itself: the `project:` field, then `project:<name>` tags."""
        tagged = [t.removeprefix("project:") for t in brief.tags if t.startswith("project:")]
        return [v for v in (brief.fields.get("project", "").strip(), *tagged) if v]

    def explicit(self, brief: InboxBrief) -> tuple[list[Evidence], list[str]]:
        """Evidence for each declared name the registry knows, and the names it does not."""
        found, unknown = [], []
        for value in self.declared(brief):
            owner = next((p for p in self.projects if p.answers_to(value)), None)
            if owner is None:
                unknown.append(value)
            else:
                found.append(Evidence(owner.slug, Tier.EXPLICIT, value))
        return found, unknown

    def owned(self, brief: InboxBrief) -> list[Evidence]:
        """Jira keys cited anywhere in the brief, and its `requires-repo:`, that one project owns."""
        cited = set(JIRA_KEY.findall(brief.text))
        repo = brief.fields.get("requires-repo", "").strip()
        keys = [Evidence(p.slug, Tier.KEY, k) for p in self.takers for k in sorted(cited & p.keys)]
        repos = [Evidence(p.slug, Tier.KEY, repo) for p in self.takers if p.owns_repo(repo)]
        return keys + repos

    def keyworded(self, brief: InboxBrief) -> list[Evidence]:
        head = _headline(brief)
        return [
            Evidence(p.slug, Tier.KEYWORD, t)
            for p in self.takers
            for t in p.keywords
            if _word(t).search(head)
        ]

    def match(self, brief: InboxBrief) -> Match:
        declared, unknown = self.explicit(brief)
        if unknown:
            return Match(Outcome.UNKNOWN_PROJECT)
        pool = declared or [*self.owned(brief), *self.keyworded(brief)]
        slugs = sorted({e.slug for e in pool})
        if not slugs:
            return Match(Outcome.UNMATCHED)
        if len(slugs) > 1:
            return Match(Outcome.AMBIGUOUS)
        terms = tuple(sorted({e.term for e in pool}))
        if not self.by_slug(slugs[0]).takes_briefs:
            return Match(Outcome.NOT_ACCEPTING, slugs[0], terms)
        return Match(Outcome.ROUTED, slugs[0], terms)


@dataclasses.dataclass(frozen=True)
class Route:
    """One row of the route log: where a brief went, or that it was put back."""

    id: str
    project: str
    queue: str
    folder: str
    date: str
    event: str


class RouteLog:
    """`route-inbox.jsonl` in the origin queue: append-only, so the last row per id is its state."""

    def __init__(self, root: pathlib.Path) -> None:
        self.path = root / LOG_NAME

    def rows(self) -> list[Route]:
        if not self.path.exists():
            return []
        return [Route(**json.loads(line)) for line in self.path.read_text().splitlines() if line]

    def latest(self) -> dict[str, Route]:
        return {r.id: r for r in self.rows()}

    def append(self, route: Route) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(dataclasses.asdict(route)) + "\n")


@dataclasses.dataclass(frozen=True)
class Router:
    root: pathlib.Path
    matcher: Matcher
    dest_root: pathlib.Path | None = None
    folders: tuple[str, ...] = ("inbox",)

    def landing(self, project: Project, brief_id: str) -> Outcome:
        """Whether a matched brief can be written into the project's queue right now."""
        if not project.queue_ready(self.dest_root):
            return Outcome.HELD_NO_QUEUE
        if project.queue_mode != "direct" and self.dest_root is None:
            return Outcome.HELD_PR_MODE
        if holding_folder(project.holds(self.dest_root), brief_id):
            return Outcome.HELD_CONFLICT
        return Outcome.ROUTED

    def decide(self, brief: InboxBrief, folder: str, seen: dict[str, Route]) -> Decision:
        if brief.id in seen:
            return Decision(brief.id, folder, Outcome.ALREADY_ROUTED, seen[brief.id].project)
        match = self.matcher.match(brief)
        outcome = match.outcome
        if outcome is Outcome.ROUTED:
            outcome = self.landing(self.matcher.by_slug(match.slug), brief.id)
        return Decision(brief.id, folder, outcome, match.slug, match.terms)

    def plan(self) -> list[Decision]:
        """Read-only: what each brief in the source folders would do."""
        seen = RouteLog(self.root).latest()
        paths = sorted(
            (f, p) for f in self.folders for p in (self.root / f).glob("*.md") if p.is_file()
        )
        return [
            self.decide(InboxBrief(p, p.read_text(errors="replace")), f, seen) for f, p in paths
        ]

    def move(self, decision: Decision, today: str) -> Decision:
        """Relocate one routed brief and log it; a brief claimed meanwhile is reported as a conflict."""
        project = self.matcher.by_slug(decision.slug)
        dest = project.holds(self.dest_root)
        note = f"routed from {self.root}/{decision.folder}/{decision.brief_id}.md into project {decision.slug} by {ACTOR}"
        try:
            relocate_brief(
                decision.brief_id, self.root, dest, decision.folder, DEST_FOLDER, note, today
            )
        except ValueError:
            return dataclasses.replace(decision, outcome=Outcome.HELD_CONFLICT)
        route = Route(decision.brief_id, decision.slug, str(dest), decision.folder, today, "moved")
        RouteLog(self.root).append(route)
        return decision

    def apply(self, today: str) -> list[Decision]:
        return [self.move(d, today) if d.outcome is Outcome.ROUTED else d for d in self.plan()]


def reverse(root: pathlib.Path, brief_id: str, today: str) -> Route:
    """Put a routed brief back in its origin queue and folder, and log that so it is not routed again."""
    log = RouteLog(root)
    last = log.latest().get(brief_id)
    if last is None or last.event != "moved":
        raise ValueError(f"{brief_id!r} has no route to reverse in {log.path}")
    note = f"returned from project {last.project} by {ACTOR} reverse"
    relocate_brief(brief_id, pathlib.Path(last.queue), root, DEST_FOLDER, last.folder, note, today)
    undone = dataclasses.replace(last, date=today, event="reversed")
    log.append(undone)
    return undone


_MATCHED = (
    Outcome.ROUTED,
    Outcome.HELD_PR_MODE,
    Outcome.HELD_NO_QUEUE,
    Outcome.HELD_CONFLICT,
)


@dataclasses.dataclass(frozen=True)
class Report:
    decisions: Sequence[Decision]
    dry_run: bool

    def counts(self) -> dict[str, Any]:
        """Routed per project, projects matched per project (held ones included), and each other outcome."""
        tally = Counter(d.outcome for d in self.decisions)
        matched = Counter(d.slug for d in self.decisions if d.slug and d.outcome in _MATCHED)
        routed = Counter(d.slug for d in self.decisions if d.outcome is Outcome.ROUTED)
        return {
            "routed": dict(sorted(routed.items())),
            "matched": dict(sorted(matched.items())),
            **{o.value: tally[o] for o in Outcome if o is not Outcome.ROUTED},
        }

    def to_json(self, counts_only: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"dry_run": self.dry_run, "counts": self.counts()}
        if not counts_only:
            body["decisions"] = [d.row() for d in self.decisions]
        return body


def _today(value: str | None) -> str:
    return value or datetime.date.today().isoformat()


def _common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--root", default=None, help="the generic queue (default: resolved queue)")
    sub.add_argument("--today", default=None, help="override date (testing)")
    sub.add_argument("--sync", action="store_true", help="commit the change in the generic queue")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Route generic briefs into their project queues.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route", help="move briefs that belong to one project")
    _common(r)
    r.add_argument("--registry", default=None, help="projects.toml (default: $PROJECT_REGISTRY)")
    r.add_argument(
        "--dest-root", default=None, help="worktree of a project's sync_repo to write into"
    )
    r.add_argument("--status", action="append", choices=SOURCE_FOLDERS, help="folders to scan")
    r.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    r.add_argument("--counts-only", action="store_true", help="print counts, not each brief")
    v = sub.add_parser("reverse", help="put a routed brief back in its origin queue")
    _common(v)
    v.add_argument("brief_id")
    return ap


def _route(a: argparse.Namespace, root: pathlib.Path) -> int:
    try:
        projects = load_projects(registry_path(a.registry))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"error: cannot read the project registry: {e}", file=sys.stderr)
        return 2
    dest = pathlib.Path(a.dest_root).expanduser() if a.dest_root else None
    router = Router(root, Matcher(tuple(projects)), dest, tuple(a.status or ("inbox",)))
    decisions = router.plan() if a.dry_run else router.apply(_today(a.today))
    report = Report(decisions, a.dry_run)
    print(json.dumps(report.to_json(a.counts_only), indent=2))
    moved = [d.brief_id for d in decisions if d.outcome is Outcome.ROUTED]
    if a.sync and moved and not a.dry_run:
        sync_commit(f"route_inbox: {len(moved)} brief(s) into projects", root, moved, [LOG_NAME])
    return 0


def _reverse(a: argparse.Namespace, root: pathlib.Path) -> int:
    try:
        undone = reverse(root, a.brief_id, _today(a.today))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if a.sync:
        sync_commit(f"route_inbox: {a.brief_id} returned", root, [a.brief_id], [LOG_NAME])
    print(f"{a.brief_id} returned from {undone.project}")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    root = pathlib.Path(a.root).expanduser() if a.root else resolve_root()
    return _route(a, root) if a.cmd == "route" else _reverse(a, root)


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

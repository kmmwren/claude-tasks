#!/usr/bin/env python3
"""
fingerprint.py — the normalised identity of a brief: its title plus the sources it cites.

Two captures of one task share a fingerprint whatever their casing, punctuation or whether a PR is
written as a URL or `owner/repo#N`. A same title citing different sources is a different task, so
it does not match. add_task.py refuses or merges on a match; triage marks the later briefs as
duplicates of the first.

Stdlib only.
"""

import dataclasses
import pathlib
import re

from deps import fm
from readiness import raw_title
from triage_brief import EXECUTION_LOG, FRONTMATTER
from triage_sources import GH_ISSUE, cited_sources

ACTIVE_FOLDERS = ("inbox", "ready", "in-progress", "blocked", "parked")


def normal_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.casefold()))


def canonical_source(source: str) -> str:
    """A PR or issue as `owner/repo#N`, any other reference without a trailing slash."""
    if m := GH_ISSUE.fullmatch(source):
        return f"{m[1]}#{m[2]}".lower()
    return source.rstrip("/")


def authored_body(text: str) -> str:
    """The brief without its frontmatter and its execution log, which only records which tool filed it."""
    m = FRONTMATTER.match(text)
    return EXECUTION_LOG.sub("", text[m.end() :] if m else text)


@dataclasses.dataclass(frozen=True)
class Fingerprint:
    title: str
    sources: frozenset[str]

    @classmethod
    def of_text(cls, text: str) -> "Fingerprint":
        title = raw_title(text)
        sources = cited_sources(f"{title}\n{authored_body(text)}")
        return cls(normal_title(title), frozenset(map(canonical_source, sources)))


@dataclasses.dataclass(frozen=True)
class Located:
    id: str
    folder: str
    path: pathlib.Path
    created: str

    @property
    def rank(self) -> tuple[bool, str, str]:
        """Briefs already past the inbox come first, then the earliest filed."""
        return self.folder == "inbox", self.created, self.id


def index(root: pathlib.Path) -> dict[Fingerprint, list[Located]]:
    """Every active brief grouped by fingerprint, the one to keep first in each group."""
    groups: dict[Fingerprint, list[Located]] = {}
    for folder in ACTIVE_FOLDERS:
        for path in sorted((root / folder).glob("*.md")):
            text = path.read_text()
            where = Located(path.stem, folder, path, fm(text).get("created", ""))
            groups.setdefault(Fingerprint.of_text(text), []).append(where)
    return {fp: sorted(members, key=lambda m: m.rank) for fp, members in groups.items()}


def find_duplicate(root: pathlib.Path, fingerprint: Fingerprint) -> Located | None:
    """The brief a new capture with this fingerprint repeats, if any."""
    return next(iter(index(root).get(fingerprint, [])), None)

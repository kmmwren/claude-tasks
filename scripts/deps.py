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
"""
import dataclasses
import pathlib
import re

from _paths import TASK_DIRS, strip_comment

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


def parse_list(raw: str) -> list[str]:
    """Items of a `[a, b, c]` frontmatter list, in order. Reasons may not contain commas."""
    return [t.strip() for t in (raw or "").strip().strip("[]").split(",") if t.strip()]


def is_external(item: str) -> bool:
    return item.startswith(EXTERNAL_PREFIX)


@dataclasses.dataclass(frozen=True)
class Brief:
    """One brief as the dependency tools see it: its folder (status) and parsed frontmatter."""
    id: str
    status: str
    fields: dict[str, str]

    @property
    def blocked_by(self) -> list[str]:
        return parse_list(self.fields.get("blocked_by", ""))

    @property
    def milestone(self) -> str:
        return self.fields.get("milestone", "")

    @property
    def depends_tags(self) -> set[str]:
        return {t[len("depends:"):] for t in parse_list(self.fields.get("tags", ""))
                if t.startswith("depends:")}

    @property
    def external_blockers(self) -> list[str]:
        return [b for b in self.blocked_by if is_external(b)]

    @property
    def declared_ids(self) -> set[str]:
        """Ids this brief names as dependencies in the strict forms (blocked_by, depends:)."""
        return {b for b in self.blocked_by if not is_external(b)} | self.depends_tags

    @property
    def legacy_tokens(self) -> set[str]:
        """Every word of the free-text `blockers:` field; some are ids, some are prose."""
        return set(re.findall(r"[\w-]+", self.fields.get("blockers", "") or ""))


def load_queue(root: pathlib.Path) -> list[Brief]:
    """Every brief across the lifecycle folders. A folder that does not exist is empty."""
    out: list[Brief] = []
    for d in BRIEF_DIRS:
        for f in sorted((root / d).glob("*.md")):
            fields = fm(f.read_text())
            out.append(Brief(fields.get("id", f.stem), d, fields))
    return out


def unmet(brief: Brief, done_ids: set[str]) -> list[str]:
    """Dependencies that block `brief`: external reasons always, ids until they are done."""
    ids = brief.declared_ids | brief.legacy_tokens
    return sorted({d for d in ids if d not in done_ids} | set(brief.external_blockers))


def awaits_release(brief: Brief, done_ids: set[str]) -> bool:
    """A blocked brief that names something to wait for, all of it now satisfied."""
    named = brief.blocked_by or brief.declared_ids or brief.legacy_tokens
    return bool(named) and not unmet(brief, done_ids)


def dependency_edges(brief: Brief, known_ids: set[str]) -> set[str]:
    """Brief ids `brief` waits on: the strict forms, plus legacy words that are real ids.

    Legacy `blockers:` prose contains ordinary words, so only tokens naming an existing
    brief are treated as edges; a strict-form id that names nothing is a dangling dependency.
    """
    return brief.declared_ids | (brief.legacy_tokens & known_ids)

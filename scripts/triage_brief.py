#!/usr/bin/env python3
"""
triage_brief.py — an inbox brief as the triage sees it: its text plus the facts derived from it.

Edits return a new InboxBrief, so a candidate can be checked against the readiness gate
before anything touches disk.

Stdlib only.
"""

import dataclasses
import functools
import pathlib
import re

from deps import fm, parse_list
from readiness import EXECUTION_LOG, brief_missing_fields, raw_title, section
from transition import set_field
from triage_sources import cited_sources

GENERATED_BY = "inbox-triage"
FRONTMATTER = re.compile(r"(---\n)(.*?)(\n---)", re.DOTALL)


@dataclasses.dataclass(frozen=True)
class InboxBrief:
    path: pathlib.Path
    text: str

    @property
    def id(self) -> str:
        return self.path.stem

    @functools.cached_property
    def fields(self) -> dict[str, str]:
        return fm(self.text)

    @property
    def structured(self) -> bool:
        """Filed through add_task with frontmatter, as opposed to a raw capture the enrich skill has yet to shape."""
        return bool(FRONTMATTER.match(self.text)) and bool(raw_title(self.text))

    @property
    def title(self) -> str:
        return raw_title(self.text) or self.id

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(sorted(parse_list(self.fields.get("tags", ""))))

    @functools.cached_property
    def missing(self) -> list[str]:
        return brief_missing_fields(self.text)

    @property
    def context(self) -> str:
        return section(self.text, "Context")

    @functools.cached_property
    def sources(self) -> set[str]:
        """Sources the brief's author cited; the execution log only records which tool filed it."""
        return cited_sources(EXECUTION_LOG.sub("", self.text))

    def with_field(self, key: str, value: str) -> "InboxBrief":
        """This brief with frontmatter `key: value` set."""
        text = FRONTMATTER.sub(
            lambda m: m.group(1) + set_field(m.group(2), key, value) + m.group(3),
            self.text,
            count=1,
        )
        return dataclasses.replace(self, text=text)

    def with_section(self, name: str, body: str) -> "InboxBrief":
        """This brief with the `## name` section's body replaced, marked as generated."""
        pattern = re.compile(
            rf"(^##[ \t]+{re.escape(name)}[ \t]*\n).*?(?=^##\s|\Z)", re.DOTALL | re.MULTILINE
        )
        text, found = pattern.subn(
            lambda m: f"{m.group(1)}\n{body.strip()}\n\n", self.text, count=1
        )
        text = text if found else self.text.rstrip("\n") + f"\n\n## {name}\n\n{body.strip()}\n"
        return dataclasses.replace(self, text=text).with_field("generated-by", GENERATED_BY)


def load_inbox(root: pathlib.Path) -> list[InboxBrief]:
    """Every structured brief in root/inbox, in id order; raw captures are left for the enrich skill."""
    briefs = (InboxBrief(f, f.read_text()) for f in sorted((root / "inbox").glob("*.md")))
    return [b for b in briefs if b.structured]

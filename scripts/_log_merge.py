"""
_log_merge.py: three-way merge for a brief where both sides only appended Execution-log lines.

Two writers (append_log.py pushing from a worktree of origin, a local writer committing in the
queue tree) each add a line at the end of the same brief, so git sees overlapping edits and
refuses to merge. The log is append-only and each line is dated, so the right merge keeps the
base entries exactly as they were and adds both sides' new entries after them in date order.
Any change elsewhere in the brief, or to an existing entry, is a real conflict left to a human.
"""

import re
from collections import Counter
from dataclasses import dataclass

from readiness import EXECUTION_LOG

_DATED = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class LoggedBrief:
    """A brief split around its Execution-log section into the text before, the entries, the rest."""

    before: str  # everything up to the first entry, heading and its blank lines included
    entries: tuple[str, ...]
    after: str  # the section's trailing newlines and whatever follows the section

    @classmethod
    def parse(cls, text: str) -> "LoggedBrief | None":
        """The brief split around its log, or None when it has no Execution log section."""
        section = EXECUTION_LOG.search(text)
        if section is None:
            return None
        heading, newline, rest = section.group().partition("\n")
        body = rest.strip("\n")
        lead = rest[: len(rest) - len(rest.lstrip("\n"))]
        trail = rest[len(rest.rstrip("\n")) :] if body else ""
        before = text[: section.start()] + heading + newline + lead
        return cls(before, _entries(body), trail + text[section.end() :])

    def render(self, entries: tuple[str, ...]) -> str:
        return self.before + "\n".join(entries) + self.after


def _entries(body: str) -> tuple[str, ...]:
    """An entry is a dated line plus the undated or blank lines beneath it."""
    entries: list[str] = []
    for line in body.split("\n") if body else []:
        if _DATED.match(line) or not entries:
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    return tuple(entries)


def _merge_text(base: str, upstream: str, replayed: str) -> str | None:
    """Three-way merge of text outside the log: take the side that changed, else conflict."""
    if upstream == replayed or replayed == base:
        return upstream
    if upstream == base:
        return replayed
    return None


def _date_key(entry: str) -> str:
    return entry[:10] if _DATED.match(entry) else ""


def _appended(base: tuple[str, ...], side: tuple[str, ...]) -> tuple[str, ...] | None:
    """The entries `side` added after `base`, or None when it did not keep `base` as a prefix."""
    return side[len(base) :] if side[: len(base)] == base else None


def _new_in(replayed: tuple[str, ...], upstream: tuple[str, ...]) -> list[str]:
    """The replayed appends upstream did not already make, matching duplicates one for one."""
    consumed = Counter(upstream)
    fresh = []
    for entry in replayed:
        if consumed[entry] > 0:
            consumed[entry] -= 1
        else:
            fresh.append(entry)
    return fresh


def merge_log_appends(base: str, upstream: str, replayed: str) -> str | None:
    """Merge `upstream` and `replayed` onto `base`, or None when they differ by more than appends.

    None means: the text outside the log changed on both sides, or a side did not keep the base
    entries as an exact prefix. The base entries stay in place; both sides' appended entries
    follow in date order, `upstream` first on a tie.
    """
    parsed = (LoggedBrief.parse(text) for text in (base, upstream, replayed))
    base_log, upstream_log, replayed_log = parsed
    if base_log is None or upstream_log is None or replayed_log is None:
        return None
    upstream_tail = _appended(base_log.entries, upstream_log.entries)
    replayed_tail = _appended(base_log.entries, replayed_log.entries)
    before = _merge_text(base_log.before, upstream_log.before, replayed_log.before)
    after = _merge_text(base_log.after, upstream_log.after, replayed_log.after)
    if upstream_tail is None or replayed_tail is None or before is None or after is None:
        return None
    tail = sorted([*upstream_tail, *_new_in(replayed_tail, upstream_tail)], key=_date_key)
    return LoggedBrief(before, (), after).render((*base_log.entries, *tail))

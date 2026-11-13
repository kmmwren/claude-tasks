"""
_log_merge.py: three-way merge for a brief where both sides only appended Execution-log lines.

Two writers (append_log.py pushing from a worktree of origin, a local writer committing in the
queue tree) each add a line at the end of the same brief, so git sees overlapping edits and
refuses to merge. The log is append-only and each line is dated, so the right merge is the union
of both sides' entries in date order. Any change elsewhere in the brief is a real conflict and
is left to a human.
"""

import re

LOG_HEADING = "## Execution log"
_DATED = re.compile(r"\d{4}-\d{2}-\d{2}")


def _split(text: str) -> tuple[str, list[str]]:
    """The brief up to and including the log heading, and the log's entries.

    An entry is a dated line plus any undated continuation lines beneath it.
    """
    head, heading, tail = text.partition(LOG_HEADING)
    entries: list[str] = []
    for line in tail.splitlines():
        if not line.strip():
            continue
        if _DATED.match(line) or not entries:
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    return head + heading, entries


def _merge_head(base: str, ours: str, theirs: str) -> str | None:
    """Three-way merge of the text outside the log: take the side that changed, else conflict."""
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    return None


def _date_key(entry: str) -> str:
    return entry[:10] if _DATED.match(entry) else ""


def merge_log_appends(base: str, ours: str, theirs: str) -> str | None:
    """Merge `ours` and `theirs` onto `base`, or None when they differ by more than log appends.

    None means: the text outside the log changed on both sides, or a side removed or rewrote an
    existing log entry. Entries are the union of both sides in date order, `ours` first on a tie.
    """
    base_head, base_entries = _split(base)
    ours_head, ours_entries = _split(ours)
    theirs_head, theirs_entries = _split(theirs)
    head = _merge_head(base_head, ours_head, theirs_head)
    kept = set(base_entries)
    if head is None or not kept <= set(ours_entries) or not kept <= set(theirs_entries):
        return None
    union = list(dict.fromkeys([*ours_entries, *theirs_entries]))
    entries = sorted(union, key=_date_key)
    return head + "\n\n" + "\n".join(entries) + "\n"

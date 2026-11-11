"""
_log_merge.py: three-way merge for a brief where both sides only appended Execution-log lines.

The log is append-only and each entry is dated, so the merge is the base entries followed by
both sides' new entries in date order. A change anywhere else is a real conflict.
"""
import re
from collections import Counter

LOG_HEADING = "## Execution log"
_HEADING_LINE = re.compile(rf"^{re.escape(LOG_HEADING)}[ \t]*$", re.MULTILINE)
_DATED = re.compile(r"\d{4}-\d{2}-\d{2}")


def _split(text: str) -> tuple[str, list[str]] | None:
    """The brief up to and including its log heading, and the log's entries.

    An entry is a dated line plus any undated continuation lines beneath it. None when the
    heading is absent, or the log holds a blank line between entries or a later section, so the
    layout is not one this merge can rebuild exactly.
    """
    headings = list(_HEADING_LINE.finditer(text))
    if not headings:
        return None
    after = text[headings[-1].end():]
    gap = after[: len(after) - len(after.lstrip("\n"))] or "\n\n"
    lines = after.strip("\n").splitlines()
    if any(not line.strip() or line.startswith("## ") for line in lines):
        return None
    entries: list[str] = []
    for line in lines:
        if _DATED.match(line) or not entries:
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    return text[: headings[-1].end()] + gap, entries


def _merge_head(base: str, ours: str, theirs: str) -> str | None:
    """Three-way merge of the text outside the log: take the side that changed, else conflict."""
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    return None


def _take(available: Counter[str], entry: str) -> bool:
    """Consume one occurrence of `entry` from `available`; True if there was one."""
    if available[entry] > 0:
        available[entry] -= 1
        return True
    return False


def _date_key(entry: str) -> str:
    return entry[:10] if _DATED.match(entry) else ""


def merge_log_appends(base: str, ours: str, theirs: str) -> str | None:
    """Merge `ours` and `theirs` onto `base`, or None when they differ by more than log appends.

    None means: the text outside the log changed differently on the two sides, a side did not keep
    the base entries as the start of its log, or a log layout is not a plain run of dated lines. New entries are the union of both sides in date order,
    `ours` first on a tie; base entries keep their order.
    """
    base_parts, ours_parts, theirs_parts = _split(base), _split(ours), _split(theirs)
    if base_parts is None or ours_parts is None or theirs_parts is None:
        return None
    (base_head, kept), (ours_head, ours_all), (theirs_head, theirs_all) = (
        base_parts, ours_parts, theirs_parts)
    head = _merge_head(base_head, ours_head, theirs_head)
    n = len(kept)
    if head is None or ours_all[:n] != kept or theirs_all[:n] != kept:
        return None
    shared = Counter(ours_all[n:])
    theirs_only = [entry for entry in theirs_all[n:] if not _take(shared, entry)]
    added = sorted([*ours_all[n:], *theirs_only], key=_date_key)
    entries = [*kept, *added]
    return head + "".join(f"{entry}\n" for entry in entries)

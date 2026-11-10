"""Tests for _log_merge.merge_log_appends: union of Execution-log appends, nothing else."""
import pytest
from _log_merge import merge_log_appends

HEAD = "---\nstatus: {s}\n---\n\n## Goal\n\n{g}\n\n## Execution log\n\n"


def brief(*lines, s="inbox", g="g"):
    return HEAD.format(s=s, g=g) + "".join(f"{line}\n" for line in lines)


BASE = brief("2026-11-09: captured.")


def test_union_of_both_sides_appends_in_date_order_ours_first_on_a_tie():
    ours = brief("2026-11-09: captured.", "2026-11-09: ours.", "2026-11-11: ours late.")
    theirs = brief("2026-11-09: captured.", "2026-11-09: theirs.", "2026-11-10: theirs mid.")
    assert merge_log_appends(BASE, ours, theirs) == brief(
        "2026-11-09: captured.", "2026-11-09: ours.", "2026-11-09: theirs.",
        "2026-11-10: theirs mid.", "2026-11-11: ours late.")


def test_a_line_both_sides_added_appears_once():
    both = brief("2026-11-09: captured.", "2026-11-09: same.")
    assert merge_log_appends(BASE, both, both) == both


def test_the_side_that_changed_the_frontmatter_wins():
    moved = brief("2026-11-09: captured.", "2026-11-09: closed.", s="done")
    logged = brief("2026-11-09: captured.", "2026-11-09: logged.")
    assert merge_log_appends(BASE, logged, moved) == brief(
        "2026-11-09: captured.", "2026-11-09: logged.", "2026-11-09: closed.", s="done")
    assert merge_log_appends(BASE, moved, logged) == brief(
        "2026-11-09: captured.", "2026-11-09: closed.", "2026-11-09: logged.", s="done")


def test_both_sides_making_the_same_frontmatter_change_is_not_a_conflict():
    ours = brief("2026-11-09: captured.", "2026-11-09: a.", s="done")
    theirs = brief("2026-11-09: captured.", "2026-11-09: b.", s="done")
    assert merge_log_appends(BASE, ours, theirs) == brief(
        "2026-11-09: captured.", "2026-11-09: a.", "2026-11-09: b.", s="done")


def test_different_frontmatter_changes_on_each_side_conflict():
    ours = brief("2026-11-09: captured.", s="done")
    theirs = brief("2026-11-09: captured.", s="parked")
    assert merge_log_appends(BASE, ours, theirs) is None


@pytest.mark.parametrize("side", ["ours", "theirs"])
def test_a_removed_or_rewritten_log_entry_is_a_conflict(side):
    rewritten = brief("2026-11-09: rewritten.")
    other = brief("2026-11-09: captured.", "2026-11-09: x.")
    args = (rewritten, other) if side == "ours" else (other, rewritten)
    assert merge_log_appends(BASE, *args) is None


def test_a_continuation_line_stays_with_its_entry():
    ours = brief("2026-11-09: captured.", "2026-11-12: late", "  continued")
    theirs = brief("2026-11-09: captured.", "2026-11-10: mid")
    assert merge_log_appends(BASE, ours, theirs) == brief(
        "2026-11-09: captured.", "2026-11-10: mid", "2026-11-12: late\n  continued")


def test_an_undated_first_line_is_kept_as_its_own_entry():
    undated = HEAD.format(s="inbox", g="g") + "stray note\n"
    merged = merge_log_appends(undated, undated, undated)
    assert merged is not None and merged.endswith("\n\nstray note\n")

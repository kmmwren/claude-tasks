"""Tests for _log_merge.merge_log_appends: union of Execution-log appends, nothing else."""

import pytest
from _log_merge import merge_log_appends

HEAD = "---\nstatus: {s}\n---\n\n## Goal\n\n{g}\n\n## Execution log\n\n"


def brief(*lines, s="inbox", g="g"):
    return HEAD.format(s=s, g=g) + "".join(f"{line}\n" for line in lines)


BASE = brief("2026-11-09: captured.")


def base_lines(text):
    return text.removeprefix(HEAD.format(s="inbox", g="g")).splitlines()


def test_both_sides_appends_follow_the_base_in_date_order_upstream_first_on_a_tie():
    upstream = brief("2026-11-09: captured.", "2026-11-09: upstream.", "2026-11-11: upstream late.")
    replayed = brief("2026-11-09: captured.", "2026-11-09: replayed.", "2026-11-10: replayed mid.")
    assert merge_log_appends(BASE, upstream, replayed) == brief(
        "2026-11-09: captured.",
        "2026-11-09: upstream.",
        "2026-11-09: replayed.",
        "2026-11-10: replayed mid.",
        "2026-11-11: upstream late.",
    )


def test_a_line_both_sides_added_appears_once():
    both = brief("2026-11-09: captured.", "2026-11-09: same.")
    assert merge_log_appends(BASE, both, both) == both


def test_the_side_that_changed_the_frontmatter_wins():
    moved = brief("2026-11-09: captured.", "2026-11-09: closed.", s="done")
    logged = brief("2026-11-09: captured.", "2026-11-09: logged.")
    assert merge_log_appends(BASE, logged, moved) == brief(
        "2026-11-09: captured.", "2026-11-09: logged.", "2026-11-09: closed.", s="done"
    )
    assert merge_log_appends(BASE, moved, logged) == brief(
        "2026-11-09: captured.", "2026-11-09: closed.", "2026-11-09: logged.", s="done"
    )


def test_both_sides_making_the_same_frontmatter_change_is_not_a_conflict():
    upstream = brief("2026-11-09: captured.", "2026-11-09: a.", s="done")
    replayed = brief("2026-11-09: captured.", "2026-11-09: b.", s="done")
    assert merge_log_appends(BASE, upstream, replayed) == brief(
        "2026-11-09: captured.", "2026-11-09: a.", "2026-11-09: b.", s="done"
    )


def test_different_frontmatter_changes_on_each_side_conflict():
    upstream = brief("2026-11-09: captured.", s="done")
    replayed = brief("2026-11-09: captured.", s="parked")
    assert merge_log_appends(BASE, upstream, replayed) is None


@pytest.mark.parametrize("side", ["upstream", "replayed"])
def test_a_removed_or_rewritten_log_entry_is_a_conflict(side):
    rewritten = brief("2026-11-09: rewritten.")
    other = brief("2026-11-09: captured.", "2026-11-09: x.")
    args = (rewritten, other) if side == "upstream" else (other, rewritten)
    assert merge_log_appends(BASE, *args) is None


def test_a_continuation_line_stays_with_its_entry():
    upstream = brief("2026-11-09: captured.", "2026-11-12: late", "  continued")
    replayed = brief("2026-11-09: captured.", "2026-11-10: mid")
    assert merge_log_appends(BASE, upstream, replayed) == brief(
        "2026-11-09: captured.", "2026-11-10: mid", "2026-11-12: late\n  continued"
    )


def test_an_undated_first_line_is_kept_as_its_own_entry():
    undated = HEAD.format(s="inbox", g="g") + "stray note\n"
    merged = merge_log_appends(undated, undated, undated)
    assert merged is not None and merged.endswith("\n\nstray note\n")


def test_an_append_identical_to_an_existing_entry_is_kept():
    upstream = brief("2026-11-09: captured.", "2026-11-10: up.")
    replayed = brief("2026-11-09: captured.", "2026-11-09: captured.")
    assert merge_log_appends(BASE, upstream, replayed) == brief(
        "2026-11-09: captured.", "2026-11-09: captured.", "2026-11-10: up."
    )


def test_a_line_both_sides_appended_twice_is_kept_twice():
    both = brief("2026-11-09: captured.", "2026-11-09: tick.", "2026-11-09: tick.")
    assert merge_log_appends(BASE, both, both) == both


def test_existing_duplicate_entries_survive():
    base = brief("2026-11-09: tick.", "2026-11-09: tick.")
    upstream = brief("2026-11-09: tick.", "2026-11-09: tick.", "2026-11-10: up.")
    assert merge_log_appends(base, upstream, base) == upstream


def test_backdated_history_keeps_its_order():
    base = brief("2026-11-10: later.", "2026-11-09: earlier, written after.")
    upstream = brief(*base_lines(base), "2026-11-11: up.")
    replayed = brief(*base_lines(base), "2026-11-09: replayed.")
    assert merge_log_appends(base, upstream, replayed) == brief(
        "2026-11-10: later.",
        "2026-11-09: earlier, written after.",
        "2026-11-09: replayed.",
        "2026-11-11: up.",
    )


def test_blank_lines_inside_the_log_are_preserved():
    base = brief("2026-11-09: captured.", "", "2026-11-09: after a gap.")
    upstream = brief("2026-11-09: captured.", "", "2026-11-09: after a gap.", "2026-11-10: up.")
    assert merge_log_appends(base, upstream, base) == upstream


def test_a_side_that_reorders_the_base_is_a_conflict():
    base = brief("2026-11-10: b.", "2026-11-09: a.")
    reordered = brief("2026-11-09: a.", "2026-11-10: b.")
    assert merge_log_appends(base, reordered, base) is None
    assert merge_log_appends(base, base, reordered) is None


def test_text_after_the_log_is_untouched_when_only_one_side_changed_it():
    base = brief("2026-11-09: captured.") + "\n## Notes\n\nn\n"
    upstream = brief("2026-11-09: captured.", "2026-11-10: up.") + "\n## Notes\n\nn\n"
    replayed = brief("2026-11-09: captured.", "2026-11-11: re.") + "\n## Notes\n\nedited\n"
    assert merge_log_appends(base, upstream, replayed) == (
        brief("2026-11-09: captured.", "2026-11-10: up.", "2026-11-11: re.")
        + "\n## Notes\n\nedited\n"
    )


def test_text_after_the_log_changed_differently_on_both_sides_is_a_conflict():
    def with_notes(notes):
        return brief("2026-11-09: captured.") + f"\n## Notes\n\n{notes}\n"

    assert merge_log_appends(with_notes("n"), with_notes("a"), with_notes("b")) is None


def test_a_brief_without_a_log_section_is_a_conflict():
    bare = "---\nstatus: inbox\n---\n\n## Goal\n\ng\n"
    assert merge_log_appends(bare, bare, bare) is None

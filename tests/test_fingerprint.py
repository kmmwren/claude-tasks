"""Tests for fingerprint.py: the normalised identity two captures of one task share."""

import pytest
from fingerprint import Fingerprint, find_duplicate, index

from tests.queue_helpers import inbox_text, make_inbox


def _fp(title, body=""):
    return Fingerprint.of_text(inbox_text("x", title=title, context=body or "_none recorded_"))


@pytest.mark.parametrize(
    "a, b",
    [
        ("Rotate the API key", "rotate  the api-key!"),
        ("Review o/r#4", "REVIEW o/r#4"),
    ],
)
def test_titles_match_regardless_of_case_punctuation_and_spacing(a, b):
    assert _fp(a) == _fp(b)


def test_different_titles_differ():
    assert _fp("Rotate the key") != _fp("Rotate the keys")


def test_a_github_url_and_a_short_ref_are_one_source():
    long = _fp("Fix", "See https://github.com/o/r/pull/4.")
    assert long == _fp("Fix", "See o/r#4.") and long.sources == frozenset({"o/r#4"})


def test_trailing_slashes_on_other_urls_are_ignored():
    assert _fp("Fix", "See https://x.test/a/") == _fp("Fix", "See https://x.test/a")


def test_the_same_title_citing_different_sources_is_a_different_task():
    assert _fp("Review it", "o/r#1") != _fp("Review it", "o/r#2")


def test_the_execution_log_is_not_a_source():
    text = inbox_text("x", title="Fix") + "\nfiled by add_task.py\n"
    assert Fingerprint.of_text(text).sources == frozenset()


def test_frontmatter_other_than_the_title_is_not_a_source():
    text = inbox_text("x", title="Fix", parent="batch.md")
    assert Fingerprint.of_text(text).sources == frozenset()


def _queue(tmp_path):
    root = make_inbox(tmp_path, {"b-late": inbox_text("b-late", title="Same")})
    (root / "inbox" / "a-early.md").write_text(inbox_text("a-early", title="Same"))
    (root / "ready" / "r.md").write_text(inbox_text("r", title="Same"))
    (root / "done" / "d.md").write_text(inbox_text("d", title="Same"))
    return root


def test_the_index_groups_active_briefs_canonical_first_and_skips_done(tmp_path):
    root = _queue(tmp_path)
    group = index(root)[_fp("Same")]
    assert [(m.folder, m.id) for m in group] == [
        ("ready", "r"),
        ("inbox", "a-early"),
        ("inbox", "b-late"),
    ]


def test_find_duplicate_returns_the_canonical_member_or_none(tmp_path):
    root = _queue(tmp_path)
    assert find_duplicate(root, _fp("Same")).id == "r"
    assert find_duplicate(root, _fp("Other")) is None


def test_created_date_orders_members_of_one_folder(tmp_path):
    root = make_inbox(tmp_path, {})
    for bid, created in (("z", "2026-01-01"), ("a", "2026-02-01")):
        text = inbox_text(bid, title="Same").replace("status: inbox", f"created: {created}\nstatus: inbox")
        (root / "inbox" / f"{bid}.md").write_text(text)
    assert [m.id for m in index(root)[_fp("Same")]] == ["z", "a"]

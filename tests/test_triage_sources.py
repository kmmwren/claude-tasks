"""Tests for triage_sources.py: what counts as a citable source, and grounding of generated text."""
import pytest
from triage_sources import cited_sources, grounded


@pytest.mark.parametrize("text, source", [
    ("see https://github.com/o/r/pull/7.", "https://github.com/o/r/pull/7"),
    ("lives at ~/.claude/hooks/gate.py", "~/.claude/hooks/gate.py"),
    ("landed as kmmwren/claude-tasks#24", "kmmwren/claude-tasks#24"),
    ("tracked in ABC-42", "ABC-42"),
    ("edit build_anatomy.py first", "build_anatomy.py"),
])
def test_each_kind_of_source_is_found(text, source):
    assert source in cited_sources(text)


def test_encodings_shaped_like_ticket_keys_are_not_sources():
    assert cited_sources("UTF-8 and SHA-256 and ISO-8601") == set()


def test_generated_text_citing_the_original_is_grounded():
    assert grounded("Start from ABC-42.", "Filed against ABC-42 and o/r#3.")


def test_generated_text_citing_nothing_is_not_grounded():
    assert not grounded("Make it better.", "Filed against ABC-42.")


def test_an_invented_reference_fails_even_beside_a_real_one():
    assert not grounded("See ABC-42 and ABC-43.", "Filed against ABC-42.")

"""triage_declare.py: declaring an empty inbox brief says why when the declaration is refused."""

import declaration
import triage_declare
from triage_declare import declare_empty

from tests.queue_helpers import inbox_text, make_inbox, queue_lock_held_elsewhere

TODAY = "2026-11-27"


def test_a_brief_that_is_not_in_the_queue_is_reported_and_not_declared(tmp_path, capsys):
    root = make_inbox(tmp_path, {})

    assert triage_declare._declare("ghost", root, TODAY) is False

    assert capsys.readouterr().err == (
        "inbox-triage: ghost: not declared: no brief with id 'ghost'\n"
    )


def test_a_declaration_that_cannot_take_the_queue_lock_is_reported_and_left_undone(
    tmp_path, monkeypatch, capsys
):
    root = make_inbox(tmp_path, {"b": inbox_text("b")})
    with queue_lock_held_elsewhere(root, monkeypatch, declaration):
        assert triage_declare._declare("b", root, TODAY) is False

    assert capsys.readouterr().err == (
        "inbox-triage: b: not declared: timed out waiting for the queue lock; 'b' was NOT declared\n"
    )
    assert declare_empty(root, apply=False, today=TODAY).declared == ("b",)

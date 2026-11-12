"""The project page's wording helpers, for the inputs the fixtures do not reach."""

import project_view as pv


def test_day_formats_iso_and_passes_anything_else_through():
    assert pv._day("2026-10-05") == "5 Oct"
    assert pv._day("2026-10-05", year=True) == "5 Oct 2026"
    assert pv._day("soon") == "soon"


def test_since_line_rewords_one_change_as_a_sentence():
    m = {"delta": {"since": "2026-10-05", "lines": ["Milestone reached: Beta"]}}
    assert pv._since_line(m) == '<p class="since">Since 5 Oct: Milestone done: Beta.</p>'


def test_since_line_lists_several_changes_in_plain_words():
    lines = ["3 more acceptance criteria met (12 of 20)", "1 acceptance criteria no longer met (x)",
             "Health moved from green to amber"]
    got = pv._since_line({"delta": {"since": "2026-10-05", "lines": lines}})
    assert got == ('<div class="since"><p>Since 5 Oct:</p><ul><li>3 more checks done</li>'
                   "<li>1 checks reopened</li><li>Health moved from On track to At risk</li></ul></div>")


def test_signal_text_names_the_late_milestones():
    m = {"milestones": [{"plain": "Beta", "missed": True}, {"plain": "GA", "missed": False}]}
    assert pv._signal_text({"signal": "missed milestone date", "detail": "x"}, m) == (
        "milestones past their date: Beta")


def test_group_state_is_done_only_when_every_milestone_is():
    assert pv._group_state([{"status": "done"}, {"status": "done"}]) == "done"
    assert pv._group_state([{"status": "done"}, {"status": "todo"}]) == "active"
    assert pv._group_state([]) == "not-started"


def test_recommended_prefers_the_decision_then_its_recommended_option():
    m = {"ask": {}}
    assert pv._recommended({"question": "q", "recommended": "Do A"}, m) == "Do A"
    options = [{"label": "B", "effect": "slow"}, {"label": "A (recommended).", "effect": "fast"}]
    assert pv._recommended({"question": "q", "options": options}, m) == "A. fast"


def test_decision_lists_its_options_in_the_full_text():
    d = {"status": "decided", "question": "Which?", "chosen": "A", "why": "", "date": "",
         "options": [{"label": "A", "effect": "fast"}]}
    got = pv._decision(d, {"ask": {}})
    assert "<ul><li><b>A</b> fast</li></ul>" in got
    assert '<p class="answer">A</p>' in got


def test_since_line_says_nothing_changed():
    m = {"delta": {"since": "2026-10-05", "lines": []}}
    assert pv._since_line(m) == '<p class="since">No change since 5 Oct.</p>'


def test_decided_full_text_carries_the_reason_after_the_choice():
    d = {"status": "decided", "question": "Which?", "chosen": "A", "why": "it is fast", "date": ""}
    assert "<summary>Full text</summary><p>A</p><p>it is fast</p></details>" in pv._decision(d, {"ask": {}})

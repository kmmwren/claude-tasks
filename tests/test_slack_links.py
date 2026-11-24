"""Tests for slack_links.py: permalink parsing and note grouping for Slack handoffs."""

import json

import pytest
import slack_links

from tests.queue_helpers import NOT_ASCII, piped

TOP = "https://example.slack.com/archives/C0123ABCD/p1773265865773179"
REPLY = (
    "https://example.slack.com/archives/C0999ZZZZ/p1773300000000002"
    "?thread_ts=1773299999.000001&cid=C0999ZZZZ"
)


def test_top_level_message_is_its_own_thread_root():
    link = slack_links.parse(TOP).links[0]
    assert (link.channel, link.message_ts, link.thread_ts) == (
        "C0123ABCD",
        "1773265865.773179",
        "1773265865.773179",
    )


def test_reply_takes_thread_root_from_query():
    link = slack_links.parse(REPLY).links[0]
    assert (link.message_ts, link.thread_ts) == ("1773300000.000002", "1773299999.000001")


def test_note_belongs_to_preceding_link_and_preamble_is_direction():
    text = f"all of these as ready\n{TOP} - fix the flaky test\n\n{REPLY}\nask the team\nwhy"
    batch = slack_links.parse(text)
    assert batch.direction == "all of these as ready"
    assert [x.note for x in batch.links] == ["fix the flaky test", "ask the team why"]


def test_slack_formatted_link_is_unwrapped():
    link = slack_links.parse(f"<{TOP}|message> look at this").links[0]
    assert (link.url, link.note) == (TOP, "look at this")


def test_link_without_note_has_empty_note():
    assert slack_links.parse(TOP).links[0].note == ""


def test_main_exits_2_when_no_links(capsys):
    assert slack_links.main(["no links here"]) == 2
    assert json.loads(capsys.readouterr().out) == {"direction": "no links here", "links": []}


def test_main_prints_json(capsys):
    assert slack_links.main([f"{TOP} note"]) == 0
    assert json.loads(capsys.readouterr().out)["links"][0]["note"] == "note"


def test_links_are_not_parked_by_default():
    assert slack_links.parse(f"{TOP} fix the flaky test").links[0].parked is False


def test_direction_parks_every_link():
    batch = slack_links.parse(f"park these\n{TOP} one\n{REPLY} two")
    assert [x.parked for x in batch.links] == [True, True]


def test_note_parks_only_its_own_link():
    batch = slack_links.parse(f"{TOP} as a task, parked\n{REPLY} look at this")
    assert [x.parked for x in batch.links] == [True, False]


def test_park_matches_any_case_and_form():
    assert slack_links.parse(f"Parking these\n{TOP}").links[0].parked is True


def test_park_must_be_a_whole_word():
    assert slack_links.parse(f"{TOP} check the parkway office").links[0].parked is False


def test_main_prints_parked(capsys):
    slack_links.main([f"parked {TOP}"])
    assert json.loads(capsys.readouterr().out)["links"][0]["parked"] is True


def test_a_top_level_link_and_its_thread_link_share_one_key():
    plain = "https://example.slack.com/archives/C0123ABCD/p1773265865773179"
    threaded = f"{plain}?thread_ts=1773265865.773179&cid=C0123ABCD"
    keys = {slack_links.parse(u).links[0].key for u in (plain, threaded)}
    assert keys == {"slack:C0123ABCD/1773265865.773179"}


def test_a_reply_is_keyed_by_its_thread_root():
    assert slack_links.parse(REPLY).links[0].key == "slack:C0999ZZZZ/1773299999.000001"


def test_permalink_key_is_none_for_other_urls():
    assert slack_links.permalink_key("https://example.test/archives/C1/p1") is None


def test_main_reads_a_utf8_pipe_whatever_the_locale(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", piped(f"{NOT_ASCII} {TOP} note"))
    assert slack_links.main([]) == 0
    assert json.loads(capsys.readouterr().out)["direction"] == NOT_ASCII


@pytest.mark.parametrize(
    "words", ["don't park these", "do not park this", "never park it", "why was this parked?"]
)
def test_words_that_negate_or_ask_about_parking_do_not_park(words):
    assert slack_links.parse(f"{words}\n{TOP}").links[0].parked is False

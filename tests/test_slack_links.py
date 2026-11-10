"""Tests for slack_links.py: permalink parsing and note grouping for Slack handoffs."""
import json

import slack_links

TOP = "https://panaseer.slack.com/archives/C0123ABCD/p1773265865773179"
REPLY = ("https://panaseer.slack.com/archives/C0999ZZZZ/p1773300000000002"
         "?thread_ts=1773299999.000001&cid=C0999ZZZZ")


def test_top_level_message_is_its_own_thread_root():
    link = slack_links.parse(TOP).links[0]
    assert (link.channel, link.message_ts, link.thread_ts) == (
        "C0123ABCD", "1773265865.773179", "1773265865.773179")


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


def test_links_go_to_inbox_by_default():
    assert slack_links.parse(f"{TOP} fix the flaky test").links[0].target == "inbox"


def test_direction_parks_every_link():
    batch = slack_links.parse(f"park these\n{TOP} one\n{REPLY} two")
    assert [x.target for x in batch.links] == ["parked", "parked"]


def test_note_parks_only_its_own_link():
    batch = slack_links.parse(f"{TOP} as a task, parked\n{REPLY} look at this")
    assert [x.target for x in batch.links] == ["parked", "inbox"]


def test_park_matches_any_case_and_form():
    assert slack_links.parse(f"Parking these\n{TOP}").links[0].target == "parked"


def test_park_must_be_a_whole_word():
    assert slack_links.parse(f"{TOP} check the parkway office").links[0].target == "inbox"


def test_main_prints_target(capsys):
    slack_links.main([f"parked {TOP}"])
    assert json.loads(capsys.readouterr().out)["links"][0]["target"] == "parked"

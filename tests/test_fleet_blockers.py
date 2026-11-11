"""Tests for fleet_blockers.py and the fleet-blocker lane in prioritise.py and rank.py.

A brief's `clears: <id>` names a fleet blocker; while that blocker's record (in the directory
tasks.toml's `fleet_blockers_dir` names) is active or unverified, the brief sorts ahead of every
other lane, outside the expedite cap, and rank.py pins it above the judged list.
"""

import datetime
import json

import fleet_blockers
import init_queue
import prioritise
import pytest
import rank
import taste
from fleet_blockers import BlockerStatus, FleetBlocker, FleetBlockers
from rank import AllScope, Ranker

TODAY = datetime.date(2026, 11, 11)
W = prioritise.DEFAULT_WEIGHTS


def _record(directory, bid, status="active", summary="Hooks block every session", **extra):
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "id": bid,
        "summary": summary,
        "raised_at": "2026-11-11T09:00:00Z",
        "raised_by": "claude",
        "owner": "claude",
        "probe": "true",
        "evidence": "",
        "status": status,
        "checked_at": "2026-11-11T09:05:00Z",
        **extra,
    }
    (directory / f"{bid}.json").write_text(json.dumps(body))


def _brief(bid, *, clears="", cos="", importance="3", autonomy="full", blocked_by="[]", effort="m"):
    return f"""---
id: {bid}
title: Title of {bid}
status: ready
type: todo
importance: {importance}
autonomy: {autonomy}
estimated-effort: {effort}
cos: {cos}
clears: {clears}
tags: []
blockers: []
blocked_by: {blocked_by}
related: []
---

## Goal

g
"""


def _queue(tmp_path, briefs, *, blockers_dir="blockers", extra_toml=""):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    for name, text in briefs.items():
        (root / "ready" / f"{name}.md").write_text(text)
    key = f'fleet_blockers_dir = "{blockers_dir}"\n' if blockers_dir else ""
    (root / "tasks.toml").write_text(f'name = "demo"\n{key}{extra_toml}\n')
    return root


def _ids(ranked):
    return [b["id"] for b in ranked]


# ── the record reader ─────────────────────────────────────────────────────────


def test_active_and_unverified_records_are_open_and_cleared_ones_are_not(tmp_path):
    for bid, status in (("a", "active"), ("u", "unverified"), ("c", "cleared")):
        _record(tmp_path, bid, status, summary=f"summary {bid}")
    blockers = FleetBlockers.read(tmp_path)
    assert blockers.open == {
        "a": FleetBlocker("a", "summary a", BlockerStatus.ACTIVE),
        "u": FleetBlocker("u", "summary u", BlockerStatus.UNVERIFIED),
    }


def test_an_unset_directory_turns_the_feature_off_without_a_warning(tmp_path, capsys):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    assert FleetBlockers.for_queue(root).open == {}
    assert capsys.readouterr().err == ""


def test_a_relative_directory_is_resolved_beside_the_queue_and_a_tilde_expands(
    tmp_path, monkeypatch
):
    _record(tmp_path / "blockers", "a")
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    (root / "tasks.toml").write_text('name = "demo"\nfleet_blockers_dir = "blockers"\n')
    assert set(FleetBlockers.for_queue(root).open) == {"a"}
    monkeypatch.setenv("HOME", str(tmp_path))
    (root / "tasks.toml").write_text('name = "demo"\nfleet_blockers_dir = "~/blockers"\n')
    assert set(FleetBlockers.for_queue(root).open) == {"a"}


def test_a_missing_directory_warns_once_and_falls_back_to_no_blockers(tmp_path, capsys):
    root = _queue(tmp_path, {"a": _brief("a", clears="x")}, blockers_dir="nowhere")
    assert _ids(prioritise.rank(root, today=TODAY)) == ["a"]
    err = capsys.readouterr().err
    assert err.count("fleet_blockers_dir") == 1 and "using no fleet blockers" in err


def test_a_bad_record_is_skipped_with_one_warning_and_the_rest_still_load(tmp_path, capsys):
    _record(tmp_path, "good")
    (tmp_path / "torn.json").write_text("{not json")
    (tmp_path / "list.json").write_text("[]")
    _record(tmp_path, "odd", status="exploded")
    blockers = FleetBlockers.read(tmp_path)
    assert set(blockers.open) == {"good"}
    err = capsys.readouterr().err
    assert err.count("fleet-blockers") == 1
    assert all(name in err for name in ("torn.json", "list.json", "odd.json"))


def test_a_record_without_an_id_is_named_by_its_file(tmp_path):
    (tmp_path / "by-file.json").write_text(json.dumps({"summary": "s", "status": "active"}))
    assert FleetBlockers.read(tmp_path).open["by-file"].summary == "s"


@pytest.mark.parametrize(
    "clears, expected",
    [
        ("a", "a"),
        ("[a]", "a"),
        ("[nope, a]", "a"),
        ("[b, a]", "b"),
        ("nope", None),
        ("", None),
        ("[]", None),
    ],
)
def test_clearing_reads_one_id_or_a_list_and_ignores_unknown_ones(tmp_path, clears, expected):
    _record(tmp_path, "a")
    _record(tmp_path, "b")
    found = FleetBlockers.read(tmp_path).clearing({"clears": clears})
    assert (found.id if found else None) == expected


def test_a_brief_without_the_field_clears_nothing(tmp_path):
    _record(tmp_path, "a")
    assert FleetBlockers.read(tmp_path).clearing({}) is None


# ── prioritise: the lane ──────────────────────────────────────────────────────


@pytest.mark.parametrize("scorer", list(prioritise.Scorer))
def test_a_clearing_brief_outranks_every_other_lane_under_either_scorer(tmp_path, scorer):
    root = _queue(
        tmp_path,
        {
            "boss": _brief("boss", importance="1", cos="expedite"),
            "fix": _brief("fix", clears="hooks-down", importance="4", effort="xl"),
            "date": _brief("date", cos="fixed-date"),
        },
    )
    _record(root.parent / "blockers", "hooks-down")
    ranked = prioritise.rank(root, today=TODAY, scorer=scorer)
    assert ranked[0]["id"] == "fix" and ranked[0]["_lane"] is prioritise.Lane.FLEET_BLOCKER
    assert ranked[1]["id"] == "boss"


def test_fleet_blocker_briefs_are_not_held_to_the_expedite_cap(tmp_path):
    root = _queue(
        tmp_path,
        {
            **{f"fix{n}": _brief(f"fix{n}", clears="hooks-down") for n in range(3)},
            **{f"exp{n}": _brief(f"exp{n}", cos="expedite") for n in range(3)},
        },
        extra_toml="[priority]\nexpedite_cap = 1",
    )
    _record(root.parent / "blockers", "hooks-down")
    lanes = {
        b["id"]: b["_lane"].value
        for b in prioritise.rank(root, today=TODAY, scorer=prioritise.Scorer.WSJF)
    }
    assert [lanes[f"fix{n}"] for n in range(3)] == ["fleet-blocker"] * 3
    assert sorted(v for k, v in lanes.items() if k.startswith("exp")) == [
        "expedite",
        "standard",
        "standard",
    ]


@pytest.mark.parametrize("status", ["active", "unverified"])
def test_an_unverified_blocker_counts_as_active(tmp_path, status):
    root = _queue(tmp_path, {"a": _brief("a"), "fix": _brief("fix", clears="hooks-down")})
    _record(root.parent / "blockers", "hooks-down", status)
    assert _ids(prioritise.rank(root, today=TODAY)) == ["fix", "a"]


@pytest.mark.parametrize("record", ["cleared", None])
def test_a_cleared_or_missing_record_leaves_the_brief_in_its_normal_lane(tmp_path, capsys, record):
    root = _queue(
        tmp_path, {"a": _brief("a", importance="1"), "fix": _brief("fix", clears="hooks-down")}
    )
    if record:
        _record(root.parent / "blockers", "hooks-down", record)
    else:
        (root.parent / "blockers").mkdir()
    ranked = prioritise.rank(root, today=TODAY)
    assert _ids(ranked) == ["a", "fix"] and ranked[1]["_lane"] is prioritise.Lane.STANDARD
    assert "fleet_blocker" not in ranked[1]["_why"] and capsys.readouterr().err == ""


def test_without_the_config_key_clears_is_ignored(tmp_path):
    root = _queue(
        tmp_path,
        {"a": _brief("a", importance="1"), "fix": _brief("fix", clears="hooks-down")},
        blockers_dir="",
    )
    _record(root.parent / "blockers", "hooks-down")
    assert _ids(prioritise.rank(root, today=TODAY)) == ["a", "fix"]


def test_a_blocked_brief_does_not_take_the_lane_but_a_needs_input_one_does(tmp_path):
    root = _queue(
        tmp_path,
        {
            "stuck": _brief("stuck", clears="hooks-down", blocked_by="[external:vendor]"),
            "ask": _brief("ask", clears="hooks-down", autonomy="needs-input"),
            "a": _brief("a", importance="1"),
        },
    )
    _record(root.parent / "blockers", "hooks-down")
    lanes = {b["id"]: b["_lane"] for b in prioritise.rank(root, today=TODAY)}
    assert (
        lanes["ask"] is prioritise.Lane.FLEET_BLOCKER and lanes["stuck"] is prioritise.Lane.STANDARD
    )


def test_the_why_names_the_blocker_in_json_and_text(tmp_path, monkeypatch, capsys):
    root = _queue(tmp_path, {"a": _brief("a"), "fix": _brief("fix", clears="hooks-down")})
    _record(root.parent / "blockers", "hooks-down", summary="Hooks block every session")
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main(["--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert (row["id"], row["lane"]) == ("fix", "fleet-blocker")
    assert row["why"]["fleet_blocker"] == "hooks-down: Hooks block every session"
    prioritise.main([])
    first = capsys.readouterr().out.splitlines()[1]
    assert "fleet-blocker" in first and "clears hooks-down" in first


def test_the_why_text_of_an_ordinary_brief_is_unchanged(tmp_path, monkeypatch, capsys):
    root = _queue(tmp_path, {"a": _brief("a")})
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main([])
    assert "clears" not in capsys.readouterr().out


def test_config_default_for_the_directory_is_unset():
    import config

    assert config.DEFAULTS["fleet_blockers_dir"] == ""
    assert fleet_blockers.CONFIG_KEY == "fleet_blockers_dir"


# ── rank.py: pinned above the judged list ─────────────────────────────────────

PROFILE = "---\ndraft: false\nupdated: 2026-11-01\n---\n## Goals\n- FDO | weight 1 | match fdo\n"


class FakePredict:
    def __init__(self, *outputs):
        self.outputs, self.prompts = list(outputs), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def _reply(*ids):
    return json.dumps({"ranking": [{"id": i, "reason": f"serves {i}"} for i in ids]})


def _rank_queue(tmp_path):
    root = _queue(tmp_path, {n: _brief(n) for n in ("pin-one", "pin-two", "a", "b", "c")})
    taste.profile_path(root).parent.mkdir()
    taste.profile_path(root).write_text(PROFILE)
    return root


def _rows(root):
    return rank.prioritise_rows(root)


def _candidates(root, limit=15):
    return rank.candidates(
        root,
        AllScope(""),
        limit,
        lambda r: [
            {
                **row,
                "lane": "fleet-blocker",
                "why": {"fleet_blocker": "hooks-down: Hooks block every session"},
            }
            if row["id"] in ("pin-one", "pin-two")
            else row
            for row in sorted(_rows(r), key=lambda x: x["id"] not in ("pin-one", "pin-two"))
        ],
    )


def test_the_filter_row_marks_a_brief_as_pinned_with_the_blocker_summary(tmp_path):
    briefs = _candidates(_rank_queue(tmp_path))
    assert [(b.id, bool(b.fleet_blocker)) for b in briefs[:3]] == [
        ("pin-one", True),
        ("pin-two", True),
        ("a", False),
    ]
    assert briefs[0].fleet_blocker == "hooks-down: Hooks block every session"
    assert not briefs[2].fleet_blocker


def test_pinned_briefs_lead_the_list_and_never_reach_the_judge(tmp_path):
    root = _rank_queue(tmp_path)
    fake = FakePredict(_reply("c", "b", "a"), _reply("c", "b", "a"))
    ranking = Ranker(root, fake).rank(AllScope(""), _candidates(root), 4)
    assert [i.id for i in ranking.items] == ["pin-one", "pin-two", "c", "b"]
    assert "pin-" not in "".join(fake.prompts)
    assert ranking.items[0].reason.startswith(
        "clears fleet blocker hooks-down: Hooks block every session"
    )
    assert ranking.items[0].confidence is None and ranking.total == 5
    assert [i.filter_position for i in ranking.items] == [1, 2, 5, 4]
    assert ranking.source == "judge"


def test_pinned_briefs_beyond_the_top_n_are_all_shown_and_the_judge_is_skipped(tmp_path):
    root = _rank_queue(tmp_path)
    fake = FakePredict()
    ranking = Ranker(root, fake).rank(AllScope(""), _candidates(root), 1)
    assert [i.id for i in ranking.items] == ["pin-one", "pin-two"] and fake.prompts == []
    assert ranking.source == "filter" and "fleet blockers fill the list" in ranking.fallback_reason


def test_without_a_profile_the_pinned_briefs_still_lead_the_filter_order(tmp_path):
    root = _rank_queue(tmp_path)
    taste.profile_path(root).unlink()
    ranking = Ranker(root, FakePredict()).rank(AllScope(""), _candidates(root), 4)
    assert [i.id for i in ranking.items][:2] == [
        "pin-one",
        "pin-two",
    ] and ranking.source == "filter"


def test_end_to_end_a_clearing_brief_is_the_top_row_of_the_filter(tmp_path):
    root = _queue(
        tmp_path,
        {"a": _brief("a", importance="1"), "pin-one": _brief("pin-one", clears="hooks-down")},
    )
    _record(root.parent / "blockers", "hooks-down")
    briefs = rank.candidates(root, AllScope(""), 15, rank.prioritise_rows)
    assert [b.id for b in briefs] == ["pin-one", "a"] and briefs[0].fleet_blocker.startswith(
        "hooks-down"
    )

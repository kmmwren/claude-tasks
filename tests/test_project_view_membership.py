"""Milestone rows and membership as the project view builds them from brief frontmatter."""

import datetime as dt
import tomllib

import project_view as pv
import pytest

from tests.project_fixtures import make_queue

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)

# The rows `build_model` returns for the shared fixture, written out so a change in membership
# or row shape cannot move them unnoticed.
FIXTURE_MILESTONES = [
    {
        "id": "m1",
        "title": "First",
        "reader_title": "The first thing works",
        "due": "2026-10-01",
        "release": "",
        "briefs": ["b1"],
        "criteria": [{"done": True, "text": "works"}],
        "missed": False,
        "ticked": 1,
        "total": 1,
        "status": "done",
        "percent_done": 100,
    },
    {
        "id": "m2",
        "title": "Second",
        "reader_title": "The second thing works",
        "due": "",
        "release": "2026.12",
        "briefs": ["b2"],
        "criteria": [{"done": True, "text": "a"}, {"done": False, "text": "b"}],
        "missed": False,
        "ticked": 1,
        "total": 2,
        "status": "active",
        "percent_done": 50,
    },
]
FIXTURE_TOTALS = {"ticked": 2, "total": 3}


def model(q):
    return pv.build_model(q, pv.Signals(now=NOW))


def retag(q, folder, bid, milestone):
    path = q / folder / f"{bid}.md"
    text = path.read_text()
    old = next(line for line in text.splitlines() if line.startswith("milestone:"))
    path.write_text(text.replace(old, f"milestone: {milestone}"))


def test_the_fixture_queue_builds_the_pinned_milestone_rows(tmp_path):
    m = model(make_queue(tmp_path, due='due = "2026-10-01"'))
    assert m["milestones"] == FIXTURE_MILESTONES and m["totals"] == FIXTURE_TOTALS


def test_retagging_a_brief_moves_it_to_the_milestone_it_now_names(tmp_path):
    q = make_queue(tmp_path)
    retag(q, "done", "b1", "m2")
    m1, m2 = model(q)["milestones"]
    assert (m1["briefs"], m1["status"]) == ([], "unmapped")
    assert sorted(m2["briefs"]) == ["b1", "b2"] and m2["total"] == 3


def test_a_row_listing_briefs_still_counts_them_and_warns_that_the_list_is_deprecated(
    tmp_path, capsys
):
    q = make_queue(tmp_path)
    toml = q / "project.toml"
    toml.write_text(toml.read_text().replace('id = "m1"\n', 'id = "m1"\nbriefs = ["b2"]\n', 1))
    retag(q, "done", "b1", "m2")
    m1, m2 = model(q)["milestones"]
    assert (m1["briefs"], m1["status"]) == (["b2"], "active")
    assert sorted(m2["briefs"]) == ["b1", "b2"]
    err = capsys.readouterr().err.splitlines()
    assert err == [
        f"warning: {q / 'project.toml'}: milestones 'm1' lists briefs, which is deprecated;"
        " give each brief `milestone: m1`"
    ]


def test_a_malformed_tasks_file_warns_once_and_keeps_the_listed_milestones(tmp_path, capsys):
    q = make_queue(tmp_path)
    (q / "tasks.toml").write_text("not = [valid")
    assert [m["id"] for m in model(q)["milestones"]] == ["m1", "m2"]
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and "tasks.toml" in err[0] and err[0].startswith("warning:")


def test_an_unreadable_milestones_file_warns_naming_it(tmp_path, capsys):
    q = make_queue(tmp_path)
    (q / "tasks.toml").write_text('name = "Demo"\nmilestones = "missing.toml"\n')
    assert [m["id"] for m in model(q)["milestones"]] == ["m1", "m2"]
    assert "missing.toml" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        OSError("denied"),
        KeyError("k"),
        TypeError("t"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        tomllib.TOMLDecodeError("bad"),
    ],
)
def test_any_read_failure_leaves_the_rows_out_and_warns(tmp_path, capsys, monkeypatch, error):
    q = make_queue(tmp_path)

    def fail(_root):
        raise error

    monkeypatch.setattr(pv, "milestone_specs", fail)
    assert [m["id"] for m in model(q)["milestones"]] == ["m1", "m2"]
    assert str(q / "tasks.toml") in capsys.readouterr().err


def test_a_milestones_file_that_is_not_utf8_leaves_only_its_rows_out(tmp_path, capsys):
    q = make_queue(tmp_path)
    (q / "tasks.toml").write_text('name = "Demo"\nmilestones = "ms.toml"\n')
    (tmp_path / "ms.toml").write_bytes(b"\xff\xfe\x00")
    assert [m["id"] for m in model(q)["milestones"]] == ["m1", "m2"]
    assert "ms.toml" in capsys.readouterr().err

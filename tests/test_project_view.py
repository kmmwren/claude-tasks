"""Project overview generator: derived states, the health floor, delta and rendering."""
import datetime as dt

import project_view as pv
import pytest

from tests.project_fixtures import make_queue

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)

def model(q, **kw):
    sig = pv.Signals(now=kw.pop("now", NOW), **kw)
    return pv.build_model(q, sig)


def test_counts_and_states_are_derived(tmp_path):
    m = model(make_queue(tmp_path))
    assert [x["status"] for x in m["milestones"]] == ["done", "active"]
    assert m["totals"]["ticked"] == 2 and m["totals"]["total"] == 3


def test_green_with_no_signals_stays_green(tmp_path):
    h = model(make_queue(tmp_path))["health"]
    assert h["effective"] == "green" and not h["overridden"] and h["capping"] == []


def test_stale_queue_caps_green_to_amber_and_says_so(tmp_path):
    q = make_queue(tmp_path, updated="2026-10-01")
    m = model(q)
    assert m["health"]["effective"] == "amber"
    assert m["health"]["overridden"]
    assert m["health"]["capping"][0]["signal"] == "no movement"
    assert "Automatic checks lowered this from On track to At risk" in pv.render_fragment(m)


def test_floor_is_load_bearing(tmp_path, monkeypatch):
    q = make_queue(tmp_path, updated="2026-10-01")
    monkeypatch.setattr(pv, "STALE_DAYS", 10**6)
    h = model(q)["health"]
    assert h["effective"] == "green" and not h["overridden"]


def test_red_ci_on_open_pr_caps_to_amber(tmp_path):
    pr = pv.PrState("Panaseer/demo#7", "OPEN", "red", NOW)
    h = model(make_queue(tmp_path), prs={pr.key: pr})["health"]
    assert h["effective"] == "amber" and h["capping"][0]["signal"] == "red CI"


def test_unreadable_ci_is_not_red(tmp_path):
    h = model(make_queue(tmp_path), prs={})["health"]
    assert h["effective"] == "green"


def test_missed_milestone_date_caps_to_red(tmp_path):
    q = make_queue(tmp_path, due='due = "2026-10-20"\n')
    q.joinpath("done/b1.md").rename(q / "in-progress" / "b1.md")
    h = model(q)["health"]
    assert h["effective"] == "red" and h["capping"][0]["signal"] == "missed milestone date"


def test_owner_red_is_never_lowered_by_the_floor(tmp_path):
    h = model(make_queue(tmp_path, colour="red"))["health"]
    assert h["effective"] == "red" and not h["overridden"]


def test_delta_reports_progress_against_baseline(tmp_path):
    q = make_queue(tmp_path)
    first = model(q)
    base = pv.snapshot(first)
    base["criteria"] = [0, 3]
    base["milestones_done"] = []
    d = pv.delta(first, base)
    assert any("Milestone reached" in x for x in d["lines"])
    assert any("2 more acceptance criteria" in x for x in d["lines"])
    assert pv.delta(first, None)["note"].startswith("First report")


def test_pick_baseline_prefers_oldest_within_a_week():
    snaps = [{"at": (NOW - dt.timedelta(days=n)).isoformat()} for n in (30, 6, 2)]
    assert pv.pick_baseline(snaps, NOW) == snaps[1]
    assert pv.pick_baseline(snaps[:1], NOW) == snaps[0]
    assert pv.pick_baseline([], NOW) is None


@pytest.mark.parametrize("edit,message", [
    ('colour = "green"', "colour"), ('reason = "Going to plan."', "reason"),
    ('title = "Demo Project"', "title")])
def test_config_validation(tmp_path, edit, message):
    q = make_queue(tmp_path)
    text = (q / "project.toml").read_text()
    new = text.replace(edit, 'colour = "purple"' if "colour" in edit else "")
    (q / "project.toml").write_text(new)
    with pytest.raises(pv.ProjectConfigError, match=message):
        pv.load_project(q)


def test_not_a_queue_is_refused(tmp_path):
    with pytest.raises(pv.ProjectConfigError):
        model(tmp_path)


def test_render_has_header_and_stable_hash(tmp_path):
    m = model(make_queue(tmp_path))
    html = pv.render_fragment(m)
    assert "Customers can do the demo thing." in html and "<title>Demo Project overview</title>" in html
    assert 'name="claude:viewport-fit-sides"' in html
    assert pv.content_hash(m) == pv.content_hash(model(make_queue(tmp_path / "x"), now=NOW + dt.timedelta(minutes=5)))

"""Every optional section of the project page, driven from one rich fixture."""
import datetime as dt

import project_view as pv

from tests.project_fixtures import make_rich

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)


def build(tmp_path, prs=None, sessions=None, activity=None):
    q = make_rich(tmp_path)
    sig = pv.Signals(now=NOW, prs=prs or {}, sessions=sessions or {}, queue_activity=activity)
    return pv.build_model(q, sig, decision_dirs=[tmp_path / "decisions"])


def test_brief_states_and_rollups(tmp_path):
    m = build(tmp_path)
    states = {b["id"]: b["state"] for b in m["briefs"]}
    assert states["b-unverified"] == "unverified" and states["b-parked"] == "parked"
    assert states["b-queued"] == "queued" and states["b-done"] == "done"
    ws = {w["id"]: w for w in m["workstreams"]}
    assert ws["empty"]["status"] == "unmapped" and ws["empty"]["pct"] == 0
    assert [b["id"] for b in ws["w"]["next"]] == ["b-queued"]


def test_rollout_cells_cover_every_evidence_kind(tmp_path):
    merged = pv.PrState("Panaseer/demo#1", "MERGED", "green", NOW)
    open_pr = pv.PrState("Panaseer/demo#2", "OPEN", "green", NOW)
    cells = {t["id"]: t["cells"] for t in build(tmp_path, prs={merged.key: merged, open_pr.key: open_pr})["rollout"]["targets"]}
    assert cells["env"]["built"]["state"] == "verified" and cells["env"]["live"]["state"] == "verified"
    assert cells["other"]["built"]["state"] == "pending" and cells["other"]["live"]["state"] == "claimed"
    assert cells["third"]["built"]["state"] == "unchecked" and cells["third"]["live"]["state"] == "pending"
    unknown = {t["id"]: t["cells"] for t in build(tmp_path / "u")["rollout"]["targets"]}
    assert unknown["env"]["live"]["state"] == "unchecked"


def test_architecture_tree_and_decisions(tmp_path):
    m = build(tmp_path)
    assert m["architecture"][0]["children"][0]["children"][0]["id"] == "part"
    assert m["architecture"][0]["status"] == "done" and m["architecture"][0]["children"][0]["status"] == "unmapped"
    dec = {d["question"]: d for d in m["decisions"]}
    assert dec["Pick a thing"]["chosen"] == "Take the first one" and dec["Pick a thing"]["status"] == "decided"
    assert dec["Use an ADR"]["status"] == "open" and dec["Use an ADR"]["date"] == "2026-09-30"
    assert dec["Inline question?"]["status"] == "open"


def test_requirements_register_states(tmp_path):
    rq = build(tmp_path)["requirements"]
    assert {i["id"]: i["state"] for i in rq["items"]} == {"RCH-R001": "done", "RCH-R002": "active", "RCH-R003": "uncovered"}
    assert rq["problems"]


def test_floor_inside_owner_colour_is_noted_not_overriding(tmp_path):
    pr = pv.PrState("Panaseer/demo#1", "OPEN", "red", NOW)
    m = build(tmp_path, prs={pr.key: pr})
    assert m["health"]["effective"] == "amber" and not m["health"]["overridden"] and m["health"]["owner_stale"]
    html = pv.render_fragment(m)
    assert "Automatic checks also flagged" in html and "not reconfirmed recently" in html


def test_full_render_sections(tmp_path):
    pr = pv.PrState("Panaseer/demo#1", "OPEN", "red", NOW, "https://x.test/1", "Real title")
    m = build(tmp_path, prs={pr.key: pr}, sessions={"abc12345-full": NOW - dt.timedelta(days=2)},
              activity=NOW - dt.timedelta(days=1))
    html = pv.render_fragment(m)
    for text in ("Needs your decision", "Rollout", "All requirements",
                 "No work is planned for this yet", "Real title", "2 days ago", "last active",
                 "Check results could not be read for", "Live environments", "Repo", "Fallback title",
                 'role="tablist"', 'id="panel-plan"', 'id="panel-decisions" aria-labelledby="tab-decisions" hidden'):
        assert text in html, text
    assert pv.render_document(m).startswith("<!doctype html>")


def test_empty_sections_say_so(tmp_path):
    q = tmp_path / ".tasks"
    for d in ("inbox", "ready", "in-progress", "done", "parked"):
        (q / d).mkdir(parents=True)
    (q / "tasks.toml").write_text('name = "E"\n')
    (q / "project.toml").write_text('[project]\ntitle = "E"\noutcome = "o"\n[health]\ncolour = "green"\nreason = "r"\n')
    m = pv.build_model(q, pv.Signals(now=NOW))
    html = pv.render_fragment(m)
    for text in ("No milestones are defined", "No architecture has been described", "No decisions are recorded",
                 "No links have been added"):
        assert text.lower() in html.lower(), text
    assert m["health"]["effective"] == "green"


def test_cli_renders_a_queue(tmp_path, capsys):
    q = make_rich(tmp_path)
    assert pv.main([str(q)]) == 0
    assert "Rich Project" in capsys.readouterr().out


def test_config_errors_and_date_forms(tmp_path):
    import pytest
    q = make_rich(tmp_path)
    with pytest.raises(pv.ProjectConfigError, match="does not exist"):
        pv.load_project(tmp_path)
    good = (q / "project.toml").read_text()
    (q / "project.toml").write_text(good.replace("[health]", "[nothealth]"))
    with pytest.raises(pv.ProjectConfigError, match=r"\[health\] is required"):
        pv.load_project(q)
    (q / "project.toml").write_text(good.replace("[ask]\n", "").replace("[[ask.options]]", "[[ask]]\n[[ask.options]]"))
    with pytest.raises(pv.ProjectConfigError, match="single table"):
        pv.load_project(q)
    assert pv._date(dt.datetime(2026, 1, 2, 3)) == dt.date(2026, 1, 2)
    assert pv._date(dt.date(2026, 1, 2)) == dt.date(2026, 1, 2)
    assert pv._date(5) is None


def test_unreadable_register_entry_and_untouched_workstream(tmp_path):
    q = make_rich(tmp_path)
    (q / "ready" / "b-queued.md").unlink()
    (q / "ready" / "b-open.md").unlink()
    (q / "in-progress" / "b-active.md").rename(q / "ready" / "b-active.md")
    m = pv.build_model(q, pv.Signals(now=NOW))
    assert {w["id"]: w["status"] for w in m["workstreams"]}["w"] == "active"
    only_ready = pv._rollup([b for b in pv.build_board.load_briefs(q) if b["id"] == "b-active"])
    assert only_ready["status"] == "not-started" or only_ready["ticked"]


def test_delta_covers_every_line_kind(tmp_path):
    m = build(tmp_path)
    base = pv.snapshot(m)
    base.update(criteria=[m["totals"]["ticked"] + 2, m["totals"]["total"]], prs_merged=[], health="red")
    now = dict(m, prs=[{"key": "Panaseer/demo#1", "state": "MERGED"}])
    lines = pv.delta(now, base)["lines"]
    assert any("no longer met" in x for x in lines) and any(x.startswith("Merged:") for x in lines)
    assert any("Health moved" in x for x in lines)
    same = pv.delta(m, pv.snapshot(m))
    assert same["note"].startswith("Nothing has moved")


def test_evidence_with_no_rules_is_pending():
    assert pv._evidence_cell([], {}, {})["state"] == "pending"


def test_queued_only_is_not_started_and_withdrawn_requirements_are_skipped(tmp_path):
    brief = {"id": "x", "dir": "ready", "crit_done": 0, "crit_total": 2, "title": "X"}
    assert pv._rollup([brief])["status"] == "not-started"
    q = make_rich(tmp_path)
    reg = q / "requirements.md"
    reg.write_text(reg.read_text().replace('"Nobody covers this"', '"Nobody covers this (withdrawn)"'))
    monkey = pv.parse_register(reg.read_text())
    assert monkey
    w = type("W", (), {"withdrawn": True, "id": "RCH-R003", "quote": "q", "title": "t", "kind": "k"})()
    real = pv.parse_register
    pv.parse_register = lambda text: [w]
    try:
        assert pv._requirements(q, [])["items"] == []
    finally:
        pv.parse_register = real


BANNED = ("brief", "criteria met", "layer", "machine-checked", "pending", "not set", "unknown", "no target date")


def test_rendered_page_avoids_internal_vocabulary_and_ids(tmp_path):
    import re
    m = build(tmp_path, prs={"Panaseer/demo#1": pv.PrState("Panaseer/demo#1", "OPEN", "green", NOW)})
    html = pv.render_fragment(m)
    visible = re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible).lower()
    for word in BANNED:
        assert word not in visible, word
    ids = [x["id"] for x in m["milestones"]] + [w["id"] for w in m["workstreams"]]
    assert not [i for i in ids if len(i) > 2 and re.search(rf"\b{re.escape(i)}\b", visible)]


def test_tabs_render_every_panel_and_the_status_words_are_the_three(tmp_path):
    html = pv.render_fragment(build(tmp_path))
    for token in ("plan", "how-it-works", "decisions", "links"):
        assert f'data-tab="{token}"' in html and f'id="panel-{token}"' in html
    assert html.count(" hidden>") == 3
    for word in ("Verified", "Claimed", "Queued", "Parked", "Uncovered", "Unverified"):
        assert f">{word}<" not in html


def test_milestone_shows_date_only_when_set_and_check_count_only_inside(tmp_path):
    from tests.project_fixtures import make_queue
    dated = pv.render_fragment(pv.build_model(make_queue(tmp_path / "d", due='due = "2026-12-01"\n'), pv.Signals(now=NOW)))
    plain = pv.render_fragment(pv.build_model(make_queue(tmp_path / "p"), pv.Signals(now=NOW)))
    assert "Due 1 Dec 2026" in dated and "Due " not in plain
    assert "checks done" in plain.split('role="tabpanel"', 1)[1] and "checks done" not in plain.split('role="tabpanel"', 1)[0]


def test_proposed_adr_yields_its_recommendation(tmp_path):
    heading = tmp_path / "0034-a.md"
    heading.write_text("# 34. Score feedback\n\n| Status | Proposed |\n\n"
                       "### Option A, score each artifact per tenant (proposed)\n\n### Option B, weight requesters\n")
    prose = tmp_path / "0055-b.md"
    prose.write_text("# 55. Extraction\n\n| Status | Proposed |\n\nRecommended, not yet decided: a hybrid of dlt and tasks.\n")
    assert pv._read_decision(heading)["recommended"] == "score each artifact per tenant"
    assert pv._read_decision(prose)["recommended"] == "a hybrid of dlt and tasks."

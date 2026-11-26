"""The project page reads health, links, releases and membership from the recorded facts."""

import datetime as dt
import pathlib

import build_board
import project_view as pv
import pytest

from tests.project_fixtures import make_queue, make_rich

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)


def model(q, now=NOW):
    return pv.build_model(q, pv.Signals(now=now))


def record_health(q: pathlib.Path, colour: str, day: str, reason: str = "Row reason.") -> None:
    folder = q / "history" / "health"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day}-aaaaaaaa.toml").write_text(
        f'colour = "{colour}"\nreason = "{reason}"\nset_by = "Row owner"\nset_on = {day}\n'
    )


def drop_legacy_health(q: pathlib.Path) -> None:
    text = (q / "project.toml").read_text()
    start = text.index("[health]")
    end = text.index("[[milestones]]")
    (q / "project.toml").write_text(text[:start] + text[end:])


def edit_project(q: pathlib.Path, old: str, new: str) -> None:
    toml = q / "project.toml"
    assert old in toml.read_text()
    toml.write_text(toml.read_text().replace(old, new, 1))


# --- health ------------------------------------------------------------------------------


def test_the_newest_history_row_is_the_owner_health_over_the_legacy_table(tmp_path):
    q = make_queue(tmp_path)
    record_health(q, "red", "2026-10-23")
    h = model(q)["health"]
    assert (h["owner"], h["reason"], h["set_by"], h["set_on"]) == (
        "red",
        "Row reason.",
        "Row owner",
        "2026-10-23",
    )
    assert h["effective"] == "red"


def test_a_dated_legacy_table_competes_with_the_rows_by_its_date(tmp_path):
    q = make_queue(tmp_path, colour="amber")  # the legacy table is dated 2026-10-22
    assert model(q)["health"]["owner"] == "amber"
    record_health(q, "red", "2026-10-21")
    assert model(q)["health"]["owner"] == "amber"
    record_health(q, "green", "2026-10-22")
    assert model(q)["health"]["owner"] == "green"  # same day: a recorded row outranks the baseline


def test_an_undated_legacy_table_is_the_oldest_row(tmp_path):
    q = make_queue(tmp_path, colour="amber")
    edit_project(q, 'set_on = "2026-10-22"\n', "")
    assert model(q)["health"]["owner"] == "amber"
    record_health(q, "red", "2026-01-01")
    assert model(q)["health"]["owner"] == "red"


def test_health_comes_from_history_alone_when_project_toml_has_no_table(tmp_path):
    q = make_queue(tmp_path)
    drop_legacy_health(q)
    record_health(q, "green", "2026-10-22")
    assert model(q)["health"]["owner"] == "green"


@pytest.mark.parametrize(
    "set_on,stale",
    [
        ("2026-10-23", False),  # today
        ("2026-10-17", False),  # 6 days ago: fresh
        ("2026-10-16", True),  # 7 days ago: stale
        ("2026-10-01", True),
    ],
)
def test_owner_health_is_stale_from_the_seventh_day(tmp_path, set_on, stale):
    q = make_queue(tmp_path)
    drop_legacy_health(q)
    record_health(q, "green", set_on)
    m = model(q)
    assert m["health"]["owner_stale"] is stale
    assert ("not reconfirmed recently" in pv.render_fragment(m)) is stale


def test_the_stale_limit_is_seven_days():
    assert pv.OWNER_STALE_DAYS == 7


def test_a_row_that_cannot_be_read_is_ignored_with_a_warning(tmp_path, capsys):
    q = make_queue(tmp_path)
    record_health(q, "mauve", "2026-10-23")
    assert model(q)["health"]["owner"] == "green"
    assert "colour must be one of green, amber, red" in capsys.readouterr().err


def test_a_project_with_no_health_says_the_owner_has_not_set_one(tmp_path):
    q = make_queue(tmp_path)
    drop_legacy_health(q)
    m = model(q)
    h = m["health"]
    assert (h["owner"], h["reason"], h["age_days"], h["owner_stale"]) == ("", "", None, False)
    assert (h["effective"], h["overridden"], h["capping"]) == ("green", False, [])
    html = pv.render_fragment(m)
    assert pv.NOT_SET in html and "set by" not in html and "Updated" not in html


def test_automatic_checks_still_set_the_colour_when_the_owner_has_not(tmp_path):
    q = make_queue(tmp_path, updated="2026-10-01")
    drop_legacy_health(q)
    h = model(q)["health"]
    assert h["effective"] == "amber" and h["owner"] == ""
    assert h["overridden"] is False and h["capping"] == []
    assert "Automatic checks also flagged" in pv.render_fragment(model(q))


def test_a_legacy_table_without_a_date_has_no_age(tmp_path):
    q = make_queue(tmp_path)
    edit_project(q, 'set_on = "2026-10-22"\n', "")
    h = model(q)["health"]
    assert (h["set_on"], h["age_days"], h["owner_stale"]) == ("", None, False)
    assert "set by Owner" in pv.render_fragment(model(q))


def test_read_project_has_no_health_key_when_none_is_recorded(tmp_path):
    q = make_queue(tmp_path)
    drop_legacy_health(q)
    assert "health" not in pv.read_project(q)


def test_read_project_exposes_the_current_row_as_its_health(tmp_path):
    q = make_queue(tmp_path)
    record_health(q, "amber", "2026-10-23")
    assert pv.read_project(q)["health"] == {
        "colour": "amber",
        "reason": "Row reason.",
        "set_by": "Row owner",
        "set_on": dt.date(2026, 10, 23),
    }


# --- links, initiative and releases ------------------------------------------------------


def test_the_links_table_becomes_named_rows(tmp_path):
    q = make_queue(tmp_path)
    (q / "project.toml").write_text(
        (q / "project.toml").read_text() + '\n[links]\nrepo = "https://git.example.test/r"\n'
    )
    assert model(q)["links"] == [{"label": "repo", "url": "https://git.example.test/r"}]


def test_legacy_link_rows_are_kept_as_they_are(tmp_path):
    q = make_rich(tmp_path)
    links = model(q)["links"]
    assert [x["label"] for x in links] == ["Env", "Doc", "Repo"]


def test_legacy_link_rows_that_are_not_tables_are_dropped(tmp_path):
    q = make_queue(tmp_path)
    (q / "project.toml").write_text(
        'links = ["x", {label = "Doc", url = "https://example.test/d"}]\n'
        + (q / "project.toml").read_text()
    )
    assert model(q)["links"] == [{"label": "Doc", "url": "https://example.test/d"}]


def test_no_links_is_an_empty_list(tmp_path):
    assert model(make_queue(tmp_path))["links"] == []


def test_a_link_that_is_not_https_is_left_out_with_a_warning(tmp_path, capsys):
    q = make_queue(tmp_path)
    (q / "project.toml").write_text(
        (q / "project.toml").read_text() + '\n[links]\nbad = "http://x.test"\n'
    )
    assert model(q)["links"] == []
    assert "link 'bad': url must be an https link" in capsys.readouterr().err


def test_initiative_and_releases_are_in_the_model(tmp_path):
    q = make_queue(tmp_path)
    edit_project(q, "[project]\n", '[project]\ninitiative = "Grow the demo"\n')
    (q / "project.toml").write_text(
        (q / "project.toml").read_text() + '\n[[releases]]\nid = "2026.12"\ndate = 2026-12-15\n'
    )
    m = model(q)
    assert m["initiative"] == ""  # a top-level key, not one under [project]


def test_a_top_level_initiative_is_read_and_absent_means_empty(tmp_path):
    q = make_queue(tmp_path)
    assert model(q)["initiative"] == "" and model(q)["releases"] == []
    toml = q / "project.toml"
    toml.write_text(
        'initiative = "Grow the demo"\n\n[[releases]]\nid = "2026.12"\ndate = 2026-12-15\n'
        + toml.read_text()
    )
    m = model(q)
    assert m["initiative"] == "Grow the demo"
    assert m["releases"] == [{"id": "2026.12", "date": "2026-12-15"}]


# --- membership --------------------------------------------------------------------------


def name_architecture(q: pathlib.Path, folder: str, brief: str, node: str) -> None:
    path = q / folder / f"{brief}.md"
    path.write_text(path.read_text().replace("domain: demo", f"domain: demo\narchitecture: {node}"))


def node_statuses(m) -> dict[str, str]:
    seen: dict[str, str] = {}

    def walk(nodes):
        for n in nodes:
            seen[n["id"]] = n["status"]
            walk(n["children"])

    walk(m["architecture"])
    return seen


def test_a_brief_belongs_to_the_architecture_node_it_names(tmp_path, capsys):
    q = make_rich(tmp_path)
    name_architecture(q, "ready", "b-open", "part")
    name_architecture(q, "in-progress", "b-active", "box")
    statuses = node_statuses(model(q))
    assert statuses["part"] == "not-started" and statuses["box"] == "active"
    assert statuses["sys"] == "done"  # the legacy list still names b-done


def test_a_node_with_no_brief_naming_it_is_unmapped(tmp_path):
    assert node_statuses(model(make_rich(tmp_path)))["part"] == "unmapped"


def test_a_node_listing_briefs_warns_that_the_list_is_deprecated(tmp_path, capsys):
    model(make_rich(tmp_path))
    err = capsys.readouterr().err.splitlines()
    assert any(
        "architecture 'sys' lists briefs, which is deprecated; give each brief `architecture: sys`"
        in line
        for line in err
    )


def test_a_brief_named_by_both_the_list_and_its_frontmatter_counts_once(tmp_path):
    q = make_rich(tmp_path)
    name_architecture(q, "done", "b-done", "sys")
    assert node_statuses(model(q))["sys"] == "done"


# --- rendering edges the facts change leaves reachable ------------------------------------


def test_only_checkbox_lines_of_a_brief_are_criteria():
    assert pv._criteria({"crit": "- [x] a\nprose between\n* [ ] b\n"}) == [
        {"done": True, "text": "a"},
        {"done": False, "text": "b"},
    ]


def test_a_milestone_with_no_criteria_shows_no_checklist(tmp_path):
    m = model(make_queue(tmp_path))
    ms = {**m["milestones"][0], "criteria": []}
    html = pv._milestone(ms, {}, m)
    assert "checks done" not in html and ms["reader_title"] in html


def test_a_card_for_a_brief_with_no_goal_or_criteria_has_no_body(tmp_path):
    brief = {**build_board.load_briefs(make_queue(tmp_path))[0], "goal": "", "crit": ""}
    assert 'class="body"' not in build_board.card_html(brief)


# --- feature rollout ---------------------------------------------------------------------


def name_a_key_in_the_first_milestone(q: pathlib.Path) -> None:
    edit_project(q, 'plain = "The first thing works"', 'plain = "The first thing works KEY-12"')
    with (q / "project.toml").open("a") as f:
        f.write(
            '\n[[jira]]\nkey = "KEY-12"\ntitle = "The feature"\nurl = "https://example.test/KEY-12"\n'
        )


def record_rollout(
    q: pathlib.Path, key: str = "KEY-12", on: tuple[bool, ...] = (True, False)
) -> None:
    folder = q / "history" / "rollout"
    folder.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f'\n[[customer]]\ncode = "c{n}"\nrelease = "{"r1" if n < 2 else ""}"\non = {str(state).lower()}\n'
        for n, state in enumerate(on)
    )
    (folder / f"{key}-2026-10-20-aaaaaaaa.toml").write_text(
        f'key = "{key}"\nrecorded_on = 2026-10-20\n{rows}'
    )


def test_a_milestone_ref_to_a_jira_key_shows_its_rollout_counts_and_missing_customers(tmp_path):
    q = make_queue(tmp_path)
    name_a_key_in_the_first_milestone(q)
    record_rollout(q, on=(True, False, False))

    html = pv.render_fragment(model(q))

    assert "on: some 1/3, released 2/3" in html
    assert "not on: c1, c2" in html


def test_the_model_lists_the_rollout_of_each_jira_key(tmp_path):
    q = make_queue(tmp_path)
    name_a_key_in_the_first_milestone(q)
    record_rollout(q)
    record_rollout(q, "KEY-77")

    assert list(model(q)["feature_rollouts"]) == ["KEY-12"]


def test_a_jira_key_with_no_rollout_history_renders_exactly_as_before(tmp_path):
    q = make_queue(tmp_path)
    name_a_key_in_the_first_milestone(q)
    before = pv.render_fragment(model(q))

    record_rollout(q, "KEY-77")

    assert pv.render_fragment(model(q)) == before
    assert "released" not in before and model(q)["feature_rollouts"] == {}

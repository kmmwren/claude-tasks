"""The facts a queue records beside its briefs: typed rows, derived current values, tolerant reads."""

import datetime as dt
import pathlib

import facts
import pytest

D = dt.date


def put(queue: pathlib.Path, rel: str, text: str) -> pathlib.Path:
    path = queue / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def health(colour="green", reason="Going to plan.", set_on="2026-11-01", by="Owner") -> str:
    return f'colour = "{colour}"\nreason = "{reason}"\nset_by = "{by}"\nset_on = {set_on}\n'


def due(milestone="m1", day="2026-12-01", set_on="2026-11-01", reason="Scope grew.") -> str:
    return (
        f'milestone = "{milestone}"\ndue = {day}\nset_on = {set_on}\n'
        f'set_by = "Owner"\nreason = "{reason}"\n'
    )


def ci(state="green", link="https://ci.example.test/run/1", set_on="2026-11-01") -> str:
    return f'state = "{state}"\nlink = "{link}"\nset_on = {set_on}\n'


def rollout(key="KEY-12", recorded_on="2026-11-01", customers=(("acme", "r1", True),)) -> str:
    rows = "".join(
        f'\n[[customer]]\ncode = "{code}"\nrelease = "{release}"\non = {str(on).lower()}\n'
        for code, release, on in customers
    )
    return f'key = "{key}"\nrecorded_on = {recorded_on}\n{rows}'


FLAG = '[[flag]]\nname = "f"\nenvironment = "dev"\nstate = "{state}"\nchanged_on = {day}\n'
EVIDENCE = (
    '[[evidence]]\nkind = "screenshot"\nlink = "https://img.example.test/a.png"\n'
    'caption = "The page"\naccess = "org"\nrecorded_on = 2026-11-02\n'
)


@pytest.fixture
def queue(tmp_path) -> pathlib.Path:
    return tmp_path


def warnings(capsys) -> list[str]:
    return capsys.readouterr().err.splitlines()


# --- field readers -----------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 5, "", "   "])
def test_text_must_be_non_empty_text(value):
    with pytest.raises(facts.RowError, match="k must be non-empty text"):
        facts._text({"k": value}, "k")


def test_text_is_stripped():
    assert facts._text({"k": "  hi "}, "k") == "hi"


def test_choice_names_the_allowed_values():
    assert facts._choice({"k": "red"}, "k", facts.COLOURS) == "red"
    with pytest.raises(facts.RowError, match="k must be one of green, amber, red"):
        facts._choice({"k": "blue"}, "k", facts.COLOURS)


@pytest.mark.parametrize("value", ["http://x.test", "https://", "ftp://x", "x.test"])
def test_https_refuses_anything_but_an_https_link(value):
    with pytest.raises(facts.RowError, match="k must be an https link"):
        facts._https({"k": value}, "k")


def test_https_accepts_any_case_of_the_scheme():
    assert facts._https({"k": "HTTPS://x.test"}, "k") == "HTTPS://x.test"


def test_day_reads_toml_dates_and_iso_text_and_refuses_the_rest():
    assert facts._day({"k": D(2026, 1, 2)}, "k") == D(2026, 1, 2)
    assert facts._day({"k": "2026-01-02"}, "k") == D(2026, 1, 2)
    with pytest.raises(facts.RowError, match="k must be a YYYY-MM-DD date"):
        facts._day({"k": "soon"}, "k")


def test_optional_text_is_none_unless_there_is_text():
    assert facts._optional_text({"k": " v "}, "k") == "v"
    assert facts._optional_text({"k": "  "}, "k") is None
    assert facts._optional_text({"k": 3}, "k") is None
    assert facts._optional_text({}, "k") is None


# --- tolerant reads ----------------------------------------------------------------------


def test_a_missing_file_is_not_recorded_and_silent(queue, capsys):
    assert facts._read(queue / "absent.toml") is None
    assert warnings(capsys) == []


def test_a_file_that_is_not_toml_is_left_out_and_named(queue, capsys):
    path = put(queue, "bad.toml", "not = [valid")
    assert facts._read(path) is None
    (line,) = warnings(capsys)
    assert line.startswith(f"warning: {path}: cannot be read") and line.endswith("it is left out")


def test_a_file_that_is_not_text_is_left_out_and_named(queue, capsys):
    path = queue / "bin.toml"
    path.write_bytes(b"\xff\xfe\x00")
    assert facts._read(path) is None
    assert f"{path}: cannot be read" in warnings(capsys)[0]


def test_a_path_that_cannot_be_opened_is_left_out_and_named(queue, capsys):
    (queue / "dir.toml").mkdir()
    assert facts._read(queue / "dir.toml") is None
    assert "dir.toml: cannot be read" in warnings(capsys)[0]


def test_rows_that_are_not_tables_are_left_out_whole(queue, capsys):
    path = queue / "f.toml"
    assert facts._rows(path, {"flag": "oops"}, "flag", facts.Flag.parse) == []
    assert warnings(capsys) == [f"warning: {path}: flag must be [[flag]] tables; they are left out"]


def test_a_bad_row_is_named_by_number_and_the_others_kept(queue, capsys):
    path = queue / "f.toml"
    rows = [{"id": "r1", "date": "2026-12-01"}, {"id": ""}, {"id": "r3", "date": "x"}]
    got = facts._rows(path, {"releases": rows}, "releases", facts.Release.parse)
    assert got == [facts.Release("r1", D(2026, 12, 1))]
    assert warnings(capsys) == [
        f"warning: {path}: releases row 2: id must be non-empty text; that row is left out",
        f"warning: {path}: releases row 3: date must be a YYYY-MM-DD date; that row is left out",
    ]


def test_an_absent_key_has_no_rows(queue, capsys):
    assert facts._rows(queue / "f.toml", {}, "flag", facts.Flag.parse) == []
    assert warnings(capsys) == []


@pytest.mark.parametrize(
    "parse",
    [
        lambda: facts.HealthRow.parse("x", "s", True),
        lambda: facts.DueRow.parse("x", "s"),
        lambda: facts.Flag.parse("x"),
        lambda: facts.Evidence.parse("x"),
        lambda: facts.CiRow.parse("x", "s"),
        lambda: facts.Release.parse("x"),
    ],
)
def test_every_row_refuses_a_value_that_is_not_a_table(parse):
    with pytest.raises(facts.RowError, match="not a table"):
        parse()


# --- health ------------------------------------------------------------------------------


def test_no_health_recorded_is_none(queue):
    assert facts.load_health(queue) == []
    assert facts.current_health(queue) is None


def test_the_legacy_health_table_is_the_oldest_row_and_may_omit_its_date(queue):
    put(queue, "project.toml", '[health]\ncolour = "amber"\nreason = "Legacy."\n')
    put(queue, "history/health/2026-11-01-aaaaaaaa.toml", health("green"))
    rows = facts.load_health(queue)
    assert [(r.colour, r.set_on, r.source) for r in rows] == [
        ("amber", None, "project.toml"),
        ("green", D(2026, 11, 1), "2026-11-01-aaaaaaaa.toml"),
    ]
    assert facts.current_health(queue) == rows[-1]


def test_the_legacy_health_table_is_the_current_one_when_it_is_the_only_row(queue):
    put(queue, "project.toml", "[health]\n" + health("red", set_on="2026-09-01"))
    current = facts.current_health(queue)
    assert current is not None and (current.colour, current.set_on) == ("red", D(2026, 9, 1))


def test_the_newest_date_wins_then_the_later_file_name(queue):
    put(queue, "history/health/2026-11-03-bbbbbbbb.toml", health("red", set_on="2026-11-03"))
    put(queue, "history/health/2026-11-02-zzzzzzzz.toml", health("green", set_on="2026-11-02"))
    put(queue, "history/health/2026-11-03-aaaaaaaa.toml", health("amber", set_on="2026-11-03"))
    current = facts.current_health(queue)
    assert current is not None and current.colour == "red"
    assert [r.source for r in facts.load_health(queue)] == [
        "2026-11-02-zzzzzzzz.toml",
        "2026-11-03-aaaaaaaa.toml",
        "2026-11-03-bbbbbbbb.toml",
    ]


def test_a_history_row_without_a_date_is_left_out_and_named(queue, capsys):
    path = put(queue, "history/health/x.toml", 'colour = "red"\nreason = "r"\n')
    assert facts.load_health(queue) == []
    assert warnings(capsys) == [
        f"warning: {path}: health: set_on must be a YYYY-MM-DD date; that row is left out"
    ]


def test_an_unreadable_history_row_is_left_out(queue, capsys):
    put(queue, "history/health/x.toml", "not = [valid")
    assert facts.load_health(queue) == []
    assert "cannot be read" in warnings(capsys)[0]


def test_a_bad_legacy_table_is_left_out_and_named(queue, capsys):
    put(queue, "project.toml", '[health]\ncolour = "mauve"\nreason = "r"\n')
    assert facts.load_health(queue) == []
    assert "project.toml: health: colour must be one of" in warnings(capsys)[0]


def test_a_dated_legacy_table_keeps_its_date(queue):
    put(queue, "project.toml", "[health]\n" + health())
    (row,) = facts.load_health(queue)
    assert row.set_on == D(2026, 11, 1)


def test_a_health_row_writes_a_body_that_parses_back():
    dated = facts.HealthRow("amber", "r", "me", D(2026, 11, 1), "s")
    undated = facts.HealthRow("amber", "r", "me", None, "s")
    assert facts.HealthRow.parse(dated.table(), "s", True) == dated
    assert facts.HealthRow.parse(undated.table(), "s", False) == undated
    assert "set_on" not in undated.table()


def test_a_missing_set_by_is_empty_text():
    table = {"colour": "green", "reason": "r", "set_on": D(2026, 1, 1)}
    assert facts.HealthRow.parse(table, "s", True).set_by == ""


# --- due dates and slip ------------------------------------------------------------------


def test_due_rows_are_grouped_by_milestone_and_ordered_oldest_first(queue):
    put(
        queue, "history/due/m1-2026-11-05-bbbbbbbb.toml", due(day="2026-12-20", set_on="2026-11-05")
    )
    put(
        queue, "history/due/m1-2026-11-01-aaaaaaaa.toml", due(day="2026-12-10", set_on="2026-11-01")
    )
    put(queue, "history/due/m2-2026-11-02-cccccccc.toml", due("m2", "2027-01-01", "2026-11-02"))
    got = facts.load_due_rows(queue)
    assert {m: [r.due for r in rows] for m, rows in got.items()} == {
        "m1": [D(2026, 12, 10), D(2026, 12, 20)],
        "m2": [D(2027, 1, 1)],
    }


def test_a_due_row_that_cannot_be_used_is_left_out_and_named(queue, capsys):
    put(queue, "history/due/a.toml", "not = [valid")
    path = put(queue, "history/due/b.toml", 'milestone = "m1"\nreason = "r"\n')
    assert facts.load_due_rows(queue) == {}
    first, second = warnings(capsys)
    assert "cannot be read" in first
    assert f"{path}: due: due must be a YYYY-MM-DD date; that row is left out" in second


def test_a_due_row_writes_a_body_that_parses_back():
    row = facts.DueRow("m1", D(2026, 12, 1), D(2026, 11, 1), "me", "why", "s")
    assert facts.DueRow.parse(row.table(), "s") == row


def test_baselines_come_from_project_milestones_and_skip_what_is_unusable(queue):
    put(
        queue,
        "project.toml",
        '[[milestones]]\nid = "m1"\ndue = 2026-12-01\n[[milestones]]\nid = "m2"\n'
        "[[milestones]]\nid = 7\n[[milestones]]\ndue = 2026-12-01\n",
    )
    assert facts.baseline_dates(queue) == {"m1": D(2026, 12, 1), "m2": None}


def test_baselines_ignore_a_milestones_key_that_is_not_a_list(queue):
    put(queue, "project.toml", 'milestones = "x"\n')
    assert facts.baseline_dates(queue) == {}


def test_baselines_ignore_milestone_entries_that_are_not_tables(queue):
    put(queue, "project.toml", 'milestones = ["x", 3]\n')
    assert facts.baseline_dates(queue) == {}


def test_no_project_file_means_no_baselines(queue):
    assert facts.baseline_dates(queue) == {}


def test_a_schedule_with_no_dates_has_no_current_date_and_no_slip():
    s = facts.Schedule("m1", None)
    assert (s.current, s.slip_days) == (None, None)


def test_a_schedule_without_a_baseline_has_no_slip_even_with_a_forecast():
    row = facts.DueRow("m1", D(2026, 12, 5), D(2026, 11, 1), "", "r", "s")
    s = facts.Schedule("m1", None, (row,))
    assert (s.current, s.slip_days) == (D(2026, 12, 5), None)


@pytest.mark.parametrize(
    "forecast,slip",
    [
        (D(2026, 11, 30), -1),
        (D(2026, 12, 1), 0),  # on the first date: no slip
        (D(2026, 12, 2), 1),  # the first day past it counts, with no grace
        (D(2026, 12, 31), 30),
    ],
)
def test_slip_counts_from_the_first_day_past_the_first_due_date(forecast, slip):
    row = facts.DueRow("m1", forecast, D(2026, 11, 1), "", "r", "s")
    assert facts.Schedule("m1", D(2026, 12, 1), (row,)).slip_days == slip


def test_the_baseline_alone_is_the_current_date_with_no_slip():
    s = facts.Schedule("m1", D(2026, 12, 1))
    assert (s.current, s.slip_days) == (D(2026, 12, 1), 0)


def test_the_newest_forecast_is_the_current_date(queue):
    put(
        queue,
        "project.toml",
        '[[milestones]]\nid = "m1"\ndue = 2026-12-01\n[[milestones]]\nid = "m2"\ndue = 2026-12-01\n',
    )
    put(queue, "history/due/m1-a.toml", due(day="2026-12-10", set_on="2026-11-01"))
    put(queue, "history/due/m1-b.toml", due(day="2026-12-04", set_on="2026-11-09"))
    put(queue, "history/due/m3-a.toml", due("m3", "2027-02-01", "2026-11-09"))
    got = facts.load_schedules(queue)
    assert sorted(got) == ["m1", "m2", "m3"]
    assert (got["m1"].current, got["m1"].slip_days) == (D(2026, 12, 4), 3)
    assert (got["m2"].current, got["m2"].slip_days) == (D(2026, 12, 1), 0)
    assert (got["m3"].baseline, got["m3"].slip_days) == (None, None)


# --- per-brief facts ---------------------------------------------------------------------


def test_a_flag_and_an_evidence_row_write_bodies_that_parse_back():
    flag = facts.Flag("f", "dev", "on", D(2026, 11, 1))
    ev = facts.Evidence("video", "https://v.example.test/1", "Demo", "public", D(2026, 11, 2))
    assert facts.Flag.parse(flag.table()) == flag
    assert facts.Evidence.parse(ev.table()) == ev


def test_the_current_flag_is_the_newest_row_per_name_and_environment():
    flags = (
        facts.Flag("f", "dev", "off", D(2026, 11, 1)),
        facts.Flag("f", "prod", "off", D(2026, 11, 1)),
        facts.Flag("f", "dev", "on", D(2026, 11, 5)),
        facts.Flag("f", "dev", "off", D(2026, 11, 3)),
    )
    got = facts.BriefFacts("b", flags).current_flags
    assert {(f.environment, f.state) for f in got} == {("dev", "on"), ("prod", "off")}


def test_a_later_row_of_the_same_date_wins():
    flags = (
        facts.Flag("f", "dev", "off", D(2026, 11, 1)),
        facts.Flag("f", "dev", "on", D(2026, 11, 1)),
    )
    (only,) = facts.BriefFacts("b", flags).current_flags
    assert only.state == "on"


def test_no_flags_means_no_current_flags():
    assert facts.BriefFacts("b").current_flags == ()


def test_a_facts_file_is_read_into_typed_rows(queue):
    put(
        queue,
        "facts/b1.toml",
        'brief = "b1"\nshipped = " v1 "\nshipped_on = 2026-11-10\nrelease = "2026.12"\n'
        + FLAG.format(state="on", day="2026-11-04")
        + EVIDENCE,
    )
    got = facts.load_brief_facts(queue, "b1")
    assert got == facts.BriefFacts(
        "b1",
        (facts.Flag("f", "dev", "on", D(2026, 11, 4)),),
        (
            facts.Evidence(
                "screenshot", "https://img.example.test/a.png", "The page", "org", D(2026, 11, 2)
            ),
        ),
        "v1",
        D(2026, 11, 10),
        "2026.12",
    )


def test_a_facts_file_with_only_a_brief_has_nothing_recorded(queue):
    put(queue, "facts/b1.toml", 'brief = "b1"\n')
    assert facts.load_brief_facts(queue, "b1") == facts.BriefFacts("b1")


def test_a_bad_row_in_a_facts_file_leaves_out_only_that_row(queue, capsys):
    path = put(
        queue,
        "facts/b1.toml",
        'brief = "b1"\n' + FLAG.format(state="on", day="2026-11-04") + '[[flag]]\nname = "g"\n',
    )
    got = facts.load_brief_facts(queue, "b1")
    assert [f.name for f in got.flags] == ["f"]
    assert warnings(capsys) == [
        f"warning: {path}: flag row 2: environment must be non-empty text; that row is left out"
    ]


def test_no_facts_file_means_empty_facts(queue, capsys):
    assert facts.load_brief_facts(queue, "b1") == facts.BriefFacts("b1")
    assert warnings(capsys) == []


def test_a_facts_file_naming_another_brief_is_left_out(queue, capsys):
    path = put(queue, "facts/b1.toml", 'brief = "other"\n' + EVIDENCE)
    assert facts.load_brief_facts(queue, "b1") == facts.BriefFacts("b1")
    assert warnings(capsys) == [f"warning: {path}: `brief` must be 'b1'; the file is left out"]


def test_a_facts_file_that_is_not_toml_is_empty_facts(queue, capsys):
    put(queue, "facts/b1.toml", "not = [valid")
    assert facts.load_brief_facts(queue, "b1") == facts.BriefFacts("b1")
    assert "cannot be read" in warnings(capsys)[0]


def test_every_facts_file_is_loaded_by_brief_id(queue):
    put(queue, "facts/b2.toml", 'brief = "b2"\n' + EVIDENCE)
    put(queue, "facts/b1.toml", 'brief = "b1"\n')
    got = facts.load_facts(queue)
    assert list(got) == ["b1", "b2"] and got["b2"].evidence and not got["b1"].evidence


def test_no_facts_directory_means_no_facts(queue):
    assert facts.load_facts(queue) == {}


def test_a_brief_lacks_evidence_until_a_row_is_recorded(queue):
    assert facts.lacks_evidence(queue, "b1")
    put(queue, "facts/b1.toml", 'brief = "b1"\n')
    assert facts.lacks_evidence(queue, "b1")
    put(queue, "facts/b1.toml", 'brief = "b1"\n' + EVIDENCE)
    assert not facts.lacks_evidence(queue, "b1")


def test_the_declared_brief_is_what_the_file_names(queue):
    assert facts.declared_brief(put(queue, "a.toml", 'brief = "x"\n')) == "x"
    assert facts.declared_brief(put(queue, "b.toml", "brief = 3\n")) is None
    assert facts.declared_brief(put(queue, "c.toml", "")) is None


def test_the_declared_brief_raises_on_a_file_that_is_not_toml(queue):
    import tomllib

    with pytest.raises(tomllib.TOMLDecodeError):
        facts.declared_brief(put(queue, "d.toml", "not = [valid"))


def test_the_facts_path_is_under_facts(queue):
    assert facts.facts_path(queue, "b1") == queue / "facts" / "b1.toml"


@pytest.mark.parametrize(
    "key,value,message",
    [
        ("kind", "gif", "kind must be one of screenshot, video"),
        ("link", "http://x.test", "link must be an https link"),
        ("access", "world", "access must be one of public, org, private"),
        ("recorded_on", "x", "recorded_on must be a YYYY-MM-DD date"),
        ("caption", "", "caption must be non-empty text"),
    ],
)
def test_evidence_is_checked_field_by_field(key, value, message):
    table = {
        "kind": "screenshot",
        "link": "https://x.test/a",
        "caption": "c",
        "access": "org",
        "recorded_on": D(2026, 1, 1),
        key: value,
    }
    with pytest.raises(facts.RowError, match=message):
        facts.Evidence.parse(table)


# --- continuous integration --------------------------------------------------------------


def test_the_newest_ci_reading_is_current(queue):
    put(queue, "history/ci/2026-11-03-bbbbbbbb.toml", ci("red", set_on="2026-11-03"))
    put(queue, "history/ci/2026-11-01-aaaaaaaa.toml", ci("green", set_on="2026-11-01"))
    assert [r.state for r in facts.load_ci(queue)] == ["green", "red"]
    current = facts.current_ci(queue)
    assert current is not None and current.state == "red"


def test_no_ci_recorded_is_none(queue):
    assert facts.load_ci(queue) == [] and facts.current_ci(queue) is None


def test_a_ci_row_that_cannot_be_used_is_left_out_and_named(queue, capsys):
    put(queue, "history/ci/a.toml", "not = [valid")
    path = put(queue, "history/ci/b.toml", ci("amber"))
    assert facts.load_ci(queue) == []
    first, second = warnings(capsys)
    assert "cannot be read" in first
    assert f"{path}: ci: state must be one of green, red; that row is left out" in second


def test_a_ci_row_writes_a_body_that_parses_back():
    row = facts.CiRow("green", "https://ci.example.test/1", D(2026, 11, 1), "s")
    assert facts.CiRow.parse(row.table(), "s") == row


# --- project meta ------------------------------------------------------------------------


def test_project_meta_is_empty_without_a_project_file(queue):
    assert facts.load_project_meta(queue) == facts.ProjectMeta()


def test_project_meta_reads_initiative_links_and_releases(queue):
    put(
        queue,
        "project.toml",
        'initiative = " Grow "\n[links]\nrepo = "https://git.example.test/r"\n'
        '[[releases]]\nid = "2026.12"\ndate = 2026-12-15\n',
    )
    assert facts.load_project_meta(queue) == facts.ProjectMeta(
        "Grow",
        (("repo", "https://git.example.test/r"),),
        (facts.Release("2026.12", D(2026, 12, 15)),),
    )


def test_a_links_key_that_is_not_a_table_is_left_out_and_named(queue, capsys):
    put(queue, "project.toml", 'links = "x"\n')
    assert facts.load_project_meta(queue).links == ()
    assert warnings(capsys) == [
        f"warning: {queue / 'project.toml'}: links must be a [links] table; it is left out"
    ]


def test_a_link_that_is_not_https_is_left_out_and_named(queue, capsys):
    put(
        queue,
        "project.toml",
        '[links]\ngood = "https://x.test/a"\nbad = "http://x.test"\nworse = 3\n',
    )
    assert facts.load_project_meta(queue).links == (("good", "https://x.test/a"),)
    first, second = warnings(capsys)
    assert "link 'bad': url must be an https link; that link is left out" in first
    assert "link 'worse': url must be non-empty text; that link is left out" in second


def test_a_bad_release_is_left_out_and_named(queue, capsys):
    put(
        queue, "project.toml", '[[releases]]\nid = "a"\n[[releases]]\nid = "b"\ndate = 2026-12-15\n'
    )
    assert facts.load_project_meta(queue).releases == (facts.Release("b", D(2026, 12, 15)),)
    assert "releases row 1: date must be a YYYY-MM-DD date" in warnings(capsys)[0]


# --- feature rollout ---------------------------------------------------------------------

THREE = (("acme", "r2", True), ("globex", "r1", False), ("initech", "", False))


def test_a_rollout_history_file_parses_into_customers_in_code_order(queue):
    put(queue, "history/rollout/KEY-12-2026-11-01-aaaaaaaa.toml", rollout(customers=THREE[::-1]))
    (snap,) = facts.load_rollouts(queue).values()
    assert (snap.key, snap.recorded_on, snap.source) == (
        "KEY-12",
        D(2026, 11, 1),
        "KEY-12-2026-11-01-aaaaaaaa.toml",
    )
    assert snap.customers == (
        facts.CustomerRollout("acme", "r2", True),
        facts.CustomerRollout("globex", "r1", False),
        facts.CustomerRollout("initech", "", False),
    )


def test_the_newest_snapshot_of_each_key_wins(queue):
    put(queue, "history/rollout/KEY-12-2026-11-05-bbbbbbbb.toml", rollout(recorded_on="2026-11-05"))
    put(queue, "history/rollout/KEY-12-2026-11-01-cccccccc.toml", rollout(customers=THREE))
    put(queue, "history/rollout/KEY-9-2026-10-01-dddddddd.toml", rollout("KEY-9", "2026-10-01"))
    got = facts.load_rollouts(queue)
    assert list(got) == ["KEY-12", "KEY-9"]
    assert got["KEY-12"].recorded_on == D(2026, 11, 5) and len(got["KEY-12"].customers) == 1


def test_a_snapshot_dated_the_same_day_is_ordered_by_file_name(queue):
    put(queue, "history/rollout/KEY-12-2026-11-05-aaaaaaaa.toml", rollout(recorded_on="2026-11-05"))
    put(
        queue,
        "history/rollout/KEY-12-2026-11-05-zzzzzzzz.toml",
        rollout(recorded_on="2026-11-05", customers=THREE),
    )
    assert facts.load_rollouts(queue)["KEY-12"].source.endswith("zzzzzzzz.toml")


def test_counts_missing_codes_and_spread_are_derived_from_the_rows(queue):
    put(queue, "history/rollout/KEY-12-2026-11-01-aaaaaaaa.toml", rollout(customers=THREE))
    snap = facts.load_rollouts(queue)["KEY-12"]
    assert (snap.on_count, snap.released_count, snap.total) == (1, 2, 3)
    assert snap.missing == ("globex", "initech")
    assert snap.spread is facts.Spread.SOME
    assert snap.line() == "KEY-12 on: some 1/3, released 2/3"


@pytest.mark.parametrize(
    ("states", "spread"),
    [
        ([False, False], facts.Spread.NONE),
        ([True, False], facts.Spread.SOME),
        ([True, True], facts.Spread.ALL),
    ],
)
def test_spread_is_none_some_or_all(states, spread):
    customers = tuple(facts.CustomerRollout(f"c{n}", "", on) for n, on in enumerate(states))
    snap = facts.RolloutSnapshot("KEY-1", D(2026, 11, 1), customers, "s")
    assert snap.spread is spread


def test_a_rollout_snapshot_writes_a_body_that_parses_back():
    snap = facts.RolloutSnapshot(
        "KEY-12", D(2026, 11, 1), (facts.CustomerRollout("acme", "r1", True),), "s"
    )
    assert facts.RolloutSnapshot.parse(snap.table(), "s") == snap


def test_an_absent_rollout_folder_is_no_rollouts_and_no_warning(queue, capsys):
    assert facts.load_rollouts(queue) == {}
    assert warnings(capsys) == []


def test_a_rollout_file_that_cannot_be_used_is_left_out_and_named(queue, capsys):
    put(queue, "history/rollout/a.toml", "not = [valid")
    bad = put(queue, "history/rollout/b.toml", 'key = "KEY-1"\nrecorded_on = 2026-11-01\n')
    dup = put(
        queue,
        "history/rollout/c.toml",
        rollout(customers=(("acme", "", True), ("acme", "", False))),
    )
    put(queue, "history/rollout/d.toml", rollout("KEY-2").replace("on = true", 'on = "yes"'))
    assert facts.load_rollouts(queue) == {}
    first, second, third, fourth = warnings(capsys)
    assert "cannot be read" in first
    assert f"{bad}: rollout: customer must be [[customer]] tables; that row is left out" in second
    assert f"{dup}: rollout: customer code acme is listed twice; that row is left out" in third
    assert "on must be true or false" in fourth


def test_a_bad_snapshot_leaves_the_older_good_one_in_force(queue, capsys):
    put(queue, "history/rollout/KEY-12-2026-11-01-aaaaaaaa.toml", rollout())
    put(queue, "history/rollout/KEY-12-2026-11-09-bbbbbbbb.toml", "not = [valid")
    assert facts.load_rollouts(queue)["KEY-12"].recorded_on == D(2026, 11, 1)
    assert len(warnings(capsys)) == 1

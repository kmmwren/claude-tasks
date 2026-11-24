"""Roll-ups: sources, reading each from its GitHub queue, the three views, every surface."""

import json
import pathlib
import socket
import sys
import threading
import urllib.error
import urllib.request

import pytest
import queue_snapshot as qs
import rollup
import rollup_model as model
import rollup_views as rv
from _paths import ENV_VAR

from tests.project_fixtures import make_queue
from tests.remote_queue import PUSHED_AT, publish, snapshot
from tests.rollup_sources import NOW, projects, rollup_of, source_of, with_project_toml

NOWHERE = "/nowhere/claude-tasks-test-repo"
AS_AT = "2026-11-19T09:00:00+00:00"

BETA = """
[project]
title = "Beta | Two"
outcome = "Beta works."

[ask]
question = "Ship it?"
deadline = "2026-11-01"

[[milestones]]
id = "M0"
title = "Zero"
due = "2026-10-01"

[[milestones]]
id = "M1"
title = "Start"

[[milestones]]
id = "M2"
title = "Later"
"""


def brief(
    q: pathlib.Path,
    folder: str,
    bid: str,
    extra: str = "",
    updated: str = "2026-10-22",
    crit: str = "- [ ] open",
) -> None:
    (q / folder).mkdir(exist_ok=True)
    (q / folder / f"{bid}.md").write_text(
        f"---\nid: {bid}\ntitle: {bid.upper()}\nstatus: {folder}\ntype: feature\ndomain: demo\n"
        f"updated: {updated}\n{extra}---\n\n# {bid}\n\n## Success criteria\n{crit}\n"
    )


def alpha(clone: pathlib.Path) -> None:
    q = make_queue(clone)
    brief(q, "ready", "b3", "milestone: m2\nblocked_by: [external:vendor]\n")
    brief(q, "blocked", "b4", "milestone: m2\nblocked_by: [b2]\n")
    # b1 is done, so b5 waits for nothing
    brief(q, "blocked", "b5", "milestone: m2\nblocked_by: [b1]\n")


def beta(clone: pathlib.Path) -> None:
    q = clone / ".tasks"
    for d in ("inbox", "ready", "done"):
        (q / d).mkdir(parents=True)
    (q / "tasks.toml").write_text('name = "Beta"\n')
    (q / "project.toml").write_text(BETA)
    brief(q, "done", "c1", "milestone: M1\n", updated="2026-09-01", crit="- [x] ok")
    brief(q, "ready", "c2", "milestone: M0\n", updated="2026-09-01")


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    """Two queues on two bare remotes, pushed once; no test changes them."""
    root = tmp_path_factory.mktemp("remotes")
    return [
        source_of(root / "alpha", alpha, "Alpha"),
        source_of(root / "beta", beta, "Beta"),
    ]


def built(work):
    return rollup.build(work, NOW)


def only_source(path, build=None):
    return [source_of(path, build or (lambda clone: make_queue(clone)))]


# --- sources ---------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ["nolabel", "=repo", "label=", " = ", "a=#q"])
def test_source_spec_needs_label_and_repo(spec):
    with pytest.raises(rollup.RollupError, match=r"LABEL=REPO\[#QUEUE\]"):
        rollup.Source.parse(spec)


@pytest.mark.parametrize("spec", ["a=r#", "a=r#/abs", "a=r#../other"])
def test_a_queue_must_be_a_folder_inside_its_repository(spec):
    with pytest.raises(rollup.RollupError, match="must be a folder inside its repository"):
        rollup.Source.parse(spec)


def test_a_source_names_a_repo_and_optionally_the_queue_folder_in_it():
    url = "https://example.test/o/r.git"
    assert rollup.Source.parse(f"A={url}") == rollup.Source("A", url, ".tasks")
    assert rollup.Source.parse(f" A = {url}#docs/q ") == rollup.Source("A", url, "docs/q")
    assert rollup.Source("A", url).where == f"{url}#.tasks"


def test_a_repo_path_is_made_absolute_and_home_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "here").mkdir()
    assert rollup.Source.parse("A=here").repo == str((tmp_path / "here").resolve())
    assert rollup.Source.parse(" Home = ~/p ") == rollup.Source("Home", str(tmp_path / "p"))


def given(path, named=True):
    return rollup.ConfigFile(path, named)


def lazily(path, named=True):
    return lambda: given(path, named)


def make_here(tmp_path):
    make_queue(tmp_path / "repo")
    return lambda: tmp_path / "repo" / ".tasks"


def test_sources_come_from_flags_then_config_then_the_current_queue(tmp_path):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(
        '[[source]]\nlabel = "Work"\nrepo = "/w"\nqueue = "docs/q"\n'
        '[[source]]\nlabel = "Home"\nrepo = "/h"\n'
    )
    here = lambda: tmp_path / "repo" / ".tasks"  # noqa: E731
    assert rollup.load_sources([f"A={NOWHERE}"], lazily(cfg), here) == [rollup.Source("A", NOWHERE)]
    assert rollup.load_sources([], lazily(cfg), here) == [
        rollup.Source("Work", "/w", "docs/q"),
        rollup.Source("Home", "/h"),
    ]
    assert rollup.load_sources(
        [], lazily(tmp_path / "missing.toml", named=False), make_here(tmp_path)
    ) == [rollup.Source("this repository", str((tmp_path / "repo").resolve()), ".tasks")]


@pytest.mark.parametrize(
    "body,message",
    [
        ("not = [valid", "cannot read"),
        ("source = 3", "tables"),
        ("source = [1]", "no usable"),
        ('[[source]]\nlabel = "x"\n', "no usable"),
        ('[[source]]\nrepo = "/x"\n', "no usable"),
        ('[[source]]\nlabel = "x"\nrepo = "/x"\nqueue = "../y"\n', "no usable"),
    ],
)
def test_a_config_the_user_named_that_is_bad_says_what_is_wrong(tmp_path, body, message):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(body)
    with pytest.raises(rollup.RollupError, match=message):
        rollup.load_sources([], lazily(cfg))


ONE_GOOD_ONE_BAD = '[[source]]\nlabel = "Good"\nrepo = "/g"\n[[source]]\nlabel = "Label only"\n'


@pytest.mark.parametrize("named", [True, False])
def test_a_bad_source_row_is_skipped_with_a_warning_and_the_rest_still_count(
    tmp_path, capsys, named
):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(ONE_GOOD_ONE_BAD)
    found = rollup.load_sources([], lazily(cfg, named))
    assert [s.label for s in found] == ["Good"]
    warning = capsys.readouterr().err
    assert "row 2 needs a label and a repo" in warning and "that row is skipped" in warning


def test_a_bad_default_config_warns_and_falls_back_to_the_current_repository(tmp_path, capsys):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text("not = [valid")
    found = rollup.load_sources([], lazily(cfg, named=False), make_here(tmp_path))
    assert [s.label for s in found] == ["this repository"]
    assert "cannot read" in capsys.readouterr().err


def test_a_config_row_with_an_unresolvable_home_is_skipped_like_any_bad_row(tmp_path, capsys):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text('[[source]]\nlabel = "Gone"\nrepo = "~nouser-here/p"\n' + ONE_GOOD_ONE_BAD)
    found = rollup.load_sources([], lazily(cfg))
    assert [s.label for s in found] == ["Good"]
    assert "cannot expand" in capsys.readouterr().err


def test_a_default_config_with_no_usable_rows_falls_back_too(tmp_path, capsys):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text('[[source]]\nlabel = "x"\n')
    found = rollup.load_sources([], lazily(cfg, named=False), make_here(tmp_path))
    assert [s.label for s in found] == ["this repository"]
    assert "no usable [[source]] rows" in capsys.readouterr().err


REPEATED_LABELS = '[[source]]\nlabel = "A"\nrepo = "/a"\n[[source]]\nlabel = "A"\nrepo = "/b"\n'


@pytest.mark.parametrize("named", [True, False])
def test_a_repeated_label_in_a_config_skips_the_later_row_with_a_warning(tmp_path, capsys, named):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(REPEATED_LABELS + '[[source]]\nlabel = "B"\nrepo = "/c"\n')
    found = rollup.load_sources([], lazily(cfg, named))
    assert found == [rollup.Source("A", "/a"), rollup.Source("B", "/c")]
    warning = capsys.readouterr().err
    assert "used more than once" in warning and "'A'" in warning and "row is skipped" in warning


def test_an_absent_default_config_falls_back_silently(tmp_path, capsys):
    found = rollup.load_sources(
        [], lazily(tmp_path / "missing.toml", named=False), make_here(tmp_path)
    )
    assert [s.label for s in found] == ["this repository"]
    assert capsys.readouterr().err == ""


LEGACY_ROW = '[[source]]\nlabel = "Old"\npath = "/old"\nqueue = "docs/q"\n'


def test_a_source_row_that_still_says_path_is_read_as_the_repo_and_the_warning_names_it(
    tmp_path, capsys
):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(LEGACY_ROW)
    assert rollup.load_sources([], lazily(cfg)) == [rollup.Source("Old", "/old", "docs/q")]
    assert (
        "[[source]] row 1: `path` is now `repo`; reading it as the repo" in capsys.readouterr().err
    )


def test_a_repo_wins_over_a_legacy_path_without_a_warning(tmp_path, capsys):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text('[[source]]\nlabel = "New"\nrepo = "/new"\npath = "/old"\n')
    assert rollup.load_sources([], lazily(cfg)) == [rollup.Source("New", "/new")]
    assert capsys.readouterr().err == ""


def test_a_config_is_read_as_bytes_so_the_locale_cannot_misdecode_it(tmp_path, monkeypatch):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text('[[source]]\nlabel = "Café"\nrepo = "/a"\n', encoding="utf-8")

    def no_text(*_args, **_kwargs):
        raise AssertionError("read_text uses the locale encoding")

    monkeypatch.setattr(pathlib.Path, "read_text", no_text)
    assert [s.label for s in rollup.load_sources([], lazily(cfg))] == ["Café"]


def test_config_is_ignored_when_sources_are_given_and_the_run_says_so(tmp_path, capsys):
    cfg = tmp_path / "ignored.toml"
    assert rollup.main(["sources", "--source", f"W={NOWHERE}", "--config", str(cfg)], env={}) == 0
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "warning: --config is ignored because --source was given"


def test_the_default_config_is_not_looked_up_when_sources_are_given(monkeypatch, capsys):
    def unresolvable(explicit, env):
        raise rollup.RollupError("cannot expand '~': no home")

    monkeypatch.setattr(rollup, "config_file", unresolvable)
    assert rollup.main(["sources", "--source", f"W={NOWHERE}"], env={}) == 0
    assert "W:" in capsys.readouterr().out
    assert rollup.main(["sources"], env={}) == 2
    assert "no home" in capsys.readouterr().err


def test_a_run_over_a_bad_default_config_still_prints_the_current_repository(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config" / "claude-tasks"
    config.mkdir(parents=True)
    (config / "rollup.toml").write_text("not = [valid")
    monkeypatch.setenv(ENV_VAR, str(make_here(tmp_path)()))
    assert rollup.main(["sources"], env={}) == 0
    assert "this repository" in capsys.readouterr().out


def test_a_home_that_cannot_be_found_is_a_usage_error_not_a_crash(capsys):
    assert rollup.main(["json", "--source", "a=~nouser-here/p"], env={}) == 2
    assert "cannot expand" in capsys.readouterr().err


def test_config_file_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert rollup.config_file("/x.toml", {rollup.CONFIG_ENV: "/y"}) == given(
        pathlib.Path("/x.toml")
    )
    assert rollup.config_file(None, {rollup.CONFIG_ENV: "/y"}) == given(pathlib.Path("/y"))
    assert rollup.config_file(None, {}) == given(
        tmp_path / ".config" / "claude-tasks" / "rollup.toml", False
    )


def test_a_config_the_user_named_must_exist(tmp_path):
    with pytest.raises(rollup.RollupError, match=r"missing\.toml does not exist"):
        rollup.load_sources([], lazily(tmp_path / "missing.toml"))
    assert rollup.main(["sources", "--config", str(tmp_path / "missing.toml")], env={}) == 2
    assert rollup.main(["sources"], env={rollup.CONFIG_ENV: str(tmp_path / "missing.toml")}) == 2


def test_the_default_source_is_a_queue_used_directly_or_the_repository_of_a_dot_tasks_queue(
    tmp_path,
):
    repo = make_queue(tmp_path / "repo").parent
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "tasks.toml").write_text('name = "x"\n')
    assert rollup._current(repo / ".tasks") == rollup.Source(
        "this repository", str(repo.resolve()), ".tasks"
    )
    assert rollup._current(bare) == rollup.Source("this repository", str(bare.resolve()), ".")
    assert rollup._current(tmp_path / "nowhere").label == "this repository"


def test_the_default_source_never_widens_to_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    queue = make_queue(tmp_path)
    assert rollup._current(queue) == rollup.Source("this repository", str(queue.resolve()), ".")


def test_source_labels_must_be_unique(tmp_path):
    with pytest.raises(rollup.RollupError, match="more than once: A, B"):
        rollup.load_sources(["A=/a", "B=/b", "A=/c", "B=/d", "C=/e"], lazily(tmp_path / "x", False))


# --- reading a source: offline, never reached, a project that cannot be read ----------------


def test_a_source_that_cannot_be_reached_and_was_never_read_is_listed_under_not_shown(capsys):
    r = rollup.build([rollup.Source("Gone", NOWHERE)], NOW)
    assert r.entries == () and r.notes == ()
    assert r.omitted == (rv.Omission("Gone", ".tasks", rollup.NOT_READ),)
    assert "- Gone / .tasks: its queue cannot be read" in rv.render("project", "markdown", r)
    assert NOWHERE in capsys.readouterr().err
    assert NOWHERE not in rv.render("project", "json", r)


def test_an_unusable_cache_folder_skips_the_source_instead_of_stopping_the_run(
    tmp_path, monkeypatch
):
    def unwritable(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", "/secret/cache")

    monkeypatch.setattr(rollup, "read_queue", unwritable)
    r = rollup.build([rollup.Source("A", NOWHERE)], NOW)
    assert [o.reason for o in r.omitted] == [rollup.NOT_READ]


def stale_rollup(tmp_path):
    """Read once while the remote is up, then again after it has gone away."""
    source = source_of(tmp_path, alpha, "Work")
    assert rollup.build([source], NOW).notes == ()
    pathlib.Path(source.repo).rename(tmp_path / "gone")
    return rollup.build([source], NOW)


STALE = f"Work could not be reached; showing its queue as at {AS_AT}"


def test_offline_with_a_cache_shows_the_cached_queue_and_says_when_it_is_as_at(tmp_path):
    r = stale_rollup(tmp_path)
    assert [p.slug for p in projects(r)] == ["demo"] and r.omitted == ()
    assert [n.message for n in r.notes] == [STALE]
    assert projects(r)[0].as_at.stale


def test_a_stale_source_warns_in_json_markdown_and_html(tmp_path):
    r = stale_rollup(tmp_path)
    data = json.loads(rv.render("project", "json", r))
    assert data["warnings"] == [{"source": "Work", "behind": {}, "message": STALE}]
    assert data["projects"][0]["as_at"]["stale"] is True
    shown = (
        f'<h2>Source warnings</h2><ul class="items"><li><div class="head">{STALE}</div></li></ul>'
    )
    for view in rv.VIEWS:
        assert f"## Source warnings\n\n- {STALE}\n" in rv.render(view, "markdown", r)
        assert shown in rv.render(view, "html", r)


def test_sources_command_shows_the_projects_and_the_stale_warning(tmp_path, capsys):
    source = source_of(tmp_path, alpha, "Work")
    rollup.build([source], NOW)
    pathlib.Path(source.repo).rename(tmp_path / "gone")
    assert rollup.main(["sources", "--source", f"Work={source.repo}"], env={}) == 0
    out = capsys.readouterr().out
    assert f"Work: {source.repo}#.tasks\n  demo\n" in out and f"  warning: {STALE}" in out


def test_sources_command_says_why_a_source_gave_nothing(capsys):
    assert rollup.main(["sources", "--source", f"Gone={NOWHERE}"], env={}) == 0
    assert f"Gone: {NOWHERE}#.tasks\n  {rollup.NOT_READ}" in capsys.readouterr().out


def test_an_up_to_date_source_has_no_warning(work):
    assert built(work).notes == ()


def test_each_source_is_read_once_per_build(work, monkeypatch):
    calls = []
    real = rollup.read_queue
    monkeypatch.setattr(rollup, "read_queue", lambda *a: calls.append(a[0]) or real(*a))
    built(work)
    assert calls == [s.repo for s in work]


def test_broken_projects_are_skipped_with_the_reason(tmp_path):
    noout = source_of(tmp_path / "noout", with_project_toml('[project]\ntitle = "T"\n'), "Noout")
    badtoml = source_of(tmp_path / "badtoml", with_project_toml("not = [valid"), "Badtoml")
    r = rollup.build([noout, badtoml], NOW)
    assert r.entries == ()
    assert [o.source for o in r.omitted] == ["Noout", "Badtoml"]
    assert "outcome" in r.omitted[0].reason


def test_a_queue_without_a_project_file_is_omitted_without_naming_where_it_was_cached(tmp_path):
    def build(clone: pathlib.Path) -> None:
        (make_queue(clone) / "project.toml").unlink()

    r = rollup_of(tmp_path, build)
    assert [o.reason for o in r.omitted] == ["project.toml does not exist"]
    assert str(qs.cache_root()) not in "".join(rv.render("project", "markdown", r))


def test_a_broken_project_does_not_stop_the_sources_after_it(tmp_path, work):
    bad = source_of(tmp_path, with_project_toml('[project]\ntitle = "T"\n'), "Bad")
    r = rollup.build([bad, *work], NOW)
    assert [p.source for p in projects(r)] == ["Alpha", "Beta"]
    assert [o.source for o in r.omitted] == ["Bad"]


def test_two_sources_that_both_hold_projects_show_both_under_their_labels(work):
    r = built(work)
    assert [(p.source, p.slug) for p in projects(r)] == [("Alpha", "demo"), ("Beta", "Beta")]
    project = rv.render("project", "markdown", r)
    assert "**Source:** Alpha @ " in project and "**Source:** Beta @ " in project
    data = json.loads(rv.render("portfolio", "json", r))
    assert data["sources"] == ["Alpha", "Beta"]
    assert data["source_projects"] == [
        {"source": "Alpha", "projects": 1},
        {"source": "Beta", "projects": 1},
    ]


# --- model -----------------------------------------------------------------------------------


def test_a_milestone_holds_the_briefs_whose_frontmatter_names_it(work):
    a = next(p for p in projects(built(work)) if p.slug == "demo")
    m2 = a.milestones[1]
    assert [s.id for s in m2.briefs] == ["b3", "b2", "b4", "b5"]
    assert m2.waiting_count == 2 and m2.stage is model.Stage.NOW
    assert [s.waiting for s in m2.briefs] == [True, False, True, False]
    assert m2.milestone.total == 4 and m2.milestone.done == 0
    assert a.milestones[0].stage is model.Stage.DONE
    assert [s.id for s in a.shipped] == ["b1"]
    assert a.health.owner == "green" and a.health.effective == "green"
    assert a.ask is None


def test_unset_health_shows_the_floor(work):
    b = next(p for p in projects(built(work)) if p.slug == "Beta")
    assert [(m.id, m.stage) for m in b.milestones] == [
        ("M0", model.Stage.NEXT),
        ("M1", model.Stage.DONE),
        ("M2", model.Stage.NEXT),
    ]
    assert b.health.owner == "" and b.health.effective == "red"
    assert b.ask == model.Ask("Ship it?", "2026-11-01")
    assert b.shipped == ()


def test_each_project_carries_the_commit_it_was_read_at(work):
    alpha_read = projects(built(work))[0]
    head = snapshot(pathlib.Path(work[0].repo), qs.cache_root())
    assert (alpha_read.as_at.sha, alpha_read.as_at.stale) == (head.sha, False)
    assert alpha_read.as_at.committed.isoformat() == AS_AT


# --- views -----------------------------------------------------------------------------------


def test_project_json_is_the_api(work):
    r = built(work)
    data = json.loads(rv.render("project", "json", r))
    assert data["schema"] == rv.SCHEMA and data["level"] == "project"
    assert data["sources"] == ["Alpha", "Beta"]
    assert data["warnings"] == [] and data["skipped"] == []
    a, b = data["projects"]
    assert [e["id"] for e in a["now"]] == ["m2"] and [e["id"] for e in a["done"]] == ["m1"]
    assert a["now"][0]["waiting"] == 2 and [s["id"] for s in a["shipped"]] == ["b1"]
    assert "blocked" not in a["now"][0]
    assert b["ask"] == {"question": "Ship it?", "deadline": "2026-11-01"}
    head = snapshot(pathlib.Path(work[0].repo), qs.cache_root())
    assert a["as_at"] == {"sha": head.sha, "committed": AS_AT, "stale": False}
    text = json.dumps(data)
    assert "Success criteria" not in text
    assert {"brief": "b3", "waiting_on": ["external:vendor"]} in a["now"][0]["blockers"]


def test_project_markdown_reads_as_a_page(work):
    md = rv.render("project", "markdown", built(work))
    assert "# Project roll-up" in md
    assert "`Red` The owner has not set a health in project.toml." in md
    assert "**Ask:** Ship it? (by 2026-11-01)" in md
    assert "`missed`" in md and "`2 waiting`" in md


def test_ask_without_deadline(tmp_path):
    text = '[project]\ntitle = "T"\noutcome = "O"\n[ask]\nquestion = "Q?"\n'
    md = rv.render("project", "markdown", rollup_of(tmp_path, with_project_toml(text)))
    assert "**Ask:** Q?\n" in md


def test_portfolio_gives_each_project_a_sentence_and_its_reasons(work):
    r = built(work)
    data = json.loads(rv.render("portfolio", "json", r))
    a, b = data["projects"]
    assert a["milestones"] == {"done": 1, "total": 2} and a["missed"] == []
    assert a["as_at"]["committed"] == AS_AT
    assert b["next_due"] == {"id": "M0", "title": "Zero", "due": "2026-10-01"}
    assert b["missed"] == [{"id": "M0", "title": "Zero", "due": "2026-10-01"}]
    md = rv.render("portfolio", "markdown", r)
    assert "Beta \\| Two is red; 1 of 3 milestones done; 1 past their date" in md


def test_empty_and_skipped_are_said_plainly(tmp_path):
    r = rollup_of(tmp_path, with_project_toml("[project]\n"), "S")
    md = rv.render("project", "markdown", r)
    assert "No projects found" in md and "## Not shown" in md and "S / .tasks" in md


# --- HTML, files, server ---------------------------------------------------------------------


def test_project_html_is_tiles_then_a_milestone_table_then_collapsed_detail(work):
    page = rv.render("project", "html", built(work))
    assert "<title>Project roll-up</title>" in page and "<h1>Project roll-up</h1>" in page
    assert page.index("<h2>Projects</h2>") < page.index("<h2>Milestones</h2>")
    assert page.index("<h2>Milestones</h2>") < page.index("<h2>Detail</h2>")
    assert '<span class="chip chip-good">Green</span>' in page and "<table>" in page
    assert "<details><summary>Show detail</summary>" in page
    assert "<strong>Ask:</strong> Ship it? (by 2026-11-01)" in page
    assert "<h5>No release named, last 14 days</h5>" in page and "One (2026-10-22)" in page


def test_portfolio_html_groups_projects_by_initiative_with_a_sentence_each(work):
    page = rv.render("portfolio", "html", built(work))
    assert "<h2>No initiative</h2>" in page and "<h3>Beta | Two</h3>" in page
    assert "<p>Beta | Two is red; 1 of 3 milestones done;" in page
    assert '<span class="chip chip-bad">Red</span>' in page and "<table>" not in page


def test_html_says_plainly_when_there_is_nothing_to_show(tmp_path):
    page = rv.render("project", "html", rollup_of(tmp_path, with_project_toml("[project]\n")))
    assert "<p>No projects found." in page and "<h2>Not shown</h2>" in page
    assert '<div class="head">S / .tasks:' in page


WRITTEN_FILES = [
    "README.md",
    "portfolio.json",
    "portfolio.md",
    "project.json",
    "project.md",
    "work.json",
    "work.md",
]


def test_write_produces_a_folder_github_renders(work, tmp_path):
    out = tmp_path / "out" / "rollups"
    paths = rollup.write(out, built(work))
    assert sorted(p.name for p in paths) == WRITTEN_FILES
    assert "(project.md)" in (out / "README.md").read_text()
    for view in rv.VIEWS:
        assert json.loads((out / f"{view}.json").read_text())["level"] == view
        assert (out / f"{view}.md").read_text().startswith("# ")


def test_the_generated_readme_tells_someone_else_how_to_get_their_own_view(work):
    text = " ".join(rv.render_contents("markdown", built(work), with_guide=True).split())
    for line in (
        "[Work roll-up](work.md) [JSON](work.json)",
        "[Project roll-up](project.md) [JSON](project.json)",
        "[Portfolio roll-up](portfolio.md) [JSON](portfolio.json)",
        "Open `work.md`, `project.md` or `portfolio.md` in this folder; GitHub shows them as formatted pages.",
        'Install the claude-tasks plugin, then ask "how are my projects going?".',
        "attach `project.md` with `+ > Add from GitHub`",
        "rollup.py write --out <dir> --source Label=repo",
        "use the schema `claude-tasks.rollup/v1`",
    ):
        assert line in text


@pytest.mark.parametrize(
    "path,ctype,needle",
    [
        ("/", "text/html", '<a href="project"'),
        ("/work", "text/html", "Work roll-up"),
        ("/project", "text/html", "Project roll-up"),
        ("/portfolio", "text/html", "Portfolio roll-up"),
        ("/work.json", "application/json", '"work"'),
        ("/project.json", "application/json", '"project"'),
        ("/portfolio.json", "application/json", '"portfolio"'),
    ],
)
def test_routes(work, path, ctype, needle):
    got = rollup.route(path, built(work))
    assert got is not None and got[0].startswith(ctype) and needle in got[1]


@pytest.mark.parametrize(
    "path", ["/nope", "/project.txt", "/project.md", "/project.json.x", "/project.", "/.json"]
)
def test_unknown_routes(work, path):
    assert rollup.route(path, built(work)) is None


def test_server_answers_on_localhost(work, capsys):
    holder = {}
    rollup.Serve(work, lambda: NOW, 0).run(block=lambda srv: holder.setdefault("srv", srv))
    srv = holder["srv"]
    assert srv.server_address[0] == "127.0.0.1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/project.json?x=1") as resp:
            assert json.loads(resp.read())["level"] == "project"
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/nope")
        assert err.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
    assert "roll-ups at http://localhost:" in capsys.readouterr().out


# --- CLI -------------------------------------------------------------------------------------


def sources_args(work):
    return [arg for s in work for arg in ("--source", f"{s.label}={s.repo}")]


def test_cli_prints_each_format(work, capsys):
    src = sources_args(work)
    assert rollup.main(["json", "--level", "portfolio", *src], env={}) == 0
    assert json.loads(capsys.readouterr().out)["level"] == "portfolio"
    assert rollup.main(["markdown", *src], env={}) == 0
    assert "# Project roll-up" in capsys.readouterr().out
    assert rollup.main(["markdown", "--level", "work", *src], env={}) == 0
    assert "# Work roll-up" in capsys.readouterr().out
    assert rollup.main(["html", *src], env={}) == 0
    assert "<html" in capsys.readouterr().out.lower()
    assert rollup.main(["sources", *src, "--source", f"Empty={NOWHERE}"], env={}) == 0
    out = capsys.readouterr().out
    assert "Alpha" in out and "Empty" in out


def test_cli_write_and_errors(work, tmp_path, capsys):
    out = tmp_path / "o"
    assert rollup.main(["write", "--out", str(out), *sources_args(work)], env={}) == 0
    assert all((out / name).is_file() for name in WRITTEN_FILES)
    capsys.readouterr()
    assert rollup.main(["json", "--source", "bad"], env={}) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["write", "--source", "W=/w"],
        ["write", "out", "--source", "W=/w"],
        ["json", "--out", "out", "--source", "W=/w"],
        ["sources", "--out", "out"],
        [],
    ],
)
def test_only_write_takes_an_output_option(argv, capsys):
    with pytest.raises(SystemExit) as exit_:
        rollup.parse(argv, {})
    assert exit_.value.code == 2 and "usage:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv,command",
    [
        (["json", "--level", "work"], rollup.ShowJson),
        (["markdown"], rollup.ShowMarkdown),
        (["html"], rollup.ShowHtml),
        (["sources"], rollup.ListSources),
        (["write", "--out", "o"], rollup.Write),
        (["serve", "--port", "0"], rollup.Serve),
    ],
)
def test_each_subcommand_parses_to_its_own_command(argv, command):
    assert type(rollup.parse([*argv, "--source", f"W={NOWHERE}"], {})) is command


@pytest.mark.parametrize(
    "argv",
    [
        ["sources", "--level", "portfolio"],
        ["write", "--out", "o", "--port", "1"],
        ["json", "--port", "1"],
    ],
)
def test_a_flag_belongs_only_to_the_commands_that_use_it(argv):
    with pytest.raises(SystemExit) as exit_:
        rollup.parse([*argv, "--source", "W=/w"], {})
    assert exit_.value.code == 2


def test_command_names_are_unique():
    names = [c.name for c in rollup.COMMANDS]
    assert len(names) == len(set(names))


def test_cli_reads_config_from_the_environment(work, tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[[source]]\nlabel = "Cfg"\nrepo = "{work[0].repo}"\nqueue = ".tasks"\n')
    monkeypatch.setenv(rollup.CONFIG_ENV, str(cfg))
    monkeypatch.setattr(sys, "argv", ["rollup.py", "sources"])
    assert rollup.main() == 0
    assert "Cfg" in capsys.readouterr().out


# --- failures, dates, writing, ports ----------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        KeyError("k"),
        TypeError("t"),
        ValueError("v"),
        OSError("o"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
    ],
)
def test_any_read_failure_skips_the_queue_naming_the_error(tmp_path, monkeypatch, error):
    def fail(*_args):
        raise error

    monkeypatch.setattr(rollup.rv.Entry, "read", fail)
    [omitted] = rollup.build(only_source(tmp_path), NOW).omitted
    assert omitted.reason.startswith(type(error).__name__)


def test_an_undecodable_project_file_is_skipped_not_fatal(tmp_path):
    def undecodable(clone):
        (make_queue(clone) / "project.toml").write_bytes(b"\xff\xfe")

    r = rollup.build(only_source(tmp_path, undecodable), NOW)
    assert r.entries == () and "UnicodeDecodeError" in r.omitted[0].reason


def test_story_dates_are_dates_and_undated_briefs_are_not_shipped(tmp_path):
    def build(clone):
        q = make_queue(clone)
        brief(q, "done", "b8", updated="soon, ask Sam", crit="- [x] ok")
        brief(q, "done", "b9", updated="2026-10-20", crit="- [x] ok")

    r = rollup.build(only_source(tmp_path, build), NOW)
    assert [s.id for s in projects(r)[0].shipped] == ["b1", "b9"]
    data = json.loads(rv.render("project", "json", r))
    assert data["projects"][0]["shipped"][1] == {
        "id": "b9",
        "title": "B9",
        "updated": "2026-10-20",
    }
    assert "Sam" not in json.dumps(data)


@pytest.mark.parametrize("held", ["not json", "[1]", '{"schema": "other"}'])
def test_write_refuses_a_folder_that_holds_something_else(work, tmp_path, held):
    out = tmp_path / "docs"
    out.mkdir()
    (out / "project.json").write_text(held)
    (out / "notes.md").write_text("mine")
    with pytest.raises(rollup.RollupError, match="not an empty folder"):
        rollup.write(out, built(work))
    assert rollup.main(["write", "--out", str(out), *sources_args(work)], env={}) == 2
    assert (out / "notes.md").read_text() == "mine" and (out / "project.json").read_text() == held


def test_write_reports_a_destination_it_cannot_create(work, tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    out = blocker / "rollups"
    with pytest.raises(rollup.RollupError, match=r"cannot write .*rollups: Not a directory"):
        rollup.write(out, built(work))
    assert rollup.main(["write", "--out", str(out), *sources_args(work)], env={}) == 2
    assert capsys.readouterr().err.startswith("error: cannot write ")


def test_write_rewrites_only_what_changed_apart_from_the_timestamp(work, tmp_path):
    out = tmp_path / "empty"
    out.mkdir()
    assert len(rollup.write(out, built(work))) == len(WRITTEN_FILES)
    later = NOW.replace(hour=13)
    assert rollup.write(out, rollup.build(work, later)) == []
    assert "Generated 2026-10-23T12:00:00" in (out / "project.md").read_text()

    def renamed(clone):
        alpha(clone)
        toml = clone / ".tasks" / "project.toml"
        toml.write_text(toml.read_text().replace("Demo Project", "Renamed"))

    changed = [source_of(tmp_path / "changed", renamed, "Alpha"), work[1]]
    names = {p.name for p in rollup.write(out, rollup.build(changed, later))}
    assert names == {
        "project.md",
        "project.json",
        "portfolio.md",
        "portfolio.json",
        "work.md",
        "work.json",
    }
    assert rollup.main(["write", "--out", str(out), *sources_args(work)], env={}) == 0


def test_a_port_in_use_or_out_of_range_is_a_one_line_error(work, capsys):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        argv = ["serve", "--port", str(taken.getsockname()[1]), *sources_args(work)]
        assert rollup.main(argv, env={}) == 2
    assert rollup.main(["serve", "--port", "70000", *sources_args(work)], env={}) == 2
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2 and all(line.startswith("error: cannot listen on port") for line in err)


def test_the_index_links_pages_by_file_in_a_folder_and_by_route_on_the_server(work):
    r = built(work)
    assert "[Project roll-up](project.md)" in rv.render_contents("markdown", r, with_guide=True)
    got = rollup.route("/", r)
    assert (
        got is not None and '<a href="project"' in got[1] and '<a href="portfolio.json"' in got[1]
    )


def zoned(tmp_path, zone):
    def build(clone):
        q = make_queue(clone)
        toml = q / "project.toml"
        toml.write_text(
            toml.read_text().replace(
                'release = "2026.12"', 'release = "2026.12"\ndue = "2026-10-23"'
            )
        )
        brief(q, "done", "b7", updated="2026-10-09", crit="- [x] ok")
        (q / "tasks.toml").write_text(f'name = "Demo"\ntimezone = "{zone}"\n')

    return projects(rollup.build(only_source(tmp_path, build), NOW))[0]


def test_today_missed_and_the_shipped_window_follow_the_queue_timezone(tmp_path):
    utc = zoned(tmp_path / "u", "UTC")
    ahead = zoned(
        tmp_path / "k", "Pacific/Kiritimati"
    )  # already 24 October at 12:00 UTC on the 23rd
    assert (utc.milestones[1].missed, [s.id for s in utc.shipped]) == (False, ["b1", "b7"])
    assert (ahead.milestones[1].missed, [s.id for s in ahead.shipped]) == (True, ["b1"])


def test_an_unknown_timezone_warns_and_falls_back_to_utc(tmp_path, capsys):
    p = zoned(tmp_path, "Nowhere/Land")
    assert p.milestones[1].missed is False
    assert "Nowhere/Land" in capsys.readouterr().err


def test_an_unreadable_tasks_file_still_gives_a_utc_roll_up(tmp_path):
    def build(clone):
        (make_queue(clone) / "tasks.toml").write_text("not = [valid")

    assert [p.slug for p in projects(rollup.build(only_source(tmp_path, build), NOW))] == ["demo"]


# --- what a milestone says, and the rows a project file gets wrong ----------------------------

HEAD = '[project]\ntitle = "T"\noutcome = "O"\n'


def test_a_milestone_percent_is_the_one_the_project_view_computes(tmp_path):
    r = rollup.build(only_source(tmp_path), NOW)
    assert [m.percent_done for m in projects(r)[0].milestones] == [100, 50]
    project = json.loads(rv.render("project", "json", r))["projects"][0]
    assert [m["percent_done"] for m in project["now"]] == [50]


@pytest.mark.parametrize(
    "body",
    [
        '[[milestones]]\nid = "m1"\n[[milestones]]\ntitle = "No id"\n',
        '[[milestones]]\nid = "m1"\n[[milestones]]\nid = 5\n',
        '[[milestones]]\nid = "m1"\n[[milestones]]\nid = "  "\n',
    ],
)
def test_a_milestone_row_without_a_text_id_is_dropped_and_the_project_still_shows(
    tmp_path, capsys, body
):
    r = rollup.build(only_source(tmp_path, with_project_toml(HEAD + body)), NOW)
    assert r.omitted == () and [m.id for m in projects(r)[0].milestones] == ["m1"]
    warning = capsys.readouterr().err
    assert "milestones row 2 needs a text id" in warning and "that row is left out" in warning


@pytest.mark.parametrize("value", ['"m1"', "3"])
def test_milestones_that_are_not_a_list_warn_and_are_left_out(tmp_path, capsys, value):
    r = rollup.build(only_source(tmp_path, with_project_toml(f"milestones = {value}\n{HEAD}")), NOW)
    assert r.omitted == () and projects(r)[0].milestones == ()
    assert "milestones must be [[milestones]] tables" in capsys.readouterr().err


def test_milestone_entries_that_are_not_tables_are_dropped_one_by_one(tmp_path, capsys):
    r = rollup.build(only_source(tmp_path, with_project_toml(f"milestones = [1, 2]\n{HEAD}")), NOW)
    assert r.omitted == () and projects(r)[0].milestones == ()
    assert "milestones row 1 needs a text id" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["5", '""', '["a"]'])
def test_a_slug_that_is_not_text_falls_back_to_the_source_label_with_a_warning(
    tmp_path, capsys, value
):
    toml = HEAD.replace("[project]", f"[project]\nslug = {value}")
    r = rollup_of(tmp_path, with_project_toml(toml), "Label")
    assert projects(r)[0].slug == "Label"
    assert "slug must be text" in capsys.readouterr().err


# --- writing over files that are not ours -----------------------------------------------------


def test_an_existing_file_that_is_not_utf8_is_replaced_not_fatal(work, tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    rollup.write(out, built(work))
    (out / "project.md").write_bytes(b"\xff\xfe")
    assert [p.name for p in rollup.write(out, built(work))] == ["project.md"]
    assert "Project roll-up" in (out / "project.md").read_text()
    assert rollup.main(["write", "--out", str(out), *sources_args(work)], env={}) == 0


def test_the_snapshot_time_is_the_commit_time_not_the_time_of_the_run(tmp_path):
    source = source_of(tmp_path, alpha, "Work")
    later = NOW.replace(year=2027)
    assert projects(rollup.build([source], later))[0].as_at.committed.isoformat() == PUSHED_AT
    assert publish  # the fixture module is the one place a queue reaches a remote

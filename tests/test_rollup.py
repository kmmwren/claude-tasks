"""Roll-ups: sources, the typed model, the pm and cto projections, and every output surface."""

import datetime as dt
import json
import pathlib
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import project_view as pv
import pytest
import rollup
from _paths import ENV_VAR

from tests.project_fixtures import make_queue

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)

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


def alpha(root: pathlib.Path) -> pathlib.Path:
    q = make_queue(root / "alpha")
    brief(q, "ready", "b3", "milestone: m2\nblocked_by: [external:vendor]\n")
    brief(q, "blocked", "b4", "milestone: m2\nblocked_by: [b2]\n")
    brief(
        q, "blocked", "b5", "milestone: m2\nblocked_by: [b1]\n"
    )  # b1 is done, so b5 waits for nothing
    return q


def beta(root: pathlib.Path) -> pathlib.Path:
    q = root / "beta" / ".tasks"
    for d in ("inbox", "ready", "done"):
        (q / d).mkdir(parents=True)
    (q / "tasks.toml").write_text('name = "Beta"\nmilestones = "docs/milestones.toml"\n')
    (q.parent / "docs").mkdir()
    (q.parent / "docs" / "milestones.toml").write_text(
        '[milestone.M1]\ntitle = "Start"\n[milestone.M2]\n'
    )
    (q / "project.toml").write_text(BETA)
    brief(q, "done", "c1", "milestone: M1\n", updated="2026-09-01", crit="- [x] ok")
    brief(q, "ready", "c2", "milestone: M0\n", updated="2026-09-01")
    return q


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    alpha(root)
    beta(root)
    return root


def built(work):
    return rollup.build([rollup.Source("Work", work)], NOW)


# --- sources ---------------------------------------------------------------------------------


def test_find_queues_is_bounded_and_skips_hidden_and_build_folders(tmp_path):
    make_queue(tmp_path / "a")
    make_queue(tmp_path / ".hidden")
    make_queue(tmp_path / "node_modules" / "x")
    make_queue(tmp_path / "1" / "2" / "3" / "4")
    (tmp_path / "file.txt").write_text("")
    assert rollup.find_queues(tmp_path) == [tmp_path / "a" / ".tasks"]
    assert rollup.find_queues(tmp_path / "a" / ".tasks") == [tmp_path / "a" / ".tasks"]


def test_only_queues_with_a_project_file_are_projects(tmp_path):
    q = make_queue(tmp_path / "plain")
    (q / "project.toml").unlink()
    assert rollup.Source("x", tmp_path).projects() == []


@pytest.mark.parametrize("spec", ["nolabel", "=path", "label=", " = "])
def test_source_spec_needs_label_and_path(spec):
    with pytest.raises(rollup.RollupError, match="LABEL=PATH"):
        rollup.Source.parse(spec)


def test_source_spec_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert rollup.Source.parse(" Home = ~/p ") == rollup.Source("Home", tmp_path / "p")


def given(path, named=True):
    return rollup.ConfigFile(path, named)


def lazily(path, named=True):
    return lambda: given(path, named)


def test_sources_come_from_flags_then_config_then_the_current_queue(tmp_path):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(
        '[[source]]\nlabel = "Work"\npath = "/w"\n[[source]]\nlabel = "Home"\npath = "/h"\n'
    )
    here = lambda: tmp_path / "repo" / ".tasks"  # noqa: E731
    assert rollup.load_sources(["A=/a"], lazily(cfg), here) == [
        rollup.Source("A", pathlib.Path("/a"))
    ]
    assert [s.label for s in rollup.load_sources([], lazily(cfg), here)] == ["Work", "Home"]
    assert rollup.load_sources(
        [], lazily(tmp_path / "missing.toml", named=False), make_here(tmp_path)
    ) == [rollup.Source("this repository", (tmp_path / "repo").resolve())]


def make_here(tmp_path):
    make_queue(tmp_path / "repo")
    return lambda: tmp_path / "repo" / ".tasks"


@pytest.mark.parametrize(
    "body,message",
    [
        ("not = [valid", "cannot read"),
        ("source = 3", "tables"),
        ("source = [1]", "no usable"),
        ('[[source]]\nlabel = "x"\n', "no usable"),
        ('[[source]]\npath = "/x"\n', "no usable"),
    ],
)
def test_a_config_the_user_named_that_is_bad_says_what_is_wrong(tmp_path, body, message):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(body)
    with pytest.raises(rollup.RollupError, match=message):
        rollup.load_sources([], lazily(cfg))


ONE_GOOD_ONE_BAD = '[[source]]\nlabel = "Good"\npath = "/g"\n[[source]]\nlabel = "Label only"\n'


@pytest.mark.parametrize("named", [True, False])
def test_a_bad_source_row_is_skipped_with_a_warning_and_the_rest_still_count(
    tmp_path, capsys, named
):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(ONE_GOOD_ONE_BAD)
    found = rollup.load_sources([], lazily(cfg, named))
    assert [s.label for s in found] == ["Good"]
    warning = capsys.readouterr().err
    assert "row 2 needs a label and a path" in warning and "that row is skipped" in warning


def test_a_bad_default_config_warns_and_falls_back_to_the_current_repository(tmp_path, capsys):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text("not = [valid")
    found = rollup.load_sources([], lazily(cfg, named=False), make_here(tmp_path))
    assert found == [rollup.Source("this repository", (tmp_path / "repo").resolve())]
    assert "cannot read" in capsys.readouterr().err


def test_a_config_row_with_an_unresolvable_home_is_skipped_like_any_bad_row(tmp_path, capsys):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text('[[source]]\nlabel = "Gone"\npath = "~nouser-here/p"\n' + ONE_GOOD_ONE_BAD)
    found = rollup.load_sources([], lazily(cfg))
    assert [s.label for s in found] == ["Good"]
    assert "cannot expand" in capsys.readouterr().err


def test_a_default_config_with_no_usable_rows_falls_back_too(tmp_path, capsys):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text('[[source]]\nlabel = "x"\n')
    found = rollup.load_sources([], lazily(cfg, named=False), make_here(tmp_path))
    assert [s.label for s in found] == ["this repository"]
    assert "no usable [[source]] rows" in capsys.readouterr().err


REPEATED_LABELS = '[[source]]\nlabel = "A"\npath = "/a"\n[[source]]\nlabel = "A"\npath = "/b"\n'


@pytest.mark.parametrize("named", [True, False])
def test_a_repeated_label_in_a_config_skips_the_later_row_with_a_warning(tmp_path, capsys, named):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text(REPEATED_LABELS + '[[source]]\nlabel = "B"\npath = "/c"\n')
    found = rollup.load_sources([], lazily(cfg, named))
    assert found == [rollup.Source("A", pathlib.Path("/a")), rollup.Source("B", pathlib.Path("/c"))]
    warning = capsys.readouterr().err
    assert "used more than once" in warning and "'A'" in warning and "row is skipped" in warning


def test_an_absent_default_config_falls_back_silently(tmp_path, capsys):
    found = rollup.load_sources(
        [], lazily(tmp_path / "missing.toml", named=False), make_here(tmp_path)
    )
    assert [s.label for s in found] == ["this repository"]
    assert capsys.readouterr().err == ""


def test_a_config_is_read_as_bytes_so_the_locale_cannot_misdecode_it(tmp_path, monkeypatch):
    cfg = tmp_path / "rollup.toml"
    cfg.write_text('[[source]]\nlabel = "Café"\npath = "/a"\n', encoding="utf-8")

    def no_text(*_args, **_kwargs):
        raise AssertionError("read_text uses the locale encoding")

    monkeypatch.setattr(pathlib.Path, "read_text", no_text)
    assert [s.label for s in rollup.load_sources([], lazily(cfg))] == ["Café"]


def test_config_is_ignored_when_sources_are_given_and_the_run_says_so(work, tmp_path, capsys):
    cfg = tmp_path / "ignored.toml"
    assert rollup.main(["sources", "--source", f"W={work}", "--config", str(cfg)], env={}) == 0
    err = capsys.readouterr().err.splitlines()
    assert err == ["warning: --config is ignored because --source was given"]


def test_the_default_config_is_not_looked_up_when_sources_are_given(work, monkeypatch, capsys):
    def unresolvable(explicit, env):
        raise rollup.RollupError("cannot expand '~': no home")

    monkeypatch.setattr(rollup, "config_file", unresolvable)
    assert rollup.main(["sources", "--source", f"W={work}"], env={}) == 0
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
    assert rollup._current(repo / ".tasks") == rollup.Source("this repository", repo.resolve())
    assert rollup._current(bare) == rollup.Source("this repository", bare.resolve())
    assert rollup._current(tmp_path / "nowhere").label == "this repository"


def test_the_default_source_never_widens_to_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    queue = make_queue(tmp_path)
    assert rollup._current(queue) == rollup.Source("this repository", queue.resolve())


def test_source_labels_must_be_unique(tmp_path):
    with pytest.raises(rollup.RollupError, match="more than once: A, B"):
        rollup.load_sources(["A=/a", "B=/b", "A=/c", "B=/d", "C=/e"], lazily(tmp_path / "x", False))


def test_a_queue_reached_by_two_sources_is_one_project_under_the_first(tmp_path):
    make_queue(tmp_path / "demo")
    r = rollup.build(
        [rollup.Source("Outer", tmp_path), rollup.Source("Inner", tmp_path / "demo")], NOW
    )
    assert [(p.source, p.slug) for p in r.projects] == [("Outer", "demo")]


def test_a_queue_reached_through_a_symlink_is_not_followed(tmp_path):
    make_queue(tmp_path / "real")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    assert rollup.find_queues(tmp_path) == [tmp_path / "real" / ".tasks"]


def test_a_folder_that_cannot_be_listed_is_skipped(tmp_path, monkeypatch):
    make_queue(tmp_path / "ok")
    (tmp_path / "locked").mkdir()
    real = pathlib.Path.iterdir

    def iterdir(self):
        if self.name == "locked":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", iterdir)
    assert rollup.find_queues(tmp_path) == [tmp_path / "ok" / ".tasks"]


def test_a_source_that_does_not_exist_is_listed_under_not_shown(tmp_path):
    r = rollup.build([rollup.Source("Gone", tmp_path / "nope")], NOW)
    assert r.skipped == (rollup.Skipped("Gone", "nope", "not a folder that exists"),)
    assert "- Gone / nope: not a folder that exists" in rollup.LEVELS["pm"].markdown(r)


# --- model -----------------------------------------------------------------------------------


def test_a_milestone_holds_the_briefs_whose_frontmatter_names_it(work):
    a = next(p for p in built(work).projects if p.slug == "demo")
    m2 = a.milestones[1]
    assert [s.id for s in m2.briefs] == ["b3", "b2", "b4", "b5"]
    assert m2.waiting_count == 2 and m2.stage is rollup.Stage.NOW
    assert [s.waiting for s in m2.briefs] == [True, False, True, False]
    assert m2.milestone.total == 4 and m2.milestone.done == 0
    assert a.milestones[0].stage is rollup.Stage.DONE
    assert [s.id for s in a.shipped] == ["b1"]
    assert a.health.owner == "green" and a.health.effective == "green"
    assert a.ask is None


def test_milestones_file_adds_milestones_and_unset_health_shows_the_floor(work):
    b = next(p for p in built(work).projects if p.slug == "beta")
    assert [(m.id, m.stage) for m in b.milestones] == [
        ("M0", rollup.Stage.NEXT),
        ("M1", rollup.Stage.DONE),
        ("M2", rollup.Stage.NEXT),
    ]
    assert b.health.owner == "" and b.health.effective == "red"
    assert b.ask == rollup.Ask("Ship it?", "2026-11-01")
    assert b.shipped == ()


def test_unset_health_with_no_floor_is_blank():
    h = rollup.Health.of({}, [], NOW)
    assert (h.owner, h.effective, h.signals) == ("", "", ())


@pytest.mark.parametrize("raw", [None, {}, {"deadline": "x"}])
def test_ask_needs_a_question(raw):
    assert rollup.Ask.of(raw) is None


def test_broken_projects_are_skipped_with_the_reason(tmp_path):
    (make_queue(tmp_path / "noout") / "project.toml").write_text('[project]\ntitle = "T"\n')
    (make_queue(tmp_path / "badtoml") / "project.toml").write_text("not = [valid")
    r = rollup.build([rollup.Source("S", tmp_path)], NOW)
    assert r.projects == ()
    assert sorted(s.queue for s in r.skipped) == ["badtoml", "noout"]
    assert any("outcome" in s.reason for s in r.skipped)


def test_project_view_ignores_an_unreadable_milestones_file(tmp_path):
    q = make_queue(tmp_path)
    (q / "tasks.toml").write_text('name = "Demo"\nmilestones = "missing.toml"\n')
    cfg = pv.read_project(q)
    assert [m["id"] for m in pv.declared_milestones(q, cfg)] == ["m1", "m2"]


def test_a_project_with_no_owner_health_reads_without_one(tmp_path):
    q = make_queue(tmp_path)
    (q / "project.toml").write_text('[project]\ntitle = "T"\noutcome = "O"\n')
    assert "health" not in pv.read_project(q)


@pytest.mark.parametrize(
    "body,message",
    [
        ('title = "loose"\n', r"\[project\] is required"),
        ('health = 3\n[project]\ntitle = "T"\noutcome = "O"\n', r"\[health\] is required"),
    ],
)
def test_read_project_rejects_malformed_tables(tmp_path, body, message):
    q = make_queue(tmp_path)
    (q / "project.toml").write_text(body)
    with pytest.raises(pv.ProjectConfigError, match=message):
        pv.read_project(q)


# --- levels ----------------------------------------------------------------------------------


def test_pm_json_is_the_api(work):
    data = rollup.LEVELS["pm"].data(built(work))
    assert data["schema"] == rollup.SCHEMA and data["level"] == "pm" and data["sources"] == ["Work"]
    assert data["warnings"] == [] and data["skipped"] == []
    a, b = data["projects"]
    assert [e["id"] for e in a["now"]] == ["m2"] and [e["id"] for e in a["done"]] == ["m1"]
    assert a["now"][0]["waiting"] == 2 and [s["id"] for s in a["shipped"]] == ["b1"]
    assert "blocked" not in a["now"][0]
    assert b["ask"] == {"question": "Ship it?", "deadline": "2026-11-01"}
    text = json.dumps(data)
    assert "Success criteria" not in text and "vendor" not in text


def test_pm_markdown_reads_as_a_page(work):
    md = rollup.LEVELS["pm"].markdown(built(work))
    assert "# Product roll-up" in md
    assert "**Red**, not set by owner." in md and ".**." not in md
    assert "Ship it? (by 2026-11-01)" in md
    assert "missed" in md and "2 waiting" in md


def test_ask_without_deadline(tmp_path):
    q = make_queue(tmp_path / "x")
    text = (q / "project.toml").read_text()
    (q / "project.toml").write_text(text + '\n[ask]\nquestion = "Q?"\n')
    md = rollup.LEVELS["pm"].markdown(rollup.build([rollup.Source("S", tmp_path)], NOW))
    assert "- Q?\n" in md


def test_cto_is_one_table_with_reasons(work):
    r = built(work)
    data = rollup.LEVELS["cto"].data(r)
    a, b = data["projects"]
    assert a["milestones"] == {"done": 1, "total": 2} and a["missed"] == []
    assert b["next_due"] == {"id": "M0", "title": "Zero", "due": "2026-10-01"}
    assert b["missed"] == [{"id": "M0", "title": "Zero", "due": "2026-10-01"}]
    md = rollup.LEVELS["cto"].markdown(r)
    assert "Beta \\| Two (Work)" in md and "1 of 3" in md


def test_empty_and_skipped_are_said_plainly(tmp_path):
    (make_queue(tmp_path / "bad") / "project.toml").write_text("[project]\n")
    md = rollup.LEVELS["pm"].markdown(rollup.build([rollup.Source("S", tmp_path)], NOW))
    assert "No projects found" in md and "## Not shown" in md and "S / bad" in md


# --- HTML, files, server ---------------------------------------------------------------------


def test_pm_html_is_built_from_the_model(work):
    page = rollup.LEVELS["pm"].html(built(work))
    assert "<title>Product roll-up</title>" in page and "<h1>Product roll-up</h1>" in page
    assert "<h2>Demo Project</h2>" in page and "<strong>Green</strong>" in page
    assert "<h3>Now</h3>" in page and "<li>Ship it? (by 2026-11-01)</li>" in page
    assert "<h3>Shipped in the last 14 days</h3>" in page and "<li>One (2026-10-22)</li>" in page


def test_cto_html_is_one_table_and_the_reasons(work):
    page = rollup.LEVELS["cto"].html(built(work))
    assert page.count("<th>") == 5 and "<td>Beta | Two (Work)</td>" in page
    assert "<td>1 of 3</td>" in page and "<strong>Red</strong>" in page


def test_html_says_plainly_when_there_is_nothing_to_show(tmp_path):
    (make_queue(tmp_path / "bad") / "project.toml").write_text("[project]\n")
    page = rollup.LEVELS["pm"].html(rollup.build(only_source(tmp_path), NOW))
    assert "<p>No projects found." in page and "<h2>Not shown</h2>" in page
    assert "<li>S / bad:" in page


def test_write_produces_a_folder_github_renders(work, tmp_path):
    out = tmp_path / "out" / "rollups"
    paths = rollup.write(out, built(work))
    assert sorted(p.name for p in paths) == ["README.md", "cto.json", "cto.md", "pm.json", "pm.md"]
    assert "(pm.md)" in (out / "README.md").read_text()
    assert json.loads((out / "cto.json").read_text())["level"] == "cto"


def test_the_generated_readme_tells_someone_else_how_to_get_their_own_view(work):
    text = " ".join(rollup.readme_markdown(built(work)).split())
    for line in (
        "[Product roll-up](pm.md) ([JSON](pm.json))",
        "[Portfolio roll-up](cto.md) ([JSON](cto.json))",
        "Open `pm.md` or `cto.md` in this folder; GitHub shows them as formatted pages.",
        'Install the claude-tasks plugin, then ask "how are my projects going?".',
        "attach `pm.md` with **+ > Add from GitHub**",
        "rollup.py write --out <dir> --source Label=path",
        "use the schema `claude-tasks.rollup/v1`",
    ):
        assert line in text


@pytest.mark.parametrize(
    "path,ctype,needle",
    [
        ("/", "text/html", "pm"),
        ("/pm", "text/html", "Product roll-up"),
        ("/cto", "text/html", "<table>"),
        ("/cto.json", "application/json", '"cto"'),
    ],
)
def test_routes(work, path, ctype, needle):
    got = rollup.route(path, built(work))
    assert got is not None and got[0].startswith(ctype) and needle in got[1]


@pytest.mark.parametrize("path", ["/nope", "/pm.txt"])
def test_unknown_routes(work, path):
    assert rollup.route(path, built(work)) is None


def test_server_answers_on_localhost(work, capsys):
    holder = {}
    rollup.Serve([rollup.Source("Work", work)], lambda: NOW, 0).execute(
        block=lambda srv: holder.setdefault("srv", srv)
    )
    srv = holder["srv"]
    assert srv.server_address[0] == "127.0.0.1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/pm.json?x=1") as resp:
            assert json.loads(resp.read())["level"] == "pm"
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/nope")
        assert err.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
    assert "roll-ups at http://localhost:" in capsys.readouterr().out


# --- CLI -------------------------------------------------------------------------------------


def test_cli_prints_each_format(work, capsys):
    src = ["--source", f"Work={work}"]
    assert rollup.main(["json", "--level", "cto", *src], env={}) == 0
    assert json.loads(capsys.readouterr().out)["level"] == "cto"
    assert rollup.main(["markdown", *src], env={}) == 0
    assert "# Product roll-up" in capsys.readouterr().out
    assert rollup.main(["html", *src], env={}) == 0
    assert "<html" in capsys.readouterr().out.lower()
    assert rollup.main(["sources", *src, "--source", f"Empty={work / 'none'}"], env={}) == 0
    out = capsys.readouterr().out
    assert "Work" in out and "Empty" in out


def test_cli_write_and_errors(work, tmp_path, capsys):
    out = tmp_path / "o"
    assert rollup.main(["write", "--out", str(out), "--source", f"W={work}"], env={}) == 0
    assert (out / "pm.md").is_file()
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


def test_cli_reads_config_from_the_environment(work, tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[[source]]\nlabel = "Cfg"\npath = "{work}"\n')
    monkeypatch.setenv(rollup.CONFIG_ENV, str(cfg))
    monkeypatch.setattr(sys, "argv", ["rollup.py", "sources"])
    assert rollup.main() == 0
    assert "Cfg" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("health", "markdown"),
    [
        (rollup.Health("", "", "", "", ()), "**Not set by owner**."),
        (
            rollup.Health("", "", "", "amber", ("CI red",)),
            "**Amber**, not set by owner. Automatic checks: CI red.",
        ),
        (
            rollup.Health("green", "On track.", "2026-11-01", "green", ()),
            "**Green**, set 2026-11-01: On track.",
        ),
        (
            rollup.Health("green", "Fine", "2026-11-01", "red", ("2 missed",)),
            "**Red**, capped by automatic checks (owner said Green on 2026-11-01: Fine). Automatic checks: 2 missed.",
        ),
    ],
)
def test_health_markdown_bolds_only_the_colour(health, markdown):
    assert health.markdown() == markdown


def test_health_html_bolds_only_the_colour_and_escapes_the_rest():
    health = rollup.Health("green", "<b>Fine</b>", "2026-11-01", "green", ("a & b",))
    assert health.html() == (
        "<strong>Green</strong>, set 2026-11-01: &lt;b&gt;Fine&lt;/b&gt;. "
        "Automatic checks: a &amp; b."
    )


def test_cto_explains_only_projects_with_something_to_say(work):
    md = rollup.LEVELS["cto"].markdown(built(work))
    bullets = [line for line in md.splitlines() if line.startswith("- **")]
    assert bullets and not any(line.endswith("**Not set by owner**.") for line in bullets)


# --- failures, dates, writing, ports, wording -------------------------------------------------


def only_source(path):
    return [rollup.Source("S", path)]


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
    make_queue(tmp_path / "x")

    def fail(*_args):
        raise error

    monkeypatch.setattr(rollup, "project_of", fail)
    [skipped] = rollup.build(only_source(tmp_path), NOW).skipped
    assert skipped.reason.startswith(type(error).__name__)


def test_an_undecodable_project_file_is_skipped_not_fatal(tmp_path):
    (make_queue(tmp_path / "x") / "project.toml").write_bytes(b"\xff\xfe")
    r = rollup.build(only_source(tmp_path), NOW)
    assert r.projects == () and "UnicodeDecodeError" in r.skipped[0].reason


def test_story_dates_are_dates_and_undated_briefs_are_not_shipped(tmp_path):
    q = make_queue(tmp_path / "x")
    brief(q, "done", "b8", updated="soon, ask Sam", crit="- [x] ok")
    brief(q, "done", "b9", updated="2026-10-20", crit="- [x] ok")
    r = rollup.build(only_source(tmp_path), NOW)
    assert [s.id for s in r.projects[0].shipped] == ["b1", "b9"]
    assert rollup.LEVELS["pm"].data(r)["projects"][0]["shipped"][1] == {
        "id": "b9",
        "title": "B9",
        "updated": "2026-10-20",
    }
    assert "Sam" not in json.dumps(rollup.LEVELS["pm"].data(r))


@pytest.mark.parametrize("held", ["not json", "[1]", '{"schema": "other"}'])
def test_write_refuses_a_folder_that_holds_something_else(work, tmp_path, held):
    out = tmp_path / "docs"
    out.mkdir()
    (out / "pm.json").write_text(held)
    (out / "notes.md").write_text("mine")
    with pytest.raises(rollup.RollupError, match="not an empty folder"):
        rollup.write(out, built(work))
    assert rollup.main(["write", "--out", str(out), "--source", f"W={work}"], env={}) == 2
    assert (out / "notes.md").read_text() == "mine" and (out / "pm.json").read_text() == held


def test_write_reports_a_destination_it_cannot_create(work, tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    out = blocker / "rollups"
    with pytest.raises(rollup.RollupError, match=r"cannot write .*rollups: Not a directory"):
        rollup.write(out, built(work))
    assert rollup.main(["write", "--out", str(out), "--source", f"W={work}"], env={}) == 2
    assert capsys.readouterr().err.startswith("error: cannot write ")


def test_write_rewrites_only_what_changed_apart_from_the_timestamp(work, tmp_path):
    out = tmp_path / "empty"
    out.mkdir()
    assert len(rollup.write(out, built(work))) == 5
    later = dt.datetime(2026, 10, 23, 13, 0, tzinfo=dt.timezone.utc)
    assert rollup.write(out, rollup.build([rollup.Source("Work", work)], later)) == []
    assert "Generated 2026-10-23T12:00:00" in (out / "pm.md").read_text()
    toml = work / "alpha" / ".tasks" / "project.toml"
    toml.write_text(toml.read_text().replace("Demo Project", "Renamed"))
    names = {p.name for p in rollup.write(out, rollup.build([rollup.Source("Work", work)], later))}
    assert names == {"pm.md", "pm.json", "cto.md", "cto.json"}
    assert rollup.main(["write", "--out", str(out), "--source", f"W={work}"], env={}) == 0


def test_a_port_in_use_or_out_of_range_is_a_one_line_error(work, capsys):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        assert (
            rollup.main(
                ["serve", "--port", str(taken.getsockname()[1]), "--source", f"W={work}"], env={}
            )
            == 2
        )
    assert rollup.main(["serve", "--port", "70000", "--source", f"W={work}"], env={}) == 2
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2 and all(line.startswith("error: cannot listen on port") for line in err)


def test_health_markdown_omits_the_date_clause_when_the_owner_gave_none():
    capped = rollup.Health("green", "Fine.", "", "red", ("2 missed",))
    assert capped.markdown() == (
        "**Red**, capped by automatic checks (owner said Green: Fine). Automatic checks: 2 missed."
    )
    assert (
        rollup.Health("green", "Fine.", "", "green", ()).markdown()
        == "**Green**, set by owner: Fine."
    )


def test_the_index_links_pages_by_file_in_a_folder_and_by_route_on_the_server(work):
    r = built(work)
    assert "[Product roll-up](pm.md)" in rollup.readme_markdown(r)
    got = rollup.route("/", r)
    assert got is not None and '<a href="pm">' in got[1] and '<a href="cto.json">' in got[1]


def zoned(root, zone):
    q = make_queue(root)
    toml = q / "project.toml"
    toml.write_text(
        toml.read_text().replace('release = "2026.12"', 'release = "2026.12"\ndue = "2026-10-23"')
    )
    brief(q, "done", "b7", updated="2026-10-09", crit="- [x] ok")
    (q / "tasks.toml").write_text(f'name = "Demo"\ntimezone = "{zone}"\n')
    return rollup.build(only_source(root), NOW).projects[0]


def test_today_missed_and_the_shipped_window_follow_the_queue_timezone(tmp_path):
    utc = zoned(tmp_path / "u", "UTC")
    ahead = zoned(
        tmp_path / "k", "Pacific/Kiritimati"
    )  # already 24 October when UTC says 23rd, 12:00
    assert (utc.milestones[1].missed, [s.id for s in utc.shipped]) == (False, ["b1", "b7"])
    assert (ahead.milestones[1].missed, [s.id for s in ahead.shipped]) == (True, ["b1"])


def test_an_unknown_timezone_warns_and_falls_back_to_utc(tmp_path, capsys):
    p = zoned(tmp_path, "Nowhere/Land")
    assert p.milestones[1].missed is False
    assert "Nowhere/Land" in capsys.readouterr().err


def test_an_unreadable_tasks_file_still_gives_a_utc_roll_up(tmp_path):
    q = make_queue(tmp_path)
    (q / "tasks.toml").write_text("not = [valid")
    assert [p.slug for p in rollup.build(only_source(tmp_path), NOW).projects] == ["demo"]


# --- two sources, and sources that are behind their upstream ----------------------------------


def test_two_sources_that_both_hold_projects_show_both_under_their_labels(work):
    sources = [
        rollup.Source("Alpha", work / "alpha"),
        rollup.Source("Beta", work / "beta"),
        rollup.Source("Both", work),  # reaches the same two queues again
    ]
    r = rollup.build(sources, NOW)
    assert [(p.source, p.slug) for p in r.projects] == [("Alpha", "demo"), ("Beta", "beta")]
    pm = rollup.LEVELS["pm"].markdown(r)
    assert "*Alpha.*" in pm and "*Beta.*" in pm and "*Both.*" not in pm
    assert rollup.LEVELS["pm"].data(r)["sources"] == ["Alpha", "Beta", "Both"]


def test_a_source_that_adds_no_projects_is_named_rather_than_left_out(work, tmp_path):
    notes = make_queue(tmp_path / "home" / "notes")
    (notes / "project.toml").unlink()  # briefs, but nothing declaring a project
    r = rollup.build([rollup.Source("Work", work), rollup.Source("Home", notes.parent)], NOW)
    assert [s.source for s in r.skipped] == ["Home"]
    md = rollup.LEVELS["pm"].markdown(r)
    assert "- Home: no projects (a project is a queue with a project.toml)" in md
    data = rollup.LEVELS["cto"].data(r)
    assert data["source_projects"] == [
        {"source": "Work", "projects": 2},
        {"source": "Home", "projects": 0},
    ]
    assert data["skipped"][0]["reason"] == rollup.NO_PROJECTS_IN_SOURCE
    assert "Home: no projects" in rollup.LEVELS["pm"].html(r)


def git(cwd, *args):
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", *identity, "-C", str(cwd), *args], check=True, capture_output=True)


def commit(cwd, name):
    (cwd / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", name)


@pytest.fixture
def tracked(tmp_path):
    """A work tree whose upstream is a bare repository, with nothing yet to pull."""
    bare = tmp_path / "upstream.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    mine = tmp_path / "mine"
    mine.mkdir()
    git(mine, "init", "-b", "main")
    commit(mine, "first")
    git(mine, "remote", "add", "origin", str(bare))
    git(mine, "push", "-u", "origin", "main")
    return mine


def fall_behind(tracked, commits):
    other = tracked.parent / "other"
    git(tracked.parent, "clone", str(tracked.parent / "upstream.git"), str(other))
    for n in range(commits):
        commit(other, f"later{n}")
    git(other, "push")
    git(tracked, "fetch")


def test_commits_behind_counts_what_the_upstream_has_and_never_fetches(tracked):
    assert rollup.commits_behind(tracked) == 0
    fall_behind(tracked, 1)
    assert rollup.commits_behind(tracked) == 1


def test_commits_behind_is_unknown_without_an_upstream_or_a_repository(tmp_path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    git(lonely, "init", "-b", "main")
    commit(lonely, "x")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert rollup.commits_behind(lonely) is None
    assert rollup.commits_behind(plain) is None


@pytest.mark.parametrize(
    "error", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5), PermissionError("x")]
)
def test_commits_behind_never_raises_when_git_is_missing_or_slow(tmp_path, monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert rollup.commits_behind(tmp_path) is None


@pytest.mark.parametrize("count,noun", [(1, "1 commit"), (2, "2 commits")])
def test_the_warning_names_the_source_and_the_remedy(count, noun):
    assert rollup.SourceWarning("Work", count).message == (
        f"Work is {noun} behind its upstream; pull for current status"
    )


def test_an_up_to_date_source_has_no_warning(tracked):
    assert rollup.Source("S", tracked).warning() is None


def stale(tracked):
    make_queue(tracked / "demo")
    fall_behind(tracked, 2)
    return rollup.build([rollup.Source("S", tracked)], NOW)


def test_a_stale_source_warns_in_json_markdown_and_html(tracked):
    r = stale(tracked)
    message = "S is 2 commits behind its upstream; pull for current status"
    assert rollup.LEVELS["pm"].data(r)["warnings"] == [
        {"source": "S", "behind": 2, "message": message}
    ]
    for level in rollup.LEVELS.values():
        assert f"## Source warnings\n\n- {message}\n" in level.markdown(r)
        assert f"<h2>Source warnings</h2><ul><li>{message}</li></ul>" in level.html(r)


def test_sources_command_shows_the_stale_warning(tracked, capsys):
    stale(tracked)
    assert rollup.main(["sources", "--source", f"S={tracked}"], env={}) == 0
    assert "  warning: S is 2 commits behind its upstream; pull for current status" in (
        capsys.readouterr().out
    )


def test_a_source_that_is_not_a_repository_is_silent(work, capsys):
    assert rollup.build([rollup.Source("Work", work)], NOW).warnings == ()
    assert rollup.main(["sources", "--source", f"Work={work}"], env={}) == 0
    assert "warning" not in capsys.readouterr().out


# --- the server remembers how far behind a source is, briefly ---------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_server_asks_git_once_per_source_per_ttl_window(work):
    calls = []
    clock = Clock()

    def behind(path):
        calls.append(path)
        return 2

    serve = rollup.Serve(
        [rollup.Source("Work", work), rollup.Source("Other", work / "alpha")],
        lambda: NOW,
        0,
        behind=rollup.BehindCounts(behind, clock),
    )
    first = serve.rollup()
    clock.now += rollup.BEHIND_TTL_SECONDS - 1
    assert serve.rollup() == first and len(calls) == 2
    assert [w.behind for w in first.warnings] == [2, 2]
    clock.now += 1
    serve.rollup()
    assert len(calls) == 4


def test_a_source_git_cannot_read_is_remembered_as_unknown(tmp_path):
    clock, calls = Clock(), []
    counts = rollup.BehindCounts(lambda path: calls.append(path), clock)
    assert counts(tmp_path) is None and counts(tmp_path) is None and calls == [tmp_path]


# --- what a milestone says, and the rows a project file gets wrong ----------------------------

HEAD = '[project]\ntitle = "T"\noutcome = "O"\n'


def test_a_milestone_percent_is_the_one_the_project_view_computes(tmp_path):
    q = make_queue(tmp_path / "demo")
    declared = pv.declared_milestones(q, pv.read_project(q))
    rows = pv.milestone_rows(declared, pv.project_briefs(q), NOW.date())
    r = rollup.build(only_source(tmp_path), NOW)
    assert [m.percent_done for m in r.projects[0].milestones] == [100, 50]
    assert [m.percent_done for m in r.projects[0].milestones] == [x["percent_done"] for x in rows]
    pm = rollup.LEVELS["pm"].data(r)["projects"][0]
    assert [m["percent_done"] for m in pm["now"]] == [50]


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
    q = make_queue(tmp_path / "demo")
    (q / "project.toml").write_text(HEAD + body)
    r = rollup.build(only_source(tmp_path), NOW)
    assert r.skipped == () and [m.id for m in r.projects[0].milestones] == ["m1"]
    warning = capsys.readouterr().err
    assert "milestones row 2 needs a text id" in warning and "that row is left out" in warning


@pytest.mark.parametrize("value", ['"m1"', "3"])
def test_milestones_that_are_not_a_list_warn_and_are_left_out(tmp_path, capsys, value):
    q = make_queue(tmp_path / "demo")
    (q / "project.toml").write_text(f"milestones = {value}\n{HEAD}")
    r = rollup.build(only_source(tmp_path), NOW)
    assert r.skipped == () and r.projects[0].milestones == ()
    assert "milestones must be [[milestones]] tables" in capsys.readouterr().err


def test_milestone_entries_that_are_not_tables_are_dropped_one_by_one(tmp_path, capsys):
    q = make_queue(tmp_path / "demo")
    (q / "project.toml").write_text(f"milestones = [1, 2]\n{HEAD}")
    r = rollup.build(only_source(tmp_path), NOW)
    assert r.skipped == () and r.projects[0].milestones == ()
    assert "milestones row 1 needs a text id" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["5", '""', '["a"]'])
def test_a_slug_that_is_not_text_falls_back_to_the_folder_name_with_a_warning(
    tmp_path, capsys, value
):
    q = make_queue(tmp_path / "folder")
    (q / "project.toml").write_text(HEAD.replace("[project]", f"[project]\nslug = {value}"))
    assert rollup.build(only_source(tmp_path), NOW).projects[0].slug == "folder"
    assert "slug must be text" in capsys.readouterr().err


def test_a_title_of_only_whitespace_is_missing(tmp_path):
    q = make_queue(tmp_path)
    (q / "project.toml").write_text('[project]\ntitle = "  "\noutcome = "O"\n')
    with pytest.raises(pv.ProjectConfigError, match=r"\[project\] title is required"):
        pv.read_project(q)


@pytest.mark.parametrize("colour", ['["red"]', "5", "{ a = 1 }"])
def test_a_health_colour_that_is_not_text_is_a_config_error_not_a_crash(tmp_path, colour):
    q = make_queue(tmp_path)
    (q / "project.toml").write_text(f'{HEAD}[health]\ncolour = {colour}\nreason = "r"\n')
    with pytest.raises(pv.ProjectConfigError, match="green, amber or red"):
        pv.read_project(q)


# --- writing over files that are not ours -----------------------------------------------------


def test_an_existing_file_that_is_not_utf8_is_replaced_not_fatal(work, tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    rollup.write(out, built(work))
    (out / "pm.md").write_bytes(b"\xff\xfe")
    assert [p.name for p in rollup.write(out, built(work))] == ["pm.md"]
    assert "Product roll-up" in (out / "pm.md").read_text()
    assert rollup.main(["write", "--out", str(out), "--source", f"W={work}"], env={}) == 0


# --- sources that add nothing -----------------------------------------------------------------


def test_a_source_whose_projects_all_sit_under_an_earlier_source_is_named(tmp_path):
    make_queue(tmp_path / "d")
    sources = [rollup.Source("A", tmp_path / "d"), rollup.Source("B", tmp_path / "d" / ".tasks")]
    r = rollup.build(sources, NOW)
    assert [(s.source, s.reason) for s in r.skipped] == [("B", rollup.SHOWN_UNDER_ANOTHER_SOURCE)]
    assert rollup.LEVELS["pm"].data(r)["source_projects"] == [
        {"source": "A", "projects": 1},
        {"source": "B", "projects": 0},
    ]
    assert "- B: every project it finds is already shown under another source" in (
        rollup.LEVELS["pm"].markdown(r)
    )


def test_a_build_walks_each_source_once(work, tmp_path, monkeypatch):
    calls = []
    real = rollup.Source.projects

    def counted(self):
        calls.append(self.label)
        return real(self)

    monkeypatch.setattr(rollup.Source, "projects", counted)
    (tmp_path / "empty").mkdir()
    rollup.build([rollup.Source("A", work), rollup.Source("B", tmp_path / "empty")], NOW)
    assert sorted(calls) == ["A", "B"]

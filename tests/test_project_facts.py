"""project_facts.py: every subcommand writes what facts.py reads back, history is only ever added to."""

import datetime as dt
import re

import facts
import project_facts
import pytest
from _concurrency import exclusive_lock, lock_path

from tests.queue_helpers import wait_for_pusher_idle
from tests.test_git_sync import committed_files, git, make_repo

TODAY = dt.date(2026, 11, 19)
HEX_NAME = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}\.toml")
SIGNED = "https://files.example.com/a.mp4?X-Amz-Signature=abc123"


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch):
    monkeypatch.setattr(project_facts, "today", lambda: TODAY)


@pytest.fixture
def queue(tmp_path):
    """A queue in no git repo at all, so nothing is committed."""
    q = tmp_path / "queue"
    q.mkdir()
    return q


def cli(queue, *args):
    return project_facts.main(["--root", str(queue), *args])


def written(queue, folder):
    return sorted((queue / folder).glob("*.toml"))


HEALTH = ("set-health", "--colour", "amber", "--reason", "Vendor late", "--set-by", "kay")


# --- history ---------------------------------------------------------------------------------


def test_set_health_adds_a_dated_file_the_loader_reads_back(queue, capsys):
    assert cli(queue, *HEALTH) == 0

    (path,) = written(queue, "history/health")
    assert HEX_NAME.fullmatch(path.name) and path.name.startswith("2026-11-19-")
    (row,) = facts.load_health(queue)
    assert (row.colour, row.reason, row.set_by, row.set_on) == (
        "amber",
        "Vendor late",
        "kay",
        TODAY,
    )
    assert capsys.readouterr().out.strip() == "recorded"


def test_set_health_on_names_the_day(queue):
    assert cli(queue, *HEALTH, "--on", "2026-11-01") == 0

    assert facts.current_health(queue).set_on == dt.date(2026, 11, 1)


def test_history_is_added_to_never_edited(queue):
    cli(queue, *HEALTH)
    (first,) = written(queue, "history/health")
    before = first.read_text()

    cli(queue, "set-health", "--colour", "red", "--reason", "Slipped", "--set-by", "kay")

    assert len(written(queue, "history/health")) == 2
    assert first.read_text() == before


def test_a_name_that_already_exists_is_never_reused(queue, monkeypatch):
    tokens = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(project_facts, "_token", lambda: next(tokens))
    cli(queue, *HEALTH)
    (first,) = written(queue, "history/health")
    before = first.read_text()

    cli(queue, "set-health", "--colour", "red", "--reason", "Slipped", "--set-by", "kay")

    assert [p.name for p in written(queue, "history/health")] == [
        "2026-11-19-aaaaaaaa.toml",
        "2026-11-19-bbbbbbbb.toml",
    ]
    assert first.read_text() == before


def test_the_token_is_eight_hex_characters():
    assert re.fullmatch(r"[0-9a-f]{8}", project_facts._token())


def test_today_is_the_calendar_day(monkeypatch):
    monkeypatch.undo()
    assert project_facts.today() == dt.date.today()


def test_set_due_names_the_milestone_in_the_file(queue):
    args = (
        "set-due",
        "--milestone",
        "m1",
        "--due",
        "2026-12-10",
        "--reason",
        "Scope",
        "--set-by",
        "kay",
    )
    assert cli(queue, *args) == 0

    (path,) = written(queue, "history/due")
    assert path.name.startswith("m1-2026-11-19-")
    (row,) = facts.load_due_rows(queue)["m1"]
    assert (row.due, row.set_on, row.set_by, row.reason) == (
        dt.date(2026, 12, 10),
        TODAY,
        "kay",
        "Scope",
    )


def test_set_ci_records_the_build(queue):
    assert cli(queue, "set-ci", "--state", "red", "--link", "https://ci.example.com/1") == 0

    (row,) = facts.load_ci(queue)
    assert (row.state, row.link, row.set_on) == ("red", "https://ci.example.com/1", TODAY)


# --- ask -------------------------------------------------------------------------------------


def ask_args(text):
    return ("set-ask", "--text", text)


def test_set_ask_creates_project_toml_when_missing(queue):
    assert cli(queue, *ask_args("Approve the vendor?")) == 0

    assert (queue / "project.toml").read_text() == '[ask]\nquestion = "Approve the vendor?"\n'


def test_set_ask_changes_only_the_ask_and_keeps_comments(queue):
    (queue / "project.toml").write_text(
        '# the plan\ntitle = "Demo" # name\n\n[health]\ncolour = "green"\nreason = "ok"\n'
        'ask = "not ours"\n\n[ask]\nquestion = "Old?"\n\n[[ask.options]]\nlabel = "A"\n\n'
        "[other]\nkeep = 1\n"
    )

    assert cli(queue, *ask_args("New?")) == 0

    text = (queue / "project.toml").read_text()
    assert text.startswith('# the plan\ntitle = "Demo" # name\n\n[health]')
    assert 'ask = "not ours"' in text
    assert text.endswith('[other]\nkeep = 1\n\n[ask]\nquestion = "New?"\n')
    assert "Old?" not in text and "options" not in text


def test_set_ask_replaces_a_plain_top_level_ask_key(queue):
    (queue / "project.toml").write_text('title = "Demo"\nask = "old"\n')

    assert cli(queue, *ask_args("New?")) == 0

    assert (queue / "project.toml").read_text() == 'title = "Demo"\n\n[ask]\nquestion = "New?"\n'


def test_set_ask_clear_removes_every_ask_part(queue):
    (queue / "project.toml").write_text(
        'title = "Demo"\n[ask]\nquestion = "Q?"\n[[ask.options]]\nlabel = "A"\n'
    )

    assert cli(queue, "set-ask", "--clear") == 0

    assert (queue / "project.toml").read_text() == 'title = "Demo"\n'


def test_set_ask_clear_that_empties_the_file_leaves_it_empty(queue):
    (queue / "project.toml").write_text('[ask]\nquestion = "Q?"\n')

    assert cli(queue, "set-ask", "--clear") == 0

    assert (queue / "project.toml").read_text() == ""


def test_clearing_an_ask_that_is_not_there_writes_nothing(queue, capsys):
    assert cli(queue, "set-ask", "--clear") == 0

    assert not (queue / "project.toml").exists()
    assert capsys.readouterr().out.strip() == "unchanged: clear ask"


def test_set_ask_refuses_a_project_toml_that_is_not_toml(queue, capsys):
    (queue / "project.toml").write_text("title = \n")

    assert cli(queue, *ask_args("Q?")) == 1
    assert "not valid TOML" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    ['note = """\n[ask]\nx = 1\n"""\n', 'ask = """\nlong\n"""\n'],
    ids=["header inside a string", "multi-line value"],
)
def test_set_ask_refuses_a_rewrite_that_would_touch_other_keys(queue, capsys, body):
    (queue / "project.toml").write_text(body)

    assert cli(queue, *ask_args("Q?")) == 1
    assert (queue / "project.toml").read_text() == body
    assert "ask cannot be rewritten" in capsys.readouterr().err


def test_set_ask_needs_text_or_clear_and_not_both(queue):
    assert cli(queue, "set-ask") == 1
    assert cli(queue, "set-ask", "--text", "Q?", "--clear") == 1


# --- per-brief facts -------------------------------------------------------------------------

FLAG = ("set-flag", "--brief", "b1", "--name", "new-ui", "--environment", "prod", "--state", "on")


def test_set_flag_creates_the_facts_file(queue):
    assert cli(queue, *FLAG, "--changed-on", "2026-11-02") == 0

    (flag,) = facts.load_brief_facts(queue, "b1").flags
    assert (flag.name, flag.environment, flag.state, flag.changed_on) == (
        "new-ui",
        "prod",
        "on",
        dt.date(2026, 11, 2),
    )


def test_set_flag_replaces_its_own_row_and_keeps_everything_else(queue):
    (queue / "facts").mkdir()
    (queue / "facts" / "b1.toml").write_text(
        'brief = "b1"\nshipped = "Done"\nextra = "kept"\n\n'
        '[[flag]]\nname = "new-ui"\nenvironment = "prod"\nstate = "off"\nchanged_on = 2026-10-01\n\n'
        '[[flag]]\nname = "new-ui"\nenvironment = "dev"\nstate = "off"\nchanged_on = 2026-10-01\n'
    )

    assert cli(queue, *FLAG) == 0

    table = facts.load_brief_facts(queue, "b1")
    states = {(f.name, f.environment): (f.state, f.changed_on) for f in table.current_flags}
    assert states == {
        ("new-ui", "prod"): ("on", TODAY),
        ("new-ui", "dev"): ("off", dt.date(2026, 10, 1)),
    }
    assert len(table.flags) == 2 and table.shipped == "Done"
    assert 'extra = "kept"' in (queue / "facts" / "b1.toml").read_text()


def test_add_evidence_appends_each_call(queue):
    args = (
        "add-evidence", "--brief", "b1", "--kind", "video", "--link", "https://v.example.com/1",
        "--caption", "Demo", "--access", "org",
    )  # fmt: skip
    assert cli(queue, *args) == 0
    assert cli(queue, *args, "--recorded-on", "2026-11-03") == 0

    first, second = facts.load_brief_facts(queue, "b1").evidence
    assert (first.kind, first.access, first.recorded_on) == ("video", "org", TODAY)
    assert second.recorded_on == dt.date(2026, 11, 3)


@pytest.mark.parametrize(
    ("link", "complaint"),
    [
        ("http://v.example.com/1", "--link: must be an https link"),
        ("https://", "--link: must be an https link"),
        (SIGNED, "--link: must not carry a credential"),
    ],
)
def test_add_evidence_refuses_a_link_that_is_not_plain_https(queue, capsys, link, complaint):
    args = ("add-evidence", "--brief", "b1", "--kind", "video", "--link", link)

    assert cli(queue, *args, "--caption", "Demo", "--access", "org") == 1
    assert complaint in capsys.readouterr().err
    assert not (queue / "facts").exists()


def test_set_shipped_records_the_line_the_day_and_the_release(queue):
    assert (
        cli(queue, "set-shipped", "--brief", "b1", "--line", "Shipped it", "--release", "r1") == 0
    )

    shipped = facts.load_brief_facts(queue, "b1")
    assert (shipped.shipped, shipped.shipped_on, shipped.release) == ("Shipped it", TODAY, "r1")


def test_set_shipped_without_a_release_keeps_the_recorded_one(queue):
    cli(queue, "set-shipped", "--brief", "b1", "--line", "First", "--release", "r1")

    assert cli(queue, "set-shipped", "--brief", "b1", "--line", "Second", "--on", "2026-11-20") == 0

    shipped = facts.load_brief_facts(queue, "b1")
    assert (shipped.shipped, shipped.shipped_on, shipped.release) == (
        "Second",
        dt.date(2026, 11, 20),
        "r1",
    )


def test_a_facts_file_that_is_not_toml_is_refused_and_left_alone(queue, capsys):
    (queue / "facts").mkdir()
    (queue / "facts" / "b1.toml").write_text("brief = \n")

    assert cli(queue, *FLAG) == 1
    assert "not valid TOML" in capsys.readouterr().err
    assert (queue / "facts" / "b1.toml").read_text() == "brief = \n"


def test_a_facts_file_naming_another_brief_is_refused(queue, capsys):
    (queue / "facts").mkdir()
    (queue / "facts" / "b1.toml").write_text('brief = "b2"\n')

    assert cli(queue, *FLAG) == 1
    assert "--brief" in capsys.readouterr().err


@pytest.mark.parametrize("body", ['flag = "x"\n', "flag = [1]\n"], ids=["not a list", "not tables"])
def test_a_facts_file_whose_rows_are_not_tables_is_refused(queue, capsys, body):
    (queue / "facts").mkdir()
    (queue / "facts" / "b1.toml").write_text(f'brief = "b1"\n{body}')

    assert cli(queue, *FLAG) == 1
    assert "flag must be [[flag]] tables" in capsys.readouterr().err


# --- arguments -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "complaint"),
    [
        (("set-health", "--colour", "blue", "--reason", "r", "--set-by", "k"), "--colour"),
        (("set-health", "--colour", "red", "--reason", "  ", "--set-by", "k"), "--reason"),
        (("set-health", "--colour", "red", "--reason", "r"), "--set-by"),
        (("set-health", "--colour", "red", "--reason", "r", "--set-by", "k", "--on", "11/19"), "--on"),
        (("set-health", "--colour", "red", "--reason", "r", "--set-by", "k", "--on", "2026-13-40"), "--on"),
        (("set-health", "--colour", "red", "--reason", SIGNED, "--set-by", "k"), "--reason"),
        (("set-due", "--milestone", "../x", "--due", "2026-12-01", "--reason", "r", "--set-by", "k"), "--milestone"),
        (("set-flag", "--brief", "b1", "--name", "n", "--environment", "e", "--state", "maybe"), "--state"),
        (("set-ci", "--state", "green", "--link", "ftp://x"), "--link"),
        (("set-shipped", "--brief", ".", "--line", "l"), "--brief"),
        (("set-nothing",), "set-nothing"),
        ((), "subcommand"),
    ],
)  # fmt: skip
def test_a_bad_argument_exits_1_with_one_line_naming_it_and_writes_nothing(
    queue, capsys, args, complaint
):
    assert cli(queue, *args) == 1

    err = capsys.readouterr().err.strip()
    assert err.startswith("project_facts: ") and complaint in err and "\n" not in err
    assert sorted(p.name for p in queue.iterdir()) == []


def test_root_must_be_an_existing_directory(tmp_path, capsys):
    assert project_facts.main(["--root", str(tmp_path / "nope"), *HEALTH]) == 1
    assert "--root" in capsys.readouterr().err


def test_help_exits_zero():
    with pytest.raises(SystemExit) as stop:
        project_facts.main(["--help"])
    assert stop.value.code == 0


def test_every_subcommand_has_its_own_name():
    names = [c.name for c in project_facts.COMMANDS]
    assert len(set(names)) == len(names) == 7


# --- lock and commit -------------------------------------------------------------------------


def test_a_held_lock_times_out_with_exit_2_and_writes_nothing(queue, monkeypatch, capsys):
    monkeypatch.setattr(project_facts, "LOCK_TIMEOUT", 0.0)

    with exclusive_lock(lock_path(queue)):
        assert cli(queue, *HEALTH) == 2

    assert "lock" in capsys.readouterr().err
    assert not (queue / "history").exists()


def test_a_queue_that_is_its_own_repo_commits_inside_the_lock(tmp_path, capsys):
    repo = make_repo(tmp_path)

    assert cli(repo, *HEALTH) == 0

    assert capsys.readouterr().out.strip() == "recorded: set health amber"
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "facts: set health amber"
    assert [f for f in committed_files(repo) if f.startswith("history/health/")]
    assert git(repo, "status", "--porcelain").stdout.strip() == ""
    assert wait_for_pusher_idle(repo)


def test_the_commit_holds_only_what_the_command_wrote(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "scratch.txt").write_text("not mine")

    assert cli(repo, "set-ci", "--state", "green", "--link", "https://ci.example.com/2") == 0

    assert "scratch.txt" not in committed_files(repo)
    assert wait_for_pusher_idle(repo)


def test_a_queue_inside_a_host_repo_is_recorded_but_not_committed(tmp_path, capsys):
    host = make_repo(tmp_path)
    queue = host / "project" / ".tasks"
    queue.mkdir(parents=True)
    before = git(host, "rev-parse", "HEAD").stdout

    assert cli(queue, *HEALTH) == 0

    assert capsys.readouterr().out.strip() == f"recorded, ships with the next land of {host.name}"
    assert git(host, "rev-parse", "HEAD").stdout == before
    assert written(queue, "history/health")


def test_an_unchanged_ask_is_not_committed(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    calls = []
    monkeypatch.setattr(project_facts, "sync_commit", lambda *a, **k: calls.append(a))

    assert cli(repo, "set-ask", "--clear") == 0

    assert calls == []
    assert capsys.readouterr().out.strip() == "unchanged: clear ask"


def test_the_commit_is_made_with_the_lock_already_held(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    seen = {}

    def fake(message, root, ids=(), paths=(), *, lock_held=False):
        seen.update(message=message, paths=paths, lock_held=lock_held)
        return True

    monkeypatch.setattr(project_facts, "sync_commit", fake)

    assert cli(repo, "set-ask", "--text", "Q?") == 0

    assert seen == {"message": "facts: set ask", "paths": ("project.toml",), "lock_held": True}


@pytest.mark.parametrize(
    ("args", "subject"),
    [
        (HEALTH, "facts: set health amber"),
        (
            (
                "set-due",
                "--milestone",
                "m1",
                "--due",
                "2026-12-10",
                "--reason",
                "r",
                "--set-by",
                "k",
            ),
            "facts: set m1 due 2026-12-10",
        ),
        (
            ("set-ci", "--state", "green", "--link", "https://ci.example.com/3"),
            "facts: set ci green",
        ),
        (("set-ask", "--text", "Q?"), "facts: set ask"),
        (FLAG, "facts: b1: flag new-ui on in prod"),
        (
            (
                "add-evidence",
                "--brief",
                "b1",
                "--kind",
                "screenshot",
                "--link",
                "https://i.example.com/1",
            ),
            "facts: b1: add screenshot evidence",
        ),
        (("set-shipped", "--brief", "b1", "--line", "Done"), "facts: b1: shipped"),
    ],
)
def test_each_commit_says_what_it_recorded(tmp_path, monkeypatch, args, subject):
    repo = make_repo(tmp_path)
    subjects = []
    monkeypatch.setattr(
        project_facts, "sync_commit", lambda message, *a, **k: subjects.append(message)
    )
    extra = ("--caption", "c", "--access", "public") if args[0] == "add-evidence" else ()

    assert cli(repo, *args, *extra) == 0

    assert subjects == [subject]

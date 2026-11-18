"""bind_scope.py: the CLI that binds a session to a queue scope and grows a fixed set."""

import multiprocessing
import tomllib

import _paths
import bind_scope
import pytest
import scope_binding
from scope_binding import FixedMode, NoneMode, OngoingMode

from tests.queue_helpers import make_queue


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setenv(_paths.SESSIONS_ENV, str(directory))
    return directory


@pytest.fixture
def queue(tmp_path):
    return make_queue(tmp_path, {"a": {"tags": "[project:checkout]"}, "b": {"tags": "[other]"}})


def run(*argv, queue=None):
    root = ["--root", str(queue)] if queue else []
    bind_scope.main(["--session", "s1", *argv, *root])


def test_fixed_records_the_members_at_bind_time(sessions, queue, capsys):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    binding = scope_binding.read("s1")
    assert isinstance(binding.mode, FixedMode) and binding.members == ("a",)
    assert "project:checkout (fixed, 1 member(s))" in capsys.readouterr().out


@pytest.mark.parametrize("mode, mode_type", [("ongoing", OngoingMode), ("none", NoneMode)])
def test_the_other_modes_bind_without_members(sessions, queue, mode, mode_type):
    run("--scope", "tag:docs", "--mode", mode, queue=queue)
    binding = scope_binding.read("s1")
    assert isinstance(binding.mode, mode_type) and binding.members == ()


def test_binding_keeps_the_sessions_root(sessions, queue):
    sessions.mkdir()
    (sessions / "s1.toml").write_text('root = "/q"\nextra = 1\n')
    run("--scope", "all", "--mode", "ongoing", queue=queue)
    stored = tomllib.loads((sessions / "s1.toml").read_text())
    assert stored["root"] == "/q" and stored["extra"] == 1


def test_a_fixed_set_grows_with_a_reason(sessions, queue):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    run("--add", "b", "--reason", "a needs it", queue=queue)
    binding = scope_binding.read("s1")
    assert binding.pull_scope().slugs == {"a", "b"}
    assert [(a.slug, a.reason) for a in binding.additions] == [("b", "a needs it")]


def test_a_bind_and_an_addition_can_go_in_one_call(sessions, queue):
    run(
        "--scope",
        "project:checkout",
        "--mode",
        "fixed",
        "--add",
        "b",
        "--reason",
        "why",
        queue=queue,
    )
    assert scope_binding.read("s1").pull_scope().slugs == {"a", "b"}


def test_an_addition_without_a_reason_is_refused(sessions, queue, capsys):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    with pytest.raises(SystemExit) as refused:
        run("--add", "b")
    assert refused.value.code == 2
    assert "--add needs --reason" in capsys.readouterr().err
    assert scope_binding.read("s1").additions == ()


def test_a_blank_reason_is_refused_by_the_binding(sessions, queue):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    with pytest.raises(SystemExit, match="needs a reason"):
        run("--add", "b", "--reason", " ")


@pytest.mark.parametrize("mode", ["ongoing", "none"])
def test_an_addition_to_a_non_fixed_binding_is_refused(sessions, queue, mode):
    run("--scope", "all", "--mode", mode, queue=queue)
    with pytest.raises(SystemExit, match=f"a {mode} binding takes no additions"):
        run("--add", "b", "--reason", "why")
    assert scope_binding.read("s1").additions == ()


def test_an_addition_needs_an_existing_binding(sessions):
    with pytest.raises(SystemExit, match="has no binding to add to"):
        run("--add", "b", "--reason", "why")


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--scope", "all"], "--scope and --mode go together"),
        (["--mode", "none"], "--scope and --mode go together"),
        ([], "give --scope and --mode"),
        (["--scope", "all", "--mode", "sometimes"], "invalid choice"),
    ],
)
def test_flags_that_mean_nothing_are_refused(sessions, capsys, argv, message):
    with pytest.raises(SystemExit) as refused:
        run(*argv)
    assert refused.value.code == 2 and message in capsys.readouterr().err


def test_a_bad_scope_or_session_id_is_a_clear_exit(sessions, queue):
    with pytest.raises(SystemExit, match="--scope must be one of"):
        run("--scope", "nonsense", "--mode", "none", queue=queue)
    with pytest.raises(SystemExit, match="invalid session id"):
        bind_scope.main(["--session", "../x", "--scope", "all", "--mode", "none"])


def test_without_root_the_resolved_queue_is_used(sessions, queue, monkeypatch):
    monkeypatch.setenv(_paths.ENV_VAR, str(queue))
    run("--scope", "project:checkout", "--mode", "fixed")
    assert scope_binding.read("s1").members == ("a",)


def test_a_fixed_set_is_not_replaced_without_a_reason(sessions, queue):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    with pytest.raises(SystemExit, match="replacing the fixed set project:checkout needs a reason"):
        run("--scope", "all", "--mode", "fixed", queue=queue)
    assert scope_binding.read("s1").members == ("a",)


def test_a_fixed_set_replaced_with_a_reason_keeps_why(sessions, queue):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    run("--scope", "all", "--mode", "fixed", "--reason", "the goal grew", queue=queue)
    binding = scope_binding.read("s1")
    assert binding.members == ("a", "b")
    assert [(r.selector, r.mode, r.reason) for r in binding.rebinds] == [
        ("project:checkout", "fixed", "the goal grew")
    ]


def test_an_ongoing_binding_is_replaced_freely_and_keeps_earlier_rebinds(sessions, queue):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    run("--scope", "tag:docs", "--mode", "ongoing", "--reason", "now ongoing", queue=queue)
    run("--scope", "all", "--mode", "none", queue=queue)
    assert [r.reason for r in scope_binding.read("s1").rebinds] == ["now ongoing"]


def test_an_addition_that_names_no_brief_is_refused(sessions, queue):
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    with pytest.raises(SystemExit, match="typo-slug is not a brief"):
        run("--add", "typo-slug", "--reason", "why", queue=queue)
    assert scope_binding.read("s1").additions == ()


def test_a_writer_that_cannot_get_the_lock_is_a_clear_exit(sessions, queue, monkeypatch):
    import contextlib

    import _toml

    monkeypatch.setattr(_toml, "exclusive_lock", lambda *_a, **_k: contextlib.nullcontext(False))
    with pytest.raises(SystemExit, match="locked by another writer"):
        run("--scope", "all", "--mode", "none", queue=queue)


def _add_in_another_process(sessions, queue, slug):
    import os

    os.environ[_paths.SESSIONS_ENV] = str(sessions)
    run("--add", slug, "--reason", "concurrent", queue=queue)


def test_concurrent_additions_both_survive(sessions, tmp_path):
    queue = make_queue(
        tmp_path, {"a": {"tags": "[project:checkout]"}, **{f"x{n}": {} for n in range(6)}}
    )
    run("--scope", "project:checkout", "--mode", "fixed", queue=queue)
    context = multiprocessing.get_context("fork")
    writers = [
        context.Process(target=_add_in_another_process, args=(sessions, queue, f"x{n}"))
        for n in range(6)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert {a.slug for a in scope_binding.read("s1").additions} == {f"x{n}" for n in range(6)}

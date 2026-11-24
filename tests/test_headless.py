"""Tests for headless.py: the launch models and the isolated, schema-bound claude -p call."""

import json
import subprocess

import headless
import pytest
from headless import (
    DirectLaunch,
    ScriptLaunch,
    ask_json,
    command,
    configured_launch,
    launcher_env,
    launcher_script,
    rank_table,
    resolve_launch,
)

from tests.queue_helpers import installed_launcher

SCHEMA = {"type": "object"}
NOTICE = "[rank] local_ok is deprecated; use local_reason"
NO_DEFAULT = "no default launcher is assumed"


def _exiting(status, stderr):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.CalledProcessError(status, cmd, stderr=stderr)

    return run, calls


def test_a_failure_names_its_exit_status_and_reason_not_the_command(monkeypatch, capsys):
    run, _ = _exiting(1, "launcher: account refused, exit 1\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None
    err = capsys.readouterr().err
    assert "exit 1: launcher: account refused, exit 1" in err and "--json-schema" not in err


@pytest.mark.parametrize("status", sorted(headless.NO_CAPACITY))
def test_no_capacity_skips_the_rest_of_the_run(monkeypatch, capsys, status):
    run, calls = _exiting(status, "")
    monkeypatch.setattr(subprocess, "run", run)
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None
    assert len(calls) == 1 and "skipping this run's remaining calls" in capsys.readouterr().err


def test_a_timeout_is_none_and_never_prints_the_command(monkeypatch, capsys):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(subprocess, "run", slow)
    assert ask_json("prompt", SCHEMA, "sonnet", "why", timeout=5) is None
    err = capsys.readouterr().err
    assert "timed out after 5s" in err and "--json-schema" not in err


def test_reset_capacity_lets_the_next_call_spawn_again(monkeypatch):
    run, calls = _exiting(4, "")
    monkeypatch.setattr(subprocess, "run", run)
    ask_json("prompt", SCHEMA, "sonnet", "why")
    ask_json("prompt", SCHEMA, "sonnet", "why")
    assert len(calls) == 1
    headless.reset_capacity()
    ask_json("prompt", SCHEMA, "sonnet", "why")
    assert len(calls) == 2


def test_an_ordinary_failure_does_not_stop_the_run(monkeypatch):
    run, calls = _exiting(1, "boom")
    monkeypatch.setattr(subprocess, "run", run)
    ask_json("prompt", SCHEMA, "sonnet", "why")
    ask_json("prompt", SCHEMA, "sonnet", "why")
    assert len(calls) == 2


def _script(tmp_path, name="launcher.sh", mode=0o755):
    script = tmp_path / name
    script.write_text("#!/bin/sh\n")
    script.chmod(mode)
    return script


def test_uses_the_launcher_only_when_installed(tmp_path, monkeypatch):
    script = tmp_path / "launcher.sh"
    monkeypatch.setenv("CLAUDE_TASKS_LAUNCHER", str(script))
    assert resolve_launch() == DirectLaunch()
    _script(tmp_path)
    assert resolve_launch() == ScriptLaunch(str(script))


def test_defaults_to_claude_on_path_without_a_launcher(capsys):
    assert resolve_launch() == DirectLaunch()
    assert capsys.readouterr().err == ""


def test_a_configured_launcher_wins_over_the_environment(tmp_path, monkeypatch):
    configured, from_env = _script(tmp_path, "a.sh"), _script(tmp_path, "b.sh")
    monkeypatch.setenv("CLAUDE_TASKS_LAUNCHER", str(from_env))
    assert resolve_launch(str(configured)) == ScriptLaunch(str(configured))


def test_a_missing_configured_launcher_falls_through_to_the_environment(tmp_path, monkeypatch):
    from_env = _script(tmp_path)
    monkeypatch.setenv("CLAUDE_TASKS_LAUNCHER", str(from_env))
    assert launcher_script("/nowhere/x.sh") == from_env


def test_a_launcher_that_is_not_executable_is_refused_with_one_notice(tmp_path, capsys):
    script = _script(tmp_path, mode=0o644)
    assert resolve_launch(str(script)) == DirectLaunch()
    resolve_launch(str(script))
    err = capsys.readouterr().err
    assert err.count(f"launcher {script} not found or not executable; using claude -p") == 1


def test_the_deprecated_alias_still_names_a_launcher_and_says_so(tmp_path, monkeypatch, capsys):
    script = _script(tmp_path)
    monkeypatch.setenv("CLAUDE_ANY_BIN", str(script))
    assert launcher_script() == script
    assert "$CLAUDE_ANY_BIN is deprecated; set $CLAUDE_TASKS_LAUNCHER" in capsys.readouterr().err


def test_a_direct_launch_adds_dash_p_and_no_environment():
    assert DirectLaunch("claude").argv(["--x"]) == ["claude", "-p", "--x"]
    assert DirectLaunch().env("why") is None


def test_a_script_launch_runs_the_script_with_the_reason_in_its_environment():
    launch = ScriptLaunch("/w/l.sh")
    assert launch.argv(["--x"]) == ["/w/l.sh", "--x"]
    env = launch.env("why")
    assert env is not None and env["CLAUDE_TASKS_LOCAL_REASON"] == "why"


def test_rank_table_is_empty_without_a_rank_section(tmp_path):
    assert rank_table(tmp_path) == {}
    assert configured_launch(tmp_path) == DirectLaunch()


def test_the_configured_launch_reads_the_rank_table(tmp_path):
    script = installed_launcher(tmp_path)
    (tmp_path / "tasks.toml").write_text(f'[rank]\nlauncher = "{script}"\n')
    assert rank_table(tmp_path)["launcher"] == str(script)
    assert configured_launch(tmp_path) == ScriptLaunch(str(script))


def _local_ok(tmp_path, extra=""):
    (tmp_path / "tasks.toml").write_text(f'[rank]\nlocal_ok = "old"\n{extra}')


def test_local_ok_without_any_launcher_says_no_default_launcher_is_assumed(tmp_path, capsys):
    _local_ok(tmp_path)
    assert rank_table(tmp_path)["local_reason"] == "old"
    err = capsys.readouterr().err
    assert NOTICE in err and "set `[rank] launcher` or $CLAUDE_TASKS_LAUNCHER" in err
    assert NO_DEFAULT in err


def test_local_ok_with_a_configured_launcher_gives_the_plain_notice(tmp_path, capsys):
    _local_ok(tmp_path, 'launcher = "/x/l.sh"\n')
    rank_table(tmp_path)
    err = capsys.readouterr().err
    assert NOTICE in err and NO_DEFAULT not in err


def test_local_ok_with_the_environment_launcher_gives_the_plain_notice(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CLAUDE_TASKS_LAUNCHER", "/x/l.sh")
    _local_ok(tmp_path)
    rank_table(tmp_path)
    assert NO_DEFAULT not in capsys.readouterr().err


def test_a_launcher_that_is_not_a_path_is_ignored_with_a_notice(tmp_path, capsys):
    (tmp_path / "tasks.toml").write_text("[rank]\nlauncher = 1\n")
    assert "launcher" not in rank_table(tmp_path)
    assert isinstance(configured_launch(tmp_path), DirectLaunch)
    assert "[rank] launcher must be a path, ignoring 1" in capsys.readouterr().err


def test_command_turns_off_tools_mcp_settings_and_sessions():
    cmd = command("sonnet", SCHEMA, DirectLaunch("claude"))
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert {"--strict-mcp-config", "--no-session-persistence"} <= set(cmd)
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA


def test_command_starts_with_the_launchers_own_argv():
    assert command("sonnet", SCHEMA, ScriptLaunch("/w/l.sh"))[0] == "/w/l.sh"


def test_launcher_env_sets_the_generic_and_the_legacy_names(monkeypatch):
    monkeypatch.setenv("KEEP", "1")
    env = launcher_env("why")
    assert (env["CLAUDE_TASKS_STDIN"], env["CLAUDE_TASKS_LOCAL_REASON"]) == ("1", "why")
    assert (env["CLAUDE_ANY_STDIN"], env["CLAUDE_LOCAL_OK"]) == ("1", "read-only-research:why")
    assert env["KEEP"] == "1"


def _run_returning(stdout):
    def run(cmd, **kw):
        assert kw["input"] == "prompt"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout)

    return run


def test_returns_the_structured_output(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _run_returning(json.dumps({"structured_output": {"a": 1}}))
    )
    assert ask_json("prompt", SCHEMA, "sonnet", "why") == {"a": 1}


def test_ask_json_runs_the_launch_it_is_given_and_tells_it_why(tmp_path, monkeypatch):
    seen = []

    def run(cmd, **kw):
        seen.append((cmd[0], kw["env"]["CLAUDE_TASKS_LOCAL_REASON"]))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"structured_output": {}}))

    monkeypatch.setattr(subprocess, "run", run)
    ask_json("prompt", SCHEMA, "sonnet", "why", launch=ScriptLaunch("/w/l.sh"))
    assert seen == [("/w/l.sh", "why")]


@pytest.mark.parametrize("stdout", ["not json", "[]"])
def test_bad_output_is_none(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", _run_returning(stdout))
    assert ask_json("prompt", SCHEMA, "sonnet", "why") is None


def test_a_failed_call_is_none_and_names_the_program(monkeypatch, capsys):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(3, cmd)

    monkeypatch.setattr(subprocess, "run", boom)
    assert ask_json("prompt", SCHEMA, "sonnet", "why", launch=DirectLaunch("claude")) is None
    assert "headless call (claude) failed" in capsys.readouterr().err


def _launcher_that_records(tmp_path):
    script = tmp_path / "launcher.sh"
    script.write_text('#!/bin/sh\nprintf "%s|%s|%s" "$1" "$EXTRA" "$PWD" > "$OUT"\n')
    script.chmod(0o755)
    return script


def _wait_for(path):
    import time

    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return path.read_text()


def test_a_detached_run_is_not_waited_for_and_gets_its_args_env_and_cwd(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("OUT", str(out))
    work = tmp_path / "work"
    work.mkdir()
    launch = ScriptLaunch(str(_launcher_that_records(tmp_path)))
    log = tmp_path / "logs" / "run.log"
    pid = headless.spawn_detached(launch, ["go"], "why", log, work, {"EXTRA": "x"})
    assert pid > 0 and log.exists()
    assert _wait_for(out) == f"go|x|{work.resolve()}"


def test_a_detached_run_appends_its_output_to_the_log(tmp_path, monkeypatch):
    log = tmp_path / "run.log"
    log.write_bytes(b"earlier\n")
    script = tmp_path / "talk.sh"
    script.write_text("#!/bin/sh\necho hello\n")
    script.chmod(0o755)
    headless.spawn_detached(ScriptLaunch(str(script)), [], "why", log, tmp_path)
    import time

    deadline = time.monotonic() + 10
    while b"hello" not in log.read_bytes() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert log.read_bytes() == b"earlier\nhello\n"


def test_a_launcher_that_does_not_exist_raises_so_the_caller_can_report_it(tmp_path):
    with pytest.raises(OSError):
        headless.spawn_detached(
            ScriptLaunch(str(tmp_path / "missing.sh")), [], "why", tmp_path / "l.log", tmp_path
        )

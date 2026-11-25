"""The LaunchAgent template and the script that renders it; nothing here installs or loads anything."""

import os
import pathlib
import plistlib
import shutil
import subprocess
import sys

import pytest
from maintain_config import DEFAULT_HYGIENE_INTERVAL_MINUTES

LAUNCHD = pathlib.Path(__file__).resolve().parent.parent / "launchd"
INSTALL = LAUNCHD / "install.sh"
BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def render(tmp_path, **env):
    """The plist `install.sh --print` renders, with HOME pointed at a scratch directory."""
    run = subprocess.run(
        [BASH, str(INSTALL), "--print"],
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path), "PYTHON": sys.executable, **env},
        text=True,
    )
    return run, (plistlib.loads(run.stdout.encode()) if run.returncode == 0 else None)


def test_the_agent_runs_the_laptop_tick_at_the_default_hygiene_interval(tmp_path):
    run, plist = render(tmp_path, QUEUE="/q")
    assert run.returncode == 0
    assert plist["StartInterval"] == DEFAULT_HYGIENE_INTERVAL_MINUTES * 60 == 4 * 60 * 60
    assert plist["ProgramArguments"][1:] == [
        str(LAUNCHD.parent / "scripts" / "maintain.py"),
        "--tier",
        "laptop",
        "--root",
        "/q",
    ]


def test_the_interval_follows_the_queues_configured_hygiene_interval(tmp_path):
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "tasks.toml").write_text("[maintain]\nhygiene_interval_minutes = 90\n")
    assert render(tmp_path, QUEUE=str(queue))[1]["StartInterval"] == 90 * 60


def test_the_agent_logs_to_the_library_and_lets_the_run_outlive_the_tick(tmp_path):
    _, plist = render(tmp_path)
    log = str(tmp_path / "Library/Logs/claude-tasks-maintain.log")
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == log
    assert plist["AbandonProcessGroup"] is True


def test_the_program_is_an_absolute_python_of_a_supported_version(tmp_path):
    _, plist = render(tmp_path)
    program = pathlib.Path(plist["ProgramArguments"][0])
    assert program.is_absolute() and program.exists()


def test_the_launcher_is_passed_on_when_given_and_empty_otherwise(tmp_path):
    assert (
        render(tmp_path, LAUNCHER="/l.sh")[1]["EnvironmentVariables"]["CLAUDE_TASKS_LAUNCHER"]
        == "/l.sh"
    )
    assert render(tmp_path, LAUNCHER="")[1]["EnvironmentVariables"]["CLAUDE_TASKS_LAUNCHER"] == ""


def test_printing_writes_nothing_under_home(tmp_path):
    render(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_no_supported_python_is_an_error_not_a_broken_plist(tmp_path):
    run, _ = render(tmp_path, PYTHON="/nonexistent", PATH="/nonexistent")
    assert run.returncode != 0 and run.stdout == ""


def test_an_unknown_option_prints_the_usage_and_exits_two(tmp_path):
    run = subprocess.run(
        [BASH, str(INSTALL), "--bogus"],
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
    )
    assert run.returncode == 2 and "usage:" in run.stderr

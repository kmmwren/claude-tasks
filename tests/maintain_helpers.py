"""Shared builders for the maintain tests: a bare origin, clones of it, and timestamps."""

import datetime
import pathlib
import subprocess

import pytest
from maintain_state import Counts, Outcome, Tier, TierRun

T0 = datetime.datetime(2026, 11, 17, 9, 0, tzinfo=datetime.UTC)
HOUR = datetime.timedelta(hours=1)


def git(where: pathlib.Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """No global or system git config, so the identity fallback is exercised the same everywhere."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path_factory.mktemp("gitcfg") / "none"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def make_origin(tmp_path: pathlib.Path) -> pathlib.Path:
    """A bare origin whose main holds a queue's tasks.toml."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], check=True
    )
    seed = clone(tmp_path, origin, "seed")
    (seed / "tasks.toml").write_text('name = "T"\n')
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "queue")
    git(seed, "push", "-q", "origin", "main")
    return origin


def clone(tmp_path: pathlib.Path, origin: pathlib.Path, name: str) -> pathlib.Path:
    path = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True, capture_output=True)
    git(path, "config", "user.email", f"{name}@example.com")
    git(path, "config", "user.name", name)
    git(path, "checkout", "-q", "-B", "main")
    return path


def tier_run(
    tier: Tier = Tier.DETERMINISTIC,
    finished: datetime.datetime = T0,
    runner: str = "laptop",
) -> TierRun:
    return TierRun(tier, finished - HOUR / 60, finished, runner, Outcome.OK, Counts(2, 0, 1))

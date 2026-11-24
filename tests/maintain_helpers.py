"""Builders for the maintain tests: a queue repo with a bare origin, wake briefs and a recording launcher."""

import datetime as dt
import pathlib
import subprocess
from collections.abc import Callable, Sequence

from claim import Claimer
from headless import Launch, ScriptLaunch
from maintain import Maintainer
from maintain_config import MaintainSettings, Tier

from tests.queue_helpers import brief_text
from tests.test_git_sync import git, make_repo

NOW = dt.datetime(2026, 11, 26, 9, 0, tzinfo=dt.timezone.utc)
PAST = "2026-11-25"
FUTURE = "2026-11-30"
Spawned = list[tuple[Sequence[str], dict[str, str]]]


def wake_brief(brief_id: str, **fields: str) -> str:
    """A ready brief whose frontmatter also carries `fields`."""
    extra = "".join(f"{key}: {value}\n" for key, value in fields.items())
    return brief_text(brief_id).replace("\n---\n\n", f"\n{extra}---\n\n", 1)


def push_all(clone: pathlib.Path, message: str) -> None:
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", message)
    git(clone, "push", "-q", "origin", "main")


def queue_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A queue clone with a tasks.toml, so settings load without warnings."""
    clone = make_repo(tmp_path)
    (clone / "tasks.toml").write_text("")
    push_all(clone, "tasks.toml")
    return clone


def add_brief(
    clone: pathlib.Path, brief_id: str, folder: str = "ready", **fields: str
) -> pathlib.Path:
    """Write the brief into `folder` and publish it."""
    path = clone / folder / f"{brief_id}.md"
    path.write_text(wake_brief(brief_id, **fields))
    push_all(clone, f"add {brief_id}")
    return path


def second_clone(tmp_path: pathlib.Path, name: str = "other") -> pathlib.Path:
    """Another checkout of the same origin, as a second runner would have."""
    clone = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote"), str(clone)], check=True)
    git(clone, "config", "user.email", "t@t.t")
    git(clone, "config", "user.name", "t")
    git(clone, "config", "commit.gpgsign", "false")
    return clone


def origin_head(clone: pathlib.Path) -> str:
    return git(clone, "ls-remote", "origin", "refs/heads/main").stdout.split()[0]


def origin_files(clone: pathlib.Path) -> set[str]:
    git(clone, "fetch", "-q", "origin", "main")
    return set(git(clone, "ls-tree", "-r", "--name-only", "origin/main").stdout.splitlines())


def settings_for(root: pathlib.Path, **overrides: object) -> MaintainSettings:
    import dataclasses

    return dataclasses.replace(MaintainSettings.load(root), **overrides)  # type: ignore[arg-type]


def recording_spawn(spawned: Spawned) -> Callable[..., int]:
    """A stand-in for the detached launcher call: records the args and env, starts nothing."""

    def spawn(
        launch: Launch,
        args: Sequence[str],
        reason: str,
        log_path: pathlib.Path,
        cwd: pathlib.Path,
        env: dict[str, str] | None = None,
    ) -> int:
        spawned.append((args, env or {}))
        return 4242

    return spawn


def maintainer(
    clone: pathlib.Path,
    spawned: Spawned,
    tier: Tier = Tier.LAPTOP,
    now: dt.datetime = NOW,
    **overrides: object,
) -> Maintainer:
    """A laptop-tier maintainer whose launcher is stubbed and whose clock is fixed."""
    wiring: dict[str, object] = {
        "holder": f"{tier.value}@test:1",
        "clock": lambda: now,
        "spawn": recording_spawn(spawned),
        "launch": ScriptLaunch("/stub/claude-any.sh"),
        **overrides,
    }
    settings = wiring.pop("settings", None) or settings_for(clone)
    return Maintainer(clone, tier, settings, **wiring)  # type: ignore[arg-type]


def claimer_for(clone: pathlib.Path, holder: str, **kw: object) -> Claimer:
    return Claimer(clone, holder, dt.timedelta(hours=2), **kw)  # type: ignore[arg-type]

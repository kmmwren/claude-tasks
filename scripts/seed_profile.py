#!/usr/bin/env python3
"""Draft a queue's taste profile from the owner's stated goals, north stars and recent decisions.

    seed_profile.py [--root Q] [--north-stars owner/repo:path] [--force]

Sources come from `[profile]` in tasks.toml (goals_dir, decisions_dir, north_stars,
decisions_limit). Each is optional: a missing or unreadable one is skipped with a warning. The
result is marked DRAFT for the owner to edit; an existing profile is never overwritten without
--force.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _concurrency import atomic_write  # noqa: E402
from _paths import require_queue, resolve_root  # noqa: E402
from config import load_config  # noqa: E402
from git_sync import sync_commit  # noqa: E402
from taste import PROFILE_FILE, Goal, Profile, profile_path, render  # noqa: E402

DEFAULTS: dict[str, Any] = {
    "goals_dir": "~/.claude/goals",
    "decisions_dir": "~/.claude/decisions",
    "north_stars": "",
    "decisions_limit": 8,
    "top_weight": 1.0,
    "other_weight": 0.5,
}
DISLIKES = ("Busywork no goal or value above needs",)
_NORTH_STAR = re.compile(r"(?m)^## +(NS-\d+\b.*?)\s*$")
_TITLE = re.compile(r"(?m)^# +(.+?)\s*$")
_DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")

Fetch = Callable[[str], str]


def settings(root: pathlib.Path) -> dict[str, Any]:
    return {**DEFAULTS, **load_config(root).get("profile", {})}


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _one_line(text: str) -> str:
    """A profile line holds one goal; `|` separates its fields."""
    return " ".join(str(text).split()).replace("|", "/")


def goal_from(data: Mapping[str, Any], config: Mapping[str, Any]) -> Goal:
    slug = str(data["slug"])
    weight = float(config["top_weight"] if data.get("top_priority") else config["other_weight"])
    return Goal(
        _one_line(data.get("title") or slug),
        weight,
        (slug, f"project:{slug}"),
        _one_line(data.get("definition_of_done", "")),
    )


def goals(directory: pathlib.Path, config: Mapping[str, Any]) -> tuple[Goal, ...]:
    found = []
    for path in sorted(directory.glob("*.json")):
        try:
            found.append(goal_from(json.loads(path.read_text()), config))
        except (ValueError, KeyError, TypeError) as exc:
            warn(f"skipped goal {path.name}: {exc}")
    return tuple(sorted(found, key=lambda g: -g.weight))


def gh_fetch(spec: str) -> str:
    """`owner/repo:path` read raw through the gh CLI."""
    repo, _, path = spec.partition(":")
    return subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def north_stars(spec: str, fetch: Fetch) -> tuple[str, ...]:
    if not spec:
        return ()
    try:
        return tuple(_one_line(h) for h in _NORTH_STAR.findall(fetch(spec)))
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"north stars not read from {spec}: {exc}")
        return ()


def decision_title(path: pathlib.Path) -> str:
    dated = _DATED.match(path.stem)
    heading = _TITLE.search(path.read_text())
    title = (
        heading.group(1) if heading else (dated.group(2) if dated else path.stem).replace("-", " ")
    )
    return (
        f"Settled {dated.group(1)}: {_one_line(title)}" if dated else f"Settled: {_one_line(title)}"
    )


def patterns(directory: pathlib.Path, limit: int) -> tuple[str, ...]:
    recent = sorted(directory.glob("*.md"), reverse=True)[:limit]
    return tuple(decision_title(path) for path in recent)


def draft(
    root: pathlib.Path,
    north_star_spec: str | None = None,
    fetch: Fetch = gh_fetch,
    today: str | None = None,
) -> Profile:
    config = settings(root)
    return Profile(
        goals=goals(pathlib.Path(config["goals_dir"]).expanduser(), config),
        values=north_stars(
            north_star_spec if north_star_spec is not None else str(config["north_stars"]), fetch
        ),
        dislikes=DISLIKES,
        patterns=patterns(
            pathlib.Path(config["decisions_dir"]).expanduser(), int(config["decisions_limit"])
        ),
        draft=True,
        updated=today or datetime.date.today().isoformat(),
    )


def seed(root: pathlib.Path, profile: Profile, force: bool = False) -> pathlib.Path:
    path = profile_path(root)
    if path.exists() and not force:
        raise SystemExit(f"{path} exists; edit it, or re-seed with --force")
    atomic_write(path, render(profile))
    sync_commit("profile: seed draft taste profile", root, paths=[str(PROFILE_FILE)])
    return path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Draft the queue's taste profile for its owner to edit."
    )
    ap.add_argument("--root", default=None)
    ap.add_argument(
        "--north-stars", default=None, help="owner/repo:path of a doc with `## NS-n` headings"
    )
    ap.add_argument("--force", action="store_true", help="overwrite an existing profile")
    ap.add_argument("--today", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    root = require_queue(pathlib.Path(a.root) if a.root else resolve_root())
    profile = draft(root, a.north_stars, today=a.today)
    path = seed(root, profile, a.force)
    print(
        f"drafted {path}: {len(profile.goals)} goals, {len(profile.values)} values, "
        f"{len(profile.patterns)} patterns. It is a DRAFT: edit it, then set draft: false."
    )


if __name__ == "__main__":
    main()  # pragma: no cover

#!/usr/bin/env python3
"""
route_registry.py: the projects a generic brief may be routed into, read from the projects registry.

The registry is a TOML file with one `[[project]]` table per project (slug, queue, names,
route_terms, queue_mode, lifecycle, status, and optionally `repos` and `jira_keys`). It is the
only list of projects: a name that is not in it is never created here, only reported.

Stdlib only.
"""

import dataclasses
import os
import pathlib
import re
import tomllib
from typing import Any

from _paths import is_queue

REGISTRY_ENV = "PROJECT_REGISTRY"
DEFAULT_REGISTRY = "~/.claude/tools/ledger-viewer/projects.toml"
JIRA_KEY = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+-\d+(?![A-Za-z0-9])")


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in value or () if str(v).strip())


@dataclasses.dataclass(frozen=True)
class Project:
    slug: str
    queue: pathlib.Path
    names: tuple[str, ...] = ()
    route_terms: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    jira_keys: tuple[str, ...] = ()
    queue_mode: str = "direct"
    sync_repo: pathlib.Path | None = None
    lifecycle: str = "standing"
    status: str = "active"

    @property
    def takes_briefs(self) -> bool:
        """Generic briefs may go here: active, and its queue is not owned by one session."""
        return self.status == "active" and self.lifecycle == "standing"

    @property
    def labels(self) -> tuple[str, ...]:
        """Every word that names this project, for an explicit `project:` field."""
        return (self.slug, *self.names)

    @property
    def keys(self) -> frozenset[str]:
        """Jira keys it owns: declared ones, plus any key-shaped name or route term."""
        shaped = (t for t in (*self.names, *self.route_terms) if JIRA_KEY.fullmatch(t))
        return frozenset(k.upper() for k in (*self.jira_keys, *shaped))

    @property
    def keywords(self) -> tuple[str, ...]:
        """Words that evidence the project in a brief's title, id or tags; keys are matched apart."""
        every = (*self.labels, *self.route_terms)
        return tuple(dict.fromkeys(t for t in every if not JIRA_KEY.fullmatch(t)))

    def answers_to(self, value: str) -> bool:
        return value.lower() in {label.lower() for label in self.labels}

    def owns_repo(self, repo: str) -> bool:
        return bool(repo) and repo.lower() in {r.lower() for r in self.repos}

    def holds(self, dest_root: pathlib.Path | None) -> pathlib.Path:
        """The queue to write into: the registered one, or its place in a worktree of `sync_repo`."""
        if dest_root is None or self.sync_repo is None:
            return self.queue
        return dest_root / self.queue.relative_to(self.sync_repo)

    def queue_ready(self, dest_root: pathlib.Path | None) -> bool:
        return is_queue(self.holds(dest_root))


def _path(value: Any) -> pathlib.Path:
    return pathlib.Path(str(value)).expanduser()


def _project(row: dict[str, Any]) -> Project | None:
    if not row.get("slug") or not row.get("queue"):
        return None
    return Project(
        slug=str(row["slug"]),
        queue=_path(row["queue"]),
        names=_strings(row.get("names")),
        route_terms=_strings(row.get("route_terms")),
        repos=_strings(row.get("repos")),
        jira_keys=_strings(row.get("jira_keys")),
        queue_mode=str(row.get("queue_mode", "direct")),
        sync_repo=_path(row["sync_repo"]) if row.get("sync_repo") else None,
        lifecycle=str(row.get("lifecycle", "standing")),
        status=str(row.get("status", "active")),
    )


def registry_path(override: str | None = None) -> pathlib.Path:
    return _path(override or os.environ.get(REGISTRY_ENV) or DEFAULT_REGISTRY)


def load_projects(path: pathlib.Path) -> list[Project]:
    """Every registered project that has a slug and a queue; a folded one is left out."""
    rows = tomllib.loads(path.read_text()).get("project", [])
    found = (_project(r) for r in rows)
    return [p for p in found if p is not None and p.lifecycle != "folded"]

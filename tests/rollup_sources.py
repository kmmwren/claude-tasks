"""Roll-up sources backed by a queue pushed to a bare remote, as users have them on GitHub."""

import datetime as dt
import pathlib
from collections.abc import Callable

import rollup
import rollup_model as rm
import rollup_views as rv

from tests.project_fixtures import make_queue
from tests.remote_queue import publish

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
Build = Callable[[pathlib.Path], object]


def source_of(tmp_path: pathlib.Path, build: Build, label: str = "S") -> rollup.Source:
    """Push the queue `build` writes into a clone under `tmp_path`; the source reads it back."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return rollup.Source(label, str(publish(tmp_path, build)))


def rollup_of(tmp_path: pathlib.Path, build: Build, label: str = "S") -> rv.Report:
    return rollup.build([source_of(tmp_path, build, label)], NOW)


def projects(report: rv.Report) -> tuple[rm.Project, ...]:
    return tuple(e.project for e in report.entries)


def with_project_toml(text: str) -> Build:
    """A demo queue whose project.toml is replaced by `text`."""

    def build(clone: pathlib.Path) -> None:
        (make_queue(clone) / "project.toml").write_text(text)

    return build

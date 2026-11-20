#!/usr/bin/env python3
"""
rollup.py - project roll-ups for an engineer, a product manager (pm) or a CTO, over any number of
queues. Each project is read from its claude-tasks queue as the repository's default branch holds
it on GitHub, never from a local clone. Only titles, states, dates, the owner's own health, ask and
outcome, and the text of the automatic checks leave a queue; see skills/rollup.

  python scripts/rollup.py json|markdown|html [--level engineer|pm|cto] [--source LABEL=REPO[#QUEUE] ...]
  python scripts/rollup.py sources [--source LABEL=REPO[#QUEUE] ...]
  python scripts/rollup.py write --out OUT_DIR [--source LABEL=REPO[#QUEUE] ...]
  python scripts/rollup.py serve [--port 8788] [--source LABEL=REPO[#QUEUE] ...]
"""

import argparse
import dataclasses
import datetime as dt
import http.server
import json
import os
import pathlib
import re
import sys
import tomllib
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import project_view as pv
import rollup_views as rv
from _concurrency import atomic_write
from _paths import PROJECT_MARKER, is_queue, resolve_root, warn
from queue_snapshot import QueueSnapshot, SnapshotUnavailable, read_queue
from rollup_model import build_project

CONFIG_ENV = "CLAUDE_TASKS_ROLLUP_CONFIG"
DEFAULT_CONFIG = "~/.config/claude-tasks/rollup.toml"
DEFAULT_LABEL = "this repository"
DEFAULT_QUEUE = PROJECT_MARKER
DEFAULT_VIEW = "pm"
NOT_READ = "its queue cannot be read from its repository and there is no cached copy"
LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1)(:\d{1,5})?", re.IGNORECASE)
GENERATED = re.compile(r'(?m)^\*Generated \S+|^\s*"generated": "[^"]*"')
WRITTEN = ("json", "markdown")  # Markdown is a file for GitHub to render, not a page to browse
SERVED = ("html", "json")


class RollupError(Exception):
    """A source or config the user named cannot be used; the message says which and why."""


def target(view: str, fmt: str) -> str:
    """The file name a view has in a format, and the route the server answers it on."""
    return f"{view}{rv.FORMATS[fmt].suffix}"


# --- sources -------------------------------------------------------------------------------


def expand(text: str) -> pathlib.Path:
    """`~` and `~user` expanded; a home that cannot be found is a usage error, not a crash."""
    try:
        return pathlib.Path(text).expanduser()
    except RuntimeError as e:
        raise RollupError(f"cannot expand {text!r}: {e}") from e


def locate(text: str) -> str:
    """A path that exists is made absolute, since the snapshot cache runs git from elsewhere."""
    path = expand(text)
    if path.exists():
        return str(path.resolve())
    return str(path) if text.startswith("~") else text  # a URL must keep its `//`


def queue_path(queue: str) -> str:
    """The queue's folder inside its repository; one that leaves it is a usage error."""
    path = pathlib.PurePosixPath(queue.strip())
    if not queue.strip() or path.is_absolute() or ".." in path.parts:
        raise RollupError(f"queue {queue!r} must be a folder inside its repository")
    return path.as_posix()


@dataclasses.dataclass(frozen=True)
class Source:
    """One project's queue: `queue` is a folder in `repo`, a URL or a path git can read."""

    label: str
    repo: str
    queue: str = DEFAULT_QUEUE

    @classmethod
    def of(cls, label: str, repo: str, queue: str) -> "Source":
        return cls(label.strip(), locate(repo.strip()), queue_path(queue))

    @classmethod
    def parse(cls, spec: str) -> "Source":
        label, _, rest = spec.partition("=")
        head, hash_, tail = rest.rpartition("#")
        repo, queue = (head, tail) if hash_ else (rest, DEFAULT_QUEUE)
        if not (label.strip() and repo.strip()):
            raise RollupError(f"--source wants LABEL=REPO[#QUEUE], got {spec!r}")
        return cls.of(label, repo, queue)

    @classmethod
    def from_row(cls, row: object, number: int) -> "Source":
        fields = row if isinstance(row, Mapping) else {}
        label, repo = (str(fields.get(key) or "") for key in ("label", "repo"))
        if not repo.strip() and (legacy := str(fields.get("path") or "")).strip():
            warn(f"[[source]] row {number}: `path` is now `repo`; reading it as the repo")
            repo = legacy
        if not (label.strip() and repo.strip()):
            raise RollupError(f"[[source]] row {number} needs a label and a repo")
        return cls.of(label, repo, str(fields.get("queue") or DEFAULT_QUEUE))

    @property
    def where(self) -> str:
        return f"{self.repo}#{self.queue}"

    def omission(self, reason: str) -> rv.Omission:
        return rv.Omission(self.label, self.queue, reason)


@dataclasses.dataclass(frozen=True)
class ConfigFile:
    path: pathlib.Path
    named: bool  # the user asked for this file, so a fault in it is an error


def config_file(explicit: str | None, env: Mapping[str, str]) -> ConfigFile:
    named = explicit or env.get(CONFIG_ENV)
    return ConfigFile(expand(named or DEFAULT_CONFIG), bool(named))


def load_sources(
    specs: list[str],
    config: Callable[[], ConfigFile],
    here: Callable[[], pathlib.Path] = resolve_root,
) -> list[Source]:
    """`--source` flags win; the config is looked up only when there are none."""
    if not specs:
        return _from_config_or_here(config(), here)
    found = [Source.parse(s) for s in specs]
    _reject_repeated_labels(found)
    return found


def _from_config_or_here(config: ConfigFile, here: Callable[[], pathlib.Path]) -> list[Source]:
    """A fault in the default config must not stop a run the current repository can serve."""
    if not config.named and not config.path.is_file():
        return [_current(here())]
    try:
        return _configured(config)
    except RollupError as e:
        if config.named:
            raise
        warn(f"{e}; using the current repository instead")
    return [_current(here())]


def _current(queue: pathlib.Path) -> Source:
    """The queue in use, or the repository holding it when it is a project's `.tasks` folder."""
    queue = queue.resolve()
    # A `.tasks` queue directly in $HOME would make the whole home folder the repository.
    home = pathlib.Path.home().resolve()
    inside_repo = queue.name == PROJECT_MARKER and is_queue(queue) and queue.parent != home
    if inside_repo:
        return Source(DEFAULT_LABEL, str(queue.parent), PROJECT_MARKER)
    return Source(DEFAULT_LABEL, str(queue), ".")


def _configured(config: ConfigFile) -> list[Source]:
    if not config.path.is_file():
        raise RollupError(f"config file {config.path} does not exist")
    rows = _rows(config.path)
    found = [s for number, row in enumerate(rows, 1) if (s := _usable(row, number, config.path))]
    if not found:
        raise RollupError(f"{config.path} has no usable [[source]] rows")
    return _first_of_each_label(found, config.path)


def _rows(config: pathlib.Path) -> list[object]:
    try:
        with config.open("rb") as fh:
            rows = tomllib.load(fh).get("source", [])
    except (OSError, ValueError) as e:
        raise RollupError(f"cannot read {config}: {e}") from e
    if not isinstance(rows, list):
        raise RollupError(f"{config}: sources are [[source]] tables with label, repo and queue")
    return rows


def _usable(row: object, number: int, config: pathlib.Path) -> Source | None:
    """One bad row is skipped and named, so the rows after it still count."""
    try:
        return Source.from_row(row, number)
    except RollupError as e:
        warn(f"{config}: {e}; that row is skipped")
        return None


def _first_of_each_label(found: list[Source], config: pathlib.Path) -> list[Source]:
    """A label that repeats keeps its first row; each later one is named and skipped."""
    kept: dict[str, Source] = {}
    for s in found:
        if s.label in kept:
            warn(f"{config}: label {s.label!r} is used more than once; that row is skipped")
        kept.setdefault(s.label, s)
    return list(kept.values())


def _reject_repeated_labels(found: list[Source]) -> None:
    repeated = sorted(label for label, n in Counter(s.label for s in found).items() if n > 1)
    if repeated:
        raise RollupError(
            f"source labels must be unique; used more than once: {', '.join(repeated)}"
        )


# --- reading the sources -------------------------------------------------------------------


def entry_or_omission(
    source: Source, snapshot: QueueSnapshot, now: dt.datetime
) -> rv.Entry | rv.Omission:
    try:
        project = build_project(source.label, snapshot, now)
        return rv.Entry(project, rv.QueueBoard.read(snapshot, project))
    except (pv.ProjectConfigError, tomllib.TOMLDecodeError) as e:
        return source.omission(_inside(str(e), snapshot.root))
    except OSError as e:
        return source.omission(_os_reason(e, snapshot.root))
    except (KeyError, TypeError, ValueError) as e:  # ValueError covers UnicodeDecodeError
        return source.omission(f"{type(e).__name__}: {e}")


def _inside(message: str, queue: pathlib.Path) -> str:
    """Omissions are shared, so a path in a message is shown relative to its queue."""
    return message.replace(f"{queue}{os.sep}", "")


def _os_reason(error: OSError, queue: pathlib.Path) -> str:
    """Omissions are shared, so the failing file is named relative to its queue, never absolute."""
    name = pathlib.Path(str(error.filename or ""))
    inside = name.relative_to(queue) if name.is_relative_to(queue) else pathlib.Path(name.name)
    reason = f"{type(error).__name__}: {error.strerror or 'cannot be read'}"
    return f"{reason} ({inside})" if error.filename else reason


def cached_note(source: Source, snapshot: QueueSnapshot) -> rv.SourceNote | None:
    """A source whose repository could not be reached is shown from its last cached copy."""
    if not snapshot.stale:
        return None
    when = snapshot.committed.isoformat(timespec="seconds")
    return rv.SourceNote(
        source.label, f"{source.label} could not be reached; showing its queue as at {when}"
    )


@dataclasses.dataclass(frozen=True)
class SourceRead:
    """What one source gave: an entry or the reason it gave none, and a note about its copy."""

    entry: rv.Entry | None = None
    omission: rv.Omission | None = None
    note: rv.SourceNote | None = None


def read_source(source: Source, now: dt.datetime) -> SourceRead:
    try:
        snapshot = read_queue(source.repo, source.queue)
    except (SnapshotUnavailable, OSError) as e:
        # The error names the repository, which a shared "Not shown" line must not; stderr may.
        warn(f"{source.label}: {e}")
        return SourceRead(omission=source.omission(NOT_READ))
    outcome = entry_or_omission(source, snapshot, now)
    if isinstance(outcome, rv.Omission):
        return SourceRead(omission=outcome)
    return SourceRead(entry=outcome, note=cached_note(source, snapshot))


def build(sources: list[Source], now: dt.datetime) -> rv.Report:
    reads = [read_source(s, now) for s in sources]
    return rv.Report(
        now,
        tuple(s.label for s in sources),
        tuple(r.entry for r in reads if r.entry),
        tuple(r.omission for r in reads if r.omission),
        tuple(r.note for r in reads if r.note),
    )


# --- the files a roll-up is published as ------------------------------------------------------


def _files(report: rv.Report) -> dict[str, str]:
    files = {"README.md": rv.render_contents("markdown", report, with_guide=True)}
    for view in rv.VIEWS:
        for fmt in WRITTEN:
            files[target(view, fmt)] = rv.render(view, fmt, report).rstrip("\n") + "\n"
    return files


def _holds_rollup(out: pathlib.Path) -> bool:
    """True when `out` already carries a roll-up of ours: a pm.json with our schema."""
    try:
        data = json.loads((out / target(DEFAULT_VIEW, "json")).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("schema") == rv.SCHEMA


def _check_destination(out: pathlib.Path) -> None:
    empty = out.is_dir() and not any(out.iterdir())
    if out.exists() and not (empty or _holds_rollup(out)):
        raise RollupError(
            f"{out} is not an empty folder and holds no roll-up (no pm.json with "
            f"schema {rv.SCHEMA}); name a new or empty folder so nothing else is overwritten"
        )


def write_if_changed(path: pathlib.Path, text: str) -> bool:
    """Write `path` unless it only differs in its generated-on line; True when it wrote."""
    if _same_but_for_date(path, text):
        return False
    atomic_write(path, text)
    return True


def _same_but_for_date(path: pathlib.Path, text: str) -> bool:
    """A file we cannot decode is not ours to keep, so it counts as different."""
    if not path.is_file():
        return False
    try:
        current = path.read_text()
    except UnicodeDecodeError:
        return False
    return GENERATED.sub("", current) == GENERATED.sub("", text)


def write(out: pathlib.Path, report: rv.Report) -> list[pathlib.Path]:
    """Write the roll-up files that changed into `out` and return them."""
    try:
        _check_destination(out)
        return [
            out / name
            for name, text in _files(report).items()
            if write_if_changed(out / name, text)
        ]
    except OSError as e:
        raise RollupError(f"cannot write {out}: {e.strerror or type(e).__name__}") from e


@dataclasses.dataclass(frozen=True)
class Served:
    """One page the server has: a view in a format, found by the file name `write` gives it."""

    view: str
    fmt: str

    @property
    def target(self) -> str:
        return target(self.view, self.fmt)

    def answer(self, report: rv.Report) -> tuple[str, str]:
        return rv.FORMATS[self.fmt].content_type, rv.render(self.view, self.fmt, report)


PAGES = {page.target: page for page in (Served(v, f) for v in rv.VIEWS for f in SERVED)}


def route(path: str, report: rv.Report) -> tuple[str, str] | None:
    """(content type, body) for a localhost path, or None for a path the server does not have."""
    name = path.strip("/")
    if not name:
        return rv.FORMATS["html"].content_type, rv.render_contents("html", report)
    served = PAGES.get(name)
    return served.answer(report) if served else None


# --- commands ------------------------------------------------------------------------------

Now = Callable[[], dt.datetime]


class SourceOptions:
    """The `--source` and `--config` flags and how they resolve to sources."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--source", action="append", default=[], metavar="LABEL=REPO[#QUEUE]")
        parser.add_argument(
            "--config", help=f"sources file (default ${CONFIG_ENV} or {DEFAULT_CONFIG})"
        )

    @staticmethod
    def load(args: argparse.Namespace, env: Mapping[str, str]) -> list[Source]:
        if args.source and args.config:
            warn("--config is ignored because --source was given")
        return load_sources(args.source, lambda: config_file(args.config, env))


@dataclasses.dataclass(frozen=True)
class Command(ABC):
    """A subcommand: it declares its own flags, builds itself from them, and runs."""

    name: ClassVar[str]
    summary: ClassVar[str]
    sources: list[Source]
    now: Now

    @classmethod
    @abstractmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None: ...

    @classmethod
    @abstractmethod
    def from_args(cls, args: argparse.Namespace, env: Mapping[str, str], now: Now) -> "Command": ...

    def rollup(self) -> rv.Report:
        return build(self.sources, self.now())

    @abstractmethod
    def run(self) -> None: ...


@dataclasses.dataclass(frozen=True)
class ListSources(Command):
    name = "sources"
    summary = "list the projects each source finds"

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        SourceOptions.add_arguments(parser)

    @classmethod
    def from_args(cls, args: argparse.Namespace, env: Mapping[str, str], now: Now) -> "ListSources":
        return cls(SourceOptions.load(args, env), now)

    def run(self) -> None:
        r = self.rollup()
        for s in self.sources:
            found = [e.project.slug for e in r.entries if e.project.source == s.label]
            reasons = [o.reason for o in r.omitted if o.source == s.label]
            print(f"{s.label}: {s.where}\n  " + ", ".join(found or reasons))
            for note in (n for n in r.notes if n.source == s.label):
                print(f"  warning: {note.message}")


@dataclasses.dataclass(frozen=True)
class Show(Command):
    """Print the roll-up in `fmt`; each format is its own subcommand."""

    fmt: ClassVar[str]
    level: str

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        SourceOptions.add_arguments(parser)
        parser.add_argument("--level", choices=list(rv.VIEWS), default=DEFAULT_VIEW)

    @classmethod
    def from_args(cls, args: argparse.Namespace, env: Mapping[str, str], now: Now) -> "Show":
        return cls(SourceOptions.load(args, env), now, args.level)

    def run(self) -> None:
        print(rv.render(self.level, self.fmt, self.rollup()))


class ShowJson(Show):
    name = "json"
    summary = "print the roll-up as json"
    fmt = "json"


class ShowMarkdown(Show):
    name = "markdown"
    summary = "print the roll-up as markdown"
    fmt = "markdown"


class ShowHtml(Show):
    name = "html"
    summary = "print the roll-up as html"
    fmt = "html"


@dataclasses.dataclass(frozen=True)
class Write(Command):
    name = "write"
    summary = "write the roll-up files into OUT_DIR"
    out: pathlib.Path

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        SourceOptions.add_arguments(parser)
        parser.add_argument(
            "--out",
            required=True,
            metavar="OUT_DIR",
            help="a new or empty folder, or an earlier roll-up",
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace, env: Mapping[str, str], now: Now) -> "Write":
        return cls(SourceOptions.load(args, env), now, expand(args.out))

    def run(self) -> None:
        written = write(self.out, self.rollup())
        print("\n".join(map(str, written)) if written else f"{self.out} is up to date")


@dataclasses.dataclass(frozen=True)
class Serve(Command):
    name = "serve"
    summary = "serve the roll-ups on localhost"
    port: int

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        SourceOptions.add_arguments(parser)
        parser.add_argument("--port", type=int, default=8788)

    @classmethod
    def from_args(cls, args: argparse.Namespace, env: Mapping[str, str], now: Now) -> "Serve":
        return cls(SourceOptions.load(args, env), now, args.port)

    def server(self) -> http.server.ThreadingHTTPServer:
        """Localhost only, rebuilt on every request so the page is never older than the default branch."""
        run = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                # A rebinding page reaches this loopback port under its own name; only ours pass.
                if not LOCAL_HOST.fullmatch(self.headers.get("Host", "")):
                    return self._send(403, "text/plain; charset=utf-8", "forbidden host")
                got = route(self.path.split("?")[0], run.rollup())
                ctype, body = got or ("text/plain; charset=utf-8", "not found")
                self._send(200 if got else 404, ctype, body)

            def _send(self, status: int, ctype: str, body: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Security-Policy", rv.HTML_CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)

    def run(self, block: Callable[[http.server.ThreadingHTTPServer], None] | None = None) -> None:
        try:
            srv = self.server()
        except (OSError, OverflowError) as e:  # in use, or outside 0-65535
            raise RollupError(f"cannot listen on port {self.port}: {e}") from e
        print(f"roll-ups at http://localhost:{srv.server_address[1]}/ (Ctrl-C to stop)", flush=True)
        (block or serve_until_interrupted)(srv)


def serve_until_interrupted(
    srv: http.server.ThreadingHTTPServer,
) -> None:  # pragma: no cover - blocks until killed
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


COMMANDS: tuple[type[Command], ...] = (ShowJson, ShowMarkdown, ShowHtml, ListSources, Write, Serve)


def parse(argv: list[str], env: Mapping[str, str], now: Now = _now) -> Command:
    ap = argparse.ArgumentParser(description="Project roll-ups for an engineer, a PM or a CTO.")
    subparsers = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for command in COMMANDS:
        sub = subparsers.add_parser(command.name, help=command.summary, description=command.summary)
        command.add_arguments(sub)
    args = ap.parse_args(argv)
    chosen = next(c for c in COMMANDS if c.name == args.command)
    return chosen.from_args(args, env, now)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        parse(args, os.environ if env is None else env).run()
    except RollupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

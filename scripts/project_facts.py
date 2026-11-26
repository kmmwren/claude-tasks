#!/usr/bin/env python3
"""
project_facts.py - the one writer of the facts a queue records beside its briefs.

    python3 scripts/project_facts.py --root <queue-dir> <subcommand> ...

Each subcommand records one fact in the shape `facts.py` reads back. History (health, due dates, CI)
is add-only: a new file per call, never an edit. Per-brief facts and the project's `ask` are
upserts of one key or row. The write and the commit happen inside one hold of the queue lock.

Exit codes: 0 recorded, 1 an argument or a file failed validation (one stderr line names it),
2 the queue lock was not acquired in time and nothing was written.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import abc
import argparse
import dataclasses
import datetime as dt
import json
import pathlib
import re
import sys
import time
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, NoReturn

import _toml
from _concurrency import atomic_write, repo_lock
from _credentials import carries_credential
from _dates import parse_date
from _paths import warn
from facts import (
    CI_DIR,
    CI_STATES,
    COLOURS,
    DUE_DIR,
    EVIDENCE_ACCESS,
    EVIDENCE_KINDS,
    HEALTH_DIR,
    HTTPS,
    PROJECT_FILE,
    ROLLOUT_DIR,
    CiRow,
    CustomerRollout,
    DueRow,
    Evidence,
    Flag,
    HealthRow,
    RolloutSnapshot,
    RowError,
    facts_path,
    load_rollouts,
)
from git_sync import SyncResult, host_repo_name, sync_commit
from host_land import HostQueue

PROG = "project_facts"
EXIT_OK, EXIT_INVALID, EXIT_LOCKED = 0, 1, 2
LOCK_TIMEOUT = 30.0
FLAG_STATES = ("on", "off")

# A name that becomes part of a file name, so it can never climb out of its folder.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_TABLE_HEADER = re.compile(r"^\s*\[\[?[A-Za-z0-9_.\-\"' ]+\]\]?\s*(#.*)?$")
_ASK_HEADER = re.compile(r"^\s*\[\[?\s*ask\s*[.\]]")
_ASK_KEY = re.compile(r"^\s*ask\s*=")


class ValidationError(Exception):
    """An argument or a recorded file that cannot be used; the message names it."""


class LockTimeout(Exception):
    """The queue lock was not acquired in time."""


def today() -> dt.date:
    return dt.date.today()


def _token() -> str:
    """The 8 hex characters that keep two same-day history files apart."""
    return uuid.uuid4().hex[:8]


# --- argument types: each says what is wrong in the words argparse puts after the flag -------


def _text(value: str) -> str:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("must be non-empty text")
    # A credential committed here would sit in history for good, so it is refused before any write.
    if carries_credential(text):
        raise argparse.ArgumentTypeError("must not carry a credential or a signed link")
    return text


def _id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise argparse.ArgumentTypeError("must be letters, digits, '.', '_' or '-'")
    return value


def _day(value: str) -> dt.date:
    day = parse_date(value)
    if day is None:
        raise argparse.ArgumentTypeError("must be a YYYY-MM-DD date")
    return day


def _https(value: str) -> str:
    link = _text(value)
    if not link.lower().startswith(HTTPS) or len(link) == len(HTTPS):
        raise argparse.ArgumentTypeError("must be an https link")
    return link


def _directory(value: str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_dir():
        raise argparse.ArgumentTypeError("must be an existing directory")
    return path


def _file(value: str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError("must be an existing file")
    return path


class _Parser(argparse.ArgumentParser):
    """Reports a bad argument as a ValidationError, because argparse's own exit code 2 means a lock timeout here."""

    def error(self, message: str) -> NoReturn:
        raise ValidationError(message)


def _add_day(parser: argparse.ArgumentParser, flag: str) -> None:
    parser.add_argument(flag, type=_day, default=None, help="YYYY-MM-DD, default today")


def _require(parser: argparse.ArgumentParser, flag: str, **kw: Any) -> None:
    parser.add_argument(flag, required=True, **kw)


# --- commands --------------------------------------------------------------------------------


class Command(abc.ABC):
    """One subcommand: how it reads its arguments, what it writes, and how the commit is worded."""

    name: ClassVar[str]
    help: ClassVar[str]

    @classmethod
    @abc.abstractmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        """Add this subcommand's arguments."""

    @classmethod
    @abc.abstractmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "Command":
        """The command the parsed arguments describe, dates defaulted to `today`."""

    @property
    @abc.abstractmethod
    def message(self) -> str:
        """What was recorded, in a few words; it is the commit subject after `facts: `."""

    @abc.abstractmethod
    def write(self, root: pathlib.Path) -> tuple[str, ...]:
        """Write the fact under `root`; the changed paths relative to it, none if nothing changed."""


class HistoryCommand(Command):
    """A fact that is a new file in a history folder and is never edited afterwards."""

    folder: ClassVar[pathlib.PurePosixPath]

    @property
    @abc.abstractmethod
    def stem(self) -> str:
        """The file name before the random suffix."""

    @abc.abstractmethod
    def table(self) -> dict[str, Any]:
        """The file body."""

    def write(self, root: pathlib.Path) -> tuple[str, ...]:
        path = self._fresh_path(root / self.folder)
        atomic_write(path, _toml.dumps(self.table()))
        return (path.relative_to(root).as_posix(),)

    def _fresh_path(self, folder: pathlib.Path) -> pathlib.Path:
        while True:
            path = folder / f"{self.stem}-{_token()}.toml"
            if not path.exists():
                return path


@dataclasses.dataclass(frozen=True)
class SetHealth(HistoryCommand):
    row: HealthRow

    name = "set-health"
    help = "record the owner's health colour"
    folder = HEALTH_DIR

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        _require(parser, "--colour", choices=COLOURS)
        _require(parser, "--reason", type=_text)
        _require(parser, "--set-by", type=_text)
        _add_day(parser, "--on")

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "SetHealth":
        return cls(HealthRow(args.colour, args.reason, args.set_by, args.on or today, ""))

    @property
    def message(self) -> str:
        return f"set health {self.row.colour}"

    @property
    def stem(self) -> str:
        return str(self.row.set_on)

    def table(self) -> dict[str, Any]:
        return self.row.table()


@dataclasses.dataclass(frozen=True)
class SetDue(HistoryCommand):
    row: DueRow

    name = "set-due"
    help = "re-forecast a milestone's due date"
    folder = DUE_DIR

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        _require(parser, "--milestone", type=_id)
        _require(parser, "--due", type=_day, help="YYYY-MM-DD")
        _require(parser, "--reason", type=_text)
        _require(parser, "--set-by", type=_text)
        _add_day(parser, "--on")

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "SetDue":
        row = DueRow(args.milestone, args.due, args.on or today, args.set_by, args.reason, "")
        return cls(row)

    @property
    def message(self) -> str:
        return f"set {self.row.milestone} due {self.row.due}"

    @property
    def stem(self) -> str:
        return f"{self.row.milestone}-{self.row.set_on}"

    def table(self) -> dict[str, Any]:
        return self.row.table()


@dataclasses.dataclass(frozen=True)
class SetCi(HistoryCommand):
    row: CiRow

    name = "set-ci"
    help = "record the latest build as green or red"
    folder = CI_DIR

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        _require(parser, "--state", choices=CI_STATES)
        _require(parser, "--link", type=_https, help="the build's https link")
        _add_day(parser, "--on")

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "SetCi":
        return cls(CiRow(args.state, args.link, args.on or today, ""))

    @property
    def message(self) -> str:
        return f"set ci {self.row.state}"

    @property
    def stem(self) -> str:
        return str(self.row.set_on)

    def table(self) -> dict[str, Any]:
        return self.row.table()


@dataclasses.dataclass(frozen=True)
class SetRollout(HistoryCommand):
    """One feature's customers and their state; written only when it differs from the newest recorded."""

    snapshot: RolloutSnapshot

    name = "set-rollout"
    help = "record which customers have a feature, from a JSON file"
    folder = ROLLOUT_DIR

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        _require(parser, "--key", type=_id)
        _require(
            parser,
            "--from",
            dest="source",
            type=_file,
            help='JSON: {"key": K, "customers": [{"code", "release", "on"}]}',
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "SetRollout":
        return cls(_snapshot_from(args.source, args.key, today))

    @property
    def message(self) -> str:
        return f"{self.snapshot.key} rollout"

    @property
    def stem(self) -> str:
        return f"{self.snapshot.key}-{self.snapshot.recorded_on}"

    def table(self) -> dict[str, Any]:
        return self.snapshot.table()

    def _fresh_path(self, folder: pathlib.Path) -> pathlib.Path:
        """Name the file by the clock, so a second change on the same day sorts as the newest."""
        stamp = int(time.time())
        while (path := folder / f"{self.stem}-{stamp:08x}.toml").exists():
            stamp += 1
        return path

    def write(self, root: pathlib.Path) -> tuple[str, ...]:
        newest = load_rollouts(root).get(self.snapshot.key)
        if newest is not None and newest.same_state(self.snapshot):
            return ()
        return super().write(root)


def _snapshot_from(source: pathlib.Path, key: str, today: dt.date) -> RolloutSnapshot:
    """The snapshot a JSON file describes for `key`, dated `today`; a ValidationError names what is wrong."""
    try:
        body = json.loads(source.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValidationError(f"--from: {source} is not valid JSON ({e})") from e
    if not isinstance(body, dict):
        raise ValidationError(f"--from: {source} must hold a JSON object")
    if body.get("key") != key:
        raise ValidationError(f"--from: {source} names key {body.get('key')!r}, not {key!r}")
    rows = body.get("customers")
    if not isinstance(rows, list):
        raise ValidationError(f"--from: {source} needs a customers list")
    try:
        return RolloutSnapshot.of(key, today, map(CustomerRollout.parse, rows), source.name)
    except RowError as e:
        raise ValidationError(f"--from: {source}: customers: {e}") from e


@dataclasses.dataclass(frozen=True)
class SetAsk(Command):
    """The project's single ask: `[ask]` with a `question`, or no `[ask]` at all."""

    text: str | None

    name = "set-ask"
    help = "set or clear the project's ask"

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--text", type=_text, help="the question to put to the owner")
        group.add_argument("--clear", action="store_true", help="remove the ask")

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "SetAsk":
        return cls(None if args.clear else args.text)

    @property
    def message(self) -> str:
        return "clear ask" if self.text is None else "set ask"

    def write(self, root: pathlib.Path) -> tuple[str, ...]:
        path = root / PROJECT_FILE
        before = path.read_text() if path.exists() else ""
        after = self._rewritten(path, before)
        if after == before:
            return ()
        atomic_write(path, after)
        return (PROJECT_FILE,)

    def _rewritten(self, path: pathlib.Path, before: str) -> str:
        """`before` with only the ask changed; refused if any other key or table would differ."""
        try:
            old = tomllib.loads(before)
        except tomllib.TOMLDecodeError as e:
            raise ValidationError(f"{path}: not valid TOML ({e})") from e
        wanted = {k: v for k, v in old.items() if k != "ask"}
        block = ""
        if self.text is not None:
            wanted["ask"] = {"question": self.text}
            block = _toml.dumps({"ask": wanted["ask"]})
        after = "\n\n".join(p for p in (_without_ask(before).rstrip(), block.rstrip()) if p)
        after = after + "\n" if after else ""
        if _parsed(after) != wanted:
            raise ValidationError(f"{path}: ask cannot be rewritten without touching other keys")
        return after


def _parsed(text: str) -> dict[str, Any] | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _without_ask(text: str) -> str:
    """`text` minus the top-level `ask` key and any `[ask]` or `[[ask.*]]` table, every other line kept."""
    kept: list[str] = []
    in_root, skipping = True, False
    for line in text.splitlines(keepends=True):
        if _TABLE_HEADER.match(line):
            in_root, skipping = False, bool(_ASK_HEADER.match(line))
        elif in_root and _ASK_KEY.match(line):
            continue
        if not skipping:
            kept.append(line)
    return "".join(kept)


@dataclasses.dataclass
class FactsDocument:
    """A brief's facts file as written: every key it has, so an upsert never drops one it did not touch."""

    root: pathlib.Path
    brief: str
    table: dict[str, Any]

    @property
    def path(self) -> pathlib.Path:
        return facts_path(self.root, self.brief)

    @classmethod
    def load(cls, root: pathlib.Path, brief: str) -> "FactsDocument":
        path = facts_path(root, brief)
        try:
            table = _toml.read(path)
        except tomllib.TOMLDecodeError as e:
            raise ValidationError(f"{path}: not valid TOML ({e})") from e
        if table.get("brief", brief) != brief:
            raise ValidationError(f"--brief: {path} names {table['brief']!r}")
        return cls(root, brief, table)

    def rows(self, key: str) -> list[Mapping[str, Any]]:
        raw = self.table.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(r, Mapping) for r in raw):
            raise ValidationError(f"{self.path}: {key} must be [[{key}]] tables")
        return raw

    def upsert_flag(self, flag: Flag) -> None:
        """Replace the row for (name, environment), or add it."""
        others = [
            r
            for r in self.rows("flag")
            if (r.get("name"), r.get("environment")) != (flag.name, flag.environment)
        ]
        self.table["flag"] = [*others, flag.table()]

    def add_evidence(self, evidence: Evidence) -> None:
        self.table["evidence"] = [*self.rows("evidence"), evidence.table()]

    def ship(self, line: str, on: dt.date, release: str | None) -> None:
        """Set what shipped and when; an omitted release leaves the recorded one alone."""
        self.table.update({"shipped": line, "shipped_on": on})
        if release is not None:
            self.table["release"] = release

    def save(self) -> str:
        """Write the file, `brief` first; its path relative to the queue."""
        atomic_write(self.path, _toml.dumps({"brief": self.brief, **self.table}))
        return self.path.relative_to(self.root).as_posix()


class FactsCommand(Command):
    """A change to one brief's facts file."""

    brief: str

    @abc.abstractmethod
    def apply(self, document: FactsDocument) -> None:
        """Make this command's change to the loaded facts."""

    def write(self, root: pathlib.Path) -> tuple[str, ...]:
        document = FactsDocument.load(root, self.brief)
        self.apply(document)
        return (document.save(),)


@dataclasses.dataclass(frozen=True)
class SetFlag(FactsCommand):
    brief: str
    flag: Flag

    name = "set-flag"
    help = "record a feature flag's state in one environment"

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        _require(parser, "--brief", type=_id)
        _require(parser, "--name", type=_text)
        _require(parser, "--environment", type=_text)
        _require(parser, "--state", choices=FLAG_STATES)
        _add_day(parser, "--changed-on")

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "SetFlag":
        flag = Flag(args.name, args.environment, args.state, args.changed_on or today)
        return cls(args.brief, flag)

    @property
    def message(self) -> str:
        return f"{self.brief}: flag {self.flag.name} {self.flag.state} in {self.flag.environment}"

    def apply(self, document: FactsDocument) -> None:
        document.upsert_flag(self.flag)


@dataclasses.dataclass(frozen=True)
class AddEvidence(FactsCommand):
    brief: str
    evidence: Evidence

    name = "add-evidence"
    help = "attach a screenshot or recording to a brief"

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        _require(parser, "--brief", type=_id)
        _require(parser, "--kind", choices=EVIDENCE_KINDS)
        _require(parser, "--link", type=_https, help="an https link with no credential in it")
        _require(parser, "--caption", type=_text)
        _require(parser, "--access", choices=EVIDENCE_ACCESS)
        _add_day(parser, "--recorded-on")

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "AddEvidence":
        evidence = Evidence(
            args.kind, args.link, args.caption, args.access, args.recorded_on or today
        )
        return cls(args.brief, evidence)

    @property
    def message(self) -> str:
        return f"{self.brief}: add {self.evidence.kind} evidence"

    def apply(self, document: FactsDocument) -> None:
        document.add_evidence(self.evidence)


@dataclasses.dataclass(frozen=True)
class SetShipped(FactsCommand):
    brief: str
    line: str
    shipped_on: dt.date
    release: str | None

    name = "set-shipped"
    help = "record what shipped for a brief"

    @classmethod
    def configure(cls, parser: argparse.ArgumentParser) -> None:
        _require(parser, "--brief", type=_id)
        _require(parser, "--line", type=_text, help="one line saying what shipped")
        _add_day(parser, "--on")
        parser.add_argument("--release", type=_id, default=None, help="the release's id")

    @classmethod
    def from_args(cls, args: argparse.Namespace, today: dt.date) -> "SetShipped":
        return cls(args.brief, args.line, args.on or today, args.release)

    @property
    def message(self) -> str:
        return f"{self.brief}: shipped"

    def apply(self, document: FactsDocument) -> None:
        document.ship(self.line, self.shipped_on, self.release)


COMMANDS: tuple[type[Command], ...] = (
    SetHealth,
    SetDue,
    SetAsk,
    SetFlag,
    AddEvidence,
    SetShipped,
    SetCi,
    SetRollout,
)


# --- running ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG)
    parser.add_argument("--root", type=_directory, required=True, help="the queue directory")
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    for command in COMMANDS:
        sub = subcommands.add_parser(command.name, help=command.help)
        command.configure(sub)
        sub.set_defaults(command=command)
    return parser


def _land(root: pathlib.Path, command: Command, paths: tuple[str, ...]) -> str:
    """Commit `paths` when the queue is its own repo; a queue inside a host repo lands on a wip branch of it."""
    match sync_commit(f"facts: {command.message}", root, paths=paths, lock_held=True):
        case SyncResult.HOSTED:
            return f"recorded, landed on {host_repo_name(root)} origin/{_wip_branch(root)}"
        case SyncResult.DEFERRED:
            return "recorded"
        case _:
            return f"recorded: {command.message}"


def _wip_branch(root: pathlib.Path) -> str:
    queue = HostQueue.of(root)
    return queue.branch if queue else ""


def run(command: Command, root: pathlib.Path) -> str:
    """Write and commit `command` inside one hold of the queue lock; the line to print."""
    with repo_lock(root, LOCK_TIMEOUT) as acquired:
        if not acquired:
            raise LockTimeout(
                f"queue lock at {root} not acquired within {LOCK_TIMEOUT:.0f}s; nothing written"
            )
        paths = command.write(root)
        return _land(root, command, paths) if paths else f"unchanged: {command.message}"


def _fail(error: Exception, code: int) -> int:
    warn(" ".join(str(error).split()), prefix=PROG)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        print(run(args.command.from_args(args, today()), args.root))
    except ValidationError as e:
        return _fail(e, EXIT_INVALID)
    except LockTimeout as e:
        return _fail(e, EXIT_LOCKED)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

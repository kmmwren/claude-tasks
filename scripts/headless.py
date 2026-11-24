#!/usr/bin/env python3
"""
headless.py: one schema-validated `claude -p` call that treats its prompt purely as data.

Every tool, MCP server and settings file is off and no session is saved, so text inside the
prompt can ask for anything and nothing can act on it. A failed or malformed call returns None,
and once the launcher reports no capacity, every later call in the run returns None without
spawning. This module also owns how a headless call is launched, so the judge and the triage
share it.

Stdlib only.
"""

import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from typing import Any, Protocol

from _paths import warn_once
from config import load_config

LAUNCHER_ENV = "CLAUDE_TASKS_LAUNCHER"
LEGACY_LAUNCHER_ENV = "CLAUDE_ANY_BIN"
LEGACY_REASON_PREFIX = "read-only-research:"
# Launcher exits meaning no account will run any call (no quota, route refused, no usable login,
# kill switch), so every later call in this run would fail the same way.
NO_CAPACITY = frozenset({2, 3, 4, 10})
# launchd and cron start jobs with a PATH that lacks the user's shell additions, so a scheduled
# triage found no `claude` on PATH and failed every call; look where the installer puts it too.
CLAUDE_FALLBACKS = ("~/.claude/bin/claude", "~/.local/bin/claude")
_no_capacity = threading.Event()


@dataclasses.dataclass
class Tally:
    """How many headless calls this run made and how many came back with nothing, across threads."""

    calls: int = 0
    failed: int = 0
    skipped: int = 0
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record(self, answered: bool, skipped: bool = False) -> None:
        """Count one call; `skipped` is one never spawned because no account had capacity."""
        with self._lock:
            self.calls += 1
            self.failed += not answered
            self.skipped += skipped

    def reset(self) -> None:
        with self._lock:
            self.calls = self.failed = self.skipped = 0

    @property
    def all_failed(self) -> bool:
        return self.calls > 0 and self.failed == self.calls


TALLY = Tally()


def reset_capacity() -> None:
    """Start a fresh run: calls spawn again until the next no-capacity exit, and the tally restarts."""
    _no_capacity.clear()
    TALLY.reset()


def notice(message: str) -> None:
    warn_once(message, prefix="claude-tasks")


def _named_launchers(configured: str | None) -> list[str]:
    """Every launcher path the user named, most specific first."""
    if os.environ.get(LEGACY_LAUNCHER_ENV):
        notice(f"${LEGACY_LAUNCHER_ENV} is deprecated; set ${LAUNCHER_ENV}")
    names = (configured, os.environ.get(LAUNCHER_ENV), os.environ.get(LEGACY_LAUNCHER_ENV))
    return [name for name in names if name]


def _executable(raw: str) -> pathlib.Path | None:
    script = pathlib.Path(raw).expanduser()
    return script if script.is_file() and os.access(script, os.X_OK) else None


def launcher_script(configured: str | None = None) -> pathlib.Path | None:
    """The first named launcher that is an executable file; none usable is said once, then None."""
    named = _named_launchers(configured)
    usable = next(filter(None, map(_executable, named)), None)
    if named and not usable:
        notice(f"launcher {named[0]} not found or not executable; using claude -p")
    return usable


def legacy_reason(local_ok: str) -> str:
    """The reason inside a legacy `[rank] local_ok` value, which carried the launcher's class prefix."""
    return local_ok.removeprefix(LEGACY_REASON_PREFIX)


def launcher_env(reason: str) -> dict[str, str]:
    """What a launcher reads: stdin is forwarded on request and `reason` says why the run stays local.

    The legacy names keep launchers written before the generic ones working.
    """
    return {
        **os.environ,
        "CLAUDE_TASKS_STDIN": "1",
        "CLAUDE_TASKS_LOCAL_REASON": reason,
        "CLAUDE_ANY_STDIN": "1",
        "CLAUDE_LOCAL_OK": f"{LEGACY_REASON_PREFIX}{reason}",
    }


class Launch(Protocol):
    def argv(self, args: Sequence[str]) -> list[str]: ...
    def env(self, reason: str) -> dict[str, str] | None: ...


def claude_program() -> str:
    """`claude` on PATH, else the first installed fallback, else the bare name so the failure names it."""
    found = shutil.which("claude") or next(filter(None, map(_executable, CLAUDE_FALLBACKS)), None)
    return str(found or "claude")


@dataclasses.dataclass(frozen=True)
class DirectLaunch:
    """Plain `claude -p`, for a machine without a launcher script."""

    program: str = dataclasses.field(default_factory=claude_program)

    def argv(self, args: Sequence[str]) -> list[str]:
        return [self.program, "-p", *args]

    def env(self, reason: str) -> dict[str, str] | None:
        return None


@dataclasses.dataclass(frozen=True)
class ScriptLaunch:
    """A launcher script told why the run stays local, because its cloud route may answer asynchronously."""

    script: str

    def argv(self, args: Sequence[str]) -> list[str]:
        return [self.script, *args]

    def env(self, reason: str) -> dict[str, str] | None:
        return launcher_env(reason)


def resolve_launch(configured: str | None = None) -> Launch:
    """The launcher named by `configured` or the environment when it runs here, else plain claude."""
    script = launcher_script(configured)
    return ScriptLaunch(str(script)) if script else DirectLaunch()


def _local_ok_notice(configured: str | None) -> str:
    notice = "[rank] local_ok is deprecated; use local_reason"
    if _named_launchers(configured):
        return notice
    return f"{notice}; set `[rank] launcher` or ${LAUNCHER_ENV}, no default launcher is assumed"


def rank_table(root: str | os.PathLike[str]) -> dict[str, Any]:
    """The queue's `[rank]` table, the one place `launcher` and `local_reason` are read."""
    table = dict(load_config(root).get("rank", {}))
    _drop_unusable_launcher(table)
    if "local_ok" in table:
        notice(_local_ok_notice(table.get("launcher")))
        table.setdefault("local_reason", legacy_reason(str(table["local_ok"])))
    return table


def _drop_unusable_launcher(table: dict[str, Any]) -> None:
    launcher = table.get("launcher")
    if launcher is not None and not isinstance(launcher, str):
        notice(f"[rank] launcher must be a path, ignoring {launcher!r}")
        del table["launcher"]


def configured_launch(root: str | os.PathLike[str]) -> Launch:
    """The launcher every headless caller shares: `[rank] launcher`, else the environment."""
    return resolve_launch(rank_table(root).get("launcher"))


def command(model: str, schema: dict[str, Any], launch: Launch) -> list[str]:
    """Headless claude with no tools, no MCP servers, no settings and no saved session."""
    return launch.argv(
        [
            "--model",
            model,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
        ]
    )


def ask_json(
    prompt: str,
    schema: dict[str, Any],
    model: str,
    reason: str,
    timeout: int = 600,
    launch: Launch = DirectLaunch(),
) -> Any:
    """The structured output of one call, or None when it fails, answers off-schema or capacity is spent."""
    spent = _no_capacity.is_set()
    answer = None if spent else _ask(prompt, schema, model, reason, timeout, launch)
    TALLY.record(answer is not None, skipped=spent)
    return answer


def no_capacity() -> bool:
    """True once a call has shown that no account can run any more this run."""
    return _no_capacity.is_set()


def _ask(
    prompt: str, schema: dict[str, Any], model: str, reason: str, timeout: int, launch: Launch
) -> Any:
    argv = command(model, schema, launch)
    try:
        with tempfile.TemporaryDirectory() as scratch:
            out = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                encoding="utf-8",
                timeout=timeout,
                check=True,
                env=launch.env(reason),
                cwd=scratch,
            ).stdout
        return json.loads(out).get("structured_output")
    except subprocess.CalledProcessError as e:
        _refused(argv[0], e)
    except subprocess.TimeoutExpired as e:
        _failed(argv[0], f"timed out after {e.timeout}s")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError) as e:
        _failed(argv[0], f"{type(e).__name__}: {e}")
    return None


def _failed(program: str, why: str) -> None:
    print(f"inbox-triage: headless call ({program}) failed: {why}"[:300], file=sys.stderr)


def _refused(program: str, e: subprocess.CalledProcessError) -> None:
    """Report an exit by its status and the launcher's own reason; latch the run off when no account can run."""
    lines = str(e.stderr or "").strip().splitlines()
    why = f"exit {e.returncode}: {lines[-1] if lines else 'no stderr'}"
    if e.returncode in NO_CAPACITY:
        _no_capacity.set()
        why += "; skipping this run's remaining calls"
    _failed(program, why)

#!/usr/bin/env python3
"""
headless.py — one schema-validated `claude -p` call that treats its prompt purely as data.

Every tool, MCP server and settings file is off and no session is saved, so text inside the
prompt can ask for anything and nothing can act on it. A failed or malformed call returns None.

Stdlib only.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
from typing import Any

HEADLESS_WRAPPER = "~/.claude-switch/claude-any.sh"
# Wrapper exits meaning no account will run any call (no quota, route refused, no usable login,
# kill switch), so every later call in this run would fail the same way.
NO_CAPACITY = frozenset({2, 3, 4, 10})
_spent = threading.Event()


def headless_claude() -> list[str]:
    """The headless entry point: the account-switching wrapper when installed (it adds -p), else `claude -p`."""
    wrapper = pathlib.Path(os.environ.get("CLAUDE_ANY_BIN", HEADLESS_WRAPPER)).expanduser()
    return [str(wrapper)] if wrapper.is_file() else ["claude", "-p"]


def command(model: str, schema: dict[str, Any]) -> list[str]:
    """Headless claude with no tools, no MCP servers, no settings and no saved session."""
    return [
        *headless_claude(),
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


def local_run(reason: str) -> dict[str, str]:
    """The wrapper sends runs to the cloud unless they say why they are local, and forwards stdin only on request."""
    return dict(os.environ, CLAUDE_ANY_STDIN="1", CLAUDE_LOCAL_OK=f"read-only-research:{reason}")


def ask_json(
    prompt: str, schema: dict[str, Any], model: str, reason: str, timeout: int = 600
) -> Any:
    """The structured output of one call, or None when it fails, answers off-schema or capacity is spent."""
    if _spent.is_set():
        return None
    try:
        with tempfile.TemporaryDirectory() as scratch:
            out = subprocess.run(
                command(model, schema),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=local_run(reason),
                cwd=scratch,
            ).stdout
        return json.loads(out).get("structured_output")
    except subprocess.CalledProcessError as e:
        return _refused(e)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError) as e:
        print(f"inbox-triage: headless call failed: {type(e).__name__}: {e}"[:300], file=sys.stderr)
        return None


def _refused(e: subprocess.CalledProcessError) -> None:
    """Report an exit by its status and the wrapper's own reason; latch the run off when no account can run."""
    lines = str(e.stderr or "").strip().splitlines()
    why = f"exit {e.returncode}: {lines[-1] if lines else 'no stderr'}"[:300]
    if e.returncode in NO_CAPACITY:
        _spent.set()
        why += "; skipping this run's remaining calls"
    print(f"inbox-triage: headless call failed: {why}", file=sys.stderr)

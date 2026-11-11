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
from typing import Any

HEADLESS_WRAPPER = "~/.claude-switch/claude-any.sh"


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
    """The structured output of one call, or None when the call fails or answers off-schema."""
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
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError) as e:
        print(f"inbox-triage: headless call failed: {str(e)[:300]}", file=sys.stderr)
        return None

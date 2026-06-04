#!/usr/bin/env python3
"""
_compat.py - fail fast, and clearly, on unsupported Python interpreters.

The scripts target Python 3.11+ (PEP 604 unions evaluated at def time, tomllib).
On older interpreters the first symptom is a cryptic import-time TypeError from an
annotation - notably on macOS, where the system `python3` is 3.9 and is exactly what
the plugin's commands invoke. Importing this module (the first local import in
_paths.py and _concurrency.py, which every entry script imports before anything
else local) turns that crash into one actionable sentence.

This module itself must stay importable on old interpreters: stdlib only, no
syntax newer than what Python 3.8 can parse, annotations deferred via __future__.
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 11)


def ensure_supported_python(version: tuple[int, int]) -> None:
    """Exit with an actionable message if `version` cannot run these scripts."""
    if version < MIN_PYTHON:
        sys.exit(
            "claude-tasks requires Python {}.{}+ but this interpreter is {}.{} "
            "(on macOS, bare `python3` is often the system 3.9). Re-run with a newer "
            "interpreter such as python3.13 / python3.12 / python3.11, or install one "
            "(e.g. `uv python install 3.13 --default` or `brew install python@3.13`).".format(
                MIN_PYTHON[0], MIN_PYTHON[1], version[0], version[1]
            )
        )


ensure_supported_python((sys.version_info[0], sys.version_info[1]))

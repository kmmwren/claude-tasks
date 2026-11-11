#!/usr/bin/env python3
"""
config.py — load a queue's tasks.toml with sane defaults.

Config is the per-user skin over the shared engine: a project name (used in the board
title), the list of domains a brief's `domain:` can take, and a timezone for any date
logic. All optional — an absent or partial file just falls back to the defaults, so a
vanilla queue works with no config at all.
"""
import os
import pathlib
import tomllib
from typing import Any

DEFAULTS: dict[str, Any] = {
    "name": "tasks",
    "domains": [],
    "timezone": "UTC",
    "fleet_blockers_dir": "",  # unset = fleet-blocker lane off
}


def load_config(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Merge tasks.toml over the defaults. Unknown keys are preserved (forward-compat)."""
    cfg = dict(DEFAULTS)
    path = pathlib.Path(root) / "tasks.toml"
    if path.is_file():
        with path.open("rb") as fh:
            cfg.update(tomllib.load(fh))
    return cfg

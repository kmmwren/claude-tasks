#!/usr/bin/env python3
"""
maintain_capacity.py: whether any Claude account can take a headless call right now.

Usage-limit state has one store and one reader API, `limit_state.py` in claude-switch, so this asks
it rather than parsing the store. A cloud runner has no claude-switch, and a store that was never
read says nothing either way; both assume capacity, because a judgement tier run needlessly fails
harmlessly while one deferred forever stalls the queue.

Stdlib only.
"""

import datetime
import importlib
import os
import pathlib
import sys
from collections.abc import Mapping
from typing import Protocol, cast

SWITCH_HOME = "~/.claude-switch"
SWITCH_HOME_ENV = "CLAUDE_SWITCH_HOME"
MODULE = "limit_state"


class UsageStore(Protocol):
    """The part of `limit_state.Store` this needs: the accounts it knows and which are usable now."""

    accounts: Mapping[str, object]

    def free(self, now: float | None = None) -> list[str]: ...


def switch_home(env: Mapping[str, str] | None = None) -> pathlib.Path:
    env = os.environ if env is None else env
    return pathlib.Path(env.get(SWITCH_HOME_ENV) or SWITCH_HOME).expanduser()


def _load_store(home: pathlib.Path) -> UsageStore:
    """`limit_state.load()` from claude-switch, imported without leaving it in the module cache."""
    cached = sys.modules.pop(MODULE, None)
    sys.path.insert(0, str(home))
    try:
        return cast(UsageStore, importlib.import_module(MODULE).load())
    finally:
        sys.path.remove(str(home))
        sys.modules.pop(MODULE, None)
        if cached is not None:
            sys.modules[MODULE] = cached


def has_capacity(home: pathlib.Path, now: datetime.datetime) -> bool:
    """False only when claude-switch knows its accounts and none of them can take a call."""
    if not (home / f"{MODULE}.py").is_file():
        return True
    try:
        store = _load_store(home)
        return not store.accounts or bool(store.free(now.timestamp()))
    except Exception as e:  # the module is another repo's code: whatever it raises, run anyway
        print(f"maintain: ignoring unusable {MODULE}: {e!r}", file=sys.stderr)
        return True

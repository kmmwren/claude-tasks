#!/usr/bin/env python3
"""
maintain_config.py: the `[maintain]` table of tasks.toml, read into typed settings.

  [maintain]
  lease_ttl_minutes = 120
  max_claims_per_tick = 3
  max_attempts = 3
  run_args = ["--dangerously-skip-permissions"]
  [maintain.laptop]
  interval_minutes = 5
  [maintain.cloud]
  interval_minutes = 60
  capabilities = ["slack"]     # what a cloud run can serve; "*" means everything

An absent or unreadable value warns and takes its default, so a vanilla queue works unconfigured.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import dataclasses
import datetime as dt
import enum
import os
from collections.abc import Mapping
from typing import Any, TypeVar

from _paths import warn
from config import load_config_or_defaults
from wake import EVERYTHING


class Tier(enum.Enum):
    LAPTOP = "laptop"
    CLOUD = "cloud"


T = TypeVar("T")

DEFAULT_INTERVAL_MINUTES = {Tier.LAPTOP: 5.0, Tier.CLOUD: 60.0}
# A laptop has the user's whole environment; a cloud run serves only what the queue declares.
DEFAULT_CAPABILITIES = {Tier.LAPTOP: frozenset({EVERYTHING}), Tier.CLOUD: frozenset[str]()}
DEFAULT_LEASE_TTL_MINUTES = 120.0
DEFAULT_MAX_CLAIMS = 3
DEFAULT_MAX_ATTEMPTS = 3
# The coordinator's own headless runs of action-task use the same flag.
DEFAULT_RUN_ARGS = ("--dangerously-skip-permissions",)


@dataclasses.dataclass(frozen=True)
class TierSettings:
    interval: dt.timedelta
    capabilities: frozenset[str]


@dataclasses.dataclass(frozen=True)
class MaintainSettings:
    tiers: Mapping[Tier, TierSettings]
    lease_ttl: dt.timedelta
    max_claims: int
    max_attempts: int
    run_args: tuple[str, ...]

    @classmethod
    def load(cls, root: str | os.PathLike[str]) -> "MaintainSettings":
        table = load_config_or_defaults(root, "maintain").get("maintain", {})
        table = table if isinstance(table, dict) else _bad("[maintain]", table, {})
        return cls(
            {tier: _tier_settings(tier, table) for tier in Tier},
            dt.timedelta(minutes=_positive(table, "lease_ttl_minutes", DEFAULT_LEASE_TTL_MINUTES)),
            int(_positive(table, "max_claims_per_tick", DEFAULT_MAX_CLAIMS)),
            int(_positive(table, "max_attempts", DEFAULT_MAX_ATTEMPTS)),
            _words(table, "run_args", DEFAULT_RUN_ARGS),
        )


def _bad(name: str, value: object, default: T) -> T:
    warn(f"{name} is not valid here ({value!r}); using the default", "maintain")
    return default


def _positive(table: Mapping[str, Any], key: str, default: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return _bad(key, value, default)
    return float(value)


def _words(table: Mapping[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = table.get(key, default)
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        return _bad(key, value, default)
    return tuple(value)


def _tier_settings(tier: Tier, table: Mapping[str, Any]) -> TierSettings:
    own = table.get(tier.value, {})
    own = own if isinstance(own, dict) else _bad(f"[maintain.{tier.value}]", own, {})
    minutes = _positive(own, "interval_minutes", DEFAULT_INTERVAL_MINUTES[tier])
    capabilities = _words(own, "capabilities", tuple(DEFAULT_CAPABILITIES[tier]))
    return TierSettings(dt.timedelta(minutes=minutes), frozenset(capabilities))

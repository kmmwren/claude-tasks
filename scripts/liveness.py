#!/usr/bin/env python3
"""
liveness.py — whether the work a brief names is still open.

A brief whose cited PRs are all merged or closed has nothing left to land, so prioritise drops it
from the filtered set. Which PRs a brief cites, and what state `gh` reports, come from the triage
engine (fingerprint, triage_sources); this module adds only the verdict and a short-lived cache so
ranking stays fast. A PR whose state cannot be read, or a machine with no `gh`, never drops a brief:
it warns once and keeps it.
"""

import concurrent.futures
import dataclasses
import json
import pathlib
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Mapping

from _concurrency import atomic_write
from fingerprint import Fingerprint
from picks import state_dir
from triage_sources import GH_ISSUE, PrState, PrStateCheck, pr_state

TTL_SECONDS = 600
LOOKUP_WORKERS = 8
CACHE_FILE = "pr-state.json"
CLOSED_FOR_GOOD = frozenset({PrState.MERGED, PrState.CLOSED})
CACHEABLE = CLOSED_FOR_GOOD | {PrState.OPEN, PrState.NOT_A_PR}


@dataclasses.dataclass(frozen=True)
class PrRef:
    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"


def cited_prs(text: str) -> frozenset[PrRef]:
    """Every `owner/repo#N` or PR/issue URL in the brief's title and authored body."""
    sources = Fingerprint.of_text(text).sources
    return frozenset(PrRef(m[1], int(m[2])) for s in sources if (m := GH_ISSUE.fullmatch(s)))


def gh_on_path() -> bool:
    return shutil.which("gh") is not None


def warn(message: str) -> None:
    print(f"prioritise: {message}", file=sys.stderr)


class Liveness:
    """Dead briefs among a set, with PR states cached on disk for `ttl` seconds."""

    def __init__(
        self,
        cache_file: pathlib.Path,
        lookup: PrStateCheck = pr_state,
        gh_available: Callable[[], bool] = gh_on_path,
        ttl: float = TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        warn: Callable[[str], None] = warn,
    ) -> None:
        self.cache_file, self._lookup, self._gh_available = cache_file, lookup, gh_available
        self._ttl, self._clock, self._warn, self._warned = ttl, clock, warn, False

    @classmethod
    def for_queue(cls, root: pathlib.Path) -> "Liveness":
        return cls(state_dir(root) / CACHE_FILE)

    def dead(self, texts: Mapping[str, str]) -> set[str]:
        """Ids of the briefs that cite at least one PR, every one of them merged or closed."""
        cited = {bid: cited_prs(text) for bid, text in texts.items()}
        states = self._states(frozenset().union(*cited.values()))
        return {bid for bid, refs in cited.items() if _all_done(refs, states)}

    def _states(self, refs: frozenset[PrRef]) -> dict[PrRef, PrState | None]:
        cached = self._read_cache()
        now = self._clock()
        states: dict[PrRef, PrState | None] = {
            ref: state for ref in refs if (state := _fresh(cached.get(str(ref)), now, self._ttl))
        }
        missing = sorted(refs - states.keys(), key=str)
        looked_up = self._look_up(missing)
        states.update(looked_up)
        unread = [ref for ref, state in looked_up.items() if state is None]
        if unread:
            self._warn_once(
                f"could not read the state of {len(unread)} cited PRs; keeping their briefs"
            )
        self._write_cache(cached, looked_up, now)
        return states

    def _look_up(self, refs: list[PrRef]) -> dict[PrRef, PrState | None]:
        if not refs:
            return {}
        if not self._gh_available():
            self._warn_once(
                "gh is not available, so PR liveness is not checked; keeping every brief"
            )
            return {}
        with concurrent.futures.ThreadPoolExecutor(LOOKUP_WORKERS) as pool:
            return dict(zip(refs, pool.map(lambda r: self._lookup(r.repo, r.number), refs)))

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            self._warn(message)

    def _read_cache(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.cache_file.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_cache(
        self,
        cached: dict[str, dict[str, object]],
        looked_up: Mapping[PrRef, PrState | None],
        now: float,
    ) -> None:
        fresh = {
            str(ref): {"state": state.value, "at": now}
            for ref, state in looked_up.items()
            if state in CACHEABLE
        }
        if not fresh:
            return
        try:
            atomic_write(self.cache_file, json.dumps({**cached, **fresh}))
        except OSError:
            pass


def _fresh(entry: object, now: float, ttl: float) -> PrState | None:
    """The cached state when it is well formed and younger than `ttl`, else None."""
    if not isinstance(entry, dict):
        return None
    at, raw = entry.get("at"), entry.get("state")
    if not isinstance(at, int | float) or not isinstance(raw, str) or now - at > ttl:
        return None
    try:
        return PrState(raw)
    except ValueError:
        return None


def _all_done(refs: Iterable[PrRef], states: Mapping[PrRef, PrState | None]) -> bool:
    """True when `refs` holds a real PR and every real PR in it is merged or closed.

    A number that is an issue, not a PR, says nothing about the work; a PR whose state is
    unknown (absent from `states`) keeps the brief alive.
    """
    real = [states.get(ref) for ref in refs if states.get(ref) is not PrState.NOT_A_PR]
    return bool(real) and all(state in CLOSED_FOR_GOOD for state in real)

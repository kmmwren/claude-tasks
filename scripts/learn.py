#!/usr/bin/env python3
"""Propose taste-profile edits from the pick log, validated by replaying held-out picks.

    learn.py                     # propose (and print) a goal-weight diff
    learn.py --apply <id>        # apply a stored proposal
    learn.py --revert <version>  # restore an earlier profile version

A pick or bump at position p says the chosen brief beats the p briefs shown above it (skip-above,
which corrects for position: a pick at the top teaches nothing). A skip says the next brief beat it.
Goals that keep winning gain weight and goals that keep losing shed it, each change capped. The
proposal is trained on older picks and replayed on the newest: a reorder must order them better
than the current weights do, a nudge must order them no worse. Applying snapshots the old profile first, so every change is revertible.
Auto-apply stays off until `[learn] auto_apply = true` and the log holds `min_runs` lists.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import itertools
import json
import pathlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _concurrency import atomic_write  # noqa: E402
from _paths import require_queue, resolve_root  # noqa: E402
from config import load_config  # noqa: E402
from git_sync import sync_commit  # noqa: E402
from picks import Choice, PickLog, Shown, state_dir  # noqa: E402
from taste import PROFILE_FILE, Profile, load_profile, profile_path, render  # noqa: E402

DEFAULTS: dict[str, Any] = {
    "step": 0.05, "max_change": 0.2, "max_goals": 3, "floor": 0.05, "holdout": 0.3, "min_pairs": 10,
    "auto_apply": False, "min_runs": 25,
}


def settings(root: pathlib.Path) -> dict[str, Any]:
    return {**DEFAULTS, **load_config(root).get("learn", {})}


@dataclasses.dataclass(frozen=True)
class Pair:
    winner: frozenset[str]
    loser: frozenset[str]


def pairs_for(choice: Choice, shown: Shown) -> list[Pair]:
    p = choice.position
    items = [frozenset(item.goals) for item in shown.items]
    if p is None or p >= len(items):
        return []
    if choice.kind == "skip":
        return [Pair(items[p + 1], items[p])] if p + 1 < len(items) else []
    return [Pair(items[p], above) for above in items[:p]]


def pairs(log: PickLog) -> list[Pair]:
    shown = {s.impression: s for s in log.shown()}
    return [pair for c in log.choices() if c.impression in shown for pair in pairs_for(c, shown[c.impression])]


def score(weights: Mapping[str, float], goals: frozenset[str]) -> float:
    return sum(weights.get(g, 0.0) for g in goals)


def accuracy(weights: Mapping[str, float], held_out: Sequence[Pair]) -> float:
    """Share of pairs the weights order as chosen; a tie counts half."""
    def credit(pair: Pair) -> float:
        won, lost = score(weights, pair.winner), score(weights, pair.loser)
        return 1.0 if won > lost else 0.5 if won == lost else 0.0
    return sum(credit(p) for p in held_out) / len(held_out)


def net_preference(train: Sequence[Pair]) -> dict[str, int]:
    net: dict[str, int] = {}
    for pair in train:
        for goal in pair.winner - pair.loser:
            net[goal] = net.get(goal, 0) + 1
        for goal in pair.loser - pair.winner:
            net[goal] = net.get(goal, 0) - 1
    return net


def nudged(weights: Mapping[str, float], train: Sequence[Pair], config: Mapping[str, Any]) -> dict[str, float]:
    """Move the most-preferred and most-avoided goals, each by at most `max_change` of its weight."""
    net = {g: n for g, n in net_preference(train).items() if g in weights and n}
    strongest = sorted(net, key=lambda g: (-abs(net[g]), g))[: int(config["max_goals"])]
    cap, step, floor = float(config["max_change"]), float(config["step"]), float(config["floor"])
    return {**weights, **{g: max(floor, round(weights[g] * (1 + max(-cap, min(cap, step * net[g]))), 3))
                          for g in strongest}}


@dataclasses.dataclass(frozen=True)
class Change:
    goal: str
    old: float
    new: float


@dataclasses.dataclass(frozen=True)
class Proposal:
    id: str
    from_version: str
    changes: tuple[Change, ...]
    kind: str
    old_accuracy: float
    new_accuracy: float
    held_out: int
    status: str = "proposed"

    def weights(self) -> dict[str, float]:
        return {c.goal: c.new for c in self.changes}

    def describe(self) -> str:
        head = (f"proposal {self.id} ({self.kind}; held-out accuracy {self.old_accuracy:.2f} -> "
                f"{self.new_accuracy:.2f} over {self.held_out} pairs; {self.status})")
        return "\n".join([head] + [f"  {c.goal}: {c.old:g} -> {c.new:g}" for c in self.changes])

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Proposal:
        return cls(d["id"], d["from_version"], tuple(Change(**c) for c in d["changes"]), d["kind"],
                   d["old_accuracy"], d["new_accuracy"], d["held_out"], d["status"])


def ordering(weights: Mapping[str, float]) -> dict[tuple[str, str], int]:
    """Which goal of each pair outranks the other; splitting a tie counts as a change of order."""
    return {(a, b): (weights[a] > weights[b]) - (weights[a] < weights[b])
            for a, b in itertools.combinations(sorted(weights), 2)}


def validated(kind: str, before: float, after: float) -> bool:
    """A reorder must replay better on held-out picks; a nudge must only not replay worse."""
    return after > before or (kind == "nudge" and after == before)


def propose(profile: Profile, all_pairs: Sequence[Pair], config: Mapping[str, Any],
            today: str) -> Proposal | str:
    """A validated proposal, or the reason there is none."""
    if len(all_pairs) < int(config["min_pairs"]):
        return f"only {len(all_pairs)} preference pairs; learning starts at {config['min_pairs']}"
    held = max(1, int(len(all_pairs) * float(config["holdout"])))
    train, held_out = all_pairs[:-held], all_pairs[-held:]
    old = profile.weights
    new = nudged(old, train, config)
    changes = tuple(Change(g, old[g], new[g]) for g in old if new[g] != old[g])
    if not changes:
        return "the picks do not favour any goal over another"
    kind = "reorder" if ordering(new) != ordering(old) else "nudge"
    before, after = accuracy(old, held_out), accuracy(new, held_out)
    if not validated(kind, before, after):
        return f"the {kind} did not replay well enough on held-out picks ({before:.2f} -> {after:.2f})"
    version = profile.with_weights(new, today).version
    return Proposal(f"{today}-{version}", profile.version, changes, kind, before, after, len(held_out))


class ProposalStore:
    """Proposals and profile snapshots under the queue's state dir, outside its repo."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.dir = state_dir(root) / "learn"

    def save(self, proposal: Proposal) -> None:
        atomic_write(self.dir / f"{proposal.id}.json", json.dumps(dataclasses.asdict(proposal), indent=2))

    def load(self, proposal_id: str) -> Proposal:
        path = self.dir / f"{proposal_id}.json"
        if not path.is_file():
            raise SystemExit(f"no proposal {proposal_id}")
        return Proposal.from_dict(json.loads(path.read_text()))

    def snapshot(self, profile: Profile) -> None:
        atomic_write(self.dir / "profiles" / f"{profile.version}.md", render(profile))

    def snapshot_text(self, version: str) -> str:
        path = self.dir / "profiles" / f"{version}.md"
        if not path.is_file():
            raise SystemExit(f"no snapshot of profile version {version}")
        return path.read_text()


def current(root: pathlib.Path) -> Profile:
    profile = load_profile(root)
    if profile is None:
        raise SystemExit("no profile/priorities.md; run seed_profile.py first")
    return profile


def write_profile(root: pathlib.Path, text: str, message: str) -> None:
    atomic_write(profile_path(root), text)
    sync_commit(message, root, paths=[str(PROFILE_FILE)])


def apply(root: pathlib.Path, proposal: Proposal, today: str) -> Proposal:
    profile = current(root)
    if profile.version != proposal.from_version:
        raise SystemExit(f"profile is now {profile.version}, not {proposal.from_version}; re-run learn.py")
    store = ProposalStore(root)
    store.snapshot(profile)
    updated = profile.with_weights(proposal.weights(), today)
    write_profile(root, render(updated), f"profile: apply learn proposal {proposal.id}")
    applied = dataclasses.replace(proposal, status="applied")
    store.save(applied)
    return applied


def revert(root: pathlib.Path, version: str) -> str:
    store = ProposalStore(root)
    text = store.snapshot_text(version)
    store.snapshot(current(root))
    write_profile(root, text, f"profile: revert to {version}")
    return f"profile reverted to {version}"


def auto_applies(proposal: Proposal, runs: int, config: Mapping[str, Any]) -> bool:
    """Only small nudges, only once enough lists have been shown, only when the owner switched it on."""
    return bool(config["auto_apply"]) and runs >= int(config["min_runs"]) and proposal.kind == "nudge"


def learn(root: pathlib.Path, today: str) -> str:
    config = settings(root)
    log = PickLog(root)
    outcome = propose(current(root), pairs(log), config, today)
    if isinstance(outcome, str):
        return f"no change: {outcome}"
    store = ProposalStore(root)
    store.save(outcome)
    if auto_applies(outcome, len(log.shown()), config):
        return apply(root, outcome, today).describe()
    return outcome.describe() + f"\napply with: learn.py --apply {outcome.id}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Propose taste-profile edits from the pick log.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--apply", default=None, metavar="ID", help="apply a stored proposal")
    ap.add_argument("--revert", default=None, metavar="VERSION", help="restore a profile snapshot")
    ap.add_argument("--today", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    root = require_queue(pathlib.Path(a.root) if a.root else resolve_root())
    today = a.today or datetime.date.today().isoformat()
    if a.revert:
        print(revert(root, a.revert))
    elif a.apply:
        print(apply(root, ProposalStore(root).load(a.apply), today).describe())
    else:
        print(learn(root, today))


if __name__ == "__main__":
    main()  # pragma: no cover

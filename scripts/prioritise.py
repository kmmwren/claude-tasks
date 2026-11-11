#!/usr/bin/env python3
"""
prioritise.py — rank the ready/ queue, dependency-aware: "what should I do next?"

Two scorers, chosen by `[priority] scorer` in tasks.toml (default "additive") or `--scorer`:

  additive  due urgency + importance + quick-win (low effort) + lead-time bonus
            + unlock bonus + config tag weights. The original ranking.
  wsjf      Weighted Shortest Job First: cost of delay / job size + aging, where
            cost of delay = importance value + time criticality + goal (milestone) weight
            + tag weights + leverage (the longest chain of live briefs waiting on this one).
            A blocker inherits the score of the highest brief waiting on it, and a class of
            service (`cos:` field or `cos:<lane>` tag) picks the lane: expedite (capped),
            fixed-date (once its due date is near), standard, or chore (a reserved share).

Under either scorer a brief whose `clears: <id>` names an active or unverified fleet blocker
(record in tasks.toml `fleet_blockers_dir`) sorts first, in the uncapped `fleet-blocker` lane.

Both scores and both ranks are always computed and emitted in --json, so a replay can
compare them before the default moves. Every wsjf weight lives in `[priority]`.

Dependencies are real: a brief lists `blockers:` (ids or external text) and/or
`depends:<id>` tags. A dependency counts as MET only once its target brief is `done`, so
the queue self-unblocks as work completes. A brief is *actionable* when it is `autonomy:
full`, unblocked, and runnable in the current context.

Usage:
  python scripts/prioritise.py                 # ranked table (all of ready/, flagged)
  python scripts/prioritise.py --actionable    # only the auto-actionable ones
  python scripts/prioritise.py --top 5 --json  # machine-readable, top 5, with the "why"
  python scripts/prioritise.py --scorer wsjf   # rank by WSJF for this call
  python scripts/prioritise.py --context cloud # exclude briefs needing local/other-repo access
"""

import argparse
import dataclasses
import datetime
import enum
import json
import pathlib
import sys
from collections.abc import Callable
from typing import Any

from _paths import require_queue, resolve_root
from config import load_config
from deps import Brief, dependency_edges, fm, load_queue, unmet
from fleet_blockers import FleetBlockers

EFFORT_RANK = {"xs": 40, "s": 30, "m": 20, "l": 10, "xl": 5, "": 15}
EFFORT_DAYS = {"xs": 0.125, "s": 0.5, "m": 2, "l": 7, "xl": 14}
UNLOCK_WEIGHT = 150
INDEX_DIRS = ("ready", "in-progress", "blocked", "parked", "done")
LIVE_DIRS = ("ready", "in-progress", "blocked")
DEFAULT_IMPORTANCE = 3

PRIORITY_DEFAULTS: dict[str, Any] = {
    "scorer": "additive",
    "importance_values": {"1": 2500, "2": 1000, "3": 300, "4": 100, "5": 30},
    "job_size": {"xs": 1, "s": 2, "m": 3, "l": 5, "xl": 8, "": 3},
    "due_curve": [[-1, 1000], [3, 500], [7, 200], [36500, 50]],
    "lead_time_weight": 300,
    "leverage_weight": 150,
    "milestone_weight": 200,
    "aging_per_day": 2,
    "aging_cap": 120,
    "expedite_cap": 2,
    "chore_share": 0.2,
    "fixed_date_horizon_days": 7,
    "human_tags": ["tier:human"],
    "tag_weights": {},
}

# A context excludes briefs declaring a requires-* field it cannot satisfy.
CONTEXT_EXCLUSIONS = {
    # cloud: only this repo is available; no local-machine files, no other repos
    "cloud": {"requires-local", "requires-repo"},
}


class Scorer(enum.Enum):
    ADDITIVE = "additive"
    WSJF = "wsjf"


class Lane(enum.Enum):
    FLEET_BLOCKER = "fleet-blocker"  # derived from `clears:`, never declared with `cos:`
    EXPEDITE = "expedite"
    FIXED_DATE = "fixed-date"
    STANDARD = "standard"
    CHORE = "chore"


DECLARABLE_LANES = tuple(lane for lane in Lane if lane is not Lane.FLEET_BLOCKER)


class Move(enum.Enum):
    EXECUTE = "execute"
    DEFANG = "de-fang"
    ASK = "ask"
    BLOCKED = "blocked"


def _merged(overrides: dict[str, Any]) -> dict[str, Any]:
    """PRIORITY_DEFAULTS under `overrides`, merging table values one level deep."""
    out = dict(PRIORITY_DEFAULTS)
    for key, value in overrides.items():
        base = out.get(key)
        out[key] = (
            {**base, **value} if isinstance(base, dict) and isinstance(value, dict) else value
        )
    return out


@dataclasses.dataclass(frozen=True)
class Weights:
    """The `[priority]` table of tasks.toml, typed."""

    scorer: Scorer
    importance: dict[int, float]
    job_size: dict[str, float]
    due_curve: tuple[tuple[int, float], ...]
    lead_time: float
    leverage: float
    milestone: float
    aging_per_day: float
    aging_cap: float
    expedite_cap: int
    chore_share: float
    fixed_date_horizon_days: int
    human_tags: frozenset[str]
    tags: dict[str, int]

    @classmethod
    def from_priority(cls, table: dict[str, Any]) -> "Weights":
        p = _merged(table)
        return cls(
            scorer=Scorer(p["scorer"]),
            importance={int(k): float(v) for k, v in p["importance_values"].items()},
            job_size={str(k): float(v) for k, v in p["job_size"].items()},
            due_curve=tuple((int(d), float(v)) for d, v in p["due_curve"]),
            lead_time=float(p["lead_time_weight"]),
            leverage=float(p["leverage_weight"]),
            milestone=float(p["milestone_weight"]),
            aging_per_day=float(p["aging_per_day"]),
            aging_cap=float(p["aging_cap"]),
            expedite_cap=int(p["expedite_cap"]),
            chore_share=float(p["chore_share"]),
            fixed_date_horizon_days=int(p["fixed_date_horizon_days"]),
            human_tags=frozenset(p["human_tags"]),
            tags=dict(p["tag_weights"]),
        )

    @classmethod
    def for_queue(cls, root: pathlib.Path) -> "Weights":
        """A bad setting falls back to the defaults, keeping tag_weights, rather than failing."""
        table = load_config(root).get("priority", {})
        try:
            return cls.from_priority(table)
        except (ValueError, TypeError, AttributeError) as err:
            print(
                f"prioritise: ignoring [priority] settings in tasks.toml ({err}); "
                "using defaults with tag_weights",
                file=sys.stderr,
            )
            return cls.from_priority({"tag_weights": table.get("tag_weights", {})})

    def importance_value(self, raw: str) -> float:
        level = int(raw) if raw.strip().isdigit() else DEFAULT_IMPORTANCE
        return self.importance.get(level, self.importance[DEFAULT_IMPORTANCE])

    def __post_init__(self) -> None:
        if min(self.job_size.values()) <= 0:
            raise ValueError(f"job_size values must be positive: {self.job_size}")

    def size(self, effort: str) -> float:
        return self.job_size.get(effort, self.job_size[""])

    def due_value(self, days: int | None) -> float:
        if days is None:
            return 0.0
        return next((v for limit, v in self.due_curve if days <= limit), 0.0)


DEFAULT_WEIGHTS = Weights.from_priority({})


@dataclasses.dataclass(frozen=True)
class Factors:
    """One brief's WSJF score, factor by factor, so the ranking can always say why."""

    importance: float
    time_criticality: float
    goal: float
    tags: float
    leverage: float
    size: float
    age: float

    @property
    def cost_of_delay(self) -> float:
        return self.importance + self.time_criticality + self.goal + self.tags + self.leverage

    @property
    def total(self) -> float:
        return round(self.cost_of_delay / self.size + self.age, 1)

    def why(self) -> dict[str, float]:
        return {**dataclasses.asdict(self), "cost_of_delay": self.cost_of_delay}


def _parse_tags(brief: dict[str, Any]) -> set[str]:
    raw = (brief.get("tags", "") or "").strip().strip("[]")
    return {t.strip() for t in raw.split(",") if t.strip()}


def _date(raw: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def days_until_due(brief: dict[str, Any], today: datetime.date) -> int | None:
    due = _date(brief.get("due", "") or "")
    return (due - today).days if due else None


def age_days(brief: dict[str, Any], today: datetime.date) -> int:
    """Days since `created:`, else since the date an id like 2026-11-04-x starts with."""
    born = _date(brief.get("created", "") or "") or _date(brief.get("id", "") or "")
    return max(0, (today - born).days) if born else 0


def parse_deps(brief: dict[str, Any]) -> set[str]:
    """Dependency ids: `blocked_by` ids, every `blockers:` token, and each `depends:<id>` tag."""
    b = Brief("", "", brief)
    return b.declared_ids | b.legacy_tokens


def is_runnable_in_context(brief: dict[str, Any], context: str) -> bool:
    """False if the brief declares a requires-* field the context cannot satisfy."""
    for field in CONTEXT_EXCLUSIONS.get(context, set()):
        val = (brief.get(field, "") or "").strip()
        if val and val.lower() not in ("false", "[]"):
            return False
    return True


def lead_time_urgent(brief: dict[str, Any], today: datetime.date) -> bool:
    """True if the effort lead time means this must START now even though the due date is 'soon'."""
    days = days_until_due(brief, today)
    effort_days = EFFORT_DAYS.get(brief.get("estimated-effort", ""), 0.5)
    return days is not None and 0 <= days <= effort_days


def score(
    brief: dict[str, Any],
    today: datetime.date,
    *,
    unlocks: int = 0,
    tag_weights: dict[str, int] | None = None,
) -> int:
    """The additive score: the original ranking, unchanged."""
    due_score = 0
    if brief.get("due"):
        try:
            days = (datetime.date.fromisoformat(brief["due"]) - today).days
            due_score = 1000 if days < 0 else 500 if days <= 3 else 200 if days <= 7 else 50
        except ValueError:
            pass
    try:
        imp = int(brief.get("importance", "3"))
    except ValueError:
        imp = 3
    importance_score = (5 - imp) * 100
    effort_score = EFFORT_RANK.get(brief.get("estimated-effort", ""), 15)
    lead_bonus = 300 if lead_time_urgent(brief, today) else 0
    tags = _parse_tags(brief)
    tag_score = sum(w for t, w in (tag_weights or {}).items() if t in tags)
    return (
        due_score
        + importance_score
        + effort_score
        + lead_bonus
        + tag_score
        + unlocks * UNLOCK_WEIGHT
    )


def factors(
    brief: dict[str, Any],
    today: datetime.date,
    *,
    leverage: int = 0,
    weights: Weights = DEFAULT_WEIGHTS,
) -> Factors:
    tags = _parse_tags(brief)
    lead = weights.lead_time if lead_time_urgent(brief, today) else 0.0
    return Factors(
        importance=weights.importance_value(brief.get("importance", "") or ""),
        time_criticality=weights.due_value(days_until_due(brief, today)) + lead,
        goal=weights.milestone if (brief.get("milestone", "") or "").strip() else 0.0,
        tags=float(sum(w for t, w in weights.tags.items() if t in tags)),
        leverage=leverage * weights.leverage,
        size=weights.size(brief.get("estimated-effort", "") or ""),
        age=min(weights.aging_cap, age_days(brief, today) * weights.aging_per_day),
    )


def wsjf_score(
    brief: dict[str, Any],
    today: datetime.date,
    *,
    leverage: int = 0,
    weights: Weights = DEFAULT_WEIGHTS,
) -> float:
    return factors(brief, today, leverage=leverage, weights=weights).total


def index_queue(root: pathlib.Path) -> tuple[dict[str, str], dict[str, int]]:
    """Map every brief id to its status folder, and count how many briefs depend on each id."""
    status_by_id: dict[str, str] = {}
    unlocks: dict[str, int] = {}
    for d in INDEX_DIRS:
        for f in (root / d).glob("*.md"):
            b = fm(f.read_text())
            status_by_id[b.get("id", f.stem)] = d
            for dep in parse_deps(b):
                unlocks[dep] = unlocks.get(dep, 0) + 1
    return status_by_id, unlocks


@dataclasses.dataclass(frozen=True)
class WaitGraph:
    """Which live briefs wait on which: the edges leverage and priority inheritance walk."""

    waiters: dict[str, tuple[str, ...]]

    @classmethod
    def build(cls, briefs: list[Brief]) -> "WaitGraph":
        known = {b.id for b in briefs}
        edges: dict[str, list[str]] = {}
        for b in (b for b in briefs if b.status in LIVE_DIRS):
            for dep in sorted(dependency_edges(b, known)):
                edges.setdefault(dep, []).append(b.id)
        return cls({dep: tuple(ws) for dep, ws in edges.items()})

    def fold(self, base: Callable[[str], float], step: float) -> Callable[[str], float]:
        """value(n) = max(base(n), value(w) + step for every waiter w), cycle-safe."""
        memo: dict[str, float] = {}

        def value(node: str, path: frozenset[str] = frozenset()) -> float:
            if node not in memo:
                seen = path | {node}
                kids = [value(w, seen) + step for w in self.waiters.get(node, ()) if w not in seen]
                memo[node] = max([base(node), *kids])
            return memo[node]

        return value

    def depth(self) -> Callable[[str], float]:
        return self.fold(lambda _: 0.0, 1.0)

    def inherited(self, own: dict[str, float]) -> Callable[[str], float]:
        return self.fold(lambda bid: own.get(bid, 0.0), 0.0)


def is_blocked(brief: dict[str, Any], done_ids: set[str]) -> tuple[bool, list[str]]:
    """A brief is blocked if autonomy is blocked, or any dependency is not yet `done`."""
    if brief.get("autonomy") == "blocked":
        return True, []
    pending = unmet(Brief("", "", brief), done_ids)
    return bool(pending), pending


def declared_lane(brief: dict[str, Any]) -> Lane:
    raw = (brief.get("cos", "") or "").strip() or next(
        (t[4:] for t in sorted(_parse_tags(brief)) if t.startswith("cos:")), ""
    )
    return next((lane for lane in DECLARABLE_LANES if lane.value == raw), Lane.STANDARD)


def lane_of(brief: dict[str, Any], today: datetime.date, weights: Weights) -> Lane:
    """The declared lane; a fixed-date brief stays standard until its due date is near."""
    lane = declared_lane(brief)
    days = days_until_due(brief, today)
    near = days is not None and days <= weights.fixed_date_horizon_days
    return Lane.STANDARD if lane is Lane.FIXED_DATE and not near else lane


def move_of(brief: dict[str, Any], blocked: bool, weights: Weights) -> Move:
    """What to do with it: run it, ask her, make it safe for Claude to run, or wait."""
    autonomy = brief.get("autonomy", "")
    if blocked:
        return Move.BLOCKED
    if autonomy == "needs-input":
        return Move.ASK
    human = bool(_parse_tags(brief) & weights.human_tags)
    return Move.EXECUTE if autonomy == "full" and not human else Move.DEFANG


@dataclasses.dataclass(frozen=True)
class _RankContext:
    """Everything one ranking pass reads once: the queue's graph, scores and settings."""

    today: datetime.date
    context: str
    weights: Weights
    tag_weights: dict[str, int]
    unlocks: dict[str, int]
    depth: Callable[[str], float]
    scored: dict[str, Factors]
    done_ids: set[str]
    inherited: Callable[[str], float]
    fleet: FleetBlockers

    @classmethod
    def load(
        cls,
        root: pathlib.Path,
        today: datetime.date,
        context: str,
        weights: Weights,
        tag_weights: dict[str, int],
    ) -> "_RankContext":
        queue = load_queue(root)
        graph = WaitGraph.build(queue)
        depth = graph.depth()
        scored = {
            b.id: factors(b.fields, today, leverage=int(depth(b.id)), weights=weights)
            for b in queue
            if b.status in LIVE_DIRS
        }
        return cls(
            today,
            context,
            weights,
            tag_weights,
            index_queue(root)[1],
            depth,
            scored,
            {b.id for b in queue if b.status == "done"},
            graph.inherited({bid: f.total for bid, f in scored.items()}),
            FleetBlockers.for_queue(root),
        )

    def wsjf(self, bid: str) -> tuple[Factors, float]:
        """The brief's own factors and its score after inheriting from its waiters."""
        own = self.scored[bid]
        return own, max(own.total, round(self.inherited(bid), 1))


def _annotate(f: pathlib.Path, b: dict[str, Any], ctx: _RankContext) -> dict[str, Any]:
    bid = b.get("id", f.stem)
    blocked, pending = is_blocked(b, ctx.done_ids)
    own, wsjf = ctx.wsjf(bid)
    blocker = None if blocked else ctx.fleet.clearing(b)
    why: dict[str, Any] = {**own.why(), "inherited": round(wsjf - own.total, 1)}
    if blocker:
        why["fleet_blocker"] = blocker.label()
    b.update(
        {
            "_score_additive": score(
                b, ctx.today, unlocks=ctx.unlocks.get(bid, 0), tag_weights=ctx.tag_weights
            ),
            "_score_wsjf": wsjf,
            "_why": why,
            "_unlocks": ctx.unlocks.get(bid, 0),
            "_leverage": int(ctx.depth(bid)),
            "_lead_urgent": lead_time_urgent(b, ctx.today),
            "_blocked": blocked,
            "_unmet": pending,
            "_file": f.name,
            "_lane": Lane.FLEET_BLOCKER if blocker else lane_of(b, ctx.today, ctx.weights),
            "_move": move_of(b, blocked, ctx.weights),
            "_actionable": b.get("autonomy") == "full"
            and not blocked
            and is_runnable_in_context(b, ctx.context),
        }
    )
    return b


def _by(key: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda b: (-b[key], b.get("estimated-effort", "z")))


def _interleave(
    main: list[dict[str, Any]], chores: list[dict[str, Any]], share: float
) -> list[dict[str, Any]]:
    """`main` with one chore after every N items, so a `share` of slots stays reserved."""
    if share <= 0:
        return main + chores
    gap, pending, out = max(1, round(1 / share) - 1), list(chores), []
    for i, item in enumerate(main, 1):
        out.append(item)
        if i % gap == 0 and pending:
            out.append(pending.pop(0))
    return out + pending


def _fleet_first(ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Briefs clearing an open fleet blocker ahead of the rest, each group's order kept."""
    return sorted(ordered, key=lambda b: b["_lane"] is not Lane.FLEET_BLOCKER)


def order_lanes(items: list[dict[str, Any]], weights: Weights) -> list[dict[str, Any]]:
    """Fleet blockers (uncapped), then expedite (actionable, up to the cap), fixed-date, standard with chores."""
    ranked = _by("_score_wsjf", items)
    expedite = [b for b in ranked if b["_lane"] is Lane.EXPEDITE]
    held = {id(b) for b in [b for b in expedite if b["_actionable"]][: weights.expedite_cap]}
    for b in expedite:
        if id(b) not in held:
            b["_lane"] = Lane.STANDARD
    lane = {ln: [b for b in ranked if b["_lane"] is ln] for ln in Lane}
    return (
        lane[Lane.FLEET_BLOCKER]
        + lane[Lane.EXPEDITE]
        + lane[Lane.FIXED_DATE]
        + _interleave(lane[Lane.STANDARD], lane[Lane.CHORE], weights.chore_share)
    )


def _orders(items: list[dict[str, Any]], weights: Weights) -> dict[Scorer, list[dict[str, Any]]]:
    """Both orderings, each brief stamped with its rank under each."""
    orders = {
        Scorer.ADDITIVE: _fleet_first(_by("_score_additive", items)),
        Scorer.WSJF: order_lanes(items, weights),
    }
    for scorer, ordered in orders.items():
        for i, b in enumerate(ordered, 1):
            b[f"_rank_{scorer.value}"] = i
    return orders


def rank(
    root: pathlib.Path,
    today: datetime.date | None = None,
    context: str = "",
    tag_weights: dict[str, int] | None = None,
    weights: Weights | None = None,
    scorer: Scorer | None = None,
) -> list[dict[str, Any]]:
    """Every ready/ brief, scored both ways and annotated, in the chosen scorer's order."""
    weights = weights or Weights.for_queue(root)
    tags = weights.tags if tag_weights is None else tag_weights
    ctx = _RankContext.load(root, today or datetime.date.today(), context, weights, tags)
    items = [
        _annotate(f, dict(fm(f.read_text())), ctx) for f in sorted((root / "ready").glob("*.md"))
    ]
    chosen = scorer or weights.scorer
    for b in items:
        b["_score"] = b[f"_score_{chosen.value}"]
    return _orders(items, weights)[chosen]


def _flags(b: dict[str, Any]) -> str:
    flags = ""
    if not b["_actionable"]:
        if b["_blocked"]:
            flags += f" ⛔blocked({', '.join(b['_unmet']) or b.get('autonomy', '')})"
        elif b.get("autonomy") != "full":
            flags += f" 🙋{b.get('autonomy', '')}"
    if b["_lead_urgent"]:
        flags += " ⏱lead"
    if b["_unlocks"]:
        flags += f" →{b['_unlocks']}"
    return flags


def _why_text(why: dict[str, Any]) -> str:
    text = f"{why['cost_of_delay']:g}/{why['size']:g}"
    if why["age"]:
        text += f" +age {why['age']:g}"
    if why["inherited"]:
        text += f" +inh {why['inherited']:g}"
    if why.get("fleet_blocker"):
        text += f" clears {why['fleet_blocker']}"
    return text


def _row(b: dict[str, Any]) -> str:
    return (
        f"{b['_score']:>7g}  {b['_lane'].value:<13} {b['_move'].value:<8} "
        f"{b.get('importance', '?'):<2} {b.get('due', '') or '-':<10} "
        f"{b.get('estimated-effort', '') or '-':<3} {b.get('id', '?')}{_flags(b)}"
        f"  [wsjf {_why_text(b['_why'])}]"
    )


SCHEMA_VERSION = 2


def _json_row(b: dict[str, Any]) -> dict[str, Any]:
    """Pre-v2 keys keep their meaning; v2 keys are additive."""
    return {
        "id": b.get("id"),
        "score": b["_score"],
        "title": b.get("title", ""),
        "importance": b.get("importance"),
        "due": b.get("due", ""),
        "effort": b.get("estimated-effort", ""),
        "file": b["_file"],
        "blocked": b["_blocked"],
        "unmet": b["_unmet"],
        "unlocks": b["_unlocks"],
        "lead_urgent": b["_lead_urgent"],
        "actionable": b["_actionable"],
        "score_additive": b["_score_additive"],
        "score_wsjf": b["_score_wsjf"],
        "rank_additive": b["_rank_additive"],
        "rank_wsjf": b["_rank_wsjf"],
        "lane": b["_lane"].value,
        "move": b["_move"].value,
        "leverage": b["_leverage"],
        "why": b["_why"],
        "schema_version": SCHEMA_VERSION,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Rank the ready queue (dependency-aware).")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--actionable", action="store_true", help="only auto-actionable briefs")
    ap.add_argument("--context", default="", help="execution context, e.g. 'cloud'")
    ap.add_argument(
        "--scorer",
        choices=[s.value for s in Scorer],
        help="override tasks.toml [priority] scorer for this call",
    )
    a = ap.parse_args(argv)

    root = require_queue(resolve_root())
    items = rank(root, context=a.context, scorer=Scorer(a.scorer) if a.scorer else None)
    if a.actionable:
        items = [b for b in items if b["_actionable"]]
    if a.top:
        items = items[: a.top]

    if a.json:
        print(json.dumps([_json_row(b) for b in items], indent=2))
        return
    if not items:
        print("No briefs to show (try without --actionable, or add some to ready/).")
        return
    print(f"{'score':>7}  {'lane':<13} {'move':<8} {'P':<2} {'due':<10} {'eff':<3} id")
    for b in items:
        print(_row(b))


if __name__ == "__main__":
    main()  # pragma: no cover

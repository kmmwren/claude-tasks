#!/usr/bin/env python3
"""Rank a scope's ready briefs against the taste profile, with a one-line reason each.

    rank.py --scope all --top 10
    rank.py --scope tag:fdo --json
    rank.py --scope project:panaseer-lite
    rank.py --scope session:<id>          # the queue bound to that session

prioritise.py is the filter: blocked briefs drop out and its order picks the top N candidates, whose
due and unlock facts go to the judge as features. The judge ranks them against
profile/priorities.md; any failure (no profile, CLI error, invalid output) falls back to filter
order and says why. This is for lists a person reads; automations keep using prioritise.py.
"""
from __future__ import annotations

import abc
import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _concurrency import atomic_write  # noqa: E402
from _paths import ENV_VAR, SESSION_ENV, require_queue, resolve_root, session_root  # noqa: E402
from config import load_config  # noqa: E402
from deps import fm, parse_list  # noqa: E402
from judge import (  # noqa: E402
    Candidate,
    ClaudeBackend,
    DirectLaunch,
    JudgeError,
    Launch,
    PredictFn,
    SetwiseJudge,
    Verdict,
    WrapperLaunch,
)
from picks import PickLog, Shown, ShownItem, state_dir  # noqa: E402
from taste import Profile, ProfileError, load_profile  # noqa: E402

DEFAULTS: dict[str, Any] = {
    "candidates": 15, "top": 10, "model": "", "timeout": 240, "setting_sources": "project",
    "launcher": "~/.claude-switch/claude-any.sh",
    "local_ok": "read-only-research:predictor head setwise judge, tools off, latency-sensitive",
    "excerpt_chars": 600, "low_confidence": 0.75,
}
SCRIPTS = pathlib.Path(__file__).parent


def settings(root: pathlib.Path) -> dict[str, Any]:
    return {**DEFAULTS, **load_config(root).get("rank", {})}


@dataclasses.dataclass(frozen=True)
class QueueBrief:
    """One filtered brief: prioritise.py's row plus what the scope and the judge read from its file."""
    id: str
    title: str
    facts: str
    tags: frozenset[str]
    project: str
    text: str

    def candidate(self, excerpt_chars: int) -> Candidate:
        body = self.text.split("\n---", 1)[-1].strip()
        return Candidate(self.id, self.title, self.facts, body[:excerpt_chars])

    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]


class Scope(abc.ABC):
    name = ""

    def __init__(self, value: str) -> None:
        self.value = value

    @property
    def key(self) -> str:
        return f"{self.name}:{self.value}" if self.value else self.name

    def root(self, default: pathlib.Path) -> pathlib.Path:
        return default

    @abc.abstractmethod
    def admits(self, brief: QueueBrief) -> bool: ...


class AllScope(Scope):
    name = "all"

    def admits(self, brief: QueueBrief) -> bool:
        return True


class TagScope(Scope):
    name = "tag"

    def admits(self, brief: QueueBrief) -> bool:
        return self.value in brief.tags


class ProjectScope(Scope):
    name = "project"

    def admits(self, brief: QueueBrief) -> bool:
        return brief.project == self.value or f"project:{self.value}" in brief.tags


class SessionScope(AllScope):
    """Everything ready in the queue a session is bound to (its `sessions/<id>.toml`)."""
    name = "session"

    def root(self, default: pathlib.Path) -> pathlib.Path:
        bound = session_root({**os.environ, SESSION_ENV: self.value})
        if bound is None:
            raise SystemExit(f"session {self.value} is not bound to a queue")
        return bound


SCOPES: dict[str, type[Scope]] = {cls.name: cls for cls in (AllScope, TagScope, ProjectScope, SessionScope)}


def parse_scope(text: str) -> Scope:
    name, _, value = text.partition(":")
    if name not in SCOPES or (name != "all" and not value):
        raise SystemExit(f"--scope must be all, tag:<t>, project:<slug> or session:<id>, not {text!r}")
    return SCOPES[name](value)


FilterFn = Callable[[pathlib.Path], list[dict[str, Any]]]


def prioritise_rows(root: pathlib.Path) -> list[dict[str, Any]]:
    """prioritise.py's --json order, through its CLI so its scorer stays its own business."""
    proc = subprocess.run([sys.executable, str(SCRIPTS / "prioritise.py"), "--json"], capture_output=True,
                          text=True, check=False, env={**os.environ, ENV_VAR: str(root)})
    try:
        rows: Any = json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        raise SystemExit(f"prioritise.py --json failed (exit {proc.returncode}): {proc.stderr.strip()[-300:]}")
    return rows


def facts(row: Mapping[str, Any]) -> str:
    return (f"importance {row.get('importance') or '?'}; due {row.get('due') or 'none'}; "
            f"effort {row.get('effort') or '?'}; unlocks {len(row.get('unlocks') or [])} briefs")


def load_brief(root: pathlib.Path, row: Mapping[str, Any]) -> QueueBrief | None:
    """None when another session moved the brief out of ready/ after the filter listed it."""
    try:
        text = (root / "ready" / str(row.get("file") or f"{row['id']}.md")).read_text()
    except FileNotFoundError:
        return None
    meta = fm(text)
    return QueueBrief(str(row["id"]), str(row.get("title") or ""), facts(row),
                      frozenset(parse_list(meta.get("tags", ""))), meta.get("project", ""), text)


def candidates(root: pathlib.Path, scope: Scope, limit: int, filter_fn: FilterFn) -> list[QueueBrief]:
    briefs = (load_brief(root, row) for row in filter_fn(root) if not row.get("blocked"))
    return [b for b in briefs if b is not None and scope.admits(b)][:limit]


@dataclasses.dataclass(frozen=True)
class RankedItem:
    id: str
    title: str
    reason: str
    confidence: float | None
    filter_position: int


@dataclasses.dataclass(frozen=True)
class Ranking:
    scope: str
    source: str
    profile_version: str
    draft: bool
    agreement: float | None
    items: tuple[RankedItem, ...]
    total: int
    fallback_reason: str = ""
    raw: tuple[str, ...] = ()

    def how(self) -> str:
        if self.source != "judge":
            return f"filter order: {self.fallback_reason}"
        return f"judge, agreement {self.agreement:.2f}" if self.agreement is not None else "judge, one pass"

    def text(self, low_confidence: float) -> str:
        head = (f"Top {len(self.items)} of {self.total} ({self.scope}; profile {self.profile_version}"
                f"{' DRAFT' if self.draft else ''}; {self.how()})")
        return "\n".join([head] + [line for n, item in enumerate(self.items, 1)
                                   for line in _item_lines(n, item, low_confidence)])

    def json(self) -> str:
        return json.dumps({k: v for k, v in dataclasses.asdict(self).items() if k != "raw"}, indent=2)


def _item_lines(n: int, item: RankedItem, low_confidence: float) -> list[str]:
    shaky = item.confidence is not None and item.confidence < low_confidence
    return [f"{n:>2}. {item.id}  {item.title}", f"    {item.reason}{'  (low confidence)' if shaky else ''}"]


class VerdictCache:
    """Judge results keyed by brief contents, profile version and scope: an unchanged list is free."""

    def __init__(self, root: pathlib.Path) -> None:
        self.dir = state_dir(root) / "cache"

    @staticmethod
    def key(scope: Scope, profile: Profile, briefs: Sequence[QueueBrief], model: str) -> str:
        parts = [scope.key, profile.version, model, *(f"{b.id}={b.content_hash()}" for b in briefs)]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:24]

    def get(self, key: str) -> Verdict | None:
        path = self.dir / f"{key}.json"
        if not path.is_file():
            return None
        d = json.loads(path.read_text())
        return Verdict(tuple(d["order"]), d["reasons"], d["confidence"], d["agreement"], tuple(d["raw"]))

    def put(self, key: str, verdict: Verdict) -> None:
        atomic_write(self.dir / f"{key}.json", json.dumps(dataclasses.asdict(verdict)))


def judged(briefs: Sequence[QueueBrief], verdict: Verdict, top: int) -> tuple[RankedItem, ...]:
    by_id = {b.id: (n, b) for n, b in enumerate(briefs)}
    return tuple(RankedItem(i, by_id[i][1].title, verdict.reasons[i], verdict.confidence[i], by_id[i][0] + 1)
                 for i in verdict.order[:top])


def filtered(briefs: Sequence[QueueBrief], top: int) -> tuple[RankedItem, ...]:
    return tuple(RankedItem(b.id, b.title, b.facts, None, n + 1) for n, b in enumerate(briefs[:top]))


@dataclasses.dataclass
class Ranker:
    root: pathlib.Path
    predict: PredictFn
    config: dict[str, Any] = dataclasses.field(default_factory=lambda: dict(DEFAULTS))

    def rank(self, scope: Scope, briefs: Sequence[QueueBrief], top: int) -> Ranking:
        try:
            profile = load_profile(self.root)
        except ProfileError as exc:
            return self._fallback(scope, briefs, top, None, f"profile unreadable: {exc}")
        if profile is None:
            return self._fallback(scope, briefs, top, None, "no profile/priorities.md (run seed_profile.py)")
        if len(briefs) < 2:
            return self._fallback(scope, briefs, top, profile, "fewer than two candidates")
        try:
            verdict = self._verdict(scope, profile, briefs)
        except JudgeError as exc:
            return self._fallback(scope, briefs, top, profile, str(exc))
        return Ranking(scope.key, "judge", profile.version, profile.draft, verdict.agreement,
                       judged(briefs, verdict, top), len(briefs), raw=verdict.raw)

    def _verdict(self, scope: Scope, profile: Profile, briefs: Sequence[QueueBrief]) -> Verdict:
        cache = VerdictCache(self.root)
        key = cache.key(scope, profile, briefs, str(self.config["model"]))
        hit = cache.get(key)
        if hit is not None:
            return hit
        excerpt = int(self.config["excerpt_chars"])
        verdict = SetwiseJudge(self.predict).judge(profile.rubric(), [b.candidate(excerpt) for b in briefs])
        cache.put(key, verdict)
        return verdict

    @staticmethod
    def _fallback(scope: Scope, briefs: Sequence[QueueBrief], top: int, profile: Profile | None,
                  reason: str) -> Ranking:
        version, draft = (profile.version, profile.draft) if profile else ("none", False)
        return Ranking(scope.key, "filter", version, draft, None, filtered(briefs, top), len(briefs), reason)


def shown_event(ranking: Ranking, profile: Profile | None, briefs: Mapping[str, QueueBrief],
                filter_order: Sequence[str]) -> Shown:
    def goals(brief_id: str) -> tuple[str, ...]:
        brief = briefs[brief_id]
        return profile.matched_goals(brief.tags, brief.text) if profile else ()
    items = tuple(ShownItem(item.id, goals(item.id)) for item in ranking.items)
    return Shown(ranking.scope, ranking.profile_version, ranking.source, items, tuple(filter_order), ranking.raw)


def backend(root: pathlib.Path, config: Mapping[str, Any]) -> ClaudeBackend:
    neutral = state_dir(root) / "judge"
    neutral.mkdir(parents=True, exist_ok=True)
    return ClaudeBackend(str(config["model"]), float(config["timeout"]), str(config["setting_sources"]), neutral,
                         launch=launcher(config))


def launcher(config: Mapping[str, Any]) -> Launch:
    """The configured wrapper when it exists here, else plain claude so the ranker still works elsewhere."""
    script = pathlib.Path(str(config["launcher"])).expanduser()
    return WrapperLaunch(str(script), str(config["local_ok"])) if config["launcher"] and script.is_file() else DirectLaunch()


def run(root: pathlib.Path, scope: Scope, top: int, log: PickLog | None, predict: PredictFn | None = None,
        filter_fn: FilterFn = prioritise_rows) -> Ranking:
    config = settings(root)
    briefs = candidates(root, scope, int(config["candidates"]), filter_fn)
    ranking = Ranker(root, predict or backend(root, config), config).rank(scope, briefs, top)
    if log is not None:
        log.append(shown_event(ranking, readable_profile(root), {b.id: b for b in briefs}, [b.id for b in briefs]))
    return ranking


def readable_profile(root: pathlib.Path) -> Profile | None:
    try:
        return load_profile(root)
    except ProfileError:
        return None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Rank a scope's ready briefs against the taste profile.")
    ap.add_argument("--scope", default="all", help="all | tag:<t> | project:<slug> | session:<id>")
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-log", action="store_true", help="do not record this list in the pick log")
    a = ap.parse_args(argv)
    scope = parse_scope(a.scope)
    root = require_queue(scope.root(pathlib.Path(a.root) if a.root else resolve_root()))
    config = settings(root)
    ranking = run(root, scope, a.top or int(config["top"]), None if a.no_log else PickLog(root))
    print(ranking.json() if a.json else ranking.text(float(config["low_confidence"])))


if __name__ == "__main__":
    main()  # pragma: no cover

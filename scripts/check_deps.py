#!/usr/bin/env python3
"""
check_deps.py — lint a queue's dependency graph. Exit 1 when anything is wrong.

Finds, in one pass:
  cycle              briefs that wait on each other, directly or through a chain
  dangling           a `blocked_by` / `depends:` id that names no brief, or a `<queue>:<id>` naming an unknown queue or brief
  in-progress-unmet  a brief being worked whose dependencies are not all done (dates included)
  bad-date           a `date:` blocker that is not date:YYYY-MM-DD, so it would never release
  blocked-no-reason  a brief in blocked/ with an empty `blocked_by`
  blocked-no-probe   an `external:` blocker with no `blocked_probe: N: <command>` line (or `human`)
  releasable         a blocked brief whose blockers are all done (run unblock.py)
  undeclared         a ready, in-progress or parked brief with no `blocked_by:` line (`[]` declares "depends on nothing")
  prose-dependency   a ready or in-progress brief that says in prose it waits on a PR, brief or approval
                     while blocked_by is empty
  approved-needs-input  an autonomy: needs-input brief whose cited decision or prose already approves
                     the work
  unknown-milestone  a `milestone:` not in the project's milestones file (only checked when
                     tasks.toml sets `milestones`)
  milestones-file    tasks.toml names a milestones file that cannot be read
  facts-orphan       a facts/<id>.toml with no brief of that id
  facts-brief-mismatch  a facts file whose `brief` field is not its file stem
  facts-unreadable   a facts file that is not valid TOML
  unshipped          a brief in a queue inside another repo that has sat untracked or modified there for
                     over 6 hours: nothing landed it on the host's wip branch (scripts/host_land.py <queue>)

  python scripts/check_deps.py [--json]
"""

import argparse
import dataclasses
import datetime
import json
import pathlib
import sys
import time
import tomllib
from collections.abc import Sequence

from _paths import is_brief, known_roots, require_queue, resolve_root
from deps import (
    Brief,
    awaits_release,
    date_of,
    date_reached,
    dependency_edges,
    done_ids,
    load_queue,
    ref_problem,
)
from facts import declared_brief, facts_files
from host_land import HostQueue, LandError
from milestones import MilestonesError, milestone_titles
from prose_dependency import decisions_dir, prose_problems

UNSHIPPED_AFTER = 6 * 3600.0  # seconds a brief may wait in a host checkout before it is a finding


@dataclasses.dataclass(frozen=True)
class Problem:
    kind: str
    brief: str
    detail: str


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Each distinct cycle once, rotated to start at its smallest id (depth-first search)."""
    cycles: dict[tuple[str, ...], list[str]] = {}
    state: dict[str, int] = {}  # 1 = on the current path, 2 = fully explored
    path: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        path.append(node)
        for nxt in sorted(graph.get(node, ())):
            if state.get(nxt) == 1:
                loop = path[path.index(nxt) :]
                i = loop.index(min(loop))
                rotated = loop[i:] + loop[:i]
                cycles[tuple(rotated)] = rotated
            elif nxt not in state:
                visit(nxt)
        path.pop()
        state[node] = 2

    for node in sorted(graph):
        if node not in state:
            visit(node)
    return list(cycles.values())


def check(root: pathlib.Path, roots: Sequence[pathlib.Path] | None = None) -> list[Problem]:
    """Every defect in `root`'s graph; `roots` are the queues a `<queue>:<id>` blocker may name."""
    roots = known_roots(root) if roots is None else roots
    queue = load_queue(root)
    known = {b.id for b in queue}
    done = done_ids(root, roots)
    problems: list[Problem] = []
    graph = {b.id: dependency_edges(b, known) & known for b in queue}
    for cycle in find_cycles(graph):
        problems.append(Problem("cycle", cycle[0], " -> ".join([*cycle, cycle[0]])))
    directory = decisions_dir()
    for b in queue:
        problems += _brief_problems(b, known, done, roots)
        problems += [Problem(f.kind, b.id, f.detail) for f in prose_problems(b, known, directory)]
    problems += _milestone_problems(root, queue)
    problems += _unshipped_problems(root)
    for path in facts_files(root):
        problems += _facts_problems(path, known)
    return problems


def _unshipped_problems(root: pathlib.Path) -> list[Problem]:
    """One finding per brief of a nested queue left in the host checkout past UNSHIPPED_AFTER.

    The detail never carries the age or the state, so the daily alert keeps one stable key per brief.
    """
    host = HostQueue.of(root)
    if host is None:
        return []
    try:
        waiting = host.pending()
    except LandError:
        return []
    cutoff = time.time() - UNSHIPPED_AFTER
    return [
        Problem("unshipped", pathlib.Path(rel).stem, f"not landed from {host.top.name}")
        for rel in waiting
        if is_brief(rel) and _modified_before(host.top / rel, cutoff)
    ]


def _modified_before(path: pathlib.Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        return False  # deleted: a mirrored deletion, not a brief waiting


def _facts_problems(path: pathlib.Path, known: set[str]) -> list[Problem]:
    """An orphan facts file, and one whose `brief` is not its stem or which cannot be read."""
    stem = path.stem
    out = (
        []
        if stem in known
        else [Problem("facts-orphan", stem, f"{path.name} has no brief {stem!r}")]
    )
    try:
        declared = declared_brief(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return [*out, Problem("facts-unreadable", stem, f"{path.name}: {e}")]
    if declared != stem:
        out.append(Problem("facts-brief-mismatch", stem, f"`brief` is {declared!r}, not {stem!r}"))
    return out


def _brief_problems(
    b: Brief, known: set[str], done: set[str], roots: Sequence[pathlib.Path]
) -> list[Problem]:
    out = [Problem("dangling", b.id, d) for d in sorted(b.local_ids - known)]
    out += [Problem("dangling", b.id, p) for ref in b.remote_refs if (p := ref_problem(ref, roots))]
    out += [Problem("bad-date", b.id, d) for d in b.date_blockers if date_of(d) is None]
    if b.status in ("ready", "in-progress", "parked") and not b.declares_dependencies:
        out.append(
            Problem(
                "undeclared",
                b.id,
                f"no blocked_by; run transition.py declare {b.id} --no-deps, or --blocked-by <ids> (a waiting ready brief: move it to blocked)",
            )
        )
    if b.status == "in-progress":
        waiting = sorted(
            d for d in dependency_edges(b, known) | set(b.external_blockers) if d not in done
        )
        waiting += sorted(d for d in b.date_blockers if not date_reached(d, datetime.date.today()))
        if waiting:
            out.append(Problem("in-progress-unmet", b.id, ", ".join(waiting)))
    if b.status == "blocked":
        if not (b.blocked_by or b.declared_ids or b.legacy_tokens):
            out.append(Problem("blocked-no-reason", b.id, "blocked/ brief has no blocked_by"))
        elif awaits_release(b, done):
            out.append(
                Problem(
                    "releasable",
                    b.id,
                    ", ".join(b.blocked_by or sorted(b.declared_ids | b.legacy_tokens)),
                )
            )
        out += [
            Problem(
                "blocked-no-probe",
                b.id,
                f"blocker {n} ({b.blocked_by[n - 1]}) has no `blocked_probe: {n}: <command>` line",
            )
            for n in b.probe_gaps
        ]
    return out


def _milestone_problems(root: pathlib.Path, queue: list[Brief]) -> list[Problem]:
    try:
        titles = milestone_titles(root)
    except MilestonesError as e:
        return [Problem("milestones-file", "-", str(e))]
    if titles is None:
        return []
    return [
        Problem("unknown-milestone", b.id, b.milestone)
        for b in queue
        if b.milestone and b.milestone not in titles
    ]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Check a queue's dependency graph.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = require_queue(pathlib.Path(a.root) if a.root else resolve_root())
    problems = check(root)
    if a.json:
        print(json.dumps([dataclasses.asdict(p) for p in problems], indent=2))
    elif problems:
        for p in problems:
            print(f"{p.kind:<18} {p.brief}: {p.detail}")
    else:
        print("dependency graph OK")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()  # pragma: no cover

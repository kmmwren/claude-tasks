#!/usr/bin/env python3
"""
check_deps.py — lint a queue's dependency graph. Exit 1 when anything is wrong.

Finds, in one pass:
  cycle              briefs that wait on each other, directly or through a chain
  dangling           a `blocked_by` / `depends:` id that names no brief
  in-progress-unmet  a brief being worked whose dependencies are not all done
  blocked-no-reason  a brief in blocked/ with an empty `blocked_by`
  releasable         a blocked brief whose blockers are all done (run unblock.py)
  unknown-milestone  a `milestone:` not in the project's milestones file (only checked when
                     tasks.toml sets `milestones`)
  milestones-file    tasks.toml names a milestones file that cannot be read

  python scripts/check_deps.py [--json]
"""
import argparse
import dataclasses
import json
import pathlib
import sys

from _paths import require_queue, resolve_root
from deps import Brief, dependency_edges, load_queue, unmet
from milestones import MilestonesError, milestone_titles


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
                loop = path[path.index(nxt):]
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


def check(root: pathlib.Path) -> list[Problem]:
    queue = load_queue(root)
    known = {b.id for b in queue}
    done = {b.id for b in queue if b.status == "done"}
    problems: list[Problem] = []
    graph = {b.id: dependency_edges(b, known) & known for b in queue}
    for cycle in find_cycles(graph):
        problems.append(Problem("cycle", cycle[0], " -> ".join([*cycle, cycle[0]])))
    for b in queue:
        problems += _brief_problems(b, known, done)
    problems += _milestone_problems(root, queue)
    return problems


def _brief_problems(b: Brief, known: set[str], done: set[str]) -> list[Problem]:
    out = [Problem("dangling", b.id, d) for d in sorted(b.declared_ids - known)]
    if b.status == "in-progress":
        waiting = sorted(d for d in dependency_edges(b, known) | set(b.external_blockers)
                         if d not in done)
        if waiting:
            out.append(Problem("in-progress-unmet", b.id, ", ".join(waiting)))
    if b.status == "blocked":
        if not b.blocked_by:
            out.append(Problem("blocked-no-reason", b.id, "blocked/ brief has no blocked_by"))
        elif not unmet(b, done):
            out.append(Problem("releasable", b.id, ", ".join(b.blocked_by)))
    return out


def _milestone_problems(root: pathlib.Path, queue: list[Brief]) -> list[Problem]:
    try:
        titles = milestone_titles(root)
    except MilestonesError as e:
        return [Problem("milestones-file", "-", str(e))]
    if titles is None:
        return []
    return [Problem("unknown-milestone", b.id, b.milestone)
            for b in queue if b.milestone and b.milestone not in titles]


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

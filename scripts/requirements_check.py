#!/usr/bin/env python3
"""
requirements_check.py - fail when a queue's briefs and its requirements register disagree.

  python scripts/requirements_check.py <queue-dir>

The register is `<queue-dir>/requirements.md`, or the queue's parent directory's when the queue
is a project's `.tasks/`. Exit 0 when every check passes, 1 with one line per problem, 2 when
there is no queue or register to check. It fails when:

  * a requirement id has no brief citing it in `requirements: [ids]` (withdrawn ones are exempt);
  * a brief cites an id the register does not contain;
  * a table register's Briefs column and the briefs' `requirements:` fields disagree;
  * a brief that cites requirements has no checkbox criteria;
  * a brief in done/ has an unticked criterion.

Stdlib only; vendor it with `_paths.py` and `requirements_register.py` to run it in CI.
"""
import pathlib
import re
import sys
from dataclasses import dataclass, field

from _paths import TASK_DIRS, is_queue, strip_comment
from requirements_register import ID_PATTERN, parse_register, register_path

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_ID = re.compile(ID_PATTERN)
_CRITERION = re.compile(r"^\s*[-*] \[( |x|X)\]", re.MULTILINE)
_CRITERIA_SECTION = re.compile(r"^##\s+Success criteria\s*\n(.*?)(?=^##\s|\Z)", re.DOTALL | re.MULTILINE)


@dataclass
class Brief:
    path: pathlib.Path
    folder: str
    cited: list[str] = field(default_factory=list)
    criteria: int = 0
    unticked: int = 0

    @property
    def name(self) -> str:
        return self.path.stem


def cited_ids(header: str) -> list[str]:
    """Ids in a `requirements:` field, written inline `[A-R001, A-R002]` or as a `- id` list."""
    lines = header.splitlines()
    for i, line in enumerate(lines):
        key, _, value = line.partition(":")
        if key.strip() != "requirements":
            continue
        value = strip_comment(value)
        if value:
            return _ID.findall(value)
        found: list[str] = []
        for follower in lines[i + 1:]:
            item = re.match(r"^\s+-\s*(.*)$", follower)
            if not item:
                break
            found += _ID.findall(strip_comment(item[1]))
        return found
    return []


def load_briefs(queue: pathlib.Path) -> list[Brief]:
    """Every brief in the lifecycle folders. A brief without `---` fences is read whole."""
    briefs: list[Brief] = []
    for folder in TASK_DIRS:
        for path in sorted((queue / folder).glob("*.md")):
            text = path.read_text()
            match = _FRONTMATTER.match(text)
            header, body = (match[1], match[2]) if match else (text, text)
            section = _CRITERIA_SECTION.search(body)
            marks = _CRITERION.findall(section[1] if section else body)
            briefs.append(Brief(
                path, folder, cited_ids(header),
                criteria=len(marks), unticked=_CRITERION.findall(body).count(" "),
            ))
    return briefs


def check(queue: str | pathlib.Path) -> list[str]:
    queue = pathlib.Path(queue).expanduser()
    requirements = parse_register(register_path(queue).read_text())
    known = {r.id for r in requirements}
    briefs = load_briefs(queue)
    claimed: dict[str, set[str]] = {}
    for b in briefs:
        for i in b.cited:
            claimed.setdefault(i, set()).add(b.name)
    problems: list[str] = []
    for r in requirements:
        if not r.withdrawn and not claimed.get(r.id):
            problems.append(f"{r.id} has no brief: add `requirements: [{r.id}]` to a brief with criteria")
        if r.briefs is None:
            continue
        have = claimed.get(r.id, set())
        problems += [f"{r.id} lists {n} but that brief does not name it" for n in sorted(set(r.briefs) - have)]
        problems += [f"{n} names {r.id} but the register row omits it" for n in sorted(have - set(r.briefs))]
    for b in briefs:
        problems += [f"{b.name} names unknown requirement {i}" for i in b.cited if i not in known]
        if b.cited and not b.criteria:
            problems.append(f"{b.name} cites requirements but has no `- [ ]` criteria")
        if b.folder == "done" and b.unticked:
            problems.append(f"{b.name} is in done/ with {b.unticked} unticked criteria")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: requirements_check.py <queue-dir>", file=sys.stderr)
        return 2
    queue = pathlib.Path(args[0]).expanduser()
    if not is_queue(queue) or not register_path(queue).is_file():
        print(f"{queue} is not a queue with a requirements.md register", file=sys.stderr)
        return 2
    problems = check(queue)
    for p in problems:
        print(p)
    total = len(parse_register(register_path(queue).read_text()))
    if problems:
        print(f"requirements check FAILED: {len(problems)} problem(s) across {total} requirement(s)")
        return 1
    print(f"requirements check ok: {total} requirement(s) covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

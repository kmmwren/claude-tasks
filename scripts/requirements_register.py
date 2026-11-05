#!/usr/bin/env python3
"""
requirements_register.py - the requirements register that sits next to a queue.

A large project's brain dump is split into atomic requirements, each with a permanent id
(`<PREFIX>-R001`), the user's verbatim words and the timestamp of the message they came from.
This module owns the register's format and the one idempotent write path, so the
`requirements-extract` skill never edits ids by hand:

  python scripts/requirements_register.py append <queue> --json items.json
  python scripts/requirements_register.py append <queue> --json -  < items.json

`items.json` is a list of {"kind", "title", "quote", "source"}. Ids continue from the highest
existing one and are never reused or renumbered; an item whose quote and source are already
registered is skipped, so re-running extraction on the same messages changes nothing.

Stdlib only, so a repo can vendor it with `_paths.py` and `requirements_check.py`.
"""
import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass

REGISTER_FILE = "requirements.md"
KINDS = (
    "capability",
    "constraint",
    "quality",
    "user",
    "flow",
    "integration",
    "data-rule",
    "hosting-rule",
    "test-rule",
    "never",
)
MAX_QUOTE = 240
ID_PATTERN = r"[A-Z][A-Z0-9]*-R\d+"
TABLE_STATUS = "UNTRACKED"

_HEADER = re.compile(rf"^## ({ID_PATTERN}) \[([a-z-]+)\] (.+)$")
# A table register row: | id | when | her words | ...classes... | brief ids |
_ROW = re.compile(rf"^\|\s*({ID_PATTERN})\s*\|.*\|\s*$")
_PREFIX = re.compile(r"prefix:\s*([A-Z][A-Z0-9]*)")
_FIELD = re.compile(r"^- (quote|source|withdrawn): ?(.*)$")


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str
    title: str
    quote: str
    source: str
    withdrawn: str = ""
    briefs: tuple[str, ...] | None = None  # table registers list their briefs; entry registers do not

    @property
    def number(self) -> int:
        return int(self.id.rsplit("-R", 1)[1])


def stub(name: str, prefix: str) -> str:
    """An empty register: the file `tasks-init` scaffolds."""
    return (
        f"# Requirements register: {name}\n\n"
        f"<!-- requirements-register v1; prefix: {prefix}; ids are permanent, never renumber. "
        "Add entries with scripts/requirements_register.py append. -->\n"
    )


def register_path(queue: str | pathlib.Path) -> pathlib.Path:
    """The register beside the queue (`<queue>/requirements.md`) or, failing that, the queue's parent."""
    queue = pathlib.Path(queue).expanduser()
    inside = queue / REGISTER_FILE
    beside = queue.resolve().parent / REGISTER_FILE
    return beside if not inside.is_file() and beside.is_file() else inside


def parse_prefix(text: str) -> str | None:
    m = _PREFIX.search(text)
    return m.group(1) if m else None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_requirement(line: str) -> Requirement | None:
    cells = _cells(line)
    if len(cells) < 4:
        return None
    quote = cells[2].strip('"')
    briefs = tuple(b.strip() for b in cells[-1].split(",") if b.strip())
    return Requirement(cells[0], cells[-2] if len(cells) > 4 else "", quote, quote, cells[1], briefs=briefs)


def parse_register(text: str) -> list[Requirement]:
    """Every requirement in register order, from `## ID [kind] title` entries or table rows."""
    entries: list[dict[str, str]] = []
    rows: list[Requirement] = []
    for line in text.splitlines():
        if _ROW.match(line):
            row = _table_requirement(line)
            if row:
                rows.append(row)
            continue
        header = _HEADER.match(line)
        if header:
            entries.append({"id": header[1], "kind": header[2], "title": header[3]})
            continue
        field = _FIELD.match(line)
        if field and entries:
            value = field[2].strip()
            if field[1] == "quote" and len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            entries[-1][field[1]] = value
    return rows + [
        Requirement(
            e["id"], e["kind"], e["title"], e.get("quote", ""), e.get("source", ""),
            e.get("withdrawn", ""),
        )
        for e in entries
    ]


def _normalise(quote: str) -> str:
    return " ".join(quote.lower().split())


def _clean_item(item: dict[str, str]) -> dict[str, str]:
    missing = [k for k in ("kind", "title", "quote", "source") if not str(item.get(k, "")).strip()]
    if missing:
        raise SystemExit(f"requirement item is missing {', '.join(missing)}: {item!r}")
    kind = item["kind"].strip()
    if kind not in KINDS:
        raise SystemExit(f"unknown kind {kind!r}; use one of {', '.join(KINDS)}")
    quote = " ".join(item["quote"].split())
    if len(quote) > MAX_QUOTE:
        raise SystemExit(f"quote over {MAX_QUOTE} characters; quote the shortest verbatim span: {quote[:60]!r}")
    return {
        "kind": kind,
        "title": " ".join(item["title"].split()),
        "quote": quote,
        "source": item["source"].strip(),
    }


def _entry(req_id: str, item: dict[str, str]) -> str:
    return (
        f"\n## {req_id} [{item['kind']}] {item['title']}\n"
        f'- quote: "{item["quote"]}"\n'
        f"- source: {item['source']}\n"
    )


def _table_row(req_id: str, item: dict[str, str], columns: int) -> str:
    cells = [req_id, item["source"], f'"{item["quote"]}"']
    cells += [TABLE_STATUS] * (columns - 4) + [""]
    return "| " + " | ".join(cells) + " |"


def _table_prefix(requirements: list[Requirement]) -> str | None:
    prefixes = [r.id.rsplit("-R", 1)[0] for r in requirements]
    return max(set(prefixes), key=prefixes.count) if prefixes else None


def append_items(
    queue: str | pathlib.Path, items: list[dict[str, str]], prefix: str | None = None
) -> tuple[list[str], list[str]]:
    """Add new items, returning (new ids, ids of already-registered duplicates).

    An entry register gets a `## ID [kind] title` block per item. A table register gets a row
    shaped like its existing ones, with no brief yet, so the coverage check fails until one is added.
    """
    path = register_path(queue)
    if not path.is_file():
        raise SystemExit(f"No register at {path}. Run /tasks-init, or create it with a prefix line.")
    text = path.read_text()
    existing = parse_register(text)
    lines = text.splitlines()
    last_row = max((i for i, ln in enumerate(lines) if _ROW.match(ln)), default=None)
    prefix = parse_prefix(text) or _table_prefix(existing) or prefix
    if not prefix:
        raise SystemExit(f"{path} has no `prefix: XXX` header and none was given.")
    columns = len(_cells(lines[last_row])) if last_row is not None else 0
    # A table row paraphrases her words, so only the quote identifies a duplicate there.
    by_quote = last_row is not None
    seen = {(_normalise(r.quote), "" if by_quote else r.source): r.id for r in existing}
    next_number = max((r.number for r in existing), default=0) + 1
    added: list[str] = []
    skipped: list[str] = []
    new_lines: list[str] = []
    for raw in items:
        item = _clean_item(raw)
        key = (_normalise(item["quote"]), "" if by_quote else item["source"])
        if key in seen:
            skipped.append(seen[key])
            continue
        req_id = f"{prefix}-R{next_number:03d}"
        next_number += 1
        seen[key] = req_id
        new_lines.append(_table_row(req_id, item, columns) if by_quote else _entry(req_id, item))
        added.append(req_id)
    if added and last_row is not None:
        lines[last_row + 1:last_row + 1] = new_lines
        path.write_text("\n".join(lines) + "\n")
    elif added:
        path.write_text((text if text.endswith("\n") else text + "\n") + "".join(new_lines))
    return added, skipped


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Maintain a queue's requirements register.")
    sub = ap.add_subparsers(dest="command", required=True)
    add = sub.add_parser("append", help="append new requirements, skipping ones already registered")
    add.add_argument("queue", help="queue directory holding requirements.md")
    add.add_argument("--json", default="-", help="items file, or - for stdin")
    add.add_argument("--prefix", default=None, help="id prefix when the register header has none")
    a = ap.parse_args(argv)
    raw = sys.stdin.read() if a.json == "-" else pathlib.Path(a.json).read_text()
    items = json.loads(raw)
    if not isinstance(items, list):
        raise SystemExit("items JSON must be a list of objects")
    added, skipped = append_items(a.queue, items, a.prefix)
    print(f"added {len(added)}: {', '.join(added) or '-'}")
    print(f"already registered {len(skipped)}: {', '.join(skipped) or '-'}")


if __name__ == "__main__":
    main(sys.argv[1:])  # pragma: no cover

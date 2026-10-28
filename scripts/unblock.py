#!/usr/bin/env python3
"""
unblock.py — return blocked briefs to ready/ once everything they waited for is done.

A brief in blocked/ lists what it waits for in `blocked_by:`. When every entry is a brief
that is now `done` (and no `external:` entry remains), the brief goes back to ready/ with
`blocked_by` cleared and a log line naming what cleared it. Several becoming runnable at
once are released in priority order (the same score prioritise.py ranks by), so the order
they land in ready/ is the order to work them.

It runs automatically after `transition.py move <id> done`, and from the project
coordinator on every pass, so a brief that was blocked by something completed some other
way (edited by hand, finished in another clone) is still released.

An `external:<reason>` blocker is cleared explicitly, once the real-world thing is true:

  python scripts/unblock.py                      # release everything releasable
  python scripts/unblock.py --dry-run --json     # what would be released, ranked
  python scripts/unblock.py resolve <id> "login granted"
"""
import argparse
import datetime
import json
import pathlib
import re
import sys
from functools import partial

from _concurrency import atomic_write, repo_lock
from _paths import require_queue, resolve_root
from deps import EXTERNAL_PREFIX, Brief, awaits_release, fm, load_queue, unmet
from git_sync import sync_commit
from prioritise import index_queue, score
from transition import _append_log, _set_field, find_brief, move_brief


def releasable(root: pathlib.Path, today: datetime.date | None = None) -> list[Brief]:
    """Blocked briefs whose every blocker is satisfied, highest priority first."""
    today = today or datetime.date.today()
    queue = load_queue(root)
    done = {b.id for b in queue if b.status == "done"}
    _, unlocks = index_queue(root)
    ready = [b for b in queue
             if b.status == "blocked" and awaits_release(b, done)]
    ready.sort(key=lambda b: (-score(b.fields, today, unlocks=unlocks.get(b.id, 0)), b.id))
    return ready


def release_unblocked(root: pathlib.Path, today: str | None = None,
                      dry_run: bool = False) -> list[str]:
    """Move every releasable brief to ready/; return their ids in release (priority) order."""
    day = datetime.date.fromisoformat(today) if today else None
    released: list[str] = []
    for brief in releasable(root, day):
        if not dry_run:
            try:
                move_brief(brief.id, "ready", root, today=today,
                           log=f"unblocked: {', '.join(brief.blocked_by) or 'dependencies'} done",
                           only_if=partial(_still_releasable, root, brief.id))
            except ValueError:
                continue  # another actor released or moved it first
        released.append(brief.id)
    return released


def _still_releasable(root: pathlib.Path, brief_id: str) -> bool:
    """Fresh read, meant to run under the queue lock: still blocked, every wait satisfied."""
    queue = load_queue(root)
    done = {b.id for b in queue if b.status == "done"}
    return any(b.id == brief_id and b.status == "blocked" and awaits_release(b, done)
               for b in queue)


def resolve_external(root: pathlib.Path, brief_id: str, reason: str,
                     today: str | None = None, dry_run: bool = False) -> bool:
    """Drop one `external:` blocker, log why, and release the brief if that was its last wait.

    Returns True when the brief goes back to ready/. With `dry_run` nothing is written and the
    return value says what a real run would do. A brief still waiting on ids stays in blocked/,
    where `releasable` picks it up once they are done.
    """
    path = find_brief(brief_id, root)
    if path is None:
        raise ValueError(f"no brief with id {brief_id!r}")
    item = reason if reason.startswith(EXTERNAL_PREFIX) else EXTERNAL_PREFIX + reason
    day = today or datetime.date.today().isoformat()
    with repo_lock(root) as acquired:
        if not acquired:
            raise RuntimeError(f"timed out waiting for the queue lock; {brief_id!r} unchanged")
        text = path.read_text()
        fields = fm(text)
        current = Brief(brief_id, path.parent.name, fields).blocked_by
        if item not in current:
            raise ValueError(f"{brief_id!r} has no blocker {item!r} (has {current})")
        kept = [b for b in current if b != item]
        remaining = "[" + ", ".join(kept) + "]"
        done = {b.id for b in load_queue(root) if b.status == "done"}
        frees = not kept and not unmet(Brief(brief_id, "blocked", {**fields, "blocked_by": remaining}),
                                       done)
        if dry_run:
            return frees
        header = re.match(r"(---\n)(.*?)(\n---)", text, re.DOTALL)
        assert header is not None  # a brief with blocked_by always has frontmatter
        block = _set_field(header.group(2), "blocked_by", remaining)
        text = text[: header.start(2)] + block + text[header.end(2):]
        atomic_write(path, _append_log(text, day, f"cleared {item}"))
    if not frees:
        return False
    move_brief(brief_id, "ready", root, today=today, log=f"unblocked: {item} cleared")
    return True


def _commit_message(resolved: str | None, released: list[str]) -> str:
    parts = ([f"cleared an external blocker on {resolved}"] if resolved else [])
    parts += [f"released {', '.join(released)}"] if released else []
    return "unblock: " + "; ".join(parts)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Release blocked briefs whose blockers are done.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-sync", action="store_true", help="skip the git commit/push")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("resolve", help="clear an external: blocker, then release what it frees")
    r.add_argument("brief_id")
    r.add_argument("reason")
    a = ap.parse_args(argv)
    root = require_queue(pathlib.Path(a.root) if a.root else resolve_root())

    released: list[str] = []
    if a.cmd == "resolve":
        try:
            if resolve_external(root, a.brief_id, a.reason, dry_run=a.dry_run):
                released.append(a.brief_id)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
    released += release_unblocked(root, dry_run=a.dry_run)
    changed = bool(released) or a.cmd == "resolve"
    if changed and not a.dry_run and not a.no_sync:
        sync_commit(_commit_message(a.brief_id if a.cmd else None, released), root)
    if a.json:
        print(json.dumps({"released": released, "dry_run": a.dry_run}))
        return
    verb = "would release" if a.dry_run else "released"
    print(f"{verb}: {', '.join(released)}" if released else "nothing to release")


if __name__ == "__main__":
    main()  # pragma: no cover

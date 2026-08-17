#!/usr/bin/env python3
"""
transition.py — resolve a brief by fuzzy query and move it between lifecycle states.

The queue's "execute a brief" lifecycle is a fixed sequence of file mutations: set
`status`, bump `updated`, append a dated execution-log line, move the file to the matching
folder, then commit under the shared lock via git_sync. Doing that by hand in every
session is exactly the kind of repetitive, error-prone work to design away — so this
encodes it once, tested, so it can never "fail differently twice".

It is the primitive behind the `action-task` skill, but is reusable by any caller
(handoff, weekly review, a cloud worker).

Stdlib only. Every git write goes through git_sync.sync_commit (the cross-process lock);
never raw git here.

Usage:
  # find which brief a phrase refers to (ranked candidates; JSON for machines)
  python scripts/transition.py resolve "staging vm"
  python scripts/transition.py resolve "staging vm" --json

  # move a brief between states (commits via the lock unless --no-sync)
  python scripts/transition.py move <brief-id> in-progress
  python scripts/transition.py move <brief-id> done   --log "shipped; tests green"
  python scripts/transition.py move <brief-id> parked --log "ESCALATED — needs a login"
"""
import argparse
import datetime
import json
import pathlib
import re
import sys
from typing import Any

from _concurrency import atomic_write, repo_lock
from _paths import resolve_root, strip_comment
from git_sync import sync_commit

# Every lifecycle state == a folder of the same name.
VALID_STATUSES = ("inbox", "ready", "in-progress", "done", "parked")
# Folders that actually hold briefs (inbox = raw pre-briefs, never resolvable as briefs).
BRIEF_DIRS = ("ready", "in-progress", "parked", "done")
# What "action a task" should consider: actionable, not raw and not already finished.
RESOLVE_STATUSES = ("ready", "in-progress", "parked")
# Default execution-log line per target state when the caller gives none.
DEFAULT_LOG = {
    "in-progress": "started",
    "done": "completed",
    "parked": "parked",
    "ready": "moved back to ready",
    "inbox": "returned to inbox",
}


def fm(text: str) -> dict[str, str]:
    """Parse the YAML-ish frontmatter block into a flat dict (same shape as the other scripts)."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    d: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                d[k.strip()] = strip_comment(v)
    return d


def find_brief(brief_id: str, root: pathlib.Path) -> pathlib.Path | None:
    """Locate `<brief_id>.md` across the brief folders (never inbox). None if absent."""
    for d in BRIEF_DIRS:
        p = root / d / f"{brief_id}.md"
        if p.exists():
            return p
    return None


def _tokens(query: str) -> list[str]:
    """Lowercase alphanumeric tokens of length >= 2 (a lone letter would match too much)."""
    return [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) >= 2]


def _score(query: str, brief: dict[str, str]) -> int:
    """Relevance of a brief to a free-text query: exact id is decisive, else weighted token hits.

    Tie-breakers stop a long id that merely *contains* the words from outranking the short,
    specific id the query nearly IS: reward how much of the id the query covers (tightness)
    and a contiguous phrase match in the id or title.
    """
    bid = (brief.get("id") or "").lower()
    score = 1000 if (bid and query.strip().lower() == bid) else 0
    title = (brief.get("title") or "").lower()
    tags = (brief.get("tags") or "").lower()
    domain = (brief.get("domain") or "").lower()
    qtoks = _tokens(query)
    for tok in qtoks:
        if tok in bid:
            score += 100
        if tok in title:
            score += 40
        if tok in tags:
            score += 15
        if tok in domain:
            score += 10
    id_toks = _tokens(bid)
    if qtoks and id_toks:
        matched = sum(1 for t in id_toks if t in qtoks)
        score += 60 * matched // len(id_toks)   # tightness: fraction of the id the query covers
        if "-".join(qtoks) in bid:
            score += 60                          # query phrase is contiguous in the id
    if qtoks and query.strip().lower() in title:
        score += 30                              # query phrase is contiguous in the title
    return score


def resolve_brief(query: str, root: pathlib.Path,
                  statuses: tuple[str, ...] = RESOLVE_STATUSES) -> list[dict[str, Any]]:
    """Rank actionable briefs by relevance to `query`. Best first; non-matches dropped."""
    hits: list[dict[str, Any]] = []
    for d in statuses:
        for f in sorted((root / d).glob("*.md")):
            brief = fm(f.read_text())
            s = _score(query, brief)
            if s > 0:
                hits.append({
                    "id": brief.get("id", f.stem),
                    "title": brief.get("title", ""),
                    "status": d,
                    "score": s,
                    "path": f,
                })
    hits.sort(key=lambda h: (-h["score"], h["id"]))
    return hits


def _update_frontmatter(text: str, new_status: str, today: str,
                        assignee: str | None = None) -> str:
    """Set `status:`/`updated:` (and `assignee:` when given) in the frontmatter block only.

    `assignee=None` leaves any existing claim untouched, so briefs written before the field
    existed keep transitioning unchanged; pass "" to release a claim.
    """
    m = re.match(r"(---\n)(.*?)(\n---)", text, re.DOTALL)
    if not m:
        return text
    block = m.group(2)
    block = re.sub(r"(?m)^status:.*$", f"status: {new_status}", block, count=1)
    block = re.sub(r"(?m)^updated:.*$", f"updated: {today}", block, count=1)
    if assignee is not None:
        line = f"assignee: {assignee}".rstrip()
        block, n = re.subn(r"(?m)^assignee:.*$", line, block, count=1)
        if not n:
            block = block + "\n" + line
    return text[: m.start(2)] + block + text[m.end(2):]


def _append_log(text: str, today: str, message: str) -> str:
    """Append a dated line to the Execution log (creating the section if it is missing)."""
    line = f"{today}: {message}"
    if "## Execution log" in text:
        return text.rstrip("\n") + "\n" + line + "\n"
    return text.rstrip("\n") + "\n\n## Execution log\n\n" + line + "\n"


def move_brief(brief_id: str, new_status: str, root: pathlib.Path,
               log: str | None = None, today: str | None = None,
               assignee: str | None = None) -> pathlib.Path:
    """Transition a brief to `new_status`: update frontmatter, log it, move the file.

    The read-modify-write-rename is wrapped in repo_lock so two concurrent actors moving
    the same brief serialize: whichever acquires the lock first moves the file; the second
    finds the source path gone and raises ValueError (claimed by another actor).

    Strategy: locate the source path BEFORE acquiring the lock (read-only, no contention),
    then re-verify it still exists INSIDE the lock before writing. This makes the claim
    atomic even across threads that both ran find_brief concurrently.

    Callers commit separately via sync_commit. Returns the new path.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r} (one of {VALID_STATUSES})")
    path = find_brief(brief_id, root)
    if path is None:
        raise ValueError(f"no brief with id {brief_id!r}")
    today = today or datetime.date.today().isoformat()
    message = log or DEFAULT_LOG.get(new_status, "moved")
    with repo_lock(root) as acquired:
        if not acquired:
            raise RuntimeError(
                f"timed out waiting for the queue lock; {brief_id!r} was NOT moved"
            )
        # Re-verify the source still exists: another actor may have claimed it
        # (moved/deleted it) while we were waiting for the lock.
        if not path.exists():
            raise ValueError(
                f"no brief with id {brief_id!r} (claimed by another actor)"
            )
        text = path.read_text()
        text = _update_frontmatter(text, new_status, today, assignee=assignee)
        text = _append_log(text, today, message)
        new_path = root / new_status / f"{brief_id}.md"
        atomic_write(new_path, text)
        if new_path != path:
            path.unlink()
    return new_path


def _print_table(hits: list[dict[str, Any]]) -> None:
    print(f"{'score':>5}  {'status':<12} id  —  title")
    for h in hits:
        print(f"{h['score']:>5}  {h['status']:<12} {h['id']}  —  {h['title']}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Resolve and transition queue briefs.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="find which brief a phrase refers to")
    r.add_argument("query")
    r.add_argument("--root", default=None)
    r.add_argument("--json", action="store_true")

    m = sub.add_parser("move", help="transition a brief to a new lifecycle state")
    m.add_argument("brief_id")
    m.add_argument("status", choices=VALID_STATUSES)
    m.add_argument("--root", default=None)
    m.add_argument("--log", default=None, help="execution-log line (default per target state)")
    m.add_argument("--assignee", default=None,
                   help="claim the brief for someone (person or agent); '' releases it")
    m.add_argument("--today", default=None, help="override date (testing)")
    m.add_argument("--no-sync", action="store_true", help="skip the git commit/push")

    a = ap.parse_args(argv)
    root = pathlib.Path(a.root) if a.root else resolve_root()

    if a.cmd == "resolve":
        hits = resolve_brief(a.query, root)
        if a.json:
            print(json.dumps(
                [{"id": h["id"], "title": h["title"], "status": h["status"],
                  "score": h["score"], "path": str(h["path"])} for h in hits],
                indent=2,
            ))
            return
        if not hits:
            print(f"No matching brief for: {a.query}")
            return
        _print_table(hits)
        return

    try:
        new_path = move_brief(a.brief_id, a.status, root, log=a.log, today=a.today,
                              assignee=a.assignee)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if not a.no_sync:
        sync_commit(f"brief({a.brief_id}): → {a.status}", root)
    print(f"{a.brief_id} → {a.status}: {new_path}")


if __name__ == "__main__":
    main()  # pragma: no cover

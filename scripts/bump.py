#!/usr/bin/env python3
"""Raise the importance of a brief found by tag, recording why and when in its body.

    bump.py <tag-or-ref> --importance 1 --reason "blocks the FDO demo"

A ref like `product-ingest#3809` matches the tag `pr:product-ingest#3809`; anything else is matched as a
literal tag. Importance is only ever raised (a lower number), never lowered, so sessions can bump freely.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _paths import require_queue, resolve_root  # noqa: E402
from deps import fm  # noqa: E402
from git_sync import sync_commit  # noqa: E402
from picks import note_choice  # noqa: E402

FOLDERS = ("ready", "in-progress", "blocked", "parked", "inbox")
REF = re.compile(r"^[\w.-]+#\d+$")


def tag_for(ref: str) -> str:
    return f"pr:{ref}" if REF.match(ref) else ref


def find(root: pathlib.Path, tag: str) -> pathlib.Path | None:
    tagged = [path for folder in FOLDERS for path in sorted((root / folder).glob("*.md")) if tag in tags(path)]
    return next((p for p in tagged if owned_by_pr_queue(p, tag)), tagged[0] if tagged else None)


def tags(path: pathlib.Path) -> set[str]:
    return {t.strip() for t in fm(path.read_text()).get("tags", "").strip().strip("[]").split(",")}


def owned_by_pr_queue(path: pathlib.Path, tag: str) -> bool:
    """The PR-queue sync titles its own brief `PR <ref>: ...`; a session's brief on the same PR copies the tag, not the title."""
    return tag.startswith("pr:") and fm(path.read_text()).get("title", "").strip('"').startswith(f"PR {tag[3:]}: ")


def bump(root: pathlib.Path, ref: str, importance: int, reason: str, now: datetime.datetime | None = None) -> str:
    path = find(root, tag_for(ref))
    if path is None:
        raise SystemExit(f"no open brief tagged {tag_for(ref)}")
    text = path.read_text()
    current = int(fm(text).get("importance", "3"))
    if importance >= current:
        return f"unchanged: importance {current} is already as high as {importance} or higher"
    stamp = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")
    text = re.sub(r"(?m)^importance:.*$", f"importance: {importance}", text, count=1)
    path.write_text(text.rstrip("\n") + f"\n\n## Bump\n- {stamp}: importance {current} -> {importance}. {reason}\n"
                    if "\n## Bump\n" not in text else
                    text.rstrip("\n") + f"\n- {stamp}: importance {current} -> {importance}. {reason}\n")
    sync_commit(f"brief({path.stem}): importance {current} -> {importance}", root, ids=[path.stem])
    note_choice(root, "bump", path.stem)
    return f"bumped {path.stem}: importance {current} -> {importance}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Raise a brief's importance, with a recorded reason.")
    ap.add_argument("ref", help="repo#number for a PR brief, or a literal tag")
    ap.add_argument("--importance", type=int, choices=[1, 2, 3, 4], required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root) if args.root else resolve_root()
    require_queue(root)
    print(bump(root, args.ref, args.importance, args.reason))


if __name__ == "__main__":
    main()  # pragma: no cover

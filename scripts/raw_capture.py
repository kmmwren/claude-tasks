#!/usr/bin/env python3
"""
raw_capture.py — zero-friction inbox dump.

Writes inbox/YYYY-MM-DD-HHMM-<slug>.md with title + raw body, no enrichment. The lightest
possible capture: no domain, importance, or structure to decide. The `enrich` skill turns
inbox items into full briefs later.
"""
import argparse
import datetime
import re
import sys

from _paths import require_queue, resolve_root
from git_sync import sync_commit


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    return text[:40].rstrip("-")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Zero-friction raw capture to inbox/")
    ap.add_argument("title", help="One-line title for the capture")
    ap.add_argument("--body", default="", help="Raw body text (optional)")
    ap.add_argument("--root", default=None, help="queue root (default: resolved per _paths)")
    ap.add_argument("--no-sync", action="store_true", help="skip the git commit/push")
    a = ap.parse_args(argv)

    root = require_queue(a.root if a.root else resolve_root())
    now = datetime.datetime.now()
    slug = slugify(a.title)
    uid = f"{now.strftime('%Y-%m-%d-%H%M')}-{slug}"
    path = root / "inbox" / f"{uid}.md"

    content = f"""---
id: {uid}
created: {now.date().isoformat()}
raw: true
status: inbox
---

# {a.title}

{a.body}
""".lstrip()

    path.write_text(content)
    print(f"Captured: inbox/{path.name}")
    if not a.no_sync:
        sync_commit(f"brief({uid}): captured (raw)", root, ids=[uid])
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

#!/usr/bin/env python3
"""Record what was done with a ranked list: picked, bumped or skipped a brief.

    log_pick.py pick <brief-id>
    log_pick.py skip <brief-id>

`transition.py move <id> in-progress` and `bump.py` log picks and bumps themselves; this is for
choices made elsewhere and for skips, which have no queue action of their own.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from typing import cast

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _paths import require_queue, resolve_root  # noqa: E402
from picks import CHOICES, ChoiceKind, PickLog  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Record a pick, bump or skip against the last ranked list.")
    ap.add_argument("kind", choices=CHOICES)
    ap.add_argument("brief_id")
    ap.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    root = require_queue(pathlib.Path(a.root) if a.root else resolve_root())
    choice = PickLog(root).record_choice(cast(ChoiceKind, a.kind), a.brief_id)
    where = f"position {choice.position + 1} of {choice.impression}" if choice.position is not None else "off-list"
    print(f"{choice.kind} {choice.brief_id} ({where})")


if __name__ == "__main__":
    main()  # pragma: no cover

"""_rebase_recovery.py: clear a rebase abandoned in the live queue tree without losing its autostash.

A `git pull --rebase --autostash` run in the checkout itself and killed mid-way leaves
`.git/rebase-merge` behind, and git refuses every later write there. Under the sync lock such a
directory is stale once no git process is running and nothing in it changed for the lock timeout.
"""
import pathlib
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

from _concurrency import LOCK_TIMEOUT

STATE_DIRS = ("rebase-merge", "rebase-apply")
# A running pull, rebase or merge, as `git pull`, `git rebase` or `.../git-core/git-rebase`.
_GIT_BUSY = re.compile(r"(?:^|/)git[ -].*\b(?:pull|rebase|merge)\b")


def _git(root: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _state_dir(root: pathlib.Path, name: str) -> pathlib.Path | None:
    found = _git(root, "rev-parse", "--git-path", name).stdout.strip()
    path = root / found
    return path if found and path.is_dir() else None


def _last_touched(state: pathlib.Path) -> float:
    return max((entry.stat().st_mtime for entry in (state, *state.iterdir())), default=0.0)


def _git_busy() -> bool:
    commands = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True).stdout
    return any(_GIT_BUSY.search(line.strip()) for line in commands.splitlines())


def _is_stale(state: pathlib.Path) -> bool:
    return time.time() - _last_touched(state) > LOCK_TIMEOUT and not _git_busy()


def _rescue_autostash(root: pathlib.Path, state: pathlib.Path) -> bool:
    """Keep the autostash as an ordinary stash entry, then detach it so a rebase abort cannot re-apply it."""
    marker = state / "autostash"
    if not marker.is_file():
        return True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stored = _git(root, "stash", "store", "-m", f"rescued autostash {stamp}", marker.read_text().strip())
    if stored.returncode == 0:
        marker.unlink()
    return stored.returncode == 0


def _clear(root: pathlib.Path, state: pathlib.Path) -> bool:
    """Abort a rebase that holds real progress; remove a directory that holds nothing else."""
    if (state / "head-name").exists():
        return _git(root, "rebase", "--abort").returncode == 0
    shutil.rmtree(state)
    return True


def recover_stale_rebase(root: pathlib.Path) -> list[str]:
    """Clear each stale rebase state dir under `root`; the messages say what was rescued or left."""
    notes = []
    for name in STATE_DIRS:
        state = _state_dir(root, name)
        if state is None or not _is_stale(state):
            continue
        if _rescue_autostash(root, state) and _clear(root, state):
            notes.append(f"cleared a stale {name}; any autostash is kept in `git stash list` as 'rescued autostash'")
        else:
            notes.append(f"left a stale {name} in place: its autostash could not be saved or the abort failed")
    return notes

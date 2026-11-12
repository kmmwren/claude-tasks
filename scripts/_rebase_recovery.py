"""_rebase_recovery.py: clear a todo-less rebase left in the live queue tree, keeping its autostash.

A `git pull --rebase --autostash` killed between stashing and writing its todo leaves
`.git/rebase-merge` holding only `autostash`, and git refuses every later pull there. Such a
directory carries no rebase progress, so once it has sat untouched past REBASE_SETTLE the
autostash is stored as a named stash and `git rebase --quit` removes the state. A directory
that still has a todo is real progress and is only reported.
"""

import pathlib
import shutil
import subprocess
import time
from datetime import datetime, timezone

STATE_DIR = "rebase-merge"
TODO = "git-rebase-todo"
# Longer than git ever takes between creating the directory and writing the todo.
REBASE_SETTLE = 60.0
STASH_MESSAGE = "queue sync: rescued autostash"


def _git(root: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _state_dir(root: pathlib.Path) -> pathlib.Path | None:
    found = _git(root, "rev-parse", "--git-path", STATE_DIR).stdout.strip()
    path = root / found
    return path if found and path.is_dir() else None


def _settled(state: pathlib.Path) -> bool:
    touched = max(entry.stat().st_mtime for entry in (state, *state.iterdir()))
    return time.time() - touched > REBASE_SETTLE


def _store_autostash(root: pathlib.Path, state: pathlib.Path) -> bool:
    """Store the autostash as a named stash entry and detach it, so `--quit` cannot store it twice."""
    marker = state / "autostash"
    if not marker.is_file():
        return True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stored = _git(root, "stash", "store", "-m", f"{STASH_MESSAGE} {stamp}", marker.read_text().strip())
    if stored.returncode != 0:
        return False
    marker.unlink()
    return True


def _quit(root: pathlib.Path, state: pathlib.Path) -> None:
    """`rebase --quit` leaves HEAD and the tree alone; with the autostash safe, removing the dir is the fallback."""
    if _git(root, "rebase", "--quit").returncode != 0:
        shutil.rmtree(state, ignore_errors=True)


def recover_stale_rebase(root: pathlib.Path) -> list[str]:
    """Clear a settled todo-less rebase-merge under `root`; the messages say what was kept or left."""
    state = _state_dir(root)
    if state is None or not _settled(state):
        return []
    if (state / TODO).exists():
        return [
            f"WARNING: a rebase with unfinished steps is stuck in {root}; finish it with "
            "`git rebase --continue` or drop it with `git rebase --abort`"
        ]
    if not _store_autostash(root, state):
        return [f"left a stale {STATE_DIR} in place: its autostash could not be stored"]
    _quit(root, state)
    return [f"cleared a stale {STATE_DIR}; any autostash is in `git stash list` as '{STASH_MESSAGE}'"]

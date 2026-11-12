"""The coalesced pusher's per-clone state (its locks, dirty mark and log), kept inside .git."""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import contextlib
import pathlib

from _concurrency import HeldLock, clone_state_path, exclusive_lock, shared_lock, try_lock


class PushState:
    PUSHER_LOCK = ".tasks-push.lock"  # held by the one running pusher
    WRITERS_LOCK = ".tasks-writers.lock"  # shared by writers from commit start to dirty mark
    DIRTY = ".tasks-push.dirty"  # main is ahead of origin and no push has started from it yet
    LOG = ".tasks-push.log"  # the detached pusher's output: divergence reports land here

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def _path(self, name: str) -> pathlib.Path:
        return clone_state_path(self.root, name)

    @property
    def log_path(self) -> pathlib.Path:
        return self._path(self.LOG)

    def try_lock_pusher(self) -> HeldLock | None:
        return try_lock(self._path(self.PUSHER_LOCK))

    def pusher_lock(self, timeout: float) -> contextlib.AbstractContextManager[bool]:
        return exclusive_lock(self._path(self.PUSHER_LOCK), timeout=timeout)

    def writing(self) -> contextlib.AbstractContextManager[bool]:
        """Held shared while a writer commits, so a pusher settling for exclusive access waits for the burst."""
        return shared_lock(self._path(self.WRITERS_LOCK))

    def settle_writers(self, timeout: float) -> None:
        with exclusive_lock(self._path(self.WRITERS_LOCK), timeout=timeout, poll=0.05):
            pass

    def mark_dirty(self) -> None:
        self._path(self.DIRTY).touch()

    def is_dirty(self) -> bool:
        return self._path(self.DIRTY).exists()

    def take_dirty(self) -> bool:
        """Clear the dirty mark; True if it was set. Cleared before pushing, so a later mark means a later commit."""
        try:
            self._path(self.DIRTY).unlink()
        except FileNotFoundError:
            return False
        return True

    def take_report(self) -> str:
        """What the last detached pusher logged, cleared on read; empty when the log cannot be read."""
        try:
            report = self.log_path.read_text(errors="replace").strip()
            if report:
                self.log_path.write_text("")
        except FileNotFoundError:
            return ""
        except OSError as e:
            print(f"  (warning: could not read the push log {self.log_path}: {e}; pushing anyway.)")
            return ""
        return report

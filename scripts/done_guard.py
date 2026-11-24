"""done_guard: a brief in done/ has no open success criterion.

`transition.move_brief` refuses the move; `DoneBackstop` is the net under every other writer (a hand
`mv`, a script that writes done/ directly): it returns an unsettled brief to the folder it came from
before the commit that would publish it. A settled or superseded brief has every criterion ticked, so
an unticked line in done/ is exactly the evidence that no check ran. Stdlib only.
"""

import datetime
import pathlib
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from _concurrency import atomic_write

UNTICKED = re.compile(r"^\s*- \[ \].*$", re.M)
SECTION = re.compile(r"^## Success criteria\n(.*?)(?=^## |\Z)", re.M | re.S)
OPEN_BOX = re.compile(r"^(\s*- )\[ \](.*)$", re.M)
STATUS_LINE = re.compile(r"(?m)^status:.*$")
FALLBACK_FOLDER = "ready"
OPEN_FOLDERS = ("inbox", "ready", "in-progress", "blocked", "parked")
LOG_HEADING = "## Execution log"


class UntickedCriteriaError(ValueError):
    """A brief cannot be marked done while its success criteria are still open."""

    def __init__(self, brief_id: str, lines: list[str]):
        self.lines = lines
        listing = "\n".join(f"  {ln.strip()}" for ln in lines)
        super().__init__(
            f"{brief_id!r} has {len(lines)} unticked success criteria; not moved to done:\n{listing}"
        )


def unticked_criteria(text: str) -> list[str]:
    """The unticked `- [ ]` lines in the brief's Success criteria section, in order."""
    section = SECTION.search(text)
    return UNTICKED.findall(section.group(1)) if section else []


def settle_criteria(text: str, note: str) -> str:
    """Tick every open Success criteria line, appending `note`, for a brief whose work the world already settled."""
    section = SECTION.search(text)
    if not section:
        return text
    ticked = OPEN_BOX.sub(lambda box: f"{box.group(1)}[x]{box.group(2)} ({note})", section.group(1))
    return text[: section.start(1)] + ticked + text[section.end(1) :]


def _log_line(text: str, line: str) -> str:
    if LOG_HEADING in text:
        return text.rstrip("\n") + "\n" + line + "\n"
    return text.rstrip("\n") + f"\n\n{LOG_HEADING}\n\n" + line + "\n"


def _with_status(text: str, status: str) -> str:
    head, sep, rest = text.partition("\n---\n")
    return STATUS_LINE.sub(f"status: {status}", head, count=1) + sep + rest if sep else text


@dataclass(frozen=True)
class Reverted:
    """One brief the backstop returned to the folder it came from."""

    brief: str
    folder: str
    open_lines: tuple[str, ...]

    @property
    def warning(self) -> str:
        return (
            f"  (warning: {self.brief} was in done/ with {len(self.open_lines)} open success "
            f"criteria; returned to {self.folder}/ and noted in its Execution log. Close it with "
            "`transition.py move <id> done --settle-with <evidence>` or `--superseded-by <id>`.)"
        )

    @property
    def log_line(self) -> str:
        names = "; ".join(ln.strip().removeprefix("- [ ]").strip() for ln in self.open_lines)
        return (
            f"done-guard: not done, {len(self.open_lines)} success criteria still open "
            f"({names}); returned to {self.folder}/ before the commit."
        )


class DoneBackstop:
    """Before a commit, move each brief newly filed in done/ with an open criterion back where it came from."""

    def __init__(self, root: pathlib.Path, today: str | None = None):
        self.root = root
        self.today = today or datetime.date.today().isoformat()

    def _git(self, *args: str) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True
        ).stdout
        return [p for p in out.split("\0") if p]

    def _new_in_done(self) -> list[str]:
        """Briefs added or renamed into done/ that HEAD does not hold: untracked, or staged as an add or rename."""
        untracked = self._git("ls-files", "-z", "--others", "--exclude-standard", "--", "done")
        staged = self._git("diff", "-z", "--cached", "--relative", "--name-only", "--diff-filter=AR", "--", "done")
        return sorted(p for p in {*untracked, *staged} if p.endswith(".md") and p.count("/") == 1)

    def _vacated(self, name: str) -> str:
        """The open folder `name` was removed from (a deletion not yet committed), else the fallback."""
        gone = {
            *self._git("ls-files", "-z", "--deleted"),
            *self._git("diff", "-z", "--cached", "--relative", "--name-only", "--diff-filter=D"),
        }
        return next((f for f in OPEN_FOLDERS if f"{f}/{name}" in gone), FALLBACK_FOLDER)

    def _revert(self, rel: str) -> Reverted | None:
        path = self.root / rel
        text = path.read_text()
        open_lines = unticked_criteria(text)
        if not open_lines:
            return None
        reverted = Reverted(path.stem, self._vacated(path.name), tuple(open_lines))
        back = _with_status(text, reverted.folder)
        atomic_write(self.root / reverted.folder / path.name, _log_line(back, f"{self.today}: {reverted.log_line}"))
        path.unlink()
        self._git("reset", "-q", "HEAD", "--", rel)
        return reverted

    def run(self, in_scope: Callable[[str], bool] = lambda rel: True) -> list[Reverted]:
        """Revert every in-scope unsettled brief and print one warning each; never raises on a bad file."""
        reverted = []
        for rel in filter(in_scope, self._new_in_done()):
            try:
                found = self._revert(rel)
            except (OSError, UnicodeDecodeError) as err:
                print(f"  (warning: done-guard could not check {rel}: {err})")
                continue
            if found:
                print(found.warning)
                reverted.append(found)
        return reverted


def scope_of(ids: Iterable[str], paths: Sequence[str]) -> Callable[[str], bool]:
    """Whether a done/ path is part of the commit: a sweep (`.`) takes all, otherwise a named id or path."""
    wanted = set(ids)
    sweep = any(p in (".", "") for p in paths)
    return lambda rel: (
        sweep
        or pathlib.PurePath(rel).stem in wanted
        or any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in paths)
    )

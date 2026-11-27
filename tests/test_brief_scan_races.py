"""A folder scan skips a brief another session moved between the listing and the read."""

import pathlib
from collections.abc import Callable

import build_board
import fingerprint
import pytest
import requirements_check
import triage_brief
from bump import find
from deps import read_briefs
from fold_session import SessionQueue

from tests.queue_helpers import make_queue

Scan = Callable[[pathlib.Path], list[str]]

SCANS: dict[str, Scan] = {
    "build_board": lambda root: [b["id"] for b in build_board.load_briefs(root)],
    "bump": lambda root: [find(root, "pr:x#1")[0].stem],  # type: ignore[index]
    "fingerprint": lambda root: [m.id for ms in fingerprint.index(root).values() for m in ms],
    "fold_session": lambda root: [p.stem for p in SessionQueue(root, "s", root).open_briefs()],
    "requirements_check": lambda root: [b.name for b in requirements_check.load_briefs(root)],
    "triage_brief": lambda root: [b.id for b in triage_brief.load_folder(root, "ready")],
}


@pytest.mark.parametrize("scan", SCANS.values(), ids=SCANS)
def test_a_scan_skips_a_brief_moved_away_before_it_is_read(tmp_path, vanish_on_first_read, scan):
    root = make_queue(tmp_path, {"a-ghost": {"tags": "[pr:x#1]"}, "b-kept": {"tags": "[pr:x#1]"}})
    vanish_on_first_read(root / "ready" / "a-ghost.md")

    assert scan(root) == ["b-kept"]


def test_an_unreadable_brief_is_skipped_with_a_warning_naming_it(tmp_path, monkeypatch, capsys):
    root = make_queue(tmp_path, {"a-locked": {}, "b-kept": {}})
    locked = root / "ready" / "a-locked.md"
    original = pathlib.Path.read_text

    def read_text(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)

    assert [path.stem for path, _ in read_briefs(root / "ready")] == ["b-kept"]
    assert str(locked) in capsys.readouterr().err

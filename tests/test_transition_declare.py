"""Tests for `transition.py declare`: writing a brief's blocked_by in place, without moving it."""

import declaration
import pytest
import transition
from deps import declares_dependencies, fm

from tests.queue_helpers import brief_text, make_queue


def _undeclared(bid, status):
    return "".join(
        ln
        for ln in brief_text(bid, status=status).splitlines(True)
        if not ln.startswith("blocked_by:")
    )


@pytest.fixture
def root(tmp_path):
    root = make_queue(tmp_path, {"other": {"status": "ready"}})
    for bid, status in (("open", "ready"), ("doing", "in-progress"), ("shelved", "parked")):
        (root / status / f"{bid}.md").write_text(_undeclared(bid, status))
    return root


def _declare(root, *args):
    transition.main(["declare", *args, "--root", str(root), "--no-sync", "--today", "2026-11-23"])


def _front(root, status, bid):
    return fm((root / status / f"{bid}.md").read_text())


@pytest.mark.parametrize(
    "status,bid", [("ready", "open"), ("in-progress", "doing"), ("parked", "shelved")]
)
def test_no_deps_declares_nothing_in_place(root, status, bid):
    _declare(root, bid, "--no-deps")
    front = _front(root, status, bid)
    assert front["blocked_by"] == "[]" and declares_dependencies(front)
    assert "2026-11-23: declared blocked_by: []" in (root / status / f"{bid}.md").read_text()


def test_a_parked_brief_records_its_blockers_and_stays_parked(root):
    _declare(root, "shelved", "--blocked-by", "other,external:vendor reply")
    assert _front(root, "parked", "shelved")["blocked_by"] == "[other, external:vendor reply]"


def test_an_in_progress_brief_records_its_blocker(root):
    _declare(root, "doing", "--blocked-by", "other")
    assert _front(root, "in-progress", "doing")["blocked_by"] == "[other]"


def test_a_ready_brief_that_waits_is_sent_to_move_blocked(root, capsys):
    with pytest.raises(SystemExit, match="move open blocked --blocked-by other"):
        _declare(root, "open", "--blocked-by", "other")
    assert "blocked_by" not in _front(root, "ready", "open")


def test_an_unknown_blocker_is_refused_and_nothing_is_written(root):
    with pytest.raises(SystemExit, match="unknown blocker id"):
        _declare(root, "doing", "--blocked-by", "nope")
    assert "blocked_by" not in _front(root, "in-progress", "doing")


@pytest.mark.parametrize("status", ["blocked", "done"])
def test_briefs_outside_the_declarable_folders_are_refused(root, status):
    (root / status / "elsewhere.md").write_text(
        brief_text("elsewhere", status=status, blocked_by="[other]")
    )
    with pytest.raises(SystemExit, match=f"is in {status}/"):
        _declare(root, "elsewhere", "--no-deps")


def test_redeclaring_overwrites_the_value(root):
    _declare(root, "doing", "--blocked-by", "other")
    _declare(root, "doing", "--no-deps")
    assert _front(root, "in-progress", "doing")["blocked_by"] == "[]"


def test_blocked_by_and_no_deps_are_exclusive_and_one_is_required(root):
    for args in (["doing"], ["doing", "--no-deps", "--blocked-by", "other"]):
        with pytest.raises(SystemExit):
            _declare(root, *args)


def test_an_empty_blocked_by_is_refused(root):
    with pytest.raises(SystemExit, match="needs at least one id"):
        _declare(root, "doing", "--blocked-by", "")


BARE = "# Backfilled brief\n\nNotes with no frontmatter block.\n"
DATED_STEM = "2026-11-04-product-ingest-3809-refresh-buildhealth-table"


def _bare(root, status, stem):
    (root / status / f"{stem}.md").write_text(BARE)


@pytest.mark.parametrize("status", ["ready", "in-progress", "parked"])
def test_a_brief_without_frontmatter_gets_a_minimal_block_and_the_declaration(root, status):
    _bare(root, status, "backfilled")
    _declare(root, "backfilled", "--no-deps")
    text = (root / status / "backfilled.md").read_text()
    front = fm(text)
    assert front["id"] == "backfilled" and front["status"] == status
    assert front["created"] == front["updated"] == "2026-11-23"
    assert front["blocked_by"] == "[]" and declares_dependencies(front)
    assert "# Backfilled brief" in text and "2026-11-23: declared blocked_by: []" in text


def test_a_brief_without_frontmatter_records_its_blockers(root):
    _bare(root, "parked", "backfilled")
    _declare(root, "backfilled", "--blocked-by", "other")
    assert _front(root, "parked", "backfilled")["blocked_by"] == "[other]"


def test_an_existing_frontmatter_block_is_not_given_created_or_id_lines(root):
    _declare(root, "doing", "--no-deps")
    assert "created" not in _front(root, "in-progress", "doing")


def test_move_refuses_a_brief_without_frontmatter_and_writes_nothing(root):
    _bare(root, "ready", "backfilled")
    with pytest.raises(SystemExit) as exit_info:
        transition.main(
            ["move", "backfilled", "in-progress", "--root", str(root), "--no-sync", "--no-deps"]
        )
    assert exit_info.value.code != 0
    assert (root / "ready" / "backfilled.md").read_text() == BARE
    assert not (root / "in-progress" / "backfilled.md").exists()


def test_declare_finds_a_brief_by_its_frontmatter_id(root):
    (root / "ready" / f"{DATED_STEM}.md").write_text(_undeclared("refresh-buildhealth", "ready"))
    _declare(root, "refresh-buildhealth", "--no-deps")
    assert _front(root, "ready", DATED_STEM)["blocked_by"] == "[]"


def test_declare_commits_the_file_stem_when_given_a_frontmatter_id(root, monkeypatch):
    (root / "ready" / f"{DATED_STEM}.md").write_text(_undeclared("refresh-buildhealth", "ready"))
    committed = []
    monkeypatch.setattr(declaration, "sync_commit", lambda *a, **k: committed.append(k["ids"]))
    transition.main(["declare", "refresh-buildhealth", "--no-deps", "--root", str(root)])
    assert committed == [[DATED_STEM]]


def test_an_inbox_brief_is_declared_in_place_and_stays_in_the_inbox(root):
    (root / "inbox" / "filed.md").write_text(_undeclared("filed", "inbox"))
    _declare(root, "filed", "--no-deps")
    assert _front(root, "inbox", "filed")["blocked_by"] == "[]"
    assert _front(root, "inbox", "filed")["status"] == "inbox"

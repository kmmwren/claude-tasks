"""Tests for triage on briefs `add_task.py --ready` filed straight to ready/ (marked `unchecked`)."""

import add_task
import init_queue
import pytest
import triage
from git_sync import SyncResult
from triage_close import CloseRules

TITLE = "Rotate the leaked key"
EXISTING = "Vault is v2."
CAPTURED = "Second session found the key in a log."
GATE = triage.Gate(lambda _source: False)


@pytest.fixture
def queue(tmp_path):
    return init_queue.init_queue(tmp_path / "q", name="demo")


@pytest.fixture(autouse=True)
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(
        triage,
        "sync_commit",
        lambda msg, root, ids, paths=(): calls.append(sorted(ids)) or SyncResult.COMMITTED,
    )
    return calls


def file_ready(queue, context, title=TITLE):
    """File a ready brief as a capture does, allowing a repeat; returns its id."""
    add_task.main(
        ["--title", title, "--goal", "Key rotated.", "--criteria", "Old key rejected."]
        + ["--context", context, "--ready", "--no-deps", "--on-duplicate", "file"]
        + ["--root", str(queue), "--no-sync"]
    )
    return sorted(p.stem for p in (queue / "ready").glob("*.md") if context in p.read_text())[0]


def checked(queue, brief_id, created=None):
    """The brief as triage leaves it: no `unchecked` mark, optionally another created date."""
    path = queue / "ready" / f"{brief_id}.md"
    lines = [ln for ln in path.read_text().splitlines(keepends=True) if "unchecked:" not in ln]
    text = "".join(lines)
    if created:
        text = "".join(
            f"created: {created}\n" if ln.startswith("created:") else ln
            for ln in text.splitlines(keepends=True)
        )
    path.write_text(text)


def run(queue, apply=True, max_moves=200):
    opts = triage.Options(
        queue,
        apply,
        GATE,
        None,
        batch_size=5,
        jobs=1,
        limit=None,
        today="2026-11-25",
        rules=CloseRules(max_moves=max_moves),
    )
    return triage.run(opts)


def ids(queue, folder):
    return sorted(p.stem for p in (queue / folder).glob("*.md"))


def test_ready_capture_is_marked_unchecked_and_inbox_capture_is_not(queue):
    ready = file_ready(queue, EXISTING)
    add_task.main(["--title", "Other", "--root", str(queue), "--no-sync"])
    assert "unchecked: " in (queue / "ready" / f"{ready}.md").read_text()
    assert "unchecked" not in (queue / "inbox" / "other.md").read_text()


def test_a_ready_capture_repeating_a_ready_brief_is_merged_with_its_body_kept(queue):
    first = file_ready(queue, EXISTING)
    checked(queue, first)
    repeat = file_ready(queue, CAPTURED)
    report = run(queue)
    assert ids(queue, "ready") == [first] and ids(queue, "done") == [repeat]
    survivor = (queue / "ready" / f"{first}.md").read_text()
    assert f"## Merged from {repeat}" in survivor and CAPTURED in survivor
    closed = (queue / "done" / f"{repeat}.md").read_text()
    assert "closed_reason: duplicate" in closed and f"duplicate_of: {first}" in closed
    assert "closed_from: ready" in closed
    assert report.closed == {"duplicate": 1}


def test_an_unchecked_repeat_is_merged_even_into_a_checked_brief_filed_later(queue):
    first = file_ready(queue, EXISTING)
    checked(queue, first, created="2099-01-01")
    repeat = file_ready(queue, CAPTURED)
    run(queue)
    assert ids(queue, "ready") == [first] and ids(queue, "done") == [repeat]


def test_ready_briefs_already_checked_are_left_alone_even_when_they_repeat(queue):
    first, twin = file_ready(queue, EXISTING), file_ready(queue, CAPTURED)
    checked(queue, first)
    checked(queue, twin)
    run(queue)
    assert ids(queue, "ready") == sorted([first, twin]) and ids(queue, "done") == []


def test_two_unchecked_repeats_keep_the_earlier_and_clear_its_mark(queue):
    first = file_ready(queue, EXISTING)
    second = file_ready(queue, CAPTURED)
    run(queue)
    assert ids(queue, "ready") == [first] and ids(queue, "done") == [second]
    assert "unchecked" not in (queue / "ready" / f"{first}.md").read_text()


def test_a_unique_ready_capture_stays_and_loses_its_mark(queue, synced):
    only = file_ready(queue, EXISTING)
    report = run(queue)
    text = (queue / "ready" / f"{only}.md").read_text()
    assert ids(queue, "ready") == [only] and "unchecked" not in text and text.startswith("---\n")
    assert report.ready_checked == 1 and synced == [[only]]
    assert run(queue).ready_checked == 0


def test_a_dry_run_plans_the_merge_and_writes_nothing(queue):
    first = file_ready(queue, EXISTING)
    checked(queue, first)
    repeat = file_ready(queue, CAPTURED)
    before = (queue / "ready" / f"{repeat}.md").read_text()
    report = run(queue, apply=False)
    assert [m.brief_id for m in report.moves] == [repeat] and report.ready_checked == 0
    assert (queue / "ready" / f"{repeat}.md").read_text() == before


def test_a_close_held_back_by_the_cap_keeps_the_mark_for_the_next_run(queue):
    first = file_ready(queue, EXISTING)
    checked(queue, first)
    repeat = file_ready(queue, CAPTURED)
    run(queue, max_moves=0)
    assert ids(queue, "ready") == sorted([first, repeat])
    assert "unchecked" in (queue / "ready" / f"{repeat}.md").read_text()
    run(queue)
    assert ids(queue, "done") == [repeat]

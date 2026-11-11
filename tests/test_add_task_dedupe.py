"""Tests for add_task.py capture dedupe: a repeat of an existing brief is merged, refused or filed."""

import add_task
import init_queue
import pytest

TITLE = ["--title", "Rotate the leaked key", "--context", "Leaked in o/r#4."]


@pytest.fixture
def queue(tmp_path):
    return init_queue.init_queue(tmp_path / "q", name="demo")


def capture(queue, *extra, args=TITLE):
    add_task.main([*args, "--root", str(queue), "--no-sync", *extra])


def files(queue, folder="inbox"):
    return sorted(p.name for p in (queue / folder).glob("*.md"))


def test_a_repeat_capture_merges_into_the_existing_brief_and_says_which(queue, capsys):
    capture(queue)
    capsys.readouterr()
    capture(queue, "--source", "user")
    assert capsys.readouterr().out.strip() == "merged into inbox/rotate-the-leaked-key.md"
    assert files(queue) == ["rotate-the-leaked-key.md"]
    log = (queue / "inbox" / "rotate-the-leaked-key.md").read_text().rsplit("## Execution log", 1)[1]
    assert "repeat capture by user merged here" in log


def test_a_repeat_with_new_context_adds_it_as_a_note(queue):
    capture(queue)
    capture(queue, args=["--title", "Rotate the LEAKED key!", "--context", "Leaked in o/r#4. Vault is v2."])
    text = (queue / "inbox" / "rotate-the-leaked-key.md").read_text()
    assert "Leaked in o/r#4. Vault is v2." in text.split("## Notes / open questions")[1]
    assert "_none_" not in text.split("## Notes / open questions")[1].split("## Execution log")[0]


def test_a_repeat_with_context_the_brief_already_holds_adds_no_note(queue):
    capture(queue)
    before = (queue / "inbox" / "rotate-the-leaked-key.md").read_text()
    capture(queue)
    notes = (queue / "inbox" / "rotate-the-leaked-key.md").read_text().split("## Notes / open questions")[1]
    assert notes.split("## Execution log")[0] == before.split("## Notes / open questions")[1].split("## Execution log")[0]


def test_refuse_files_nothing_and_exits_nonzero(queue):
    capture(queue)
    with pytest.raises(SystemExit) as stop:
        capture(queue, "--on-duplicate", "refuse")
    assert stop.value.code == "duplicate of inbox/rotate-the-leaked-key.md"
    assert files(queue) == ["rotate-the-leaked-key.md"]


def test_file_keeps_the_old_behaviour_and_files_a_second_brief(queue):
    capture(queue)
    capture(queue, "--on-duplicate", "file")
    assert len(files(queue)) == 2


def test_the_same_title_citing_a_different_source_is_not_a_duplicate(queue):
    capture(queue)
    capture(queue, args=["--title", "Rotate the leaked key", "--context", "Leaked in o/r#9."])
    assert len(files(queue)) == 2


def test_a_duplicate_of_a_brief_already_past_the_inbox_merges_there(queue, capsys):
    capture(queue, "--status", "parked")
    capsys.readouterr()
    capture(queue)
    assert capsys.readouterr().out.strip() == "merged into parked/rotate-the-leaked-key.md"
    assert files(queue) == []


def test_a_merge_is_committed_against_the_existing_brief(queue, monkeypatch):
    synced = []
    monkeypatch.setattr(add_task, "_sync_commit", lambda msg, root, ids: synced.append((msg, ids)))
    add_task.main([*TITLE, "--root", str(queue)])
    add_task.main([*TITLE, "--root", str(queue)])
    assert [ids for _, ids in synced] == [["rotate-the-leaked-key"], ["rotate-the-leaked-key"]]
    assert synced[1][0].startswith("brief(rotate-the-leaked-key): repeat capture merged")


def test_a_brief_without_a_notes_section_gains_one():
    assert add_task.add_note("# T\n\nbody\n", "extra") == "# T\n\nbody\n\n## Notes / open questions\n\nextra\n"

"""In-process tests for add_task.py and raw_capture.py main() flows + their slug helpers.

The concurrency suite drives these as subprocesses (for real OS-level lock races), which
in-process coverage can't see — so these cover the same code paths in-process for the gate.
"""
import io

import add_task
import init_queue
import pytest
import raw_capture


@pytest.fixture
def queue(tmp_path):
    return init_queue.init_queue(tmp_path / "q", name="demo")


# ── add_task.slug ─────────────────────────────────────────────────────────────

def test_slug_lowercases_and_hyphenates():
    assert add_task.slug("Hello World") == "hello-world"


def test_slug_strips_specials_and_respects_maxwords():
    assert add_task.slug("Fix: the bug! (NOW) really soon", maxwords=3) == "fix-the-bug"


def test_slug_fallback_when_all_specials():
    assert add_task.slug("!!! ???") == "task"


# ── add_task.main ─────────────────────────────────────────────────────────────

def test_add_task_writes_ready_brief(queue, capsys):
    add_task.main(["--title", "Build the thing", "--root", str(queue), "--ready",
                   "--goal", "It exists.", "--criteria", "first", "--criteria", "second",
                   "--domain", "work", "--importance", "2", "--autonomy", "full"])
    path = queue / "ready" / "build-the-thing.md"
    text = path.read_text()
    assert "status: ready" in text
    assert "domain: work" in text
    assert "- [ ] first" in text and "- [ ] second" in text
    assert "ready/build-the-thing.md" in capsys.readouterr().out


def test_add_task_defaults_to_inbox(queue):
    add_task.main(["--title", "Quick idea", "--root", str(queue)])
    assert (queue / "inbox" / "quick-idea.md").read_text().count("status: inbox") == 1


def test_add_task_appends_stdin_to_context(queue, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("context from a pipe"))
    add_task.main(["--title", "With stdin", "--root", str(queue), "--stdin"])
    assert "context from a pipe" in (queue / "inbox" / "with-stdin.md").read_text()


def test_add_task_commit_invokes_sync(queue, monkeypatch):
    calls = {}
    monkeypatch.setattr(add_task, "_sync_commit",
                        lambda msg, root: calls.update(msg=msg, root=root))
    add_task.main(["--title", "Ship it", "--root", str(queue), "--commit"])
    assert "brief(ship-it)" in calls["msg"]
    assert calls["root"] == queue


def test_add_task_refuses_uninitialised_dir(tmp_path):
    with pytest.raises(SystemExit, match="tasks-init"):
        add_task.main(["--title", "x", "--root", str(tmp_path / "not-a-queue")])


def test_add_task_resolves_root_from_env(queue, monkeypatch):
    # no --root: must fall back to resolve_root() → the env var
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(queue))
    add_task.main(["--title", "Env routed"])
    assert (queue / "inbox" / "env-routed.md").is_file()


def test_add_task_parked_writes_to_parked_dir(queue, capsys):
    add_task.main(["--title", "Waiting on answer", "--root", str(queue), "--parked"])
    path = queue / "parked" / "waiting-on-answer.md"
    text = path.read_text()
    assert "status: parked" in text
    assert "parked/waiting-on-answer.md" in capsys.readouterr().out


def test_add_task_parked_does_not_write_to_inbox(queue):
    add_task.main(["--title", "Blocked task", "--root", str(queue), "--parked"])
    assert not (queue / "inbox" / "blocked-task.md").exists()


def test_add_task_parked_takes_precedence_over_ready(queue):
    # --parked beats --ready: parked/ wins when both flags are passed
    add_task.main(["--title", "Double flagged", "--root", str(queue), "--parked", "--ready"])
    assert (queue / "parked" / "double-flagged.md").is_file()
    assert not (queue / "ready" / "double-flagged.md").exists()


# ── raw_capture ───────────────────────────────────────────────────────────────

def test_slugify_truncates_and_cleans():
    assert raw_capture.slugify("A Very, Very Long Title!") == "a-very-very-long-title"


def test_raw_capture_writes_inbox_file(queue, capsys):
    assert raw_capture.main(["A passing thought", "--root", str(queue)]) == 0
    files = list((queue / "inbox").glob("*-a-passing-thought.md"))
    assert len(files) == 1
    assert "# A passing thought" in files[0].read_text()
    assert "Captured: inbox/" in capsys.readouterr().out


def test_raw_capture_includes_body(queue):
    raw_capture.main(["Idea", "--body", "the body text", "--root", str(queue)])
    f = next((queue / "inbox").glob("*-idea.md"))
    assert "the body text" in f.read_text()


def test_raw_capture_refuses_uninitialised_dir(tmp_path):
    with pytest.raises(SystemExit, match="tasks-init"):
        raw_capture.main(["x", "--root", str(tmp_path / "nope")])

"""Tests for init_queue.py — scaffolding a fresh queue (backs the /tasks-init command)."""
import _paths
import config
import init_queue
import pytest


def test_creates_lifecycle_dirs(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    for d in _paths.TASK_DIRS:
        assert (root / d).is_dir()
    assert (root / "view").is_dir()


def test_writes_config_making_it_a_queue(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    assert _paths.is_queue(root)
    assert config.load_config(root)["name"] == "demo"


def test_writes_template_and_gitignore(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    assert (root / "_template.md").is_file()
    assert "id:" in (root / "_template.md").read_text()
    assert (root / ".gitignore").is_file()


def test_template_offers_optional_assignee_field(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    lines = (root / "_template.md").read_text().splitlines()
    assignee = [ln for ln in lines if ln.startswith("assignee:")]
    assert len(assignee) == 1
    assert assignee[0].startswith("assignee: #"), "must ship unset (empty = unclaimed)"


def test_empty_dirs_have_keep_files(tmp_path):
    # so the lifecycle folders survive a git commit while empty
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    assert (root / "inbox" / ".keep").is_file()


def test_refuses_existing_queue_without_force(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    with pytest.raises(SystemExit, match="already"):
        init_queue.init_queue(root, name="demo")


def test_force_reinitialises(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    # should not raise with force=True
    again = init_queue.init_queue(root, name="renamed", force=True)
    assert config.load_config(again)["name"] == "renamed"


def test_name_defaults_to_dir_name(tmp_path):
    root = init_queue.init_queue(tmp_path / "myqueue")
    assert config.load_config(root)["name"] == "myqueue"


def test_main_creates_queue_and_prints(tmp_path, capsys):
    init_queue.main([str(tmp_path / "q"), "--name", "viamain"])
    out = capsys.readouterr().out
    assert "Initialised task queue" in out
    assert _paths.is_queue(tmp_path / "q")
    assert config.load_config(tmp_path / "q")["name"] == "viamain"

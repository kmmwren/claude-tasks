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


def test_creates_the_blocked_folder_and_template_fields(tmp_path):
    root = init_queue.init_queue(tmp_path / "q")
    assert (root / "blocked" / ".keep").exists()
    template = (root / "_template.md").read_text()
    assert "blocked_by:" in template and "milestone:" in template


def test_config_documents_milestones_but_leaves_them_off(tmp_path):
    import config
    root = init_queue.init_queue(tmp_path / "q")
    assert "# milestones =" in (root / "tasks.toml").read_text()
    assert "milestones" not in config.load_config(root)


def _session(tmp_path):
    parent = init_queue.init_queue(tmp_path / "parent")
    return init_queue.SessionLink("s1", parent.resolve()), {_paths.SESSIONS_ENV: str(tmp_path / "sessions")}


def test_session_writes_the_session_table(tmp_path):
    link, env = _session(tmp_path)
    root = init_queue.init_queue(tmp_path / "child", session=link, env=env)
    session = config.load_config(root)["session"]
    assert session == {"id": "s1", "lifecycle": "session", "fold_to": str(link.fold_to)}


def test_session_binds_the_queue(tmp_path):
    link, env = _session(tmp_path)
    root = init_queue.init_queue(tmp_path / "child", session=link, env=env)
    assert _paths.resolve_root(env={**env, _paths.SESSION_ENV: "s1"}, cwd=tmp_path) == root.resolve()


def test_fold_to_must_be_a_queue(tmp_path):
    link = init_queue.SessionLink("s1", tmp_path / "nowhere")
    with pytest.raises(SystemExit, match="No task queue"):
        init_queue.init_queue(tmp_path / "child", session=link, env={})


def test_session_and_fold_to_go_together(tmp_path):
    with pytest.raises(SystemExit):
        init_queue.main([str(tmp_path / "q"), "--session", "s1"])

"""Tests for fold_session.py — a session-linked queue folds its open briefs into its parent."""
import subprocess

import _paths
import config
import fold_session
import git_sync
import init_queue
import pytest


@pytest.fixture
def queues(tmp_path):
    env = {_paths.SESSIONS_ENV: str(tmp_path / "sessions")}
    parent = init_queue.init_queue(tmp_path / "parent")
    link = init_queue.SessionLink("s1", parent.resolve())
    child = init_queue.init_queue(tmp_path / "child", session=link, env=env)
    for status, name in (("ready", "a"), ("blocked", "b"), ("done", "c")):
        (child / status / f"{name}.md").write_text(f"---\nid: {name}\nstatus: {status}\n---\nbody {name}\n")
    return parent, child, env


def test_open_briefs_keep_their_status(queues):
    parent, child, env = queues
    fold_session.SessionQueue.load(child).fold(env)
    assert (parent / "ready" / "a.md").read_text().endswith("body a\n")
    assert (parent / "blocked" / "b.md").read_text().endswith("body b\n")


def test_a_pointer_stub_stays_behind(queues):
    parent, child, env = queues
    fold_session.SessionQueue.load(child).fold(env)
    assert f"moved_to: {parent.resolve() / 'ready' / 'a.md'}" in (child / "ready" / "a.md").read_text()


def test_done_briefs_stay(queues):
    parent, child, env = queues
    fold_session.SessionQueue.load(child).fold(env)
    assert not (parent / "done" / "c.md").exists()
    assert (child / "done" / "c.md").read_text().endswith("body c\n")


def test_lifecycle_becomes_folded_and_binding_goes(queues):
    _, child, env = queues
    fold_session.SessionQueue.load(child).fold(env)
    assert config.load_config(child)["session"]["lifecycle"] == "folded"
    assert not _paths.binding_path("s1", env).exists()


def test_a_second_fold_moves_nothing(queues):
    parent, child, env = queues
    fold_session.SessionQueue.load(child).fold(env)
    assert fold_session.SessionQueue.load(child).fold(env) == []
    assert not (parent / "ready" / "a-2.md").exists()


def test_a_name_clash_in_the_parent_is_bumped(queues):
    parent, child, env = queues
    (parent / "ready" / "a.md").write_text("theirs\n")
    fold_session.SessionQueue.load(child).fold(env)
    assert (parent / "ready" / "a.md").read_text() == "theirs\n"
    assert (parent / "ready" / "a-2.md").read_text().endswith("body a\n")


def test_a_body_line_naming_moved_to_is_still_a_brief(queues):
    parent, child, env = queues
    (child / "ready" / "d.md").write_text("---\nid: d\n---\nmoved_to: nowhere\n")
    fold_session.SessionQueue.load(child).fold(env)
    assert (parent / "ready" / "d.md").exists()


def test_a_hand_edited_lifecycle_still_folds(queues):
    _, child, env = queues
    config_path = child / "tasks.toml"
    config_path.write_text(config_path.read_text().replace('lifecycle = "session"', 'lifecycle="session"'))
    fold_session.SessionQueue.load(child).fold(env)
    assert config.load_config(child)["session"]["lifecycle"] == "folded"


def test_an_invalid_session_id_moves_nothing(queues):
    parent, child, env = queues
    toml = child / "tasks.toml"
    toml.write_text(toml.read_text().replace('id = "s1"', 'id = "../x"'))
    with pytest.raises(ValueError):
        fold_session.SessionQueue.load(child).fold(env)
    assert not (parent / "ready" / "a.md").exists()


def test_refuses_a_queue_with_no_session(tmp_path):
    root = init_queue.init_queue(tmp_path / "plain")
    with pytest.raises(SystemExit, match="not session-linked"):
        fold_session.SessionQueue.load(root)


def test_main_reports_the_count(queues, capsys, monkeypatch):
    _, child, env = queues
    monkeypatch.setenv(_paths.SESSIONS_ENV, env[_paths.SESSIONS_ENV])
    fold_session.main([str(child)])
    assert "Folded 2 brief(s)" in capsys.readouterr().out


def test_owns_repo_only_at_the_repo_top(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".tasks").mkdir()
    assert git_sync.owns_repo(tmp_path)
    assert not git_sync.owns_repo(tmp_path / ".tasks")

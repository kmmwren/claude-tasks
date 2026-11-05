"""Tests for fold_session.py — a session-linked queue folds its open briefs into its parent."""
import contextlib
import subprocess

import _paths
import config
import deps
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


def test_each_queue_that_owns_its_repo_is_committed(queues, monkeypatch):
    parent, child, env = queues
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    commits = []
    monkeypatch.setattr(fold_session, "sync_commit", lambda message, root: commits.append((message, root)))
    fold_session.SessionQueue.load(child).fold(env)
    assert commits == [("fold session s1: 2 brief(s) into parent", parent.resolve())]


def test_owns_repo_only_at_the_repo_top(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".tasks").mkdir()
    assert git_sync.owns_repo(tmp_path)
    assert not git_sync.owns_repo(tmp_path / ".tasks")


def test_a_lock_timeout_aborts_the_fold_with_nothing_moved(queues, monkeypatch):
    parent, child, env = queues

    @contextlib.contextmanager
    def timed_out(root):
        yield False

    monkeypatch.setattr(fold_session, "repo_lock", timed_out)
    with pytest.raises(SystemExit, match="lock"):
        fold_session.SessionQueue.load(child).fold(env)
    assert not (parent / "ready" / "a.md").exists()
    assert (child / "ready" / "a.md").read_text().endswith("body a\n")
    assert config.load_config(child)["session"]["lifecycle"] == "session"
    assert _paths.binding_path("s1", env).exists()


def test_a_bumped_brief_gets_its_id_rewritten(queues):
    parent, child, env = queues
    (parent / "ready" / "a.md").write_text("theirs\n")
    fold_session.SessionQueue.load(child).fold(env)
    assert deps.fm((parent / "ready" / "a-2.md").read_text())["id"] == "a-2"


def test_deps_among_folded_briefs_follow_a_bumped_id(queues):
    parent, child, env = queues
    (parent / "ready" / "a.md").write_text("theirs\n")
    (child / "blocked" / "b.md").write_text(
        "---\nid: b\nblocked_by: [a, external:vendor]\ntags: [x, depends:a]\nblockers: waiting on a, alpha\n---\nbody b\n"
    )
    fold_session.SessionQueue.load(child).fold(env)
    fields = deps.fm((parent / "blocked" / "b.md").read_text())
    assert fields["blocked_by"] == "[a-2, external:vendor]"
    assert fields["tags"] == "[x, depends:a-2]"
    assert fields["blockers"] == "waiting on a-2, alpha"


def test_a_brief_without_frontmatter_moves_unchanged_when_ids_are_bumped(queues):
    parent, child, env = queues
    (parent / "ready" / "a.md").write_text("theirs\n")
    (child / "ready" / "plain.md").write_text("no frontmatter\n")
    fold_session.SessionQueue.load(child).fold(env)
    assert (parent / "ready" / "plain.md").read_text() == "no frontmatter\n"


def _parent_unmet(parent, brief_id):
    queue = deps.load_queue(parent)
    done = {b.id for b in queue if b.status == "done"}
    return deps.unmet(next(b for b in queue if b.id == brief_id), done)


def test_a_done_prerequisite_left_in_the_child_stays_satisfied(queues):
    parent, child, env = queues
    (child / "ready" / "a.md").write_text("---\nid: a\nblocked_by: [c]\n---\nbody a\n")
    fold_session.SessionQueue.load(child).fold(env)
    assert _parent_unmet(parent, "a") == []
    assert (child / "done" / "c.md").exists()


def test_a_done_prerequisite_is_carried_under_a_free_name_with_its_id(queues):
    parent, child, env = queues
    (child / "ready" / "a.md").write_text("---\nid: a\ntags: [depends:c]\n---\nbody a\n")
    (parent / "done" / "c.md").write_text("---\nid: other\n---\n")
    fold_session.SessionQueue.load(child).fold(env)
    assert _parent_unmet(parent, "a") == []
    assert (parent / "done" / "c.md").read_text() == "---\nid: other\n---\n"


def test_a_prerequisite_the_parent_already_has_is_not_copied(queues):
    parent, child, env = queues
    (child / "ready" / "a.md").write_text("---\nid: a\nblocked_by: [c]\n---\nbody a\n")
    (parent / "ready" / "c.md").write_text("---\nid: c\n---\nlive in parent\n")
    fold_session.SessionQueue.load(child).fold(env)
    assert not (parent / "done" / "c.md").exists()
    assert _parent_unmet(parent, "a") == ["c"]

"""Tests for _paths.py — resolving which queue directory the scripts operate on.

The scripts live in the plugin; the queue is the user's data elsewhere. resolve_root
decides where, from (1) an env var, (2) a project-local .tasks/ found by walking up, or
(3) the ~/tasks default. require_queue refuses to operate on an uninitialised directory.
"""
import _paths
import pytest


def test_public_constants_are_the_documented_contract():
    # These are the user-facing contract (env var, marker dir, default location, config
    # filename, lifecycle folders). Pin them to literals so a change can't slip through.
    assert _paths.ENV_VAR == "CLAUDE_TASKS_DIR"
    assert _paths.PROJECT_MARKER == ".tasks"
    assert _paths.DEFAULT_ROOT == "~/tasks"
    assert _paths.CONFIG_FILE == "tasks.toml"
    assert _paths.TASK_DIRS == ("inbox", "ready", "in-progress", "done", "parked")


def test_env_var_wins(tmp_path):
    target = tmp_path / "somewhere"
    root = _paths.resolve_root(env={_paths.ENV_VAR: str(target)}, cwd=tmp_path)
    assert root == target


def test_env_var_expands_user():
    root = _paths.resolve_root(env={_paths.ENV_VAR: "~/myqueue"}, cwd="/tmp")
    assert root == _paths.pathlib.Path("~/myqueue").expanduser()


def test_walks_up_to_project_marker(tmp_path):
    (tmp_path / _paths.PROJECT_MARKER).mkdir()
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    root = _paths.resolve_root(env={}, cwd=deep)
    assert root == tmp_path / _paths.PROJECT_MARKER


def test_marker_in_cwd_itself(tmp_path):
    (tmp_path / _paths.PROJECT_MARKER).mkdir()
    root = _paths.resolve_root(env={}, cwd=tmp_path)
    assert root == tmp_path / _paths.PROJECT_MARKER


def test_falls_back_to_default(tmp_path):
    # nothing in env, no .tasks/ anywhere up the tree → the ~/tasks default
    root = _paths.resolve_root(env={}, cwd=tmp_path)
    assert root == _paths.pathlib.Path(_paths.DEFAULT_ROOT).expanduser()


def test_empty_env_var_ignored(tmp_path):
    # an empty string must not be treated as a real override
    root = _paths.resolve_root(env={_paths.ENV_VAR: ""}, cwd=tmp_path)
    assert root == _paths.pathlib.Path(_paths.DEFAULT_ROOT).expanduser()


def test_is_queue_true_when_config_present(tmp_path):
    (tmp_path / _paths.CONFIG_FILE).write_text("name = 'x'\n")
    assert _paths.is_queue(tmp_path) is True


def test_is_queue_false_when_absent(tmp_path):
    assert _paths.is_queue(tmp_path) is False


def test_require_queue_returns_root_when_initialised(tmp_path):
    (tmp_path / _paths.CONFIG_FILE).write_text("name = 'x'\n")
    assert _paths.require_queue(tmp_path) == tmp_path


def test_require_queue_raises_with_guidance(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _paths.require_queue(tmp_path)
    msg = str(exc.value)
    assert msg.startswith("No task queue at")  # names the missing queue
    assert msg.rstrip().endswith("to create one.")  # points at the fix
    assert "/tasks-init" in msg


# --------------------------------------------------------------------------- #
# strip_comment — the shared frontmatter value cleaner
# --------------------------------------------------------------------------- #
def test_strip_comment_drops_a_trailing_comment():
    assert _paths.strip_comment("ready # inbox | ready | done") == "ready"


def test_strip_comment_treats_a_whole_comment_as_empty():
    assert _paths.strip_comment("# OPTIONAL who has claimed this. Empty = unclaimed.") == ""


def test_strip_comment_leaves_a_plain_value_untouched():
    assert _paths.strip_comment("alice") == "alice"


def test_strip_comment_keeps_a_hash_with_no_leading_space():
    # a mid-word # is part of the value, not a comment marker
    assert _paths.strip_comment("issue#42") == "issue#42"


def test_strip_comment_keeps_a_hash_inside_a_tag_list():
    assert _paths.strip_comment("[a#b, c#d]") == "[a#b, c#d]"


def test_strip_comment_strips_only_at_the_first_spaced_hash():
    assert _paths.strip_comment("a # b # c") == "a"


def test_strip_comment_handles_a_tab_before_the_hash():
    assert _paths.strip_comment("ready\t# a comment") == "ready"


def test_strip_comment_strips_a_comment_after_a_hash_bearing_value():
    # the value's own # survives; only the spaced-off comment goes
    assert _paths.strip_comment("issue#42 # the tracking id") == "issue#42"


def test_strip_comment_returns_empty_for_an_empty_value():
    assert _paths.strip_comment("") == ""


def test_strip_comment_strips_surrounding_whitespace():
    assert _paths.strip_comment("  ready  ") == "ready"


def test_strip_comment_drops_a_bare_trailing_hash():
    # an empty comment is still a comment
    assert _paths.strip_comment("ready #") == "ready"


def test_strip_comment_reads_a_lone_hash_as_empty():
    assert _paths.strip_comment("#") == ""


def test_strip_comment_keeps_a_leading_hash_value_that_is_indented():
    # a value that is nothing but a comment, however indented, is an empty value
    assert _paths.strip_comment("   # just a comment") == ""

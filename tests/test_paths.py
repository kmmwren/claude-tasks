"""Tests for _paths.py — resolving which queue directory the scripts operate on.

The scripts live in the plugin; the queue is the user's data elsewhere. resolve_root
decides where, from (1) an env var, (2) a project-local .tasks/ found by walking up, or
(3) the ~/tasks default. require_queue refuses to operate on an uninitialised directory.
"""

import io
import time

import _paths
import pytest


def test_public_constants_are_the_documented_contract():
    # These are the user-facing contract (env var, marker dir, default location, config
    # filename, lifecycle folders). Pin them to literals so a change can't slip through.
    assert _paths.ENV_VAR == "CLAUDE_TASKS_DIR"
    assert _paths.PROJECT_MARKER == ".tasks"
    assert _paths.DEFAULT_ROOT == "~/tasks"
    assert _paths.CONFIG_FILE == "tasks.toml"
    assert _paths.TASK_DIRS == ("inbox", "ready", "in-progress", "blocked", "done", "parked")


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


def test_strip_comment_keeps_a_spaced_hash_inside_a_quoted_value_and_drops_the_quotes():
    assert _paths.strip_comment(' "fix PRs (#1, #2)"') == "fix PRs (#1, #2)"
    assert _paths.strip_comment(" 'see #3' # owner note") == "see #3"


def test_strip_comment_treats_an_unclosed_quote_as_plain_text():
    assert _paths.strip_comment(' "half #open') == '"half'


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


# --------------------------------------------------------------------------- #
# session bindings
# --------------------------------------------------------------------------- #
def test_session_constants_are_the_documented_contract():
    assert _paths.SESSION_ENV == "CLAUDE_CODE_SESSION_ID"
    assert _paths.SESSIONS_ENV == "CLAUDE_TASKS_SESSIONS"
    assert _paths.DEFAULT_SESSIONS == "~/.claude-tasks/sessions"


def _bound(tmp_path, root_line):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s1.toml").write_text(root_line)
    return {_paths.SESSIONS_ENV: str(sessions), _paths.SESSION_ENV: "s1"}


def test_bound_session_queue_is_used(tmp_path):
    queue = tmp_path / "q"
    queue.mkdir()
    (queue / _paths.CONFIG_FILE).write_text("")
    env = _bound(tmp_path, f'root = "{queue}"\n')
    assert _paths.resolve_root(env=env, cwd=tmp_path) == queue


def test_env_override_beats_the_binding(tmp_path):
    env = _bound(tmp_path, f'root = "{tmp_path}"\n')
    assert _paths.resolve_root(
        env={**env, _paths.ENV_VAR: "/x"}, cwd=tmp_path
    ) == _paths.pathlib.Path("/x")


@pytest.mark.parametrize("root_line", ['root = "/no/such/queue"\n', "root = [\n", "root = 3\n"])
def test_unusable_binding_falls_through(tmp_path, root_line):
    env = _bound(tmp_path, root_line)
    assert (
        _paths.resolve_root(env=env, cwd=tmp_path)
        == _paths.pathlib.Path(_paths.DEFAULT_ROOT).expanduser()
    )


def test_invalid_session_id_is_ignored(tmp_path):
    env = {_paths.SESSION_ENV: "../etc", _paths.SESSIONS_ENV: str(tmp_path)}
    assert _paths.session_root(env) is None


def test_binding_path_refuses_traversal():
    with pytest.raises(ValueError, match=r"^invalid session id '\.\./x'$"):
        _paths.binding_path("../x", {})


def test_warn_is_one_stderr_line_with_a_prefix_a_caller_can_change(capsys):
    _paths.warn("one thing")
    _paths.warn("another", prefix="prioritise")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["warning: one thing", "prioritise: another"]


@pytest.mark.parametrize(
    "path, expected",
    [
        ("queue/ready/a.md", True),
        ("queue/done/a.md", True),
        ("queue/docs/a.md", False),
        ("queue/ready/a.txt", False),
        ("queue/ready", False),
    ],
)
def test_is_brief_needs_both_a_markdown_file_and_a_state_directory(path, expected):
    assert _paths.is_brief(path) is expected


NOT_UTF8 = b"\xff\xfe\x00bad"


def _folder_with_a_bad_brief(tmp_path):
    folder = tmp_path / "ready"
    folder.mkdir(parents=True)
    (folder / "b.md").write_text("# fine\n")
    (folder / "a.md").write_text("# fine too\n")
    (folder / "bad.md").write_bytes(NOT_UTF8)
    (folder / "notes.txt").write_text("not a brief\n")
    return folder


def test_read_briefs_reads_each_brief_once_and_names_one_that_is_not_utf8_once(tmp_path, capsys):
    folder = _folder_with_a_bad_brief(tmp_path)
    first = _paths.read_briefs(folder)
    second = _paths.read_briefs(folder)
    expected = [
        _paths.BriefFile(folder / "a.md", "# fine too\n"),
        _paths.BriefFile(folder / "b.md", "# fine\n"),
    ]
    assert first == second == expected
    line = f"warning: {folder / 'bad.md'}: is not valid UTF-8; that brief is skipped\n"
    assert capsys.readouterr().err == line


def test_read_briefs_names_a_bad_brief_as_shown(tmp_path, capsys):
    folder = _folder_with_a_bad_brief(tmp_path)
    _paths.read_briefs(folder, shown_as="S/ready")
    assert (
        capsys.readouterr().err
        == "warning: S/ready/bad.md: is not valid UTF-8; that brief is skipped\n"
    )


def test_queue_briefs_reads_a_folder_when_asked_and_names_a_bad_brief_under_the_label(
    tmp_path, capsys
):
    _folder_with_a_bad_brief(tmp_path)
    briefs = _paths.QueueBriefs.read(tmp_path, shown_as="S")
    assert [(d, b.path.name) for d, b in briefs.within(("done", "ready"))] == [
        ("ready", "a.md"),
        ("ready", "b.md"),
    ]
    assert (
        capsys.readouterr().err
        == "warning: S/ready/bad.md: is not valid UTF-8; that brief is skipped\n"
    )


def _spy_on_folder_reads(monkeypatch):
    reads = []
    real = _paths.read_folder

    def spy(folder, shown_as=None, consequence=_paths.SKIPPED):
        reads.append(folder.name)
        time.sleep(0.01)  # wide enough for an unlocked second reader to get in
        return real(folder, shown_as, consequence)

    monkeypatch.setattr(_paths, "read_folder", spy)
    return reads


def test_queue_briefs_reads_nothing_until_asked_and_each_folder_at_most_once(tmp_path, monkeypatch):
    _folder_with_a_bad_brief(tmp_path)
    reads = _spy_on_folder_reads(monkeypatch)
    briefs = _paths.QueueBriefs.read(tmp_path)
    assert reads == []
    list(briefs.within(("ready", "done")))
    list(briefs.within(("done", "ready")))
    assert briefs.folder("ready") is briefs.folder("ready")
    assert reads == ["ready", "done"]


def test_queue_briefs_never_names_a_bad_brief_in_a_folder_nobody_asked_for(tmp_path, capsys):
    _folder_with_a_bad_brief(tmp_path)
    list(_paths.QueueBriefs.read(tmp_path).within(("done", "blocked")))
    assert capsys.readouterr().err == ""


def test_queue_briefs_shared_by_many_threads_reads_a_folder_once(tmp_path, monkeypatch):
    import threading

    _folder_with_a_bad_brief(tmp_path)
    reads = _spy_on_folder_reads(monkeypatch)
    briefs = _paths.QueueBriefs.read(tmp_path)
    threads = [threading.Thread(target=briefs.folder, args=("ready",)) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert reads == ["ready"]


def test_queue_briefs_lists_the_files_it_left_out_folder_by_folder(tmp_path):
    bad = _folder_with_a_bad_brief(tmp_path) / "bad.md"
    (tmp_path / "done").mkdir()
    (tmp_path / "done" / "worse.md").write_bytes(NOT_UTF8)
    briefs = _paths.QueueBriefs.read(tmp_path)
    assert briefs.undecodable(("ready", "done", "blocked")) == [bad, tmp_path / "done" / "worse.md"]
    assert [b.path.name for b in briefs.folder("ready")] == ["a.md", "b.md"]


def test_queue_briefs_warns_with_the_consequence_it_was_given(tmp_path, capsys):
    _folder_with_a_bad_brief(tmp_path)
    _paths.QueueBriefs.read(tmp_path, "S", consequence="nothing is folded").folder("ready")
    assert (
        capsys.readouterr().err
        == "warning: S/ready/bad.md: is not valid UTF-8; nothing is folded\n"
    )


def test_read_brief_names_a_bad_file_by_its_path_unless_told_otherwise(tmp_path, capsys):
    bad = _folder_with_a_bad_brief(tmp_path) / "bad.md"
    assert _paths.read_brief(bad) is None
    assert capsys.readouterr().err == f"warning: {bad}: is not valid UTF-8; that brief is skipped\n"


def test_the_same_message_about_another_copy_of_the_file_is_not_said_again(tmp_path, capsys):
    """A served roll-up reads each request into a fresh directory; the label keeps the warning to one."""
    first, second = (
        _folder_with_a_bad_brief(tmp_path / "1"),
        _folder_with_a_bad_brief(tmp_path / "2"),
    )
    _paths.read_briefs(first, shown_as="S/ready")
    _paths.read_briefs(second, shown_as="S/ready")
    assert capsys.readouterr().err.count("not valid UTF-8") == 1


def test_warn_once_says_a_message_once_until_forgotten(capsys):
    _paths.warn_once("odd", prefix="claude-tasks")
    _paths.warn_once("odd", prefix="claude-tasks")
    _paths.forget_warnings()
    _paths.warn_once("odd", prefix="claude-tasks")
    assert capsys.readouterr().err == "claude-tasks: odd\nclaude-tasks: odd\n"


def test_warn_once_from_many_threads_says_it_once(capsys):
    import threading

    threads = [threading.Thread(target=_paths.warn_once, args=("race",)) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert capsys.readouterr().err == "warning: race\n"


def _stdin_of(monkeypatch, data: bytes) -> None:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(data), encoding="ascii"))


def test_read_stdin_makes_every_line_ending_a_newline_as_text_mode_does(monkeypatch):
    _stdin_of(monkeypatch, "café\r\nb\rc\n".encode())
    assert _paths.read_stdin() == "café\nb\nc\n"


def test_read_stdin_that_is_not_utf8_exits_naming_the_byte(monkeypatch):
    _stdin_of(monkeypatch, b"ok\xff")
    with pytest.raises(SystemExit, match="standard input is not valid UTF-8 .* at byte 2"):
        _paths.read_stdin()

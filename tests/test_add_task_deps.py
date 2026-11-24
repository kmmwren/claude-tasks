"""Tests for add_task.py dependency declarations: --no-deps, --blocked-by, --probe and their refusals."""

import pathlib

import add_task
import init_queue
import pytest
from deps import fm, fm_repeated, parse_list

from tests.queue_helpers import make_queue

BRIEF = ["--goal", "It ships.", "--context", "Because.", "--criteria", "It is live."]


@pytest.fixture
def queue(tmp_path):
    return init_queue.init_queue(tmp_path / "q", name="demo")


def file_brief(queue: pathlib.Path, *extra: str) -> None:
    add_task.main(["--title", "Ship it", "--root", str(queue), "--no-sync", *BRIEF, *extra])


def only(queue: pathlib.Path, folder: str) -> str:
    (path,) = (queue / folder).glob("*.md")
    return path.read_text()


def briefs(queue: pathlib.Path) -> set[pathlib.Path]:
    return set(queue.glob("*/*.md"))


def refused(queue: pathlib.Path, capsys, *extra: str) -> str:
    """The error a refused filing prints; the queue must be left as it was."""
    before = briefs(queue)
    with pytest.raises(SystemExit) as exit_info:
        file_brief(queue, *extra)
    assert exit_info.value.code == 2
    assert briefs(queue) == before
    return capsys.readouterr().err


def test_a_ready_brief_without_a_declaration_is_refused(queue, capsys):
    assert "--no-deps" in refused(queue, capsys, "--ready")


def test_a_blocked_brief_without_a_blocker_is_refused(queue, capsys):
    assert "--blocked-by" in refused(queue, capsys, "--status", "blocked")


def test_no_deps_writes_an_empty_blocked_by(queue):
    file_brief(queue, "--ready", "--no-deps")
    assert fm(only(queue, "ready"))["blocked_by"] == "[]"


def test_blocked_by_files_to_blocked_with_the_blocker_listed(tmp_path):
    root = make_queue(tmp_path, {"dep-1": {"status": "ready"}})
    file_brief(root, "--blocked-by", "dep-1")
    text = only(root, "blocked")
    assert parse_list(fm(text)["blocked_by"]) == ["dep-1"]
    assert fm(text)["status"] == "blocked"


def test_blocked_by_takes_commas_and_repeats(tmp_path):
    root = make_queue(tmp_path, {"a": {}, "b": {}, "c": {}})
    file_brief(root, "--blocked-by", "a,b", "--blocked-by", "c")
    assert parse_list(fm(only(root, "blocked"))["blocked_by"]) == ["a", "b", "c"]


def test_an_external_blocker_files_with_its_probe(queue):
    file_brief(queue, "--blocked-by", "external:vendor PR", "--probe", "1: true")
    text = only(queue, "blocked")
    assert parse_list(fm(text)["blocked_by"]) == ["external:vendor PR"]
    assert fm_repeated(text)["blocked_probe"] == ["1: true"]


def test_an_external_blocker_without_a_probe_is_refused(queue, capsys):
    assert "no probe" in refused(queue, capsys, "--blocked-by", "external:vendor PR")


def test_a_human_probe_is_refused_until_confirmed_and_accepted_with_it(queue, capsys):
    external = ["--blocked-by", "external:sign-off", "--probe", "1: human"]
    assert "no probe" in refused(queue, capsys, *external)
    file_brief(queue, *external, "--confirmed", "1: 2026-11-01 sam")
    assert fm_repeated(only(queue, "blocked"))["blocked_confirmed"] == ["1: 2026-11-01 sam"]


def test_an_unknown_blocker_id_is_refused_and_files_nothing(queue, capsys):
    assert "unknown blocker id(s) ['nope']" in refused(queue, capsys, "--blocked-by", "nope")


def test_blocked_by_and_no_deps_contradict(tmp_path, capsys):
    root = make_queue(tmp_path, {"dep-1": {}})
    assert "contradict" in refused(root, capsys, "--blocked-by", "dep-1", "--no-deps")


def test_a_blocked_brief_cannot_be_forced_into_ready(tmp_path, capsys):
    root = make_queue(tmp_path, {"dep-1": {}})
    assert "files to blocked/" in refused(root, capsys, "--ready", "--blocked-by", "dep-1")


def test_an_inbox_capture_may_leave_the_declaration_to_triage(queue):
    add_task.main(["--title", "Idea", "--root", str(queue), "--no-sync"])
    assert "blocked_by" not in fm(only(queue, "inbox"))


def test_a_date_blocker_files_to_blocked_without_a_probe(queue):
    file_brief(queue, "--blocked-by", "date:2026-12-01")
    assert parse_list(fm(only(queue, "blocked"))["blocked_by"]) == ["date:2026-12-01"]


def test_a_malformed_date_blocker_is_refused(queue, capsys):
    assert "date:YYYY-MM-DD" in refused(queue, capsys, "--blocked-by", "date:next week")

"""Tests for triage_sources.py: what counts as a citable source, whether it exists, and grounding."""
import pathlib
import subprocess

import pytest
import triage_sources
from triage_sources import LiveSources, cited_sources, gh_resolves, grounded, specific_path


@pytest.mark.parametrize("text, source", [
    ("see https://github.com/o/r/pull/7.", "https://github.com/o/r/pull/7"),
    ("lives at ~/.claude/hooks/gate.py", "~/.claude/hooks/gate.py"),
    ("landed as kmmwren/claude-tasks#24", "kmmwren/claude-tasks#24"),
    ("tracked in ABC-42", "ABC-42"),
    ("edit build_anatomy.py first", "build_anatomy.py"),
])
def test_each_kind_of_source_is_found(text, source):
    assert source in cited_sources(text)


def test_encodings_shaped_like_ticket_keys_are_not_sources():
    assert cited_sources("UTF-8 and SHA-256 and ISO-8601") == set()


def test_generated_text_citing_a_live_source_from_the_original_is_grounded():
    assert grounded("Start from o/r#3.", "Filed against ABC-42 and o/r#3.", {"o/r#3"}.__contains__)


def test_generated_text_citing_only_dead_sources_is_not_grounded():
    assert not grounded("Start from ABC-42.", "Filed against ABC-42.", {"o/r#3"}.__contains__)


def test_generated_text_citing_nothing_is_not_grounded():
    assert not grounded("Make it better.", "Filed against ABC-42.", lambda _: True)


def test_an_invented_reference_fails_even_beside_a_real_one():
    assert not grounded("See ABC-42 and ABC-43.", "Filed against ABC-42.", lambda _: True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    (tmp_path / ".claude/skills/enrich").mkdir(parents=True)
    (tmp_path / ".claude/CLAUDE.md").write_text("x")
    return tmp_path


@pytest.mark.parametrize("rel, ok", [
    (".claude", False),
    (".claude/skills", False),
    (".claude/skills/enrich", True),
    (".claude/CLAUDE.md", True),
    (".claude/missing.md", False),
])
def test_a_path_counts_when_it_is_a_file_or_a_directory_below_a_generic_root(home, rel, ok):
    assert specific_path(home / rel) is ok


def test_a_path_outside_home_is_judged_by_its_depth_from_the_filesystem_root(home):
    assert not specific_path(pathlib.Path("/tmp"))


class FakeGh:
    def __init__(self, live: set[str]) -> None:
        self.live, self.calls = live, list[str]()

    def __call__(self, api_path: str) -> bool:
        self.calls.append(api_path)
        return api_path in self.live


@pytest.fixture
def queue(tmp_path):
    (tmp_path / "ready").mkdir()
    (tmp_path / "ready/known.md").write_text("x")
    return tmp_path


@pytest.mark.parametrize("source, ok", [
    ("https://github.com/o/r/pull/7", True),
    ("o/r#8", False),
    ("https://github.com/o/r/tree/feat/x/docs/a.md", True),
    ("https://github.com/o/r/blob/gone/a.md", False),
    ("https://github.com/o/r", False),
    ("ready/known.md", True),
    ("inbox/unknown.md", False),
    ("ABC-42", False),
    ("build_anatomy.py", False),
])
def test_live_sources_checks_each_kind_against_its_own_authority(queue, home, source, ok):
    gh = FakeGh({"repos/o/r/issues/7", "repos/o/r/branches/feat/x"})
    assert LiveSources(queue, gh)(source) is ok


def test_a_local_path_is_checked_on_disk(queue, home):
    assert LiveSources(queue, FakeGh(set()))("~/.claude/CLAUDE.md")


def test_each_source_is_checked_once_per_run(queue):
    gh = FakeGh({"repos/o/r/issues/7"})
    live = LiveSources(queue, gh)
    live("o/r#7"), live("o/r#7")
    assert gh.calls == ["repos/o/r/issues/7"]


def test_gh_resolves_reads_the_exit_code(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert gh_resolves("repos/o/r")


def test_gh_resolves_treats_a_missing_gh_as_not_found(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no gh")
    monkeypatch.setattr(triage_sources.subprocess, "run", boom)
    assert not gh_resolves("repos/o/r")

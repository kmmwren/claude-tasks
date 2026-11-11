"""Tests for triage_sources.py: what counts as a citable source, whether it exists, and grounding."""

import pathlib
import subprocess

import pytest
import triage_sources
from triage_sources import (
    LiveSources,
    PrState,
    cited_sources,
    gh_resolves,
    grounded,
    pr_state,
    specific_path,
)


@pytest.mark.parametrize(
    "text, source",
    [
        ("see https://github.com/o/r/pull/7.", "https://github.com/o/r/pull/7"),
        ("lives at ~/.claude/hooks/gate.py", "~/.claude/hooks/gate.py"),
        ("landed as kmmwren/claude-tasks#24", "kmmwren/claude-tasks#24"),
        ("tracked in ABC-42", "ABC-42"),
        ("edit build_anatomy.py first", "build_anatomy.py"),
    ],
)
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
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    (tmp_path / ".claude/skills/enrich").mkdir(parents=True)
    (tmp_path / ".claude/CLAUDE.md").write_text("x")
    return tmp_path


@pytest.mark.parametrize(
    "rel, ok",
    [
        (".claude", False),
        (".claude/skills", False),
        (".claude/skills/enrich", True),
        (".claude/CLAUDE.md", True),
        (".claude/missing.md", False),
    ],
)
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


class FakePrs:
    """`gh pr view` answers by (repo, number); anything unlisted is unverified."""

    def __init__(self, states: dict[tuple[str, int], PrState] | None = None) -> None:
        self.states, self.calls = states or {}, list[tuple[str, int]]()

    def __call__(self, repo: str, number: int) -> PrState | None:
        self.calls.append((repo, number))
        return self.states.get((repo, number))


@pytest.fixture
def queue(tmp_path):
    (tmp_path / "ready").mkdir()
    (tmp_path / "ready/known.md").write_text("x")
    return tmp_path


@pytest.mark.parametrize(
    "source, ok",
    [
        ("https://github.com/o/r/pull/7", True),
        ("o/r#8", False),
        ("https://github.com/o/r/tree/feat/x/docs/a.md", True),
        ("https://github.com/o/r/blob/gone/a.md", False),
        ("https://github.com/o/r", False),
        ("ready/known.md", True),
        ("inbox/unknown.md", False),
        ("ABC-42", False),
        ("build_anatomy.py", False),
    ],
)
def test_live_sources_checks_each_kind_against_its_own_authority(queue, home, source, ok):
    gh = FakeGh({"repos/o/r/issues/7", "repos/o/r/branches/feat/x"})
    assert LiveSources(queue, gh, FakePrs({("o/r", 7): PrState.OPEN}))(source) is ok


def test_a_local_path_is_checked_on_disk(queue, home):
    assert LiveSources(queue, FakeGh(set()))("~/.claude/CLAUDE.md")


def test_each_source_is_checked_once_per_run(queue):
    prs = FakePrs({("o/r", 7): PrState.OPEN})
    live = LiveSources(queue, FakeGh({"repos/o/r/issues/7"}), prs)
    live("o/r#7"), live("o/r#7")
    assert prs.calls == [("o/r", 7)]


@pytest.mark.parametrize("source", ["https://github.com/o/r/pull/7", "o/r#7"])
@pytest.mark.parametrize(
    "state, ok",
    [(PrState.OPEN, True), (PrState.MERGED, False), (PrState.CLOSED, False), (None, False)],
)
def test_a_pr_counts_only_while_open_and_an_unverified_one_never_counts(queue, source, state, ok):
    prs = FakePrs({("o/r", 7): state} if state else {})
    assert LiveSources(queue, FakeGh({"repos/o/r/issues/7"}), prs)(source) is ok


def test_a_number_that_is_not_a_pr_falls_back_to_the_issue_check(queue):
    prs = FakePrs({("o/r", 7): PrState.NOT_A_PR})
    assert LiveSources(queue, FakeGh({"repos/o/r/issues/7"}), prs)("o/r#7")
    assert not LiveSources(queue, FakeGh(set()), prs)("o/r#7")
    assert not LiveSources(queue, FakeGh({"repos/o/r/issues/7"}), prs)("https://github.com/o/r/pull/7")


def _gh_answers(monkeypatch, returncode, out="", err=""):
    monkeypatch.setattr(
        triage_sources.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode, out, err),
    )


@pytest.mark.parametrize(
    "out, state",
    [("OPEN\n", PrState.OPEN), ("MERGED\n", PrState.MERGED), ("CLOSED\n", PrState.CLOSED)],
)
def test_pr_state_reads_the_state_gh_prints(monkeypatch, out, state):
    _gh_answers(monkeypatch, 0, out)
    assert pr_state("o/r", 7) is state


def test_pr_state_tells_a_plain_issue_from_a_failure(monkeypatch):
    _gh_answers(monkeypatch, 1, err="GraphQL: Could not resolve to a PullRequest with the number of 7.")
    assert pr_state("o/r", 7) is PrState.NOT_A_PR
    _gh_answers(monkeypatch, 1, err="error connecting to api.github.com")
    assert pr_state("o/r", 7) is None
    _gh_answers(monkeypatch, 0, "WEIRD\n")
    assert pr_state("o/r", 7) is None


def test_pr_state_treats_a_missing_gh_as_unverified(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no gh")

    monkeypatch.setattr(triage_sources.subprocess, "run", boom)
    assert pr_state("o/r", 7) is None


def test_gh_resolves_reads_the_exit_code(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert gh_resolves("repos/o/r")


def test_gh_resolves_treats_a_missing_gh_as_not_found(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no gh")

    monkeypatch.setattr(triage_sources.subprocess, "run", boom)
    assert not gh_resolves("repos/o/r")

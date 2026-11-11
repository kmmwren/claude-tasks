"""Tests for liveness.py: a brief whose cited PRs are all merged or closed is dead."""

import json

import liveness
import pytest
from liveness import Liveness, PrRef, cited_prs
from triage_sources import PrState

OPEN, MERGED, CLOSED = PrState.OPEN, PrState.MERGED, PrState.CLOSED


def brief(*cites: str, title: str = "T") -> str:
    body = "\n".join(f"Land {c}" for c in cites)
    return (
        f"---\nid: x\ntitle: {title}\n---\n\n## Goal\n\n{body}\n\n## Execution log\n\nsee o/r#999\n"
    )


class Gh:
    """A pr_state stand-in that records every lookup."""

    def __init__(self, states: dict[str, PrState | None]) -> None:
        self.states, self.calls = states, []

    def __call__(self, repo: str, number: int) -> PrState | None:
        self.calls.append(f"{repo}#{number}")
        return self.states.get(f"{repo}#{number}")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def warnings():
    return []


def make(tmp_path, states, warnings, *, gh_present=True, clock=None, ttl=600):
    gh = Gh(states)
    live = Liveness(
        tmp_path / "state" / "pr-state.json",
        lookup=gh,
        gh_available=lambda: gh_present,
        ttl=ttl,
        clock=clock or Clock(),
        warn=warnings.append,
    )
    return live, gh


def test_cited_prs_reads_urls_and_owner_repo_refs_and_ignores_the_execution_log():
    text = brief("https://github.com/Panaseer/pres-api/pull/5300", "kmmwren/claude-tasks#24")
    assert cited_prs(text) == {PrRef("panaseer/pres-api", 5300), PrRef("kmmwren/claude-tasks", 24)}


def test_cited_prs_includes_a_pr_named_in_the_title():
    assert cited_prs(brief(title="sync o/r#4947 with base")) == {PrRef("o/r", 4947)}


def test_a_brief_citing_no_pr_is_alive_and_costs_no_lookup(tmp_path, warnings):
    live, gh = make(tmp_path, {}, warnings)
    assert live.dead({"a": brief("~/.claude/hooks/gate.py")}) == set()
    assert gh.calls == []


@pytest.mark.parametrize(
    "states, dead",
    [
        ({"o/r#1": MERGED}, True),
        ({"o/r#1": CLOSED}, True),
        ({"o/r#1": MERGED, "o/r#2": CLOSED}, True),
        ({"o/r#1": MERGED, "o/r#2": OPEN}, False),
        ({"o/r#1": OPEN}, False),
        ({"o/r#1": MERGED, "o/r#2": PrState.NOT_A_PR}, True),
        ({"o/r#1": PrState.NOT_A_PR, "o/r#2": PrState.NOT_A_PR}, False),
    ],
)
def test_a_brief_is_dead_only_when_every_cited_pr_is_merged_or_closed(
    tmp_path, warnings, states, dead
):
    live, _ = make(tmp_path, states, warnings)
    cites = [f"{key}" for key in states]
    assert (live.dead({"a": brief(*cites)}) == {"a"}) is dead
    assert warnings == []


def test_a_pr_that_could_not_be_read_keeps_the_brief_and_warns_once(tmp_path, warnings):
    live, _ = make(tmp_path, {"o/r#1": MERGED, "o/r#2": None, "o/r#3": None}, warnings)
    texts = {"a": brief("o/r#1", "o/r#2"), "b": brief("o/r#3"), "c": brief("o/r#1")}
    assert live.dead(texts) == {"c"}
    assert len(warnings) == 1
    assert "2 cited PRs" in warnings[0] and "keeping" in warnings[0]
    live.dead(texts)
    assert len(warnings) == 1


def test_a_missing_gh_keeps_every_brief_warns_once_and_makes_no_lookup(tmp_path, warnings):
    live, gh = make(tmp_path, {"o/r#1": MERGED}, warnings, gh_present=False)
    assert live.dead({"a": brief("o/r#1")}) == set()
    assert gh.calls == []
    assert len(warnings) == 1 and "gh" in warnings[0]


def test_each_pr_is_looked_up_once_however_many_briefs_cite_it(tmp_path, warnings):
    live, gh = make(tmp_path, {"o/r#1": MERGED}, warnings)
    assert live.dead({"a": brief("o/r#1"), "b": brief("O/R#1")}) == {"a", "b"}
    assert gh.calls == ["o/r#1"]


def test_a_fresh_cached_state_is_not_looked_up_again_across_runs(tmp_path, warnings):
    live, gh = make(tmp_path, {"o/r#1": MERGED}, warnings)
    live.dead({"a": brief("o/r#1")})
    again, gh2 = make(tmp_path, {}, warnings)
    assert again.dead({"a": brief("o/r#1")}) == {"a"}
    assert gh2.calls == []


def test_a_stale_cached_state_is_looked_up_again(tmp_path, warnings):
    clock = Clock()
    live, gh = make(tmp_path, {"o/r#1": OPEN}, warnings, clock=clock, ttl=600)
    live.dead({"a": brief("o/r#1")})
    clock.now += 601
    again, gh2 = make(tmp_path, {"o/r#1": MERGED}, warnings, clock=clock, ttl=600)
    assert again.dead({"a": brief("o/r#1")}) == {"a"}
    assert gh2.calls == ["o/r#1"]


def test_an_unreadable_state_is_never_cached(tmp_path, warnings):
    live, _ = make(tmp_path, {"o/r#1": None}, warnings)
    live.dead({"a": brief("o/r#1")})
    again, gh2 = make(tmp_path, {"o/r#1": MERGED}, warnings)
    assert again.dead({"a": brief("o/r#1")}) == {"a"}
    assert gh2.calls == ["o/r#1"]


@pytest.mark.parametrize(
    "junk", ["{not json", "[]", '{"o/r#1": "MERGED"}', '{"o/r#1": {"state": "BOGUS", "at": 999}}']
)
def test_a_damaged_cache_is_ignored(tmp_path, warnings, junk):
    live, gh = make(tmp_path, {"o/r#1": MERGED}, warnings)
    live.cache_file.parent.mkdir(parents=True)
    live.cache_file.write_text(junk)
    assert live.dead({"a": brief("o/r#1")}) == {"a"}
    assert gh.calls == ["o/r#1"]


def test_the_cache_records_state_and_time(tmp_path, warnings):
    live, _ = make(tmp_path, {"o/r#1": MERGED}, warnings)
    live.dead({"a": brief("o/r#1")})
    assert json.loads(live.cache_file.read_text()) == {"o/r#1": {"state": "MERGED", "at": 1000.0}}


def test_an_unwritable_cache_does_not_fail_the_run(tmp_path, warnings):
    live, _ = make(tmp_path, {"o/r#1": MERGED}, warnings)
    (tmp_path / "state").write_text("a file where the directory should be")
    assert live.dead({"a": brief("o/r#1")}) == {"a"}


def test_for_queue_keeps_its_cache_in_the_queue_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_TASKS_STATE", str(tmp_path / "st"))
    root = tmp_path / "q"
    root.mkdir()
    assert Liveness.for_queue(root).cache_file.parent.parent == tmp_path / "st"


def test_the_default_gh_check_looks_for_gh_on_the_path(monkeypatch):
    monkeypatch.setattr(liveness.shutil, "which", lambda name: "/bin/gh" if name == "gh" else None)
    assert liveness.gh_on_path() is True
    monkeypatch.setattr(liveness.shutil, "which", lambda name: None)
    assert liveness.gh_on_path() is False


def test_the_default_warn_writes_one_line_to_stderr(capsys):
    liveness.warn("careful")
    assert capsys.readouterr().err == "prioritise: careful\n"

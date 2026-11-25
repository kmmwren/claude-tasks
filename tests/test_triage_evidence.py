"""Tests for triage_evidence.py: when a brief's own evidence says its work is finished."""

import pathlib
import threading

import pytest
from triage_brief import InboxBrief
from triage_evidence import (
    PrStates,
    PullRef,
    cited_pulls,
    closing_evidence,
    deliverable_pulls,
)
from triage_sources import PrState

from tests.queue_helpers import inbox_text

SEEN = {
    ("o/r", 7): PrState.MERGED,
    ("o/r", 8): PrState.CLOSED,
    ("o/r", 9): PrState.OPEN,
    ("o/r", 11): PrState.NOT_A_PR,
}


class Recorded:
    """A fetch over a fixed table that records every call."""

    def __init__(self, table=None):
        self.table, self.calls = table or SEEN, []

    def __call__(self, repo, number):
        self.calls.append((repo, number))
        return self.table.get((repo, number))


def _brief(**kw):
    return InboxBrief(pathlib.Path("x.md"), inbox_text("x", **kw))


def test_cited_pulls_reads_urls_slugs_and_urls_with_a_suffix():
    brief = _brief(
        context="See https://github.com/o/r/pull/7/files and p/q#3, not https://github.com/o/r/issues/4."
    )
    assert cited_pulls(brief) == {PullRef("o/r", 7), PullRef("p/q", 3)}


def test_a_pull_ref_prints_as_owner_repo_hash_number():
    assert str(PullRef("o/r", 7)) == "o/r#7"


def test_a_brief_naming_its_pr_in_the_title_is_about_it():
    brief = _brief(title="Merge o/r#7", context="PR https://github.com/o/r/pull/7")
    assert deliverable_pulls(brief) == {PullRef("o/r", 7)}


def test_a_brief_naming_its_pr_in_the_goal_is_about_it():
    brief = _brief(title="Ship it", goal="Land PR #7 in main.", context="o/r#7")
    assert deliverable_pulls(brief) == {PullRef("o/r", 7)}


def test_a_pr_cited_only_as_context_is_not_the_deliverable():
    brief = _brief(title="Refactor the parser", context="Motivated by o/r#7.")
    assert deliverable_pulls(brief) == frozenset()


@pytest.mark.parametrize(
    "title",
    [
        "Follow up on o/r#7",
        "Rebase o/r#7 after merge",
        "Once o/r#7 is merged, tidy up",
        "Post-merge checks for o/r#7",
        "Merge o/r#7 then announce it",
    ],
)
def test_a_brief_that_looks_past_the_merge_is_not_closed_on_pr_state(title):
    assert deliverable_pulls(_brief(title=title, context="o/r#7")) == frozenset()


def test_a_brief_that_also_cites_an_issue_is_not_closed_on_pr_state():
    brief = _brief(title="Merge o/r#7", context="Fixes https://github.com/o/r/issues/4")
    assert deliverable_pulls(brief) == frozenset()


def test_states_are_read_once_per_pr_whatever_the_casing():
    fetch = Recorded()
    states = PrStates(fetch)
    assert states("o/r", 7) is PrState.MERGED and states("O/R", 7) is PrState.MERGED
    assert fetch.calls == [("o/r", 7)]


def test_prefetch_reads_each_distinct_pr_once_and_fills_the_cache():
    fetch = Recorded()
    states = PrStates(fetch)
    states.prefetch([PullRef("o/r", 7), PullRef("o/r", 7), PullRef("o/r", 8)], jobs=2)
    assert sorted(fetch.calls) == [("o/r", 7), ("o/r", 8)]
    assert states("o/r", 8) is PrState.CLOSED and len(fetch.calls) == 2


def test_prefetch_runs_lookups_in_parallel():
    both = threading.Barrier(2, timeout=5)

    def fetch(repo, number):
        both.wait()
        return PrState.MERGED

    PrStates(fetch).prefetch([PullRef("o/r", 1), PullRef("o/r", 2)], jobs=2)


def test_offline_states_read_nothing_so_nothing_is_evidence():
    assert PrStates.offline()("o/r", 7) is None


def test_every_criterion_ticked_is_evidence():
    brief = _brief(criteria=("It works.", "It ships."), ticked=True)
    assert closing_evidence(brief, PrStates(Recorded())) == "every success criterion is ticked"


def test_an_unticked_criterion_is_no_evidence():
    assert closing_evidence(_brief(criteria=("It works.",)), PrStates(Recorded())) is None


def test_all_deliverable_prs_merged_or_closed_is_evidence():
    brief = _brief(title="Merge o/r#7 and o/r#8", context="o/r#7 o/r#8")
    assert closing_evidence(brief, PrStates(Recorded())) == "o/r#7 MERGED, o/r#8 CLOSED"


@pytest.mark.parametrize("pr", ["o/r#9", "o/r#10", "o/r#11"])
def test_an_open_unreadable_or_not_a_pr_is_no_evidence(pr):
    brief = _brief(title=f"Merge o/r#7 and {pr}", context=f"o/r#7 {pr}")
    assert closing_evidence(brief, PrStates(Recorded())) is None


def test_a_brief_with_no_evidence_links_is_untouched_and_costs_no_lookup():
    fetch = Recorded()
    assert closing_evidence(_brief(title="Plain job"), PrStates(fetch)) is None
    assert fetch.calls == []

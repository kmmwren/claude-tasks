"""Tests for learn.py: skip-above pairs, capped nudges, held-out replay, versioned apply and revert."""
import learn
import pytest
import taste
from learn import Change, Pair, Proposal
from picks import Choice, PickLog, Shown, ShownItem
from taste import Goal, Profile

from tests.queue_helpers import make_queue

CONFIG = {**learn.DEFAULTS, "min_pairs": 4, "holdout": 0.5}
FDO, REL = frozenset({"FDO"}), frozenset({"REL"})
PROFILE = Profile(goals=(Goal("FDO", 1.0), Goal("REL", 1.0)), draft=False, updated="2026-11-01")


def shown(*goal_sets, impression="i1"):
    items = tuple(ShownItem(f"b{n}", tuple(g)) for n, g in enumerate(goal_sets))
    return Shown("all", "v", "judge", items, tuple(i.id for i in items), (), impression, "2026-11-11T09:00:00+00:00")


def test_a_pick_beats_every_brief_shown_above_it():
    assert learn.pairs_for(Choice("pick", "b2", "i1", 2), shown(REL, REL, FDO)) == [Pair(FDO, REL), Pair(FDO, REL)]


def test_a_pick_at_the_top_teaches_nothing():
    assert learn.pairs_for(Choice("bump", "b0", "i1", 0), shown(FDO, REL)) == []


def test_a_skip_loses_to_the_brief_below_it():
    assert learn.pairs_for(Choice("skip", "b0", "i1", 0), shown(REL, FDO)) == [Pair(FDO, REL)]
    assert learn.pairs_for(Choice("skip", "b1", "i1", 1), shown(REL, FDO)) == []


@pytest.mark.parametrize("position", [None, 5])
def test_an_off_list_or_stale_position_teaches_nothing(position):
    assert learn.pairs_for(Choice("pick", "x", "i1", position), shown(FDO)) == []


def test_pairs_join_choices_to_their_lists_and_ignore_unlinked_ones(tmp_path):
    log = PickLog(tmp_path)
    log.append(shown(REL, FDO))
    log.append(Choice("pick", "b1", "i1", 1))
    log.append(Choice("pick", "b1", None, None))
    log.append(Choice("pick", "b1", "gone", 1))
    assert learn.pairs(log) == [Pair(FDO, REL)]


def test_accuracy_counts_ties_as_half():
    assert learn.accuracy({"FDO": 2.0, "REL": 1.0}, [Pair(FDO, REL), Pair(REL, FDO), Pair(FDO, FDO)]) == 0.5


def test_nudges_are_capped_and_floored_and_limited_to_the_strongest_goals():
    train = [Pair(FDO, REL)] * 10 + [Pair(frozenset({"C"}), frozenset())]
    config = {**CONFIG, "max_goals": 2, "floor": 0.9}
    assert learn.nudged({"FDO": 1.0, "REL": 1.0, "C": 1.0, "unseen": 1.0}, train, config) == {
        "FDO": 1.2, "REL": 0.9, "C": 1.0, "unseen": 1.0}


def test_net_preference_ignores_goals_both_sides_share():
    assert learn.net_preference([Pair(FDO | REL, REL), Pair(REL, FDO)]) == {"FDO": 0, "REL": 1}


def test_propose_needs_enough_pairs():
    assert learn.propose(PROFILE, [Pair(FDO, REL)], CONFIG, "2026-11-11") == (
        "only 1 preference pairs; learning starts at 4")


def test_propose_returns_a_validated_reorder_when_picks_favour_one_goal():
    proposal = learn.propose(PROFILE, [Pair(FDO, REL)] * 4, CONFIG, "2026-11-11")
    assert (proposal.changes, proposal.kind, proposal.old_accuracy, proposal.new_accuracy, proposal.held_out) == (
        (Change("FDO", 1.0, 1.1), Change("REL", 1.0, 0.9)), "reorder", 0.5, 1.0, 2)
    assert proposal.from_version == PROFILE.version
    assert proposal.id == "2026-11-11-" + PROFILE.with_weights(proposal.weights(), "2026-11-11").version


def test_a_nudge_that_keeps_the_goal_order_is_not_a_reorder():
    profile = Profile(goals=(Goal("FDO", 2.0), Goal("REL", 1.0)))
    assert learn.propose(profile, [Pair(FDO, REL)] * 4, CONFIG, "2026-11-11").kind == "nudge"


def test_propose_reports_no_preference():
    assert learn.propose(PROFILE, [Pair(FDO, FDO)] * 4, CONFIG, "d") == "the picks do not favour any goal over another"


def test_propose_refuses_a_change_that_replays_worse_on_held_out_picks():
    pairs = [Pair(FDO, REL)] * 2 + [Pair(REL, FDO)] * 2
    assert learn.propose(PROFILE, pairs, CONFIG, "d") == (
        "the reorder did not replay well enough on held-out picks (0.50 -> 0.00)")


@pytest.mark.parametrize("kind, before, after, expected", [
    ("reorder", 0.5, 0.6, True), ("reorder", 0.5, 0.5, False), ("nudge", 0.5, 0.5, True), ("nudge", 0.5, 0.4, False),
])
def test_a_reorder_must_replay_better_and_a_nudge_no_worse(kind, before, after, expected):
    assert learn.validated(kind, before, after) is expected


def test_splitting_a_tie_changes_the_order():
    assert learn.ordering({"A": 1.0, "B": 1.0}) != learn.ordering({"A": 1.1, "B": 0.9})
    assert learn.ordering({"A": 2.0, "B": 1.0}) == learn.ordering({"A": 2.2, "B": 0.9})


def test_describe_lists_each_change():
    proposal = Proposal("p1", "v0", (Change("FDO", 1.0, 1.1),), "nudge", 0.5, 1.0, 2)
    assert proposal.describe() == ("proposal p1 (nudge; held-out accuracy 0.50 -> 1.00 over 2 pairs; proposed)\n"
                                   "  FDO: 1 -> 1.1")


@pytest.fixture
def root(tmp_path):
    queue = make_queue(tmp_path, {})
    taste.profile_path(queue).parent.mkdir()
    taste.profile_path(queue).write_text(taste.render(PROFILE))
    (queue / "tasks.toml").write_text((queue / "tasks.toml").read_text() + "\n[learn]\nmin_pairs = 4\nholdout = 0.5\n")
    log = PickLog(queue)
    for n in range(4):
        log.append(shown(REL, FDO, impression=f"i{n}"))
        log.append(Choice("pick", "b1", f"i{n}", 1))
    return queue


def test_learn_stores_a_proposal_without_applying_it_by_default(root):
    out = learn.learn(root, "2026-11-11")
    proposal_id = out.splitlines()[0].split()[1]
    assert out.endswith(f"apply with: learn.py --apply {proposal_id}")
    assert (learn.ProposalStore(root).load(proposal_id).status, taste.load_profile(root)) == ("proposed", PROFILE)


def test_learn_with_too_few_picks_changes_nothing(tmp_path):
    queue = make_queue(tmp_path, {})
    taste.profile_path(queue).parent.mkdir()
    taste.profile_path(queue).write_text(taste.render(PROFILE))
    assert learn.learn(queue, "d") == "no change: only 0 preference pairs; learning starts at 10"


def test_apply_snapshots_then_writes_and_revert_restores(root):
    proposal = learn.propose(PROFILE, learn.pairs(PickLog(root)), CONFIG, "2026-11-11")
    applied = learn.apply(root, proposal, "2026-11-11")
    assert (applied.status, taste.load_profile(root).weights) == ("applied", {"FDO": 1.1, "REL": 0.9})
    assert learn.revert(root, PROFILE.version) == f"profile reverted to {PROFILE.version}"
    assert taste.load_profile(root) == PROFILE


def test_apply_refuses_a_proposal_made_against_another_version(root):
    with pytest.raises(SystemExit, match="re-run learn.py"):
        learn.apply(root, Proposal("p", "other", (), "nudge", 0, 1, 1), "d")


def test_missing_proposal_snapshot_or_profile_exit_with_a_message(tmp_path):
    queue = make_queue(tmp_path, {})
    with pytest.raises(SystemExit, match="no proposal p9"):
        learn.ProposalStore(queue).load("p9")
    with pytest.raises(SystemExit, match="no snapshot of profile version v9"):
        learn.ProposalStore(queue).snapshot_text("v9")
    with pytest.raises(SystemExit, match="run seed_profile.py first"):
        learn.current(queue)


@pytest.mark.parametrize("auto, runs, kind, expected", [
    (False, 30, "nudge", False), (True, 24, "nudge", False), (True, 25, "reorder", False), (True, 25, "nudge", True),
])
def test_auto_apply_needs_the_switch_enough_runs_and_a_small_change(auto, runs, kind, expected):
    proposal = Proposal("p", "v", (), kind, 0, 1, 1)
    assert learn.auto_applies(proposal, runs, {**learn.DEFAULTS, "auto_apply": auto}) is expected


def test_an_allowed_nudge_is_applied_automatically(root):
    taste.profile_path(root).write_text(taste.render(Profile(goals=(Goal("FDO", 2.0), Goal("REL", 1.0)))))
    (root / "tasks.toml").write_text((root / "tasks.toml").read_text() + "auto_apply = true\nmin_runs = 4\n")
    assert "; applied)" in learn.learn(root, "2026-11-11").splitlines()[0]
    assert taste.load_profile(root).weights == {"FDO": 2.2, "REL": 0.9}


def test_main_proposes_applies_and_reverts(root, capsys):
    learn.main(["--root", str(root), "--today", "2026-11-11"])
    proposal_id = capsys.readouterr().out.split()[1]
    learn.main(["--root", str(root), "--apply", proposal_id])
    learn.main(["--root", str(root), "--revert", PROFILE.version])
    out = capsys.readouterr().out
    assert ("; applied)" in out, out.endswith(f"profile reverted to {PROFILE.version}\n")) == (True, True)

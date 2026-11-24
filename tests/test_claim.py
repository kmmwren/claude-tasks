"""Claiming a brief: a lease published to origin by compare-and-set, with exactly one winner."""

import datetime as dt
import subprocess
import threading

import claim as claim_mod
import pytest
from claim import Claim, Lease, Outcome, Target
from deps import fm
from wake import RunKey, Wake

from tests.maintain_helpers import (
    NOW,
    PAST,
    add_brief,
    claimer_for,
    origin_files,
    origin_head,
    queue_repo,
    second_clone,
)
from tests.test_git_sync import git

HOUR = dt.timedelta(hours=1)
TTL = dt.timedelta(hours=2)


def target_of(clone, brief_id="b1", folder="ready") -> Target:
    wake = Wake.parse(brief_id, (clone / folder / f"{brief_id}.md").read_text())
    assert wake is not None
    return Target(brief_id, folder, wake.key)


def claimed_fields(clone, brief_id="b1") -> dict[str, str]:
    git(clone, "fetch", "-q", "origin", "main")
    text = git(clone, "show", f"origin/main:in-progress/{brief_id}.md").stdout
    return fm(text)


def test_a_claim_publishes_the_lease_the_move_and_the_run_record_in_one_commit(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    target = target_of(clone)
    before = origin_head(clone)
    result = claimer_for(clone, "laptop@h:1").claim(target, NOW)
    assert result.won and result.lease is not None
    assert origin_head(clone) != before
    files = origin_files(clone)
    assert "in-progress/b1.md" in files and "ready/b1.md" not in files
    assert any(f.startswith("maintain/runs/") for f in files)
    fields = claimed_fields(clone)
    assert fields["lease_holder"] == "laptop@h:1"
    assert fields["lease_expires"] == (NOW + TTL).isoformat()
    assert fields["lease_attempts"] == "1" and fields["wake_key"] == str(target.key)
    assert fields["status"] == "in-progress"


def test_the_claiming_checkout_is_fast_forwarded_to_the_published_claim(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    claimer_for(clone, "laptop@h:1").claim(target_of(clone), NOW)
    assert (clone / "in-progress" / "b1.md").exists() and not (clone / "ready" / "b1.md").exists()


def test_two_claims_on_one_brief_give_exactly_one_winner(tmp_path):
    clone_a = queue_repo(tmp_path)
    add_brief(clone_a, "b1", not_before=PAST)
    clone_b = second_clone(tmp_path)
    target = target_of(clone_a)
    # B keeps the origin it saw before A published, which is the interleaving of a real race.
    stale_b = claimer_for(clone_b, "cloud@h:2", fetch=lambda root, branch: True)
    won_a = claimer_for(clone_a, "laptop@h:1").claim(target, NOW)
    lost_b = stale_b.claim(target, NOW)
    assert (won_a.outcome, lost_b.outcome) == (Outcome.CLAIMED, Outcome.LOST)
    assert "rejected" in lost_b.detail
    assert claimed_fields(clone_a)["lease_holder"] == "laptop@h:1"


def test_the_loser_that_fetches_first_finds_the_brief_gone(tmp_path):
    clone_a = queue_repo(tmp_path)
    add_brief(clone_a, "b1", not_before=PAST)
    clone_b = second_clone(tmp_path)
    target = target_of(clone_a)
    assert claimer_for(clone_a, "laptop@h:1").claim(target, NOW).won
    lost = claimer_for(clone_b, "cloud@h:2").claim(target, NOW)
    assert lost.outcome is Outcome.LOST and "no longer in ready/" in lost.detail


def test_racing_threads_publish_one_claim(tmp_path):
    clone_a = queue_repo(tmp_path)
    add_brief(clone_a, "b1", not_before=PAST)
    clone_b = second_clone(tmp_path)
    target = target_of(clone_a)
    claimers = [claimer_for(clone_a, "a"), claimer_for(clone_b, "b")]
    outcomes: list[Claim] = []
    start = threading.Barrier(2)

    def run(claimer):
        start.wait()
        outcomes.append(claimer.claim(target, NOW))

    threads = [threading.Thread(target=run, args=(c,)) for c in claimers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(o.outcome.value for o in outcomes).count("claimed") == 1
    assert claimed_fields(clone_a)["lease_attempts"] == "1"


def test_an_expired_lease_can_be_reclaimed_with_the_attempt_counted(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    target = target_of(clone)
    claimer_for(clone, "laptop@h:1").claim(target, NOW)
    later = NOW + TTL + dt.timedelta(minutes=1)
    again = claimer_for(clone, "cloud@h:2").claim(Target("b1", "in-progress", target.key), later)
    assert again.won and again.lease == Lease("cloud@h:2", later + TTL, 2)
    assert claimed_fields(clone)["lease_holder"] == "cloud@h:2"


def test_a_live_lease_is_not_reclaimable(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    target = target_of(clone)
    claimer_for(clone, "laptop@h:1").claim(target, NOW)
    lost = claimer_for(clone, "cloud@h:2").claim(
        Target("b1", "in-progress", target.key), NOW + HOUR
    )
    assert lost.outcome is Outcome.LOST and "live lease" in lost.detail
    assert claimed_fields(clone)["lease_holder"] == "laptop@h:1"


def test_a_reclaim_does_not_write_a_second_run_record(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    target = target_of(clone)
    claimer_for(clone, "laptop@h:1").claim(target, NOW)
    runs = {f for f in origin_files(clone) if f.startswith("maintain/runs/")}
    claimer_for(clone, "cloud@h:2").claim(Target("b1", "in-progress", target.key), NOW + TTL + HOUR)
    assert {f for f in origin_files(clone) if f.startswith("maintain/runs/")} == runs


def test_a_wake_that_already_ran_is_not_claimed_again(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    target = target_of(clone)
    assert claimer_for(clone, "a").claim(target, NOW).won
    (clone / "ready").mkdir(exist_ok=True)  # git removed the emptied folder with the move
    git(clone, "mv", "in-progress/b1.md", "ready/b1.md")
    git(clone, "commit", "-qam", "back to ready")
    git(clone, "push", "-q", "origin", "main")
    again = claimer_for(clone, "b").claim(target, NOW + TTL)
    assert again.outcome is Outcome.LOST and "already ran" in again.detail


def test_a_wake_edited_on_origin_is_not_claimed_as_it_was_seen(tmp_path):
    clone = queue_repo(tmp_path)
    path = add_brief(clone, "b1", not_before=PAST)
    target = target_of(clone)
    path.write_text(path.read_text().replace(f"not_before: {PAST}", "not_before: 2026-11-24"))
    git(clone, "commit", "-qam", "edit")
    git(clone, "push", "-q", "origin", "main")
    lost = claimer_for(clone, "a").claim(target, NOW)
    assert lost.outcome is Outcome.LOST and "wake of b1 changed" in lost.detail


def test_a_brief_that_cannot_move_fails_the_claim_and_publishes_nothing(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", "inbox", not_before=PAST)
    before = origin_head(clone)
    failed = claimer_for(clone, "a").claim(Target("b1", "inbox", RunKey("b1", "x", 0)), NOW)
    assert failed.outcome in (Outcome.FAILED, Outcome.LOST) and not failed.won
    assert origin_head(clone) == before


def test_an_unreachable_origin_fails_the_claim_without_a_worktree(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    result = claimer_for(clone, "a", fetch=lambda root, branch: False).claim(target_of(clone), NOW)
    assert result == Claim(Outcome.FAILED, "could not fetch origin")


def test_a_queue_that_is_a_subfolder_of_its_repo_is_not_claimed(tmp_path):
    clone = queue_repo(tmp_path)
    nested = clone / "scripts"
    result = claimer_for(nested, "a").claim(Target("b1", "ready", RunKey("b1", "x", 0)), NOW)
    assert result.outcome is Outcome.FAILED and "not the root" in result.detail


def test_a_push_refused_for_another_reason_is_failed_not_lost(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    hook = tmp_path / "remote" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    result = claimer_for(clone, "a").claim(target_of(clone), NOW)
    assert result.outcome is Outcome.FAILED and "policy says no" in result.detail


def test_a_commit_that_fails_is_reported_as_failed(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    real = subprocess.run

    def no_commit(argv, *a, **kw):
        if "commit" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "no identity")
        return real(argv, *a, **kw)

    monkeypatch.setattr(claim_mod.subprocess, "run", no_commit)
    result = claimer_for(clone, "a").claim(target_of(clone), NOW)
    assert result.outcome is Outcome.FAILED and "commit failed" in result.detail


def test_a_fetch_that_times_out_counts_as_failed(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)

    def hang(argv, *a, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(claim_mod.subprocess, "run", hang)
    assert claim_mod.fetch_origin(clone, "main") is False


def test_a_worktree_that_cannot_be_cut_fails_the_claim(tmp_path):
    clone = queue_repo(tmp_path)
    add_brief(clone, "b1", not_before=PAST)
    git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    stale = claimer_for(clone, "a", fetch=lambda root, branch: True)
    result = stale.claim(target_of(clone), NOW)
    assert result.outcome is Outcome.FAILED and "could not cut a worktree" in result.detail


def test_refresh_fast_forwards_and_reports_failure(tmp_path):
    clone_a = queue_repo(tmp_path)
    clone_b = second_clone(tmp_path)
    add_brief(clone_a, "b1", not_before=PAST)
    assert claimer_for(clone_b, "b").refresh()
    assert (clone_b / "ready" / "b1.md").exists()
    (clone_b / "local.md").write_text("x")
    git(clone_b, "add", "-A")
    git(clone_b, "commit", "-qm", "local only")
    add_brief(clone_a, "b2", not_before=PAST)
    assert not claimer_for(clone_b, "b").refresh()
    assert not claimer_for(clone_b, "b", fetch=lambda root, branch: False).refresh()


def test_a_published_claim_whose_checkout_cannot_advance_says_so(tmp_path, capsys):
    clone_a = queue_repo(tmp_path)
    add_brief(clone_a, "b1", not_before=PAST)
    (clone_a / "local.md").write_text("x")
    git(clone_a, "add", "-A")
    git(clone_a, "commit", "-qm", "local only")
    assert claimer_for(clone_a, "a").claim(target_of(clone_a), NOW).won
    assert "not fast-forwarded" in capsys.readouterr().err


def test_a_lease_that_cannot_be_read_counts_as_expired(capsys):
    lease = Lease.from_fields({"lease_holder": "x", "lease_expires": "tomorrow-ish"})
    assert lease is not None and lease.expired(NOW) and lease.attempts == 0
    assert "unreadable lease" in capsys.readouterr().err


def test_a_brief_without_a_holder_has_no_lease():
    assert Lease.from_fields({"lease_expires": "2026-11-26"}) is None


def test_a_lease_round_trips_through_frontmatter_fields():
    lease = Lease.first("h", NOW, TTL)
    assert Lease.from_fields(lease.fields()) == lease
    assert lease.renewed("g", NOW + HOUR, TTL) == Lease("g", NOW + HOUR + TTL, 2)
    assert not lease.expired(NOW + HOUR) and lease.expired(NOW + TTL)


@pytest.mark.parametrize(
    "folder,reclaim", [("in-progress", True), ("ready", False), ("inbox", False)]
)
def test_only_an_in_progress_target_is_a_reclaim(folder, reclaim):
    assert Target("b", folder, RunKey("b", "probe", 0)).reclaim is reclaim

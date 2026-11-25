"""The hygiene lease: published to origin like a claim, so exactly one runner tidies the queue at a time."""

import datetime as dt
import json
import pathlib

import hygiene_lease as hl
import origin_push
import pytest
from claim import Claim, Lease, Outcome
from maintain_state import LastRun

from tests.maintain_helpers import (
    NOW,
    claimer_for,
    origin_files,
    origin_head,
    push_all,
    queue_repo,
    second_clone,
)
from tests.test_git_sync import git

TTL = dt.timedelta(hours=2)


def leaser(clone: pathlib.Path, holder: str = "laptop@h:1", **kw: object) -> hl.HygieneLeaser:
    return hl.HygieneLeaser(claimer_for(clone, holder, **kw))


def lease_at_origin(clone: pathlib.Path) -> dict[str, str] | None:
    git(clone, "fetch", "-q", "origin", "main")
    shown = git(clone, "show", f"origin/main:{hl.HYGIENE_LEASE}", check=False)
    return json.loads(shown.stdout) if shown.returncode == 0 else None


def decline_every_push(tmp_path: pathlib.Path) -> None:
    hook = tmp_path / "remote" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)


def test_no_lease_file_reads_as_none_without_a_warning(tmp_path, capsys):
    assert hl.read_lease(tmp_path) is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
def test_an_unreadable_lease_warns_and_reads_as_none(tmp_path, capsys, body):
    (tmp_path / "maintain").mkdir()
    (tmp_path / hl.HYGIENE_LEASE).write_text(body)
    assert hl.read_lease(tmp_path) is None
    assert "ignoring unreadable" in capsys.readouterr().err


def test_a_lease_whose_expiry_cannot_be_read_is_an_expired_one_that_can_be_taken(tmp_path, capsys):
    (tmp_path / "maintain").mkdir()
    (tmp_path / hl.HYGIENE_LEASE).write_text('{"lease_holder": "x", "lease_expires": "soon"}')
    lease = hl.read_lease(tmp_path)
    assert lease is not None and lease.expired(NOW) and not lease.live(NOW, TTL)
    assert "treating it as expired" in capsys.readouterr().err


def test_taking_the_lease_publishes_it_and_hands_back_what_was_written(tmp_path):
    clone = queue_repo(tmp_path)
    claim = leaser(clone).acquire(NOW)
    assert claim.won and claim.lease == Lease("laptop@h:1", NOW + TTL, 1)
    assert lease_at_origin(clone) == claim.lease.fields()


def test_a_live_lease_held_by_another_is_not_taken(tmp_path):
    clone = queue_repo(tmp_path)
    leaser(clone).acquire(NOW)
    lost = leaser(second_clone(tmp_path), "cloud@h:2").acquire(NOW + dt.timedelta(hours=1))
    assert lost.outcome is Outcome.LOST and "held by laptop@h:1" in lost.detail
    assert lost.lease is None
    assert lease_at_origin(clone)["lease_holder"] == "laptop@h:1"


def test_an_expired_lease_is_taken_over(tmp_path):
    clone = queue_repo(tmp_path)
    leaser(clone).acquire(NOW)
    later = NOW + TTL + dt.timedelta(minutes=1)
    taken = leaser(second_clone(tmp_path), "cloud@h:2").acquire(later)
    assert taken.won and lease_at_origin(clone)["lease_holder"] == "cloud@h:2"


def test_a_lease_stamped_far_ahead_by_a_runaway_clock_is_taken_over(tmp_path):
    clone = queue_repo(tmp_path)
    leaser(clone).acquire(NOW + dt.timedelta(days=1))
    assert leaser(second_clone(tmp_path), "cloud@h:2").acquire(NOW).won


def test_releasing_removes_the_lease_and_writes_the_records_in_the_same_commit(tmp_path):
    clone = queue_repo(tmp_path)
    runner = leaser(clone)
    lease = runner.acquire(NOW).lease
    before = origin_head(clone)
    released = runner.release(lease, [LastRun("deterministic", NOW, (), ("skipped route",))])
    assert released.won and origin_head(clone) != before
    files = origin_files(clone)
    assert hl.HYGIENE_LEASE not in files and "maintain/last-run/deterministic.json" in files
    assert git(clone, "rev-list", "--count", f"{before}..origin/main").stdout.strip() == "1"


def test_releasing_a_lease_another_runner_took_over_publishes_nothing(tmp_path):
    clone = queue_repo(tmp_path)
    ours = leaser(clone)
    lease = ours.acquire(NOW).lease
    leaser(second_clone(tmp_path), "cloud@h:2").acquire(NOW + TTL + dt.timedelta(minutes=1))
    before = origin_head(clone)
    lost = ours.release(lease, [LastRun("deterministic", NOW)])
    assert lost.outcome is Outcome.LOST and "no longer ours" in lost.detail
    assert origin_head(clone) == before


def test_a_declined_push_is_a_refusal_and_not_a_lost_race(tmp_path):
    clone = queue_repo(tmp_path)
    decline_every_push(tmp_path)
    refused = leaser(clone).acquire(NOW)
    assert refused.outcome is Outcome.REFUSED and "policy says no" in refused.detail


def test_a_branch_that_never_stops_moving_is_lost_not_failed(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    monkeypatch.setattr(origin_push, "_backoff", lambda attempt: None)
    (tmp_path / "remote" / "refs" / "heads" / "main.lock").write_text("")
    assert leaser(clone).acquire(NOW).outcome is Outcome.LOST


def test_an_origin_that_cannot_be_reached_fails_the_take_and_never_raises(tmp_path):
    clone = queue_repo(tmp_path)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    failed = leaser(clone).acquire(NOW)
    assert failed.outcome is Outcome.FAILED and "PushError" in failed.detail


def test_an_os_error_while_publishing_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(hl, "push_from_fresh_origin", broken)
    assert leaser(clone).acquire(NOW) == Claim(Outcome.FAILED, "OSError: disk full")


def test_refresh_fast_forwards_the_checkout_to_origin(tmp_path):
    clone = queue_repo(tmp_path)
    other = second_clone(tmp_path)
    (other / "marker.txt").write_text("x")
    push_all(other, "marker")
    assert git(clone, "rev-parse", "HEAD").stdout.strip() != origin_head(clone)
    assert leaser(clone).refresh()
    assert git(clone, "rev-parse", "HEAD").stdout.strip() == origin_head(clone)
    assert (clone / "marker.txt").exists()


def test_a_refresh_that_cannot_fetch_leaves_the_checkout_where_it_was(tmp_path):
    clone = queue_repo(tmp_path)
    other = second_clone(tmp_path)
    (other / "marker.txt").write_text("x")
    push_all(other, "marker")
    before = git(clone, "rev-parse", "HEAD").stdout
    assert not leaser(clone, fetch=lambda root, branch: False).refresh()
    assert git(clone, "rev-parse", "HEAD").stdout == before


def test_a_queue_is_shared_nested_or_local_by_its_origin_and_its_place_in_the_repo(tmp_path):
    clone = queue_repo(tmp_path)
    (clone / "queue").mkdir()
    assert leaser(clone).standing is hl.Standing.SHARED
    assert leaser(clone / "queue").standing is hl.Standing.NESTED
    assert leaser(tmp_path).standing is hl.Standing.LOCAL


def test_a_nested_queue_cannot_take_the_lease_and_a_local_one_is_given_one_unpublished(tmp_path):
    clone = queue_repo(tmp_path)
    (clone / "queue").mkdir()
    before = origin_head(clone)
    refused = leaser(clone / "queue").acquire(NOW)
    assert refused.outcome is Outcome.REFUSED and refused.lease is None
    local = leaser(tmp_path).acquire(NOW)
    assert local.won and local.lease == Lease("laptop@h:1", NOW + TTL, 1)
    assert origin_head(clone) == before


def test_a_local_release_writes_the_records_beside_the_queue(tmp_path):
    runner = leaser(tmp_path)
    released = runner.release(runner.acquire(NOW).lease, [LastRun("deterministic", NOW)])
    assert released.won and LastRun.read(tmp_path, "deterministic").at == NOW


def test_a_nested_or_local_queue_has_nothing_to_refresh(tmp_path):
    clone = queue_repo(tmp_path)
    (clone / "queue").mkdir()
    refuse = dict(fetch=lambda root, branch: False)
    assert leaser(clone / "queue", **refuse).refresh() and leaser(tmp_path, **refuse).refresh()


class FlakyPush:
    """A stand-in for the push that fails `failures` times, then publishes for real."""

    def __init__(self, failures: int):
        self.calls = 0
        self.failures = failures

    def __call__(self, root, edit, message):
        self.calls += 1
        if self.calls <= self.failures:
            raise origin_push.PushError("origin hiccup")
        return origin_push.push_from_fresh_origin(root, edit, message)


def test_a_release_that_fails_is_tried_again_until_it_publishes(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    pauses: list[float] = []
    runner = hl.HygieneLeaser(claimer_for(clone, "laptop@h:1"), pauses.append)
    lease = runner.acquire(NOW).lease
    flaky = FlakyPush(failures=2)
    monkeypatch.setattr(hl, "push_from_fresh_origin", flaky)
    assert runner.release(lease, [LastRun("deterministic", NOW)]).won
    assert flaky.calls == 3 and pauses == [hl.RELEASE_PAUSE, 2 * hl.RELEASE_PAUSE]
    assert hl.HYGIENE_LEASE not in origin_files(clone)


def test_a_release_gives_up_after_a_bounded_number_of_attempts(tmp_path, monkeypatch):
    clone = queue_repo(tmp_path)
    runner = hl.HygieneLeaser(claimer_for(clone, "laptop@h:1"), lambda seconds: None)
    lease = runner.acquire(NOW).lease
    flaky = FlakyPush(failures=99)
    monkeypatch.setattr(hl, "push_from_fresh_origin", flaky)
    assert runner.release(lease, []).outcome is Outcome.FAILED
    assert flaky.calls == hl.RELEASE_ATTEMPTS

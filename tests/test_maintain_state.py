"""The run record, the lease, and the local, git and fallback exchanges that carry them."""

import datetime
import json
import pathlib
import subprocess
from collections.abc import Callable

import maintain_state as ms
import pytest
from maintain_state import (
    Acquired,
    Counts,
    ExchangeUnavailable,
    FallbackExchange,
    GitExchange,
    Held,
    Lease,
    LocalExchange,
    Outcome,
    RunRecord,
    Tier,
)

from tests.maintain_helpers import (  # noqa: F401
    HOUR,
    T0,
    clone,
    git,
    isolated_git,
    make_origin,
    tier_run,
)

pytestmark = pytest.mark.usefixtures("isolated_git")

TTL = datetime.timedelta(minutes=30)


@pytest.mark.parametrize(
    ("text", "seconds"), [("90s", 90), ("30m", 1800), ("4h", 14400), (" 2d ", 172800)]
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert ms.parse_duration(text).total_seconds() == seconds


@pytest.mark.parametrize("text", ["4", "h", "1w", "", "-3h", "1.5h"])
def test_parse_duration_rejects_what_is_not_a_number_and_unit(text: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        ms.parse_duration(text)


@pytest.mark.parametrize(
    ("seconds", "shown"), [(5, "<1m"), (60, "1m"), (3599, "59m"), (7200, "2h"), (172800, "2d")]
)
def test_format_age(seconds: int, shown: str) -> None:
    assert ms.format_age(datetime.timedelta(seconds=seconds)) == shown


def test_only_the_judgement_tier_needs_capacity() -> None:
    assert [t for t in Tier if t.needs_capacity] == [Tier.JUDGEMENT]


def test_record_round_trips_through_its_text() -> None:
    record = RunRecord().with_runs([tier_run(), tier_run(Tier.JUDGEMENT, runner="cloud")])
    again = RunRecord.parse(record.to_text())
    assert again == record
    assert again.last(Tier.JUDGEMENT) is not None
    assert again.last(Tier.JUDGEMENT).runner == "cloud"  # type: ignore[union-attr]


def test_a_run_replaces_an_older_one_but_never_a_newer_one() -> None:
    newer, older = tier_run(finished=T0 + HOUR), tier_run(finished=T0, runner="cloud")
    assert RunRecord().with_runs([older]).with_runs([newer]).last(Tier.DETERMINISTIC) == newer
    assert RunRecord().with_runs([newer]).with_runs([older]).last(Tier.DETERMINISTIC) == newer


def test_merging_two_records_keeps_the_newest_run_per_tier() -> None:
    mine = RunRecord().with_runs([tier_run(finished=T0 + HOUR)])
    theirs = RunRecord().with_runs([tier_run(finished=T0), tier_run(Tier.JUDGEMENT)])
    merged = mine.merged(theirs)
    assert merged.last(Tier.DETERMINISTIC) == tier_run(finished=T0 + HOUR)
    assert merged.last(Tier.JUDGEMENT) is not None


def test_a_run_is_fresh_until_it_is_max_age_old() -> None:
    run = tier_run(finished=T0)
    assert run.fresh(T0 + HOUR, 2 * HOUR)
    assert not run.fresh(T0 + 3 * HOUR, 2 * HOUR)


def test_a_run_describes_when_and_by_whom() -> None:
    assert (
        tier_run(finished=T0).describe(T0 + HOUR) == "last run 2026-11-17 09:00Z (1h ago) by laptop"
    )


def test_an_absent_record_is_empty_without_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    assert RunRecord.parse("") == RunRecord()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"tiers": {"deterministic": {"runner": 3}}}',
        json.dumps(
            {
                "tiers": {
                    "deterministic": {**tier_run().to_json(), "finished": "2026-11-17T09:00:00"}
                }
            }
        ),
        json.dumps({"tiers": {"deterministic": {**tier_run().to_json(), "outcome": "great"}}}),
    ],
)
def test_a_corrupt_record_warns_and_counts_as_empty(
    text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert RunRecord.parse(text) == RunRecord()
    assert "unreadable run record" in capsys.readouterr().err


def test_a_tier_this_version_does_not_know_is_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    text = json.dumps({"tiers": {"future": tier_run().to_json()}})
    assert RunRecord.parse(text) == RunRecord()
    assert capsys.readouterr().err == ""


def test_a_lease_is_live_until_it_expires() -> None:
    lease = Lease.take("laptop", T0, TTL)
    assert lease.live(T0 + TTL / 2)
    assert not lease.live(T0 + TTL)
    assert Lease.parse(lease.to_text()) == lease


def test_leases_of_one_runner_carry_distinct_tokens() -> None:
    assert Lease.take("laptop", T0, TTL).token != Lease.take("laptop", T0, TTL).token


@pytest.mark.parametrize("text", ["", "{}", "garbage", "[]"])
def test_an_unreadable_lease_is_no_lease(text: str) -> None:
    assert Lease.parse(text) is None


def test_local_lease_is_taken_once_and_held_against_others(tmp_path: pathlib.Path) -> None:
    first = LocalExchange(tmp_path).acquire("laptop", T0, TTL)
    second = LocalExchange(tmp_path).acquire("cloud", T0 + TTL / 2, TTL)
    assert isinstance(first, Acquired)
    assert isinstance(second, Held)
    assert second.lease == first.lease


def test_local_expired_lease_is_taken_over(tmp_path: pathlib.Path) -> None:
    LocalExchange(tmp_path).acquire("laptop", T0, TTL)
    taken = LocalExchange(tmp_path).acquire("cloud", T0 + 2 * TTL, TTL)
    assert isinstance(taken, Acquired)
    assert taken.lease.runner == "cloud"


def test_local_unreadable_lease_is_taken_over(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".state").mkdir()
    (tmp_path / ms.LEASE_PATH).write_text("garbage")
    assert isinstance(LocalExchange(tmp_path).acquire("cloud", T0, TTL), Acquired)


def test_local_finish_records_the_runs_and_releases_the_lease(tmp_path: pathlib.Path) -> None:
    local = LocalExchange(tmp_path)
    leased = local.acquire("laptop", T0, TTL)
    local.finish(leased.lease, [tier_run()])
    assert local.record().last(Tier.DETERMINISTIC) == tier_run()
    assert not (tmp_path / ms.LEASE_PATH).exists()


def test_local_finish_without_runs_leaves_the_record_alone(tmp_path: pathlib.Path) -> None:
    local = LocalExchange(tmp_path)
    local.finish(local.acquire("laptop", T0, TTL).lease, [])
    assert not (tmp_path / ms.RECORD_PATH).exists()
    assert not (tmp_path / ms.LEASE_PATH).exists()


def test_local_finish_leaves_a_lease_another_runner_took_over(tmp_path: pathlib.Path) -> None:
    local = LocalExchange(tmp_path)
    slow = local.acquire("laptop", T0, TTL).lease
    rival = local.acquire("cloud", T0 + 2 * TTL, TTL).lease
    local.finish(slow, [tier_run()])
    assert Lease.parse((tmp_path / ms.LEASE_PATH).read_text()) == rival


def test_local_finish_with_no_lease_file_still_records(tmp_path: pathlib.Path) -> None:
    local = LocalExchange(tmp_path)
    local.finish(Lease.take("laptop", T0, TTL), [tier_run()])
    assert local.record().last(Tier.DETERMINISTIC) is not None


@pytest.fixture
def origin(tmp_path: pathlib.Path) -> pathlib.Path:
    return make_origin(tmp_path)


def runner_on(tmp_path: pathlib.Path, origin: pathlib.Path, name: str) -> GitExchange:
    root = clone(tmp_path, origin, name)
    return GitExchange(root, LocalExchange(root))


def origin_text(origin: pathlib.Path, relative: str) -> str:
    shown = subprocess.run(
        ["git", "-C", str(origin), "show", f"main:{relative}"], capture_output=True, text=True
    )
    return shown.stdout if shown.returncode == 0 else ""


def origin_commits(origin: pathlib.Path) -> int:
    return int(git(origin, "rev-list", "--count", "main"))


def after_first_fetch(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    """Let origin change between a runner's fetch and its push, which is what a lost race looks like."""
    real, fired = GitExchange._fetch, []

    def fetch(self: GitExchange) -> None:
        real(self)
        if not fired:
            fired.append(True)
            action()

    monkeypatch.setattr(GitExchange, "_fetch", fetch)


def test_git_lease_is_pushed_to_origin_and_held_against_other_runners(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    mine, theirs = runner_on(tmp_path, origin, "a"), runner_on(tmp_path, origin, "b")
    first = mine.acquire("laptop", T0, TTL)
    second = theirs.acquire("cloud", T0 + TTL / 2, TTL)
    assert isinstance(first, Acquired)
    assert Lease.parse(origin_text(origin, ms.LEASE_PATH)) == first.lease
    assert isinstance(second, Held)
    assert second.lease == first.lease


def test_git_never_commits_a_lease_to_the_runners_own_checkout(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    mine = runner_on(tmp_path, origin, "a")
    before = git(tmp_path / "a", "rev-parse", "HEAD")
    mine.acquire("laptop", T0, TTL)
    assert git(tmp_path / "a", "rev-parse", "HEAD") == before
    assert git(tmp_path / "a", "status", "--porcelain") == ""


def test_git_finish_publishes_the_record_and_releases_the_lease(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    mine, theirs = runner_on(tmp_path, origin, "a"), runner_on(tmp_path, origin, "b")
    leased = mine.acquire("laptop", T0, TTL)
    mine.finish(leased.lease, [tier_run()])
    assert origin_text(origin, ms.LEASE_PATH) == ""
    assert theirs.record().last(Tier.DETERMINISTIC) == tier_run()
    assert isinstance(theirs.acquire("cloud", T0 + TTL / 2, TTL), Acquired)


def test_git_expired_lease_is_taken_over(tmp_path: pathlib.Path, origin: pathlib.Path) -> None:
    runner_on(tmp_path, origin, "a").acquire("laptop", T0, TTL)
    taken = runner_on(tmp_path, origin, "b").acquire("cloud", T0 + 2 * TTL, TTL)
    assert isinstance(taken, Acquired)
    assert Lease.parse(origin_text(origin, ms.LEASE_PATH)) == taken.lease


def test_git_finish_leaves_a_lease_another_runner_took_over(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    slow, rival = runner_on(tmp_path, origin, "a"), runner_on(tmp_path, origin, "b")
    mine = slow.acquire("laptop", T0, TTL).lease
    theirs = rival.acquire("cloud", T0 + 2 * TTL, TTL).lease
    slow.finish(mine, [tier_run()])
    assert Lease.parse(origin_text(origin, ms.LEASE_PATH)) == theirs
    assert slow.record().last(Tier.DETERMINISTIC) is not None


def test_git_finish_that_changes_nothing_pushes_nothing(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    mine = runner_on(tmp_path, origin, "a")
    lease = mine.acquire("laptop", T0, TTL).lease
    mine.finish(lease, [])
    commits = origin_commits(origin)
    mine.finish(lease, [])
    assert origin_commits(origin) == commits


def test_git_rejected_push_retries_on_the_new_tip(
    tmp_path: pathlib.Path, origin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, other = runner_on(tmp_path, origin, "a"), clone(tmp_path, origin, "c")

    def unrelated_commit() -> None:
        (other / "note.md").write_text("n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "note")
        git(other, "push", "-q", "origin", "main")

    after_first_fetch(monkeypatch, unrelated_commit)
    assert isinstance(mine.acquire("laptop", T0, TTL), Acquired)
    assert origin_text(origin, "note.md") == "n"
    assert origin_text(origin, ms.LEASE_PATH) != ""


def test_git_losing_the_race_to_another_lease_stands_down(
    tmp_path: pathlib.Path, origin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, rival = runner_on(tmp_path, origin, "a"), runner_on(tmp_path, origin, "b")
    winner: list[Lease] = []

    def rival_wins() -> None:
        winner.append(rival.acquire("cloud", T0, TTL).lease)

    after_first_fetch(monkeypatch, rival_wins)
    lost = mine.acquire("laptop", T0, TTL)
    assert isinstance(lost, Held)
    assert lost.lease == winner[0]


def test_git_origin_that_never_holds_still_is_unavailable(
    tmp_path: pathlib.Path, origin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = runner_on(tmp_path, origin, "a")
    monkeypatch.setattr(GitExchange, "_publish", lambda self, worktree, message: False)
    with pytest.raises(ExchangeUnavailable, match="moved 5 times"):
        mine.acquire("laptop", T0, TTL)


def test_git_record_merges_a_run_left_locally_while_origin_was_unreachable(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    root = clone(tmp_path, origin, "a")
    local = LocalExchange(root)
    local.finish(Lease.take("laptop", T0, TTL), [tier_run(Tier.JUDGEMENT)])
    record = GitExchange(root, local).record()
    assert record.last(Tier.JUDGEMENT) == tier_run(Tier.JUDGEMENT)


def test_git_unreachable_origin_is_unavailable(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    mine = runner_on(tmp_path, origin, "a")
    (tmp_path / "origin.git").rename(tmp_path / "gone.git")
    with pytest.raises(ExchangeUnavailable, match="fetch failed"):
        mine.record()


def test_git_hung_remote_is_unavailable(
    tmp_path: pathlib.Path, origin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = runner_on(tmp_path, origin, "a")

    def hang(*args: str, **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(ms, "_git", hang)
    with pytest.raises(ExchangeUnavailable, match="timed out"):
        mine.record()


def test_git_push_that_fails_for_another_reason_is_unavailable(
    tmp_path: pathlib.Path, origin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, real = runner_on(tmp_path, origin, "a"), ms._network

    def network(where: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 128, "", "fatal: unable to access")
        return real(where, *args)

    monkeypatch.setattr(ms, "_network", network)
    with pytest.raises(ExchangeUnavailable, match="push failed"):
        mine.acquire("laptop", T0, TTL)


def test_git_commit_that_fails_is_unavailable(tmp_path: pathlib.Path, origin: pathlib.Path) -> None:
    mine = runner_on(tmp_path, origin, "a")
    hook = tmp_path / "a" / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(ExchangeUnavailable, match="commit failed"):
        mine.acquire("laptop", T0, TTL)


def test_git_missing_origin_tip_is_unavailable(
    tmp_path: pathlib.Path, origin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = runner_on(tmp_path, origin, "a")
    git(tmp_path / "a", "update-ref", "-d", "refs/remotes/origin/main")
    monkeypatch.setattr(GitExchange, "_fetch", lambda self: None)
    with pytest.raises(ExchangeUnavailable, match="no worktree"):
        mine.acquire("laptop", T0, TTL)


def test_git_commits_with_a_default_identity_when_none_is_configured(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    mine = runner_on(tmp_path, origin, "a")
    git(tmp_path / "a", "config", "--unset", "user.email")
    mine.acquire("laptop", T0, TTL)
    assert git(origin, "log", "-1", "--format=%an <%ae>", "main").strip() == (
        "claude-tasks <claude-tasks@localhost>"
    )


class Broken(ms.Exchange):
    def record(self) -> RunRecord:
        raise ExchangeUnavailable("down")

    def acquire(
        self, runner: str, now: datetime.datetime, ttl: datetime.timedelta
    ) -> ms.LeaseResult:
        raise ExchangeUnavailable("down")

    def finish(self, lease: Lease, runs: ms.Sequence[ms.TierRun]) -> None:
        raise ExchangeUnavailable("down")


def test_fallback_switches_to_local_files_and_stays_there(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = LocalExchange(tmp_path)
    exchange = FallbackExchange(Broken(), local)
    assert exchange.record() == RunRecord()
    leased = exchange.acquire("laptop", T0, TTL)
    exchange.finish(leased.lease, [tier_run()])
    assert local.record().last(Tier.DETERMINISTIC) == tier_run()
    assert capsys.readouterr().err.count("using local files") == 1


def test_fallback_uses_the_shared_exchange_while_it_works(tmp_path: pathlib.Path) -> None:
    shared = LocalExchange(tmp_path / "shared")
    exchange = FallbackExchange(shared, Broken())
    exchange.finish(exchange.acquire("laptop", T0, TTL).lease, [tier_run()])
    assert exchange.record().last(Tier.DETERMINISTIC) == tier_run()


def test_fallback_has_nowhere_left_to_go_when_the_local_exchange_fails() -> None:
    broken = Broken()
    with pytest.raises(ExchangeUnavailable):
        FallbackExchange(broken, broken).record()


def test_a_queue_that_is_its_own_repo_with_an_origin_uses_git(
    tmp_path: pathlib.Path, origin: pathlib.Path
) -> None:
    root = clone(tmp_path, origin, "a")
    assert isinstance(ms.open_exchange(root), FallbackExchange)


def test_a_queue_without_a_remote_or_repo_uses_local_files(tmp_path: pathlib.Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert isinstance(ms.open_exchange(plain), LocalExchange)
    local_repo = tmp_path / "repo"
    local_repo.mkdir()
    git(local_repo, "init", "-q")
    assert isinstance(ms.open_exchange(local_repo), LocalExchange)


def test_outcome_and_counts_survive_the_record(tmp_path: pathlib.Path) -> None:
    run = tier_run()
    again = RunRecord.parse(RunRecord().with_runs([run]).to_text()).last(Tier.DETERMINISTIC)
    assert again is not None
    assert again.outcome is Outcome.OK
    assert again.counts == Counts(2, 0, 1)

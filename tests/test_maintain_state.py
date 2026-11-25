"""The last-run record and the run ledger `maintain` keeps in the queue repo, plus its settings."""

import datetime as dt

import pytest
from maintain_config import (
    DEFAULT_MAX_ATTEMPTS,
    MaintainSettings,
    Tier,
)
from maintain_state import LastRun, RunLedger
from wake import RunKey

UTC = dt.timezone.utc
AT = dt.datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
FIVE = dt.timedelta(minutes=5)


def test_a_last_run_round_trips_through_the_queue(tmp_path):
    LastRun("laptop", AT, ("b1",), ("overdue b2",)).write(tmp_path)
    assert LastRun.read(tmp_path, "laptop") == LastRun("laptop", AT, ("b1",), ("overdue b2",))
    assert LastRun.path(tmp_path, "laptop") == tmp_path / "maintain/last-run/laptop.json"


def test_each_tier_has_its_own_record(tmp_path):
    LastRun("laptop", AT).write(tmp_path)
    assert LastRun.read(tmp_path, "cloud") is None


def test_a_missing_record_reads_as_none_without_a_warning(tmp_path, capsys):
    assert LastRun.read(tmp_path, "laptop") is None
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("body", ["not json", '{"claimed": []}', '{"at": "yesterday"}', "[1]"])
def test_an_unreadable_record_warns_and_reads_as_none(tmp_path, capsys, body):
    path = LastRun.path(tmp_path, "laptop")
    path.parent.mkdir(parents=True)
    path.write_text(body)
    assert LastRun.read(tmp_path, "laptop") is None
    assert "ignoring unreadable" in capsys.readouterr().err


@pytest.mark.parametrize(
    "age,fresh",
    [(dt.timedelta(minutes=4), True), (FIVE, False), (dt.timedelta(hours=1), False)],
)
def test_a_record_is_fresh_until_the_interval_has_passed(age, fresh):
    assert LastRun("laptop", AT).fresh(AT + age, FIVE) is fresh


def test_a_clock_that_ran_backwards_is_stale_not_fresh():
    assert not LastRun("laptop", AT).fresh(AT - dt.timedelta(minutes=1), FIVE)


def test_the_ledger_remembers_a_wake_by_its_key(tmp_path):
    key = RunKey("b1", "2026-11-25T00:00:00+00:00", 0)
    ledger = RunLedger(tmp_path)
    assert not ledger.has(key)
    written = ledger.record(key, "laptop@h:1", AT)
    assert ledger.has(key) and written == ledger.path(key)
    assert not ledger.has(RunKey("b1", "2026-11-25T00:00:00+00:00", 1))


def test_settings_default_to_five_minutes_on_the_laptop_and_an_hour_in_the_cloud(tmp_path):
    (tmp_path / "tasks.toml").write_text("")
    settings = MaintainSettings.load(tmp_path)
    assert settings.tiers[Tier.LAPTOP].interval == FIVE
    assert settings.tiers[Tier.CLOUD].interval == dt.timedelta(minutes=60)
    assert settings.tiers[Tier.LAPTOP].capabilities == {"*"}
    assert settings.tiers[Tier.CLOUD].capabilities == frozenset()
    assert settings.lease_ttl == dt.timedelta(hours=2)
    assert (settings.max_claims, settings.max_attempts) == (3, DEFAULT_MAX_ATTEMPTS)


def test_settings_are_read_from_the_maintain_table(tmp_path):
    (tmp_path / "tasks.toml").write_text(
        "[maintain]\nlease_ttl_minutes = 30\nmax_claims_per_tick = 1\nmax_attempts = 2\n"
        'run_args = ["--model", "x"]\n'
        "[maintain.laptop]\ninterval_minutes = 1\n"
        '[maintain.cloud]\ninterval_minutes = 15.5\ncapabilities = ["slack", "github"]\n'
    )
    settings = MaintainSettings.load(tmp_path)
    assert settings.lease_ttl == dt.timedelta(minutes=30)
    assert (settings.max_claims, settings.max_attempts) == (1, 2)
    assert settings.run_args == ("--model", "x")
    assert settings.tiers[Tier.LAPTOP].interval == dt.timedelta(minutes=1)
    assert settings.tiers[Tier.CLOUD].interval == dt.timedelta(minutes=15.5)
    assert settings.tiers[Tier.CLOUD].capabilities == {"slack", "github"}


@pytest.mark.parametrize(
    "toml",
    [
        "[maintain]\nlease_ttl_minutes = -3\n",
        "[maintain]\nlease_ttl_minutes = true\n",
        '[maintain]\nmax_claims_per_tick = "many"\n',
        "[maintain]\nrun_args = [1, 2]\n",
        'maintain = "oops"\n',
        '[maintain]\nlaptop = "oops"\n',
    ],
)
def test_an_invalid_value_warns_and_takes_the_default(tmp_path, capsys, toml):
    (tmp_path / "tasks.toml").write_text(toml)
    settings = MaintainSettings.load(tmp_path)
    assert settings.lease_ttl == dt.timedelta(hours=2) and settings.max_claims == 3
    assert settings.tiers[Tier.LAPTOP].interval == FIVE
    assert "using the default" in capsys.readouterr().err


def test_a_record_a_peers_clock_stamped_slightly_ahead_is_fresh_but_one_far_ahead_is_ignored():
    assert LastRun("laptop", AT).fresh(AT - dt.timedelta(seconds=10), FIVE)
    assert not LastRun("laptop", AT).fresh(AT - dt.timedelta(days=3), FIVE)


def test_hygiene_runs_every_four_hours_unless_the_queue_says_otherwise(tmp_path):
    (tmp_path / "tasks.toml").write_text("")
    assert MaintainSettings.load(tmp_path).hygiene_interval == dt.timedelta(hours=4)
    (tmp_path / "tasks.toml").write_text("[maintain]\nhygiene_interval_minutes = 90\n")
    assert MaintainSettings.load(tmp_path).hygiene_interval == dt.timedelta(minutes=90)
    (tmp_path / "tasks.toml").write_text("[maintain]\nhygiene_interval_minutes = -1\n")
    assert MaintainSettings.load(tmp_path).hygiene_interval == dt.timedelta(hours=4)

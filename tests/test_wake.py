"""When a brief is due: `not_before`, `wake_probe`, validity, capabilities and the run key."""

import datetime as dt
import pathlib
import subprocess

import pytest
from _dates import parse_moment
from wake import (
    LAPTOP,
    Due,
    MalformedWake,
    ProbeRunner,
    Reading,
    RunKey,
    Wake,
    shell,
)

from tests.maintain_helpers import wake_brief

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 26, 9, 0, tzinfo=UTC)


def parse(**fields: str) -> Wake:
    wake = Wake.parse("b1", wake_brief("b1", **fields))
    assert wake is not None
    return wake


def never(command: str) -> Reading:
    raise AssertionError(f"the probe ran: {command}")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-11-26", dt.datetime(2026, 11, 26, tzinfo=UTC)),
        ("2026-11-26T10:30:00Z", dt.datetime(2026, 11, 26, 10, 30, tzinfo=UTC)),
        ("2026-11-26T10:30:00+02:00", dt.datetime(2026, 11, 26, 8, 30, tzinfo=UTC)),
        ("2026-11-26T10:30:00", dt.datetime(2026, 11, 26, 10, 30, tzinfo=UTC)),
        ("  2026-11-26  ", dt.datetime(2026, 11, 26, tzinfo=UTC)),
    ],
)
def test_a_date_or_datetime_is_read_as_a_utc_moment(value, expected):
    parsed = parse_moment(value)
    assert parsed == expected and parsed.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize("value", ["soon", "", "2026-13-45", "26/11/2026"])
def test_text_that_is_not_a_moment_reads_as_none(value):
    assert parse_moment(value) is None


def test_a_brief_naming_neither_trigger_is_not_a_wake():
    assert Wake.parse("b1", wake_brief("b1")) is None


def test_a_past_not_before_is_due_and_the_probe_is_not_asked():
    assert parse(not_before="2026-11-25").due(NOW, never) is Due.DUE


def test_a_not_before_that_is_exactly_now_is_due():
    assert parse(not_before="2026-11-26T09:00:00Z").due(NOW, never) is Due.DUE


def test_a_future_not_before_is_not_yet_due():
    assert parse(not_before="2026-11-30").due(NOW, never) is Due.NOT_YET


def test_a_future_not_before_still_lets_the_probe_decide():
    wake = parse(not_before="2026-11-30", wake_probe="check")
    assert wake.due(NOW, lambda c: Reading(Due.DUE, "exit 0")) is Due.DUE


def test_the_probe_is_given_its_command():
    seen: list[str] = []
    wake = parse(wake_probe="gh pr view 7")
    wake.due(NOW, lambda c: (seen.append(c), Reading(Due.NOT_YET, ""))[1])
    assert seen == ["gh pr view 7"]


def probe_runner(code: int, stderr: str = "") -> ProbeRunner:
    return ProbeRunner(
        pathlib.Path("."),
        run=lambda command, cwd: subprocess.CompletedProcess(command, code, "", stderr),
    )


@pytest.mark.parametrize(
    "code,due",
    [(0, Due.DUE), (10, Due.NOT_YET), (1, Due.ERROR), (2, Due.ERROR), (127, Due.ERROR)],
)
def test_the_probe_exit_status_is_read_by_the_blocker_contract(code, due):
    assert probe_runner(code)("x").due is due


def test_an_erroring_probe_is_not_due_and_says_why(capsys):
    wake = parse(wake_probe="x")
    assert wake.due(NOW, probe_runner(1, "boom\nline two")) is Due.ERROR
    assert "line two" in str(probe_runner(1, "boom\nline two")("x").detail)
    assert "not due" in capsys.readouterr().err


def test_an_error_with_no_stderr_still_has_a_detail():
    assert probe_runner(3)("x").detail == "exit 3: no stderr"


def test_a_probe_that_cannot_start_is_an_error_not_a_crash():
    def broken(command: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        raise OSError("no bash")

    assert ProbeRunner(pathlib.Path("."), run=broken)("x").due is Due.ERROR


def test_a_probe_that_times_out_is_an_error():
    def slow(command: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1)

    assert ProbeRunner(pathlib.Path("."), run=slow)("x").due is Due.ERROR


@pytest.mark.parametrize("code,due", [(0, Due.DUE), (10, Due.NOT_YET), (4, Due.ERROR)])
def test_the_real_shell_probe_reports_its_exit(tmp_path, code, due):
    assert ProbeRunner(tmp_path)(f"exit {code}").due is due


def test_the_shell_runs_in_the_queue_with_stdin_closed(tmp_path):
    done = shell("pwd; cat", tmp_path)
    assert done.returncode == 0 and done.stdout.strip() == str(tmp_path.resolve())


def test_a_brief_past_valid_until_has_lapsed():
    wake = parse(not_before=PAST_DAY, valid_until="2026-11-26T08:00:00Z")
    assert wake.lapsed(NOW) and not parse(not_before=PAST_DAY).lapsed(NOW)


PAST_DAY = "2026-11-20"


def test_escalate_after_is_a_moment_that_passes():
    wake = parse(not_before=PAST_DAY, escalate_after="2026-11-26")
    assert wake.escalated(NOW) and not parse(not_before=PAST_DAY).escalated(NOW)


@pytest.mark.parametrize("field", ["not_before", "valid_until", "escalate_after"])
def test_an_unreadable_moment_is_a_malformed_wake(field):
    fields = {"not_before": PAST_DAY, field: "next tuesday"}
    with pytest.raises(MalformedWake, match=field):
        parse(**fields)


def test_an_unreadable_generation_is_a_malformed_wake():
    with pytest.raises(MalformedWake, match="wake_generation"):
        parse(not_before=PAST_DAY, wake_generation="two")


def test_needs_are_listed_and_requires_local_adds_the_laptop():
    wake = parse(not_before=PAST_DAY, needs="[aws, slack]", **{"requires-local": "true"})
    assert wake.needs == ("aws", "slack", LAPTOP)


def test_a_capability_listed_twice_counts_once():
    wake = parse(not_before=PAST_DAY, needs="[laptop]", **{"requires-local": "true"})
    assert wake.needs == (LAPTOP,)


def test_a_tier_serves_the_needs_it_declares_and_a_wildcard_serves_all():
    wake = parse(not_before=PAST_DAY, needs="[aws, slack]")
    assert wake.missing(frozenset({"slack"})) == ("aws",)
    assert wake.missing(frozenset({"*"})) == ()
    assert wake.missing(frozenset({"aws", "slack"})) == ()


def test_only_unattended_autonomy_may_run_without_a_person():
    assert parse(not_before=PAST_DAY, autonomy="full").unattended
    assert not parse(not_before=PAST_DAY, autonomy="ask").unattended


def test_the_key_names_the_trigger_and_the_generation():
    dated = parse(not_before="2026-11-25", wake_generation="2")
    assert str(dated.key) == "b1|2026-11-25T00:00:00+00:00|g2"
    assert str(parse(wake_probe="x").key) == "b1|probe|g0"


def test_a_new_generation_is_a_new_wake():
    assert parse(not_before=PAST_DAY).key != parse(not_before=PAST_DAY, wake_generation="1").key


def test_the_key_slug_is_a_stable_filename():
    slug = RunKey("b1", "probe", 0).slug
    assert slug == RunKey("b1", "probe", 0).slug and slug.isalnum() and len(slug) == 20

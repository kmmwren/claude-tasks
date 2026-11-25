"""Reading a calendar date the way every queue file writes it."""

import datetime as dt

import pytest
from _dates import parse_date, parse_duration


@pytest.mark.parametrize(
    "value,expected",
    [
        (dt.datetime(2026, 1, 2, 3, 4), dt.date(2026, 1, 2)),
        (dt.date(2026, 1, 2), dt.date(2026, 1, 2)),
        ("2026-01-02", dt.date(2026, 1, 2)),
        ("  2026-01-02 ", dt.date(2026, 1, 2)),
        ("2026-13-45", None),  # shaped like a date, not one
        ("2026-1-2", None),
        ("soon", None),
        ("", None),
        (20260102, None),
        (None, None),
    ],
)
def test_a_date_is_read_from_toml_dates_and_iso_text_only(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("90s", dt.timedelta(seconds=90)),
        ("30m", dt.timedelta(minutes=30)),
        (" 4h ", dt.timedelta(hours=4)),
        ("2d", dt.timedelta(days=2)),
    ],
)
def test_a_duration_is_a_whole_number_and_a_unit(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "4", "h", "0m", "1.5h", "-1h", "4 hours", "1w", "soon"])
def test_anything_else_is_not_a_duration(value):
    with pytest.raises(ValueError, match="not a duration"):
        parse_duration(value)

"""Reading a calendar date the way every queue file writes it."""

import datetime as dt

import pytest
from _dates import parse_date


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

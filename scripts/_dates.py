"""Reading a calendar date the way every queue file writes it."""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import datetime as dt
import re
from typing import Any

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_date(value: Any) -> dt.date | None:
    """A TOML date, a datetime or a `YYYY-MM-DD` string as a date; None for anything else."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and _ISO_DAY.fullmatch(value.strip()):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def parse_moment(value: str) -> dt.datetime | None:
    """An ISO date or datetime as an aware UTC moment: a bare date is its 00:00 UTC, a naive time is UTC.

    None for an empty or unreadable value, so a caller decides whether that is a warning.
    """
    try:
        moment = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)

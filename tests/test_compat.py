"""The Python-version guard: old interpreters get one actionable sentence, not a
cryptic import-time TypeError from a PEP 604 annotation.

The real-world failure this prevents: macOS ships `python3` = 3.9, the plugin's
commands invoke bare `python3`, and every script died with
`TypeError: unsupported operand type(s) for |: 'types.GenericAlias' and 'NoneType'`
before main() ever ran.
"""

import pytest
from _compat import ensure_supported_python


def test_old_python_exits_with_actionable_message() -> None:
    with pytest.raises(SystemExit) as excinfo:
        ensure_supported_python((3, 9))
    message = str(excinfo.value)
    assert "3.11" in message  # what is required
    assert "3.9" in message  # what was found
    assert "python3" in message  # what to do about it


def test_minimum_supported_version_passes() -> None:
    assert ensure_supported_python((3, 11)) is None


def test_newer_version_passes() -> None:
    assert ensure_supported_python((3, 14)) is None


def test_just_below_minimum_exits() -> None:
    with pytest.raises(SystemExit):
        ensure_supported_python((3, 10))

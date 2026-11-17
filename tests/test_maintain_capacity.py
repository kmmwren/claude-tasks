"""Capacity is whatever claude-switch's own `limit_state` module says; this never parses its store."""

import pathlib
import sys

import maintain_capacity as mc
import pytest

from tests.maintain_helpers import T0

FAKE = """
class Store:
    accounts = {accounts}

    def free(self, now=None):
        return {free}


def load():
    return Store()
"""


def switch_home(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    home = tmp_path / "switch"
    home.mkdir()
    (home / "limit_state.py").write_text(body)
    return home


def fake(tmp_path: pathlib.Path, accounts: str, free: str) -> pathlib.Path:
    return switch_home(tmp_path, FAKE.format(accounts=accounts, free=free))


def test_an_account_the_store_calls_free_is_capacity(tmp_path: pathlib.Path) -> None:
    assert mc.has_capacity(fake(tmp_path, "{'a': 1, 'b': 2}", "['b']"), T0)


def test_no_free_account_is_no_capacity(tmp_path: pathlib.Path) -> None:
    assert not mc.has_capacity(fake(tmp_path, "{'a': 1, 'b': 2}", "[]"), T0)


def test_the_store_is_asked_at_the_time_of_the_run(tmp_path: pathlib.Path) -> None:
    body = FAKE.format(accounts="{'a': 1}", free="[] if now < 0 else ['a']")
    assert mc.has_capacity(switch_home(tmp_path, body), T0)


def test_a_store_that_was_never_read_is_unknown_so_capacity_is_assumed(
    tmp_path: pathlib.Path,
) -> None:
    assert mc.has_capacity(fake(tmp_path, "{}", "[]"), T0)


def test_no_claude_switch_means_capacity_without_a_warning(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert mc.has_capacity(tmp_path / "absent", T0)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "body",
    ["raise RuntimeError('boom')", "def load():\n    raise OSError('locked')", "def other(): ..."],
)
def test_a_module_that_fails_warns_and_capacity_is_assumed(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    assert mc.has_capacity(switch_home(tmp_path, body), T0)
    assert "ignoring unusable limit_state" in capsys.readouterr().err


def test_importing_it_leaves_no_trace_in_the_module_cache(tmp_path: pathlib.Path) -> None:
    home = fake(tmp_path, "{'a': 1}", "['a']")
    before = list(sys.path)
    mc.has_capacity(home, T0)
    assert "limit_state" not in sys.modules
    assert sys.path == before


def test_a_module_already_imported_elsewhere_is_put_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    monkeypatch.setitem(sys.modules, "limit_state", sentinel)  # type: ignore[arg-type]
    mc.has_capacity(fake(tmp_path, "{'a': 1}", "['a']"), T0)
    assert sys.modules["limit_state"] is sentinel


def test_each_call_reads_the_module_afresh(tmp_path: pathlib.Path) -> None:
    home = fake(tmp_path, "{'a': 1}", "['a']")
    assert mc.has_capacity(home, T0)
    (home / "limit_state.py").write_text(FAKE.format(accounts="{'a': 1}", free="[]"))
    assert not mc.has_capacity(home, T0)


def test_switch_home_follows_the_environment_and_defaults_to_the_tool_directory() -> None:
    assert mc.switch_home({"CLAUDE_SWITCH_HOME": "/x/switch"}) == pathlib.Path("/x/switch")
    assert mc.switch_home({}) == pathlib.Path("~/.claude-switch").expanduser()


def test_switch_home_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SWITCH_HOME", "/y/switch")
    assert mc.switch_home() == pathlib.Path("/y/switch")

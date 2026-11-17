"""_toml.py: what it writes reads back unchanged, and an update keeps the keys it did not touch."""

import datetime
import tomllib

import _toml
import pytest

NESTED = {
    "root": "/q",
    "count": 3,
    "ratio": 0.5,
    "on": True,
    "off": False,
    "when": datetime.datetime(2026, 1, 2, 3, 4, 5),
    "tags": ["a", "b"],
    "odd key": "x",
    "scope_binding": {
        "mode": "fixed",
        "deeper": {"quote": 'say "hi"\n', "empty": []},
        "additions": [{"slug": "a", "reason": "r"}, {"slug": "b", "reason": "s"}],
    },
}


def test_what_is_written_reads_back_unchanged():
    assert tomllib.loads(_toml.dumps(NESTED)) == NESTED


def test_a_plain_file_is_one_line_per_key():
    assert _toml.dumps({"root": "/q"}) == 'root = "/q"\n'


def test_a_value_toml_cannot_hold_is_refused():
    with pytest.raises(TypeError, match="cannot write NoneType"):
        _toml.dumps({"x": None})


def test_a_missing_file_reads_as_empty(tmp_path):
    assert _toml.read(tmp_path / "none.toml") == {}


def test_a_malformed_file_raises(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("root = [")
    with pytest.raises(tomllib.TOMLDecodeError):
        _toml.read(path)


def test_update_keeps_every_other_key(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text('root = "/q"\nextra = 7\n\n[other]\nk = "v"\n')

    _toml.update(path, {"scope_binding": {"mode": "none"}})

    assert tomllib.loads(path.read_text()) == {
        "root": "/q",
        "extra": 7,
        "other": {"k": "v"},
        "scope_binding": {"mode": "none"},
    }


def test_update_creates_the_file(tmp_path):
    path = tmp_path / "sessions" / "s.toml"
    _toml.update(path, {"root": "/q"})
    assert path.read_text() == 'root = "/q"\n'


def test_del_and_other_control_characters_read_back():
    data = {"k\x7f": "a\x7fb\x00\x1f"}
    assert tomllib.loads(_toml.dumps(data)) == data


def test_an_unreadable_file_is_healed_by_a_writer_that_owns_it(tmp_path, capsys):
    path = tmp_path / "s.toml"
    path.write_text("root = \n")
    _toml.update(path, {"root": "/q"}, existing=_toml.read_or_empty)
    assert _toml.read(path) == {"root": "/q"}
    assert "not valid TOML, rewriting it" in capsys.readouterr().err


def test_an_unreadable_file_is_not_overwritten_by_default(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text("root = \n")
    with pytest.raises(tomllib.TOMLDecodeError):
        _toml.update(path, {"other": 1})

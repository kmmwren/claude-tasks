"""Tests for config.py — loading the per-queue tasks.toml with sane defaults."""

import config


def test_defaults_when_no_file(tmp_path):
    cfg = config.load_config(tmp_path)
    assert cfg["name"] == "tasks"  # literal, not config.DEFAULTS["name"] (that's a tautology)
    assert cfg["domains"] == []
    assert cfg["timezone"] == "UTC"


def test_file_values_override_defaults(tmp_path):
    (tmp_path / "tasks.toml").write_text(
        'name = "my-project"\ndomains = ["work", "home"]\ntimezone = "Europe/London"\n'
    )
    cfg = config.load_config(tmp_path)
    assert cfg["name"] == "my-project"
    assert cfg["domains"] == ["work", "home"]
    assert cfg["timezone"] == "Europe/London"


def test_partial_file_keeps_other_defaults(tmp_path):
    (tmp_path / "tasks.toml").write_text('name = "only-name"\n')
    cfg = config.load_config(tmp_path)
    assert cfg["name"] == "only-name"
    assert cfg["domains"] == []  # untouched default


def test_unknown_keys_preserved(tmp_path):
    (tmp_path / "tasks.toml").write_text('name = "x"\nfuture_setting = 42\n')
    cfg = config.load_config(tmp_path)
    assert cfg["future_setting"] == 42


BAD = "name = [unterminated\n"
PARSE_ERROR = "Invalid value (at line 1, column 9)"


def test_an_unreadable_file_warns_one_exact_line_naming_its_path_and_yields_defaults(
    tmp_path, capsys
):
    (tmp_path / "tasks.toml").write_text(BAD)
    assert config.load_config_or_defaults(tmp_path, "reader") == config.DEFAULTS
    path = tmp_path / "tasks.toml"
    expected = f"reader: cannot read {path} ({PARSE_ERROR}); using the defaults\n"
    assert capsys.readouterr().err == expected


def test_an_unreadable_file_is_named_as_the_caller_shows_it(tmp_path, capsys):
    (tmp_path / "tasks.toml").write_text(BAD)
    config.load_config_or_defaults(tmp_path, "reader", "source/tasks.toml")
    expected = f"reader: cannot read source/tasks.toml ({PARSE_ERROR}); using the defaults\n"
    assert capsys.readouterr().err == expected

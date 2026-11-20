"""No script reads or writes text in the locale's encoding; ruff's PLW1514 needs preview, so this guards it."""

import ast
import pathlib

import pytest

SCRIPTS = pathlib.Path(__file__).parent.parent / "scripts"
NOT_FILES = {("os", "open"), ("tarfile", "open"), ("webbrowser", "open")}
BUILTIN_OPEN = {None, "io", "builtins"}  # open(path, mode); a Path's own open takes the mode first
# (call name, mode when none is given)
OPENERS = {"open": "r", "fdopen": "r", "NamedTemporaryFile": "w+b"}


def _mode(call: ast.Call, position: int, default: str) -> str:
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return str(call.args[position].value)
    return default


def _name(call: ast.Call) -> tuple[str, str | None] | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        receiver = func.value.id if isinstance(func.value, ast.Name) else None
        return func.attr, receiver
    if isinstance(func, ast.Name):
        return func.id, None
    return None


def is_text_io_without_encoding(call: ast.Call) -> bool:
    named = _name(call)
    if named is None or any(k.arg == "encoding" for k in call.keywords):
        return False
    name, receiver = named
    if name in {"read_text", "write_text"}:
        return isinstance(call.func, ast.Attribute)
    if name in OPENERS and (receiver, name) not in NOT_FILES:
        after_path = name == "fdopen" or (name == "open" and receiver in BUILTIN_OPEN)
        return "b" not in _mode(
            call, int(after_path) if name != "NamedTemporaryFile" else 0, OPENERS[name]
        )
    return False


def unspecified(source: str) -> list[int]:
    calls = (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call))
    return sorted(c.lineno for c in calls if is_text_io_without_encoding(c))


@pytest.mark.parametrize("path", sorted(SCRIPTS.glob("*.py")), ids=lambda p: p.name)
def test_every_text_read_and_write_names_its_encoding(path):
    assert unspecified(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "line",
    [
        "p.read_text()",
        "p.write_text(x)",
        "open(p)",
        "open(p, 'a')",
        "p.open('w')",
        "os.fdopen(fd, 'w')",
        "tempfile.NamedTemporaryFile('w', dir=d)",
        "p.read_text(errors='replace')",
    ],
)
def test_the_guard_catches_a_text_call_without_an_encoding(line):
    assert unspecified(line) == [1]


@pytest.mark.parametrize(
    "line",
    [
        "p.read_text(encoding='utf-8')",
        "open(p, 'rb')",
        "p.open('wb')",
        "os.open(p, flags)",
        "tarfile.open(fileobj=f)",
        "open(p, 'a', encoding='utf-8')",
        "tempfile.NamedTemporaryFile(dir=d)",
        "read_text()",
    ],
)
def test_the_guard_passes_binary_and_encoded_calls(line):
    assert unspecified(line) == []

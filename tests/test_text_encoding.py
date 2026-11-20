"""No script decodes text in the locale's encoding: files, subprocess output or stdin.

ruff's PLW1514 covers only files and needs preview, so this guards all three.
"""

import ast
import pathlib

import pytest

SCRIPTS = pathlib.Path(__file__).parent.parent / "scripts"
NOT_FILES = {("os", "open"), ("tarfile", "open"), ("webbrowser", "open")}
BUILTIN_RECEIVERS = {"io", "builtins"}  # open(path, mode); a Path's own open takes the mode first
# (call name, mode when none is given)
OPENERS = {"open": "r", "fdopen": "r", "NamedTemporaryFile": "w+b"}
TEXT_MODE_FLAGS = {"text", "universal_newlines"}
STDIN_READS = {"read", "readline", "readlines"}


def _mode(call: ast.Call, position: int, default: str) -> str:
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return str(call.args[position].value)
    return default


def _name(call: ast.Call) -> tuple[str, ast.expr | None] | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr, func.value
    if isinstance(func, ast.Name):
        return func.id, None
    return None


def _is_name(node: ast.expr | None, names: set[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in names


def _names_encoding(call: ast.Call) -> bool:
    return any(
        k.arg == "encoding" and not (isinstance(k.value, ast.Constant) and k.value.value is None)
        for k in call.keywords
    )


def _asks_for_text(call: ast.Call) -> bool:
    return any(
        k.arg in TEXT_MODE_FLAGS and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in call.keywords
    )


def _reads_stdin(name: str, receiver: ast.expr | None) -> bool:
    return (
        name in STDIN_READS
        and isinstance(receiver, ast.Attribute)
        and receiver.attr == "stdin"
        and _is_name(receiver.value, {"sys"})
    )


def _opens_text(call: ast.Call, name: str, receiver: ast.expr | None) -> bool:
    if isinstance(receiver, ast.Name) and (receiver.id, name) in NOT_FILES:
        return False
    builtin = receiver is None or _is_name(receiver, BUILTIN_RECEIVERS)
    after_path = name == "fdopen" or (name == "open" and builtin)
    position = 0 if name == "NamedTemporaryFile" else int(after_path)
    return "b" not in _mode(call, position, OPENERS[name])


def is_text_io_without_encoding(call: ast.Call) -> bool:
    named = _name(call)
    if named is None or _names_encoding(call):
        return False
    name, receiver = named
    if _asks_for_text(call) or _reads_stdin(name, receiver):
        return True
    if name in {"read_text", "write_text"}:
        return receiver is not None
    return name in OPENERS and _opens_text(call, name, receiver)


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
        "io.open(p)",
        "p.open('w')",
        "self.path.open('w')",
        "os.fdopen(fd, 'w')",
        "tempfile.NamedTemporaryFile('w', dir=d)",
        "p.read_text(errors='replace')",
        "p.read_text(encoding=None)",
        "subprocess.run(cmd, text=True)",
        "subprocess.run(cmd, capture_output=True, universal_newlines=True)",
        "_git(root, 'log', text=True)",
        "subprocess.run(cmd, text=True, encoding=None)",
        "sys.stdin.read()",
        "sys.stdin.readline()",
        "sys.stdin.readlines()",
    ],
)
def test_the_guard_catches_a_text_call_without_an_encoding(line):
    assert unspecified(line) == [1]


@pytest.mark.parametrize(
    "line",
    [
        "p.read_text(encoding='utf-8')",
        "open(p, 'rb')",
        "builtins.open(p, 'wb')",
        "p.open('wb')",
        "self.path.open('rb')",
        "Path(p).open('rb')",
        "os.open(p, flags)",
        "tarfile.open(fileobj=f)",
        "open(p, 'a', encoding='utf-8')",
        "tempfile.NamedTemporaryFile(dir=d)",
        "read_text()",
        "subprocess.run(cmd, encoding='utf-8')",
        "subprocess.run(cmd, capture_output=True)",
        "subprocess.run(cmd, text=False)",
        "sys.stdin.isatty()",
        "sys.stdin.buffer.read()",
        "f.read()",
    ],
)
def test_the_guard_passes_binary_and_encoded_calls(line):
    assert unspecified(line) == []

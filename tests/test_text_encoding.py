"""No script decodes text in the locale's encoding: files, temp files, subprocess output, stdin or input().

ruff's PLW1514 covers only files and needs preview, so this guards the rest too.
"""

import ast
import pathlib
from typing import NamedTuple

import pytest

SCRIPTS = pathlib.Path(__file__).parent.parent / "scripts"
NOT_FILES = {("os", "open"), ("tarfile", "open"), ("webbrowser", "open")}
BUILTIN_RECEIVERS = {"io", "builtins"}  # open(path, mode); a Path's own open takes the mode first
CODECS_RECEIVERS = {"codecs"}
# errors= implies text mode for subprocess
TEXT_MODE_FLAGS = {"text", "universal_newlines", "errors"}
# bytes.decode and str.encode take errors=; replace's keywords name dataclass fields, such as text=
NOT_MODE_KEYWORDS = {"decode", "encode", "replace"}
# .buffer is bytes; isatty and fileno read nothing
BINARY_STDIN_ATTRS = {"buffer", "isatty", "fileno"}


class Signature(NamedTuple):
    encoding: int  # positional index of the encoding parameter
    mode: int | None = None  # positional index of the mode; None when the call is always text
    default: str = "r"  # mode when none is given


BUILTIN_OPEN = Signature(encoding=3, mode=1)
PATH_OPEN = Signature(encoding=2, mode=0)
CODECS_OPEN = Signature(encoding=2, mode=1)
# Temp files default to binary, unlike open
FACTORIES = {
    "fdopen": BUILTIN_OPEN,
    "NamedTemporaryFile": Signature(encoding=2, mode=0, default="w+b"),
    "TemporaryFile": Signature(encoding=2, mode=0, default="w+b"),
    "SpooledTemporaryFile": Signature(encoding=3, mode=1, default="w+b"),
}
PATH_TEXT_METHODS = {"read_text": Signature(encoding=0), "write_text": Signature(encoding=1)}


def _mode(call: ast.Call, signature: Signature) -> str:
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    position = signature.mode
    if position is not None and len(call.args) > position:
        argument = call.args[position]
        if isinstance(argument, ast.Constant):
            return str(argument.value)
    return signature.default


def _name(call: ast.Call) -> tuple[str, ast.expr | None] | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr, func.value
    if isinstance(func, ast.Name):
        return func.id, None
    return None


def _is_name(node: ast.expr | None, names: set[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in names


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_off(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and not node.value


def _open_signature(name: str, receiver: ast.expr | None) -> Signature | None:
    if isinstance(receiver, ast.Name) and (receiver.id, name) in NOT_FILES:
        return None
    if _is_name(receiver, CODECS_RECEIVERS):
        return CODECS_OPEN
    if receiver is None or _is_name(receiver, BUILTIN_RECEIVERS):
        return BUILTIN_OPEN
    return PATH_OPEN


def _signature(name: str, receiver: ast.expr | None) -> Signature | None:
    if name == "open":
        return _open_signature(name, receiver)
    if name in PATH_TEXT_METHODS:
        return PATH_TEXT_METHODS[name] if receiver is not None else None
    return FACTORIES.get(name)


def _names_encoding(call: ast.Call, signature: Signature | None) -> bool:
    if any(k.arg == "encoding" and not _is_none(k.value) for k in call.keywords):
        return True
    if signature is None or len(call.args) <= signature.encoding:
        return False
    return not _is_none(call.args[signature.encoding])


def _asks_for_text(call: ast.Call, name: str) -> bool:
    if name in NOT_MODE_KEYWORDS:
        return False
    return any(k.arg in TEXT_MODE_FLAGS and not _is_off(k.value) for k in call.keywords)


def _opens_text(call: ast.Call, signature: Signature) -> bool:
    return signature.mode is None or "b" not in _mode(call, signature)


def is_text_io_without_encoding(call: ast.Call) -> bool:
    named = _name(call)
    if named is None:
        return False
    name, receiver = named
    if name == "input" and receiver is None:
        return True
    signature = _signature(name, receiver)
    if _names_encoding(call, signature):
        return False
    if _asks_for_text(call, name):
        return True
    return signature is not None and _opens_text(call, signature)


def _is_stdin(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute) and node.attr == "stdin" and _is_name(node.value, {"sys"})
    )


def _locale_stdin_lines(nodes: list[ast.AST]) -> list[int]:
    binary = {
        id(n.value)
        for n in nodes
        if isinstance(n, ast.Attribute) and n.attr in BINARY_STDIN_ATTRS and _is_stdin(n.value)
    }
    return [
        n.lineno
        for n in nodes
        if isinstance(n, ast.Attribute) and _is_stdin(n) and id(n) not in binary
    ]


def unspecified(source: str) -> list[int]:
    nodes = list(ast.walk(ast.parse(source)))
    calls = [n.lineno for n in nodes if isinstance(n, ast.Call) and is_text_io_without_encoding(n)]
    return sorted(calls + _locale_stdin_lines(nodes))


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
        "tempfile.TemporaryFile('w')",
        "tempfile.SpooledTemporaryFile(mode='w')",
        "codecs.open(p)",
        "p.read_text(errors='replace')",
        "p.read_text(encoding=None)",
        "subprocess.run(cmd, text=True)",
        "subprocess.run(cmd, capture_output=True, universal_newlines=True)",
        "subprocess.run(cmd, errors='replace')",
        "subprocess.run(cmd, text=wants_text)",
        "subprocess.run(cmd, text=not binary)",
        "subprocess.run(cmd, universal_newlines=wants_text)",
        "_git(root, 'log', text=True)",
        "subprocess.run(cmd, text=True, encoding=None)",
        "sys.stdin.read()",
        "sys.stdin.readline()",
        "sys.stdin.readlines()",
        "for line in sys.stdin: pass",
        "json.load(sys.stdin)",
        "input()",
        "input('> ')",
    ],
)
def test_the_guard_catches_a_text_call_without_an_encoding(line):
    assert unspecified(line) == [1]


def test_the_guard_reports_the_line_of_a_stdin_read_inside_a_longer_call():
    assert unspecified("import json\n\ndata = json.load(\n    sys.stdin\n)\n") == [4]


@pytest.mark.parametrize(
    "line",
    [
        "p.read_text(encoding='utf-8')",
        "p.read_text('utf-8')",
        "p.write_text(x, 'utf-8')",
        "open(p, 'rb')",
        "builtins.open(p, 'wb')",
        "p.open('wb')",
        "self.path.open('rb')",
        "Path(p).open('rb')",
        "p.open('r', -1, 'utf-8')",
        "open(p, 'r', -1, 'utf-8')",
        "codecs.open(p, 'r', 'utf-8')",
        "os.open(p, flags)",
        "tarfile.open(fileobj=f)",
        "open(p, 'a', encoding='utf-8')",
        "tempfile.NamedTemporaryFile(dir=d)",
        "tempfile.TemporaryFile()",
        "tempfile.SpooledTemporaryFile()",
        "read_text()",
        "subprocess.run(cmd, encoding='utf-8')",
        "subprocess.run(cmd, capture_output=True)",
        "subprocess.run(cmd, text=False)",
        "subprocess.run(cmd, text=None)",
        "run.stderr.decode(errors='replace').strip()",
        "_git(root, 'show', capture_output=True).stdout.decode(errors='ignore')",
        "b.decode(errors='replace')",
        "s.encode(errors='ignore')",
        "dataclasses.replace(self, text=text)",
        "sys.stdin.isatty()",
        "sys.stdin.fileno()",
        "sys.stdin.buffer.read()",
        "f.read()",
    ],
)
def test_the_guard_passes_binary_and_encoded_calls(line):
    assert unspecified(line) == []

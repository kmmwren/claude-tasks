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
# bytes.decode, str() and str.encode take errors= but decode as UTF-8; replace and _replace take
# fields such as text=; mkstemp(text=True) returns a descriptor, which fdopen is caught opening
NOT_MODE_KEYWORDS = {"decode", "encode", "str", "replace", "_replace", "mkstemp"}
# A capitalised call builds an object whose text= is a field; Popen is the one that runs a command
SUBPROCESS_CLASSES = {"Popen"}
STDIN_NAMES = {"stdin", "__stdin__"}
# .buffer is bytes; isatty and fileno read nothing
BINARY_STDIN_ATTRS = {"buffer", "isatty", "fileno"}


class Signature(NamedTuple):
    encoding: int | None  # positional index of the encoding parameter; None when keyword-only
    mode: int | None = None  # positional index of the mode; None when only a keyword sets it
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
    "TextIOWrapper": Signature(encoding=1),
    "FileType": Signature(encoding=2, mode=0),
}
QUALIFIED_FACTORIES = {
    ("os", "popen"): Signature(encoding=None, mode=1),
    ("fileinput", "input"): Signature(encoding=None),
    ("gzip", "open"): Signature(encoding=3, mode=1, default="rb"),
    ("bz2", "open"): Signature(encoding=3, mode=1, default="rb"),
    ("lzma", "open"): Signature(encoding=None, mode=1, default="rb"),
}
PATH_TEXT_METHODS = {"read_text": Signature(encoding=0), "write_text": Signature(encoding=1)}


def _mode(call: ast.Call, signature: Signature) -> str | None:
    """The mode the call opens with, or None when it is computed and so cannot be known here."""
    given = [k.value for k in call.keywords if k.arg == "mode"]
    if signature.mode is not None and len(call.args) > signature.mode:
        given.append(call.args[signature.mode])
    if not given:
        return signature.default
    return str(given[0].value) if isinstance(given[0], ast.Constant) else None


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
    if isinstance(receiver, ast.Name) and (receiver.id, name) in QUALIFIED_FACTORIES:
        return QUALIFIED_FACTORIES[receiver.id, name]
    if name == "open":
        return _open_signature(name, receiver)
    if name in PATH_TEXT_METHODS:
        return PATH_TEXT_METHODS[name] if receiver is not None else None
    return FACTORIES.get(name)


def _names_encoding(call: ast.Call, signature: Signature | None) -> bool:
    if any(k.arg == "encoding" and not _is_none(k.value) for k in call.keywords):
        return True
    position = None if signature is None else signature.encoding
    if position is None or len(call.args) <= position:
        return False
    return not _is_none(call.args[position])


def _builds_an_object(name: str) -> bool:
    return name[:1].isupper() and name not in SUBPROCESS_CLASSES


def _asks_for_text(call: ast.Call, name: str) -> bool:
    if name in NOT_MODE_KEYWORDS or _builds_an_object(name):
        return False
    return any(k.arg in TEXT_MODE_FLAGS and not _is_off(k.value) for k in call.keywords)


def _opens_text(call: ast.Call, signature: Signature) -> bool:
    mode = _mode(call, signature)
    return mode is None or "b" not in mode


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
        isinstance(node, ast.Attribute)
        and node.attr in STDIN_NAMES
        and _is_name(node.value, {"sys"})
    )


def _imports_stdin(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.ImportFrom)
        and node.module == "sys"
        and any(alias.name in STDIN_NAMES for alias in node.names)
    )


def _locale_stdin_lines(nodes: list[ast.AST]) -> list[int]:
    binary = {
        id(n.value)
        for n in nodes
        if isinstance(n, ast.Attribute) and n.attr in BINARY_STDIN_ATTRS and _is_stdin(n.value)
    }
    reads = [
        n.lineno
        for n in nodes
        if isinstance(n, ast.Attribute) and _is_stdin(n) and id(n) not in binary
    ]
    imports = [n.lineno for n in nodes if isinstance(n, ast.ImportFrom) and _imports_stdin(n)]
    return reads + imports


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
        "io.TextIOWrapper(b)",
        "os.popen(cmd)",
        "argparse.FileType('r')",
        "fileinput.input()",
        "gzip.open(p, 'rt')",
        "lzma.open(p, 'wt')",
        "from sys import stdin",
        "sys.__stdin__.read()",
        "tempfile.NamedTemporaryFile(mode)",
        "tempfile.TemporaryFile(mode=wanted)",
        "subprocess.Popen(cmd, text=True)",
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
        "BriefFile(p, text=x)",
        "brief._replace(text=x)",
        "tempfile.mkstemp(text=True)",
        "str(b, errors='replace')",
        "io.TextIOWrapper(b, 'utf-8')",
        "argparse.FileType('rb')",
        "fileinput.input(encoding='utf-8')",
        "gzip.open(p)",
        "gzip.open(p, 'rt', 9, 'utf-8')",
        "lzma.open(p, 'rt', encoding='utf-8')",
        "from sys import argv",
    ],
)
def test_the_guard_passes_binary_and_encoded_calls(line):
    assert unspecified(line) == []

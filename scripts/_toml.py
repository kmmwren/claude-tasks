"""Write TOML, which `tomllib` only reads, and update one top-level key of a file without losing the rest.

The per-session file holds keys owned by different scripts (`root`, `[scope_binding]`), so each writer
changes its own key and round-trips everything else.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import contextlib
import datetime
import json
import pathlib
import re
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from _concurrency import atomic_write, exclusive_lock

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_TEMPORAL = (datetime.datetime, datetime.date, datetime.time)
_DEL = re.compile("\x7f")


def _string(text: str) -> str:
    """A TOML basic string: JSON escapes every control character but DEL, which TOML also forbids."""
    return _DEL.sub(r"\\u007f", json.dumps(text, ensure_ascii=False))


def _key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else _string(name)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, Mapping) for v in value)


def _is_section(value: Any) -> bool:
    return isinstance(value, Mapping) or _is_table_array(value)


def _value(value: Any) -> str:
    """One inline TOML value; a bool is checked before an int because it is one."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, _TEMPORAL):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def _lines(data: Mapping[str, Any], path: tuple[str, ...]) -> list[str]:
    """`data`'s plain keys, then each sub-table and array of tables under `path`."""
    lines = [f"{_key(k)} = {_value(v)}" for k, v in data.items() if not _is_section(v)]
    for name, value in data.items():
        if isinstance(value, Mapping):
            lines += ["", f"[{'.'.join((*path, _key(name)))}]", *_lines(value, (*path, _key(name)))]
        elif _is_table_array(value):
            for item in value:
                lines += [
                    "",
                    f"[[{'.'.join((*path, _key(name)))}]]",
                    *_lines(item, (*path, _key(name))),
                ]
    return lines


def dumps(data: Mapping[str, Any]) -> str:
    """`data` as TOML text that `tomllib.loads` reads back to the same mapping."""
    return "\n".join(_lines(data, ())).lstrip("\n") + "\n"


def read(path: pathlib.Path) -> dict[str, Any]:
    """The file's table, `{}` when it does not exist; a file that is not valid TOML raises."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def read_or_empty(path: pathlib.Path) -> dict[str, Any]:
    """The file's table, or `{}` with a warning when it is not valid TOML, for a writer that may heal it."""
    try:
        return read(path)
    except tomllib.TOMLDecodeError as err:
        print(f"{path}: not valid TOML, rewriting it: {err}", file=sys.stderr)
        return {}


Reader = Callable[[pathlib.Path], Mapping[str, Any]]


@contextlib.contextmanager
def locked(path: pathlib.Path) -> Iterator[None]:
    """Hold the file's writer lock, so a read-change-write inside it never drops another writer's keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(".lock")) as held:
        if not held:
            raise TimeoutError(f"{path} is locked by another writer")
        yield


def merge(path: pathlib.Path, changes: Mapping[str, Any], existing: Reader = read) -> None:
    """Set the top-level keys in `changes` over what `existing` reads; the caller holds `locked(path)`."""
    text = dumps({**existing(path), **changes})
    tomllib.loads(text)
    atomic_write(path, text)


def update(path: pathlib.Path, changes: Mapping[str, Any], existing: Reader = read) -> None:
    """Set the top-level keys in `changes` and keep every other key, under the file's lock."""
    with locked(path):
        merge(path, changes, existing)

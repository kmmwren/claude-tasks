#!/usr/bin/env python3
"""
bind_scope.py: bind a Claude Code session to the part of the queue it may work.

A session works only the briefs its binding admits (see scope_binding.py). The binding is stored in
the session's own file next to the queue it is bound to, so `root` and every other key survive.

Usage:
  python scripts/bind_scope.py --session <id> --scope project:lite --mode fixed
  python scripts/bind_scope.py --session <id> --scope tag:fdo --mode ongoing
  python scripts/bind_scope.py --session <id> --scope all --mode none
  python scripts/bind_scope.py --session <id> --add some-brief --reason "found while fixing the parent"

`fixed` records the open briefs the scope admits now and grows only through `--add` with a `--reason`;
binding the session again over a fixed set also needs `--reason`, which is kept with the binding.
"""

import _compat  # noqa: F401  - version guard; must run before the imports and annotations below

# isort: split
import argparse
import pathlib

import scope_binding
from _paths import resolve_root
from scope import SCOPE_HELP
from scope_binding import Binding, BindingError


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Bind a session to the queue scope it may work")
    ap.add_argument("--session", required=True, help="the Claude Code session id")
    ap.add_argument("--scope", help=SCOPE_HELP)
    ap.add_argument("--mode", choices=list(scope_binding.MODES), help="how the scope is worked")
    ap.add_argument("--add", metavar="SLUG", help="add a brief to this session's fixed set")
    ap.add_argument(
        "--reason", help="why a brief is added or a fixed set replaced (required for either)"
    )
    ap.add_argument(
        "--root", help="the queue to resolve a fixed set from (default: the resolved queue)"
    )
    return ap


def refuse_incomplete(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Every combination of flags that cannot mean one clear thing."""
    if bool(args.scope) != bool(args.mode):
        ap.error("--scope and --mode go together")
    if not args.scope and not args.add:
        ap.error("give --scope and --mode to bind, or --add and --reason to grow a fixed set")
    if args.add and not args.reason:
        ap.error("--add needs --reason: a fixed set grows only with a stated reason")


def current(existing: Binding | None, args: argparse.Namespace) -> Binding:
    """The binding being changed: a new one when --scope is given, else the session's existing one."""
    if args.scope:
        return rebound(existing, args)
    if existing is None:
        raise BindingError(
            f"session {args.session} has no binding to add to; bind it with --scope and --mode"
        )
    return existing


def queue_root(args: argparse.Namespace) -> pathlib.Path:
    return pathlib.Path(args.root) if args.root else resolve_root()


def rebound(existing: Binding | None, args: argparse.Namespace) -> Binding:
    """A new binding from --scope; replacing a fixed set needs --reason, so it cannot widen silently."""
    fresh = Binding.bind(args.scope, scope_binding.parse_mode(args.mode), queue_root(args))
    if existing is None:
        return fresh
    return existing.replaced_by(fresh, args.reason or "", scope_binding.now())


def apply(existing: Binding | None, args: argparse.Namespace) -> Binding:
    binding = current(existing, args)
    return grown(binding, args) if args.add else binding


def grown(binding: Binding, args: argparse.Namespace) -> Binding:
    """The binding with --add, once its mode allows it and the slug names a brief."""
    added = binding.added(args.add, args.reason, scope_binding.now())
    scope_binding.require_brief(binding.selector, args.add, queue_root(args))
    return added


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    refuse_incomplete(ap, args)
    try:
        binding, path = scope_binding.change(args.session, lambda existing: apply(existing, args))
    except (BindingError, ValueError, TimeoutError) as err:
        raise SystemExit(f"bind_scope: {err}") from err
    print(
        f"{path}: {binding.selector} ({binding.mode.name}, {len(binding.member_slugs)} member(s))"
    )


if __name__ == "__main__":
    main()  # pragma: no cover

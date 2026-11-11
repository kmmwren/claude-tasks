#!/usr/bin/env python3
"""
triage_sources.py — what counts as a source a brief can cite, and whether a citation holds up.

A brief leaves the inbox only when its context cites a source that exists right now: a path on
disk, a GitHub PR, issue or repo, or another brief in the queue. Text an LLM wrote may cite only
sources already in the brief it came from, so an invented reference can never carry a brief to
ready/. Bare file names and ticket keys are recognised but cannot be checked, so they never
qualify a brief on their own.

Stdlib only.
"""
import pathlib
import re
import subprocess
from collections.abc import Callable

from transition import find_brief

_URL = r"https?://[^\s)>\]`'\"]+"
_PATH = r"(?:~|/Users|/tmp|\.\.?)/[\w./@-]+"
_PR_REF = r"\b[\w.-]+/[\w.-]+#\d+\b"
_TICKET = r"\b[A-Z][A-Z0-9]+-\d+\b"
_FILE = r"\b[\w./-]+\.(?:py|sh|md|ts|js|json|toml|ya?ml|scala|java|sql|plist|html|conf|gradle)\b"
SOURCE = re.compile("|".join((_URL, _PATH, _PR_REF, _TICKET, _FILE)))
# Standards and encodings shaped like ticket keys.
NOT_TICKETS = frozenset({"UTF", "SHA", "ISO", "RFC", "TLS", "HTTP", "ES", "PEP"})
GH_ISSUE = re.compile(r"(?:https://github\.com/)?([\w.-]+/[\w.-]+)(?:/(?:pull|issues)/|#)(\d+)")
GH_REPO = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?(?:/.*)?")
QUEUE_BRIEF = re.compile(r"(?:inbox|ready|in-progress|blocked|done|parked)/([\w.-]+)\.md")

SourceCheck = Callable[[str], bool]


def _is_source(token: str) -> bool:
    prefix = token.partition("-")[0]
    return not (re.fullmatch(_TICKET, token) and prefix in NOT_TICKETS)


def cited_sources(text: str) -> set[str]:
    """Every source reference in `text`."""
    return {t.rstrip(".,;:") for t in SOURCE.findall(text) if _is_source(t)}


def gh_resolves(api_path: str) -> bool:
    """True when the GitHub API answers `api_path`; offline or unknown counts as not found."""
    try:
        return subprocess.run(["gh", "api", api_path, "--silent"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class LiveSources:
    """Whether a cited source exists now, each answer cached for the run."""

    def __init__(self, root: pathlib.Path, gh: Callable[[str], bool] = gh_resolves) -> None:
        self._root, self._gh, self._seen = root, gh, dict[str, bool]()

    def __call__(self, source: str) -> bool:
        if source not in self._seen:
            self._seen[source] = self._exists(source)
        return self._seen[source]

    def _exists(self, source: str) -> bool:
        if m := GH_ISSUE.fullmatch(source):
            return self._gh(f"repos/{m[1]}/issues/{m[2]}")
        if m := GH_REPO.fullmatch(source):
            return self._gh(f"repos/{m[1]}")
        if m := QUEUE_BRIEF.fullmatch(source):
            return find_brief(m[1], self._root) is not None
        return source.startswith(("~", "/")) and pathlib.Path(source).expanduser().exists()


def grounded(generated: str, original: str, exists: SourceCheck) -> bool:
    """`generated` cites only sources from `original`, and at least one of them exists."""
    cited = cited_sources(generated)
    return cited <= cited_sources(original) and any(map(exists, cited))

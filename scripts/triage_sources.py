#!/usr/bin/env python3
"""
triage_sources.py — what counts as a real source a brief can cite, and whether generated text is grounded.

A brief leaves the inbox only when its context points at something checkable: a URL, a file
path, a PR reference or a ticket key. Text an LLM wrote must cite a source that already
appears in the brief it came from, so an invented reference can never carry a brief to ready/.

Stdlib only.
"""
import re

_URL = r"https?://[^\s)>\]]+"
_PATH = r"(?:~|/Users|/tmp|\.\.?)/[\w./@-]+"
_PR_REF = r"\b[\w.-]+/[\w.-]+#\d+\b"
_TICKET = r"\b[A-Z][A-Z0-9]+-\d+\b"
_FILE = r"\b[\w./-]+\.(?:py|sh|md|ts|js|json|toml|ya?ml|scala|java|sql|plist|html|conf|gradle)\b"
SOURCE = re.compile("|".join((_URL, _PATH, _PR_REF, _TICKET, _FILE)))
# Standards and encodings shaped like ticket keys.
NOT_TICKETS = frozenset({"UTF", "SHA", "ISO", "RFC", "TLS", "HTTP", "ES", "PEP"})


def _is_source(token: str) -> bool:
    prefix = token.partition("-")[0]
    return not (re.fullmatch(_TICKET, token) and prefix in NOT_TICKETS)


def cited_sources(text: str) -> set[str]:
    """Every source reference in `text`."""
    return {t.rstrip(".,;:") for t in SOURCE.findall(text) if _is_source(t)}


def grounded(generated: str, original: str) -> bool:
    """True when `generated` cites at least one source and every source it cites appears in `original`."""
    cited = cited_sources(generated)
    return bool(cited) and cited <= cited_sources(original)

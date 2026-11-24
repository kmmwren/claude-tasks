"""
supersession.py: decide whether the briefs that replace a closing brief already carry its open criteria.

The check is lexical so it needs no model, and it fails towards refusing the close. Stdlib only.
"""

import pathlib
import re
from collections.abc import Callable
from dataclasses import dataclass

# A note that says "the work moved elsewhere" is a supersession claim, which a plain tick cannot verify.
SUPERSEDED_NOTE = re.compile(r"supersed|duplicate|replaced by|folded into", re.I)
# A criterion counts as carried when this share of its significant words appear in the successors.
CARRY_THRESHOLD = 0.6
MIN_TOKEN_LEN = 4
STOPWORDS = frozenset(
    {
        "with",
        "that",
        "this",
        "from",
        "have",
        "been",
        "will",
        "into",
        "when",
        "then",
        "than",
        "them",
        "they",
        "their",
        "there",
        "which",
        "while",
        "what",
        "where",
        "were",
        "does",
        "your",
        "also",
        "each",
        "must",
        "should",
        "would",
        "could",
        "only",
        "ever",
        "just",
        "such",
        "both",
    }
)


class UncarriedCriteriaError(ValueError):
    """A brief cannot close as superseded while its successors lack one of its open criteria."""

    def __init__(self, brief_id: str, lines: list[str], successors: list[str]):
        self.lines = lines
        listing = "\n".join(f"  {ln.strip()}" for ln in lines)
        super().__init__(
            f"{brief_id!r} has {len(lines)} unticked success criteria not carried by "
            f"{', '.join(successors)}; not moved to done:\n{listing}\n"
            f"Append each of these to a successor brief's Success criteria (triage never loses "
            f"work), then re-run the move."
        )


def significant_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric words long enough to carry meaning, minus filler."""
    words = re.split(r"[^a-z0-9]+", text.lower())
    return {w for w in words if len(w) >= MIN_TOKEN_LEN and w not in STOPWORDS}


@dataclass(frozen=True)
class Successors:
    """The live briefs that take over a closing brief's work."""

    paths: tuple[pathlib.Path, ...]

    @classmethod
    def resolve(
        cls, brief_id: str, successor_ids: list[str], find: Callable[[str], pathlib.Path | None]
    ) -> "Successors":
        """Locate each successor; a missing, finished or self-referencing one cannot carry the work."""
        paths = []
        for sid in dict.fromkeys(successor_ids):
            path = find(sid)
            if sid == brief_id or path is None or path.parent.name == "done":
                raise ValueError(
                    f"successor {sid!r} must be an existing brief outside done/ (and not "
                    f"{brief_id!r} itself); {brief_id!r} was NOT moved"
                )
            paths.append(path)
        return cls(tuple(paths))

    @property
    def ids(self) -> list[str]:
        return [p.stem for p in self.paths]

    def carries(self, criterion: str, tokens: set[str]) -> bool:
        """Whether the successors' words cover enough of the criterion's to hold the same requirement.

        A criterion with no significant words cannot be verified, so it counts as not carried.
        """
        wanted = significant_tokens(criterion)
        return bool(wanted) and len(wanted & tokens) / len(wanted) >= CARRY_THRESHOLD

    def require_carried(self, brief_id: str, criteria: list[str]) -> None:
        """Raise UncarriedCriteriaError naming every criterion the successors' combined text lacks."""
        tokens = significant_tokens("\n".join(p.read_text(encoding="utf-8") for p in self.paths))
        missing = [c for c in criteria if not self.carries(c, tokens)]
        if missing:
            raise UncarriedCriteriaError(brief_id, missing, self.ids)

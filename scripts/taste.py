#!/usr/bin/env python3
"""The taste profile: what the queue's owner values, as a human-edited markdown file.

    <queue>/profile/priorities.md

    ---
    draft: true
    updated: 2026-11-11
    ---
    ## Goals
    - Fast data onboarding | weight 1 | match fast-data-onboarding, project:fdo | analytics packs live
    ## Values
    - NS-1 An instinct-style assistant that says what to do next
    ## Dislikes
    - Busywork that no goal needs
    ## Patterns
    - Settled 2026-11-11: predictor head ranks, prioritise.py filters

The ranker hands it to the judge as the rubric; learn.py proposes versioned goal-weight edits.
"""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import re
import sys
from collections.abc import Mapping

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from deps import fm  # noqa: E402

PROFILE_FILE = pathlib.PurePosixPath("profile/priorities.md")
LISTS = ("Values", "Dislikes", "Patterns")
DEFAULT_WEIGHT = 1.0
_SECTION = re.compile(r"(?m)^## +(\w+)[ \t]*$")
_BULLET = re.compile(r"(?m)^- +(.+?)\s*$")


class ProfileError(ValueError):
    """The profile file cannot be read as a profile; the ranker falls back to filter order."""


@dataclasses.dataclass(frozen=True)
class Goal:
    name: str
    weight: float = DEFAULT_WEIGHT
    match: tuple[str, ...] = ()
    note: str = ""

    @classmethod
    def parse(cls, line: str) -> Goal:
        name, *fields = (part.strip() for part in line.split("|"))
        weight = next((f[7:] for f in fields if f.startswith("weight ")), str(DEFAULT_WEIGHT))
        match = next((f[6:] for f in fields if f.startswith("match ")), "")
        note = " | ".join(f for f in fields if not f.startswith(("weight ", "match ")))
        try:
            return cls(name, float(weight), tuple(t.strip() for t in match.split(",") if t.strip()), note)
        except ValueError:
            raise ProfileError(f"goal {name!r}: weight {weight!r} is not a number") from None

    def render(self) -> str:
        fields = [self.name, f"weight {self.weight:g}"]
        fields += [f"match {', '.join(self.match)}"] if self.match else []
        return "- " + " | ".join(fields + ([self.note] if self.note else []))

    def matches(self, tags: frozenset[str], text: str) -> bool:
        """A goal matches a brief tagged with one of its tokens or naming one in its text."""
        lowered = text.lower()
        return any(token in tags or token.lower() in lowered for token in self.match)


@dataclasses.dataclass(frozen=True)
class Profile:
    goals: tuple[Goal, ...] = ()
    values: tuple[str, ...] = ()
    dislikes: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    draft: bool = True
    updated: str = ""

    @property
    def version(self) -> str:
        """Hash of the canonical render, so prose outside the sections never invalidates the cache."""
        return hashlib.sha256(render(self).encode()).hexdigest()[:12]

    @property
    def weights(self) -> dict[str, float]:
        return {goal.name: goal.weight for goal in self.goals}

    def with_weights(self, weights: Mapping[str, float], updated: str) -> Profile:
        goals = tuple(dataclasses.replace(g, weight=weights.get(g.name, g.weight)) for g in self.goals)
        return dataclasses.replace(self, goals=goals, updated=updated)

    def matched_goals(self, tags: frozenset[str], text: str) -> tuple[str, ...]:
        return tuple(goal.name for goal in self.goals if goal.matches(tags, text))

    def rubric(self) -> str:
        """The profile as the judge reads it: goals heaviest first, then the plain lists."""
        goals = sorted(self.goals, key=lambda g: -g.weight)
        lines = ["Goals (weight = importance):"] + [f"- {g.name} (weight {g.weight:g}) {g.note}".rstrip()
                                                     for g in goals]
        for title, items in zip(LISTS, (self.values, self.dislikes, self.patterns)):
            lines += [f"{title}:"] + [f"- {item}" for item in items]
        return "\n".join(lines)


def parse(text: str) -> Profile:
    meta = fm(text)
    sections = _sections(text)
    return Profile(
        goals=tuple(Goal.parse(line) for line in sections.get("Goals", [])),
        values=tuple(sections.get("Values", [])),
        dislikes=tuple(sections.get("Dislikes", [])),
        patterns=tuple(sections.get("Patterns", [])),
        draft=meta.get("draft", "true").lower() != "false",
        updated=meta.get("updated", ""),
    )


def _sections(text: str) -> dict[str, list[str]]:
    parts = _SECTION.split(text)
    return {name: _BULLET.findall(body) for name, body in zip(parts[1::2], parts[2::2])}


def render(profile: Profile) -> str:
    head = f"---\ndraft: {str(profile.draft).lower()}\nupdated: {profile.updated}\n---\n# Priorities\n"
    if profile.draft:
        head += ("\n> DRAFT seeded by seed_profile.py. Edit freely: reweight, cut, add dislikes,"
                 " then set `draft: false`.\n")
    body = [("Goals", [g.render() for g in profile.goals])]
    body += [(title, [f"- {item}" for item in items])
             for title, items in zip(LISTS, (profile.values, profile.dislikes, profile.patterns))]
    return head + "".join(f"\n## {title}\n" + "".join(f"{line}\n" for line in lines) for title, lines in body)


def profile_path(root: pathlib.Path) -> pathlib.Path:
    return root / PROFILE_FILE


def load_profile(root: pathlib.Path) -> Profile | None:
    """The queue's profile, or None when it has not been seeded yet."""
    path = profile_path(root)
    return parse(path.read_text()) if path.is_file() else None

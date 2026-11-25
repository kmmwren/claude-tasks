#!/usr/bin/env python3
"""
slack_links.py: split a pasted block of Slack permalinks and notes into handoff items.

Each permalink starts an item; the text after it, up to the next permalink, is that item's
note. Text before the first permalink is a direction shared by the whole batch. A link is
flagged parked when the direction or its own note mentions parking.
Each link carries `key`, the one canonical thread identity (`slack:<channel>/<thread_ts>`) that
dedupes a pasted link against an intake-watched thread. Nothing else builds that key.
Prints JSON: {"direction": str, "links": [{url, channel, message_ts, thread_ts, key, note, parked}]}.
"""

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlparse

from _paths import read_stdin

# Only archives/<channel>/p<ts> is a permalink; a link cut off at the channel (a truncated
# session name) reads as plain text, since locate() cannot place it in a thread.
PERMALINK = re.compile(
    r"<?(https://[\w.-]+\.slack\.com/archives/[A-Za-z0-9]+/p\d+/?(?:\?[^\s>|]*)?)(?:\|[^>]*)?>?"
)
PARK = re.compile(r"\bpark(?:ed|ing)?\b", re.IGNORECASE)
# Parking needs her explicit word, so a negated or questioning mention never parks.
NOT_PARK = re.compile(
    r"\b(?:don'?t|do not|never|not|no)\s+(?:\w+\s+)?park|\bwhy\b[^.?!]*\bpark|\bpark[^.?!]*\?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SlackLink:
    url: str
    channel: str
    message_ts: str
    thread_ts: str
    key: str
    note: str
    parked: bool


@dataclass(frozen=True)
class Batch:
    direction: str
    links: list[SlackLink]


def to_ts(segment: str) -> str:
    digits = segment.removeprefix("p")
    return f"{digits[:-6]}.{digits[-6:]}"


def thread_key(channel: str, thread_ts: str) -> str:
    return f"slack:{channel}/{thread_ts}"


def asks_to_park(direction: str, note: str) -> bool:
    words = f"{direction} {note}"
    return PARK.search(words) is not None and NOT_PARK.search(words) is None


def locate(url: str) -> tuple[str, str, str]:
    """The (channel, message_ts, thread_ts) a permalink points at; a top-level message roots its own thread."""
    parsed = urlparse(url)
    channel, segment = parsed.path.removeprefix("/archives/").split("/")[:2]
    message_ts = to_ts(segment)
    return channel, message_ts, parse_qs(parsed.query).get("thread_ts", [message_ts])[0]


def permalink_key(url: str) -> str | None:
    """The thread key of a Slack permalink, or None when `url` is not one."""
    if not PERMALINK.fullmatch(url):
        return None
    channel, _, thread_ts = locate(url)
    return thread_key(channel, thread_ts)


def parse_link(url: str, note: str, direction: str) -> SlackLink:
    channel, message_ts, thread_ts = locate(url)
    return SlackLink(
        url,
        channel,
        message_ts,
        thread_ts,
        thread_key(channel, thread_ts),
        tidy(note),
        asks_to_park(direction, note),
    )


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t\n-:,;")


def parse(text: str) -> Batch:
    matches = list(PERMALINK.finditer(text))
    if not matches:
        return Batch(tidy(text), [])
    direction = tidy(text[: matches[0].start()])
    ends = [m.start() for m in matches[1:]] + [len(text)]
    links = [
        parse_link(m.group(1), text[m.end() : end], direction) for m, end in zip(matches, ends)
    ]
    return Batch(direction, links)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Split pasted Slack links + notes into items")
    ap.add_argument("text", nargs="?", default=None, help="pasted block (default: stdin)")
    a = ap.parse_args(argv)
    batch = parse(a.text if a.text is not None else read_stdin())
    print(
        json.dumps(
            {"direction": batch.direction, "links": [asdict(x) for x in batch.links]}, indent=2
        )
    )
    return 0 if batch.links else 2


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

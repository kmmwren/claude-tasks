#!/usr/bin/env python3
"""
slack_links.py: split a pasted block of Slack permalinks and notes into handoff items.

Each permalink starts an item; the text after it, up to the next permalink, is that item's
note. Text before the first permalink is a direction shared by the whole batch. A link is
parked when the direction or its own note asks to park it; otherwise it goes to the inbox.
Prints JSON: {"direction": str, "links": [{url, channel, message_ts, thread_ts, note, target}]}.
"""
import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlparse

PERMALINK = re.compile(r"<?(https://[\w.-]+\.slack\.com/archives/[^\s>|]+)(?:\|[^>]*)?>?")
PARK = re.compile(r"\bpark(?:ed|ing)?\b", re.IGNORECASE)


class Target(StrEnum):
    INBOX = "inbox"
    PARKED = "parked"


@dataclass(frozen=True)
class SlackLink:
    url: str
    channel: str
    message_ts: str
    thread_ts: str
    note: str
    target: Target


@dataclass(frozen=True)
class Batch:
    direction: str
    links: list[SlackLink]


def to_ts(segment: str) -> str:
    digits = segment.removeprefix("p")
    return f"{digits[:-6]}.{digits[-6:]}"


def target_for(direction: str, note: str) -> Target:
    return Target.PARKED if PARK.search(f"{direction} {note}") else Target.INBOX


def parse_link(url: str, note: str, direction: str) -> SlackLink:
    parsed = urlparse(url)
    channel, segment = parsed.path.removeprefix("/archives/").split("/")[:2]
    message_ts = to_ts(segment)
    thread_ts = parse_qs(parsed.query).get("thread_ts", [message_ts])[0]
    return SlackLink(url, channel, message_ts, thread_ts, tidy(note), target_for(direction, note))


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t\n-:,;")


def parse(text: str) -> Batch:
    matches = list(PERMALINK.finditer(text))
    if not matches:
        return Batch(tidy(text), [])
    direction = tidy(text[:matches[0].start()])
    ends = [m.start() for m in matches[1:]] + [len(text)]
    links = [parse_link(m.group(1), text[m.end():end], direction) for m, end in zip(matches, ends)]
    return Batch(direction, links)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Split pasted Slack links + notes into items")
    ap.add_argument("text", nargs="?", default=None, help="pasted block (default: stdin)")
    a = ap.parse_args(argv)
    batch = parse(a.text if a.text is not None else sys.stdin.read())
    print(json.dumps({"direction": batch.direction, "links": [asdict(x) for x in batch.links]},
                     indent=2))
    return 0 if batch.links else 2


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

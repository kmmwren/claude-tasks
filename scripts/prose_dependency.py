"""
prose_dependency.py — find a brief that says in prose what its frontmatter does not.

The prioritiser reads only `blocked_by`, `blockers` and `autonomy`, so two prose-only mistakes
leave a brief mis-ranked: "after #5449 merges" with `blocked_by` empty runs too early, and an
`autonomy: needs-input` brief whose approval is already on record waits for a human who has
nothing left to decide.

Only a wait the brief has not yet satisfied counts: past-tense history, the execution log, quoted
examples, decisions already made and waits on a human the autonomy field already carries are skipped.
"""

import dataclasses
import os
import pathlib
import re

from deps import Brief

OPEN_STATUSES = ("ready", "in-progress")
LOG_HEADING = "## Execution log"
DATED_LOG_LINE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\b")
UNCHECKED_CRITERION = re.compile(r"^\s*[-*]\s*\[ \]")
QUOTED = re.compile(r"\"[^\"\n]*\"|`[^`\n]*`|“[^”\n]*”")
PR_REF = re.compile(r"^(?:[\w.-]+/[\w.-]+)?#(\d+)$")
MERGED_REF = re.compile(r"#(\d+)(?:\]\([^)\s]*\))?\s+(?:merged|landed)\b", re.I)
AFTER_MERGES = re.compile(
    r"\bafter\s+(?:PR\s+|brief\s+)?(\S+)\s+(?P<passive>(?:is|are)\s+)?(?P<verb>merges|merged|lands|landed)\b", re.I
)
FUTURE_VERBS = ("merges", "lands")
WAIT_PHRASE = re.compile(
    r"(?:\b(?:waits?\s+for|waiting\s+(?:for|on))"
    r"|(?:^|[.;:(*\-]\s*|\b(?:is|are|currently|still|now)\s+)(?:gated\s+on|blocked\s+(?:by|on)))"
    r"((?:\s+\S+){1,4})",
    re.I,
)
AWAITING_APPROVAL = re.compile(r"\bawaiting\s+(?:(?:her|his|their)\s+)?(?:approval|go-ahead|sign-?off)\b", re.I)
HUMAN_WORDS = {"approval", "go-ahead", "decision", "sign-off", "signoff"}
DECISION_STEM = r"\d{4}-\d{2}-\d{2}-[A-Za-z0-9][A-Za-z0-9_.-]*?"
DECISION_REF = re.compile(rf"(?<![\w-])({DECISION_STEM})(?:\.md)?(?![\w-])")
DECISION_MENTION = re.compile(rf"(?:\bdecision\s+)?(?<![\w-]){DECISION_STEM}(?:\.md)?(?![\w-])", re.I)
APPROVES = re.compile(r"go-ahead|green-?light|approved (?:the|to|that)|has approved|gave (?:the |her )?(?:go|approval)", re.I)
APPROVER = r"(?:she|he|they|the\s+owner|(?-i:[A-Z][a-z]+))"
POSSESSIVE = r"(?:her|his|their)"
APPROVAL_RECORD = re.compile(
    rf"\b{POSSESSIVE} ask (?:is|was) the approval\b"
    rf"|\b{APPROVER}\s+(?:has\s+)?approved\b"
    rf"|\bapproved\s+by\s+(?:her|him|them|the\s+owner|(?-i:[A-Z][a-z]+))"
    rf"|\b{APPROVER}\s+gave\s+(?:the|{POSSESSIVE})\s+go-ahead\b"
    rf"|\b(?:with|has|have|got)\s+{POSSESSIVE}\s+go-ahead\b",
    re.I,
)
NEGATED = re.compile(r"\b(?:not|no|until|unless|if|once|awaiting|needs?|requires?|before|without)\b", re.I)
DECISION_BODY = re.compile(r"\*\*Decision:\*\*\s*(.+?)(?=\n\*\*[A-Z]|\Z)", re.S)


@dataclasses.dataclass(frozen=True)
class Finding:
    kind: str
    detail: str


def decisions_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CLAUDE_DECISIONS_DIR") or pathlib.Path.home() / ".claude" / "decisions")


def prose_lines(body: str) -> list[str]:
    """The brief's own text: frontmatter, the execution log and dated log lines are history, not plan."""
    text = re.sub(r"\A---\n.*?\n---\n", "", body, count=1, flags=re.S).split(LOG_HEADING)[0]
    return [ln for ln in text.splitlines() if not DATED_LOG_LINE.match(ln)]


def plan_line(line: str) -> str:
    """A line with quoted examples and made decisions removed: neither is a statement of a wait."""
    return DECISION_MENTION.sub("", QUOTED.sub("", line))


def merged_numbers(lines: list[str]) -> set[str]:
    """PR numbers the brief itself reports as merged outside its acceptance criteria."""
    return {m.group(1) for ln in lines if not UNCHECKED_CRITERION.match(ln) for m in MERGED_REF.finditer(ln)}


def names_work(token: str, known: set[str], merged: set[str]) -> bool:
    """True for a brief id or a PR the brief has not itself reported merged."""
    word = token.strip("`'\"()[],.;:")
    pr = PR_REF.match(word)
    return word in known if pr is None else pr.group(1) not in merged


def names_human(token: str) -> bool:
    return token.strip("`'\"()[],.;:").lower() in HUMAN_WORDS


def after_merge_wait(line: str, known: set[str], merged: set[str]) -> str | None:
    """"after X merges" is a wait; "after X merged" is history unless it is an unmet acceptance criterion."""
    m = AFTER_MERGES.search(line)
    if not m or not names_work(m.group(1), known, merged):
        return None
    future = bool(m.group("passive")) or m.group("verb").lower() in FUTURE_VERBS
    return m.group(0) if future or UNCHECKED_CRITERION.match(line) else None


def waits_in_prose(line: str, known: set[str], merged: set[str], humans: bool = True) -> str | None:
    """The phrase in `line` that makes the brief wait on a PR, brief or (when `humans`) a person."""
    after = after_merge_wait(line, known, merged)
    if after:
        return after
    for m in WAIT_PHRASE.finditer(line):
        tokens = m.group(1).split()
        if any(names_work(t, known, merged) or (humans and names_human(t)) for t in tokens):
            return m.group(0).strip(" .;:(*-")
    awaiting = AWAITING_APPROVAL.search(line) if humans else None
    return awaiting.group(0) if awaiting else None


def declares_dependency(b: Brief) -> bool:
    return bool(b.blocked_by or b.declared_ids or b.legacy_tokens)


def approving_decision(b: Brief, directory: pathlib.Path) -> str | None:
    """A decision file the brief cites whose Decision text approves work, else None."""
    cited = {m.group(1) for m in DECISION_REF.finditer("\n".join(prose_lines(b.body)))}
    for stem in sorted(cited):
        path = directory / f"{stem}.md"
        body = DECISION_BODY.search(path.read_text(errors="replace")) if path.is_file() else None
        if body and APPROVES.search(body.group(1)):
            return path.name
    return None


def approval_in_prose(lines: list[str]) -> str | None:
    """The phrase in which the brief records that its work is approved; an unmet criterion or a condition is not a record."""
    for ln in lines:
        open_condition = NEGATED.search(ln) or UNCHECKED_CRITERION.match(ln)
        m = None if open_condition else APPROVAL_RECORD.search(ln)
        if m:
            return m.group(0)
    return None


def dependency_findings(b: Brief, known: set[str], lines: list[str]) -> list[Finding]:
    if declares_dependency(b):
        return []
    merged = merged_numbers(lines)
    humans = b.fields.get("autonomy") == "full"
    phrases = [p for p in (waits_in_prose(ln, known, merged, humans) for ln in lines) if p]
    if not phrases:
        return []
    return [Finding("prose-dependency", f'prose says "{phrases[0]}" but blocked_by is empty; move it to blocked/ with --blocked-by')]


def approval_findings(b: Brief, directory: pathlib.Path, lines: list[str]) -> list[Finding]:
    if b.fields.get("autonomy") != "needs-input":
        return []
    decision = approving_decision(b, directory)
    phrase = approval_in_prose(lines)
    source = f"cites {decision}, which approves the work" if decision else f'says "{phrase}"' if phrase else None
    return [Finding("approved-needs-input", f"autonomy is needs-input but it {source}; set autonomy: full")] if source else []


def prose_problems(b: Brief, known: set[str], directory: pathlib.Path) -> list[Finding]:
    """Each prose-only dependency or stale needs-input in one brief; empty unless it is ready or in progress."""
    if b.status not in OPEN_STATUSES:
        return []
    lines = [plan_line(ln) for ln in prose_lines(b.body)]
    return dependency_findings(b, known, lines) + approval_findings(b, directory, lines)

---
name: handoff
description: >-
  Use when the user wants to STOP a Claude Code session and hand its still-open work to the task queue: "handoff", "I'm done with this session", "stop and queue what's left", "offload this session", "park this and pick it up later", "hand off everything to parked", or pastes a claude.ai artifact link and asks to hand it off. Captures the session's open work items into the queue (inbox/ by default, or a directed target like parked/ or ready/) AND adds a judgement layer of things discussed-but-never-tracked, so nothing is stranded when the session ends.
---

# handoff

Turn this session's still-open work into queue briefs so nothing is lost when the user
stops. Over-capture into `inbox/`; the `enrich` skill filters and structures later. Do
**not** make the user approve each item — capture quietly, then let enrichment shrink it.

**Never work an item during a handoff.** No commits, pushes, PR actions, CI waits or fixes,
even when an item looks one step from done and even after a compaction tells you to
resume. Record its exact state and next step in the brief instead. The resume and
open-ledger hooks detect a handoff in the transcript and say the same.

## Long-running sessions: capture, keep the ledger, keep running

First run `python3 ~/.claude/hooks/lib/long_running.py check`. Exit 0 means this session is on the
shortlist in `~/.claude/long-running-sessions.json`. For a long-running session a handoff is a capture
check, not an ending: do Steps 1 to 3 so every open item has a queue brief, but **never close a ledger
item** (record the brief path in its `metadata.brief` instead), and **keep working** the items
afterwards; the "never work an item" rule above does not apply. The Step 4 report names how many
briefs were filed or sharpened and that the session carries on.

Add a session to the shortlist with `long_running.py add <session-id> "<name>"`. There are only a few.

## Headroom check

Unless the input is a session digest, run `python3 ~/.claude/scripts/handoff_external.py check`.
Exit 0: hand off in-session. Exit 3: run `python3 ~/.claude/scripts/handoff_external.py launch`
(add `--direction "<target>"` when the user gave one, e.g. "to parked/"), report in one line that
the handoff runs externally with the log path, and stop: no briefs, no ledger closes. If the
script is missing or errors, carry on in-session. A session whose post-compact floor plus the
handoff's own needs exceed its autocompact window thrashes mid-handoff, so it hands off from
outside instead.

## Input: this session, an artifact, or a session digest

When her message carries a claude.ai artifact link (`claude.ai/artifact/…` or
`claude.ai/code/artifact/…`) and asks to hand it off, the artifact is the input in place of
the session, and every step below runs the same way with these substitutions:

- Read it with the `Artifact` tool, `action: "read"`, never WebFetch.
- **Items:** its open work (tasks, decisions, testing still needed, follow-ups, plans) stands
  in for the ledger in Step 1, and the page stands in for the conversation in Step 2.
- **Name:** its title is the `<session name>` passed to `router.py which` in Step 0.
- **No ledger to close:** its items were never on this session's ledger, so Step 1 files
  them without closing anything.
- **Provenance:** each brief's `--context` opens with
  `From artifact <url> (snapshot <page date>); re-check before acting.`

A message that asks for both runs two handoffs, the artifact first.

When the prompt names a session id and a digest path (`Hand off session <sid> ("<name>") from
its digest at <path>`), the digest is the input in place of the session:

- **Items:** the digest's `## Open ledger items` JSON lines stand in for the ledger in Step 1.
- **Name:** the digest's `# Handoff digest: <name> (<sid>)` heading.
- **Judgement layer:** Step 2 reads the digest's `## Her prompts`, `## Last compaction
  summaries` and `## Recent assistant text` in place of the conversation.
- **Provenance:** each brief's `--context` opens with
  `From session <sid> (<name>) via external handoff; re-check before acting.`
- **Closing:** close each source item with
  `python3 ~/.claude/scripts/handoff_external.py close <sid> <id> <brief path>` instead of
  `TaskUpdate`, which edits the current session's ledger, not the source's.
- **Customer data:** briefs never carry customer security data copied from the digest;
  summarise as conclusions.

All `add_task.py` calls below resolve the queue via `$CLAUDE_TASKS_DIR` → the queue
bound to `$CLAUDE_CODE_SESSION_ID` → nearest `.tasks/` → `~/tasks` (or pass `--root <path>`).

## Step 0: Project mode (project work goes home, never into the generic queue)

If `~/.claude/tools/project-coordinator/router.py` exists, ask it which project owns this
session, passing the session name and the open items' text:

```bash
printf '%s' "<open items, one per line>" | \
  python3 ~/.claude/tools/project-coordinator/router.py which "<session name>" --text -
```

An empty `project` means the session is generic: carry on with Step 1 unchanged. Otherwise
split the items. **Project-specific** ones (about that project's code, milestones, decisions
or deliverables) go to the project's own queue. Everything else (tooling, harness, other
repos' chores) goes to the generic queue as usual.

For each project item, add `milestone: <id>` frontmatter when one of the returned
`milestones` fits; the project's `check_deps` validates it against its milestones file.

- `queue_mode: direct`: `add_task.py --root <queue> ... --commit`.
- `queue_mode: pr`: make a worktree of `sync_repo`, rerun `which ... --dest-root <worktree>`
  to get the queue path inside it, file with `add_task.py --root <that queue>` (no
  `--commit`), add the milestone frontmatter, commit in the worktree and land it as the
  `project-coordinator` skill says for that repo.

If the router is absent or errors, warn in the report and file everything generically.

In a session-linked queue (`[session]` in `tasks.toml`), items stay in that queue while the
session lives; `fold_session.py` folds them into the `fold_to` queue when the session ends,
so never move them by hand.

## Step 1 — The tracked-but-open items

List everything still open in this session's task state — your current TodoWrite /
Task list items whose status is `pending` or `in_progress` (skip anything `completed`
or `cancelled`). For each, file an inbox brief:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/add_task.py" \
  --title "<lead with the noun>" \
  --context "<what + why + where; note it came from this session's open work>" \
  --source claude --commit
```

Then close that ledger item (`TaskUpdate` status `completed`, `metadata.evidence` = the brief's path),
unless the session is long-running, where the item stays open with `metadata.brief` = the path.
A handoff ends this session's ownership; an item left open makes the session keep
claiming and chasing work that now lives in the queue.

## Step 2 — The judgement layer (discussed but never tracked)

Now re-read the conversation and capture the **follow-ups that never made it into a todo**
— the things the user would wish had been captured: a fix flagged in passing, a "we
should also…", a decision deferred, a bug noticed out of scope. File each the same way.
Be generous (over-capture); leave them all in `inbox/` (no `--ready`) for `enrich` to weigh.

## Step 3 — Dedup as you go

Before each capture, check for an existing brief and sharpen it instead of duplicating:
```bash
grep -rl "<keyword>" "$CLAUDE_TASKS_DIR"/{inbox,ready,in-progress,done} 2>/dev/null
```

## Directed handoff (steer where the items go)

By default everything lands in `inbox/`. When the user gives a **direction**, interpret it
and route accordingly — pass `--status <state>` to `add_task.py` and/or a shared directive
note via `--context`. The target state is one of `inbox` (default), `ready`, or `parked`.

- **"hand off everything to parked"** → file every item with `--status parked`. Parked =
  consciously suspended; add a one-line directive note so future-you knows why and when to
  resume, e.g. `--context "Parked from session <date>: paused until <trigger>."`
- **"hand these off as ready to go"** → `--status ready` (they're actionable as-is).
- **"park the X work, queue the rest"** → split: the X items `--status parked`, the others
  default. Use your judgement on which item is which.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/add_task.py" \
  --title "<lead with the noun>" \
  --context "<what + why + the handoff direction>" \
  --status parked --source claude --commit
```

If no direction is given, behave as the default sections above (everything to `inbox/`).

## Step 4 — Report (quiet, tiny)

One short line, never the whole pile. Name the direction if one was given. e.g.
*"Parked 6 open items + 2 follow-ups under '<note>'; resume with `/tasks-next` when ready."*
or *"Filed 6 open items to the inbox; run `/enrich` to turn them into briefs."*

## Note

A `SessionStart(compact)` hook (`catch-compact-thrash.py`) launches the external handoff
automatically when a session thrashes mid-handoff.

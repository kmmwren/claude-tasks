---
name: handoff
description: "Use when the user wants to STOP a Claude Code session and hand its still-open work to the task queue: \"handoff\", \"I'm done with this session\", \"stop and queue what's left\", \"offload this session\", \"park this and pick it up later\", \"hand off everything to parked\", or pastes a claude.ai artifact link and asks to hand it off, or pastes Slack message links (each optionally with her notes) and asks to hand them off, add them as tasks or park them (pass the pasted links verbatim in `args`, so the hooks treat it as a capture and the session carries on). Captures the session's open work items into the queue (inbox/ by default, or a directed target like parked/ or ready/) AND adds a judgement layer of things discussed-but-never-tracked, so nothing is stranded when the session ends."
---

# handoff

Turn this session's still-open work into queue briefs so nothing is lost when the user
stops. Over-capture into `inbox/`; the `enrich` skill filters and structures later. Do
**not** make the user approve each item — capture quietly, then let enrichment shrink it.

**Never work an item during a handoff.** No commits, pushes, PR actions, CI waits or fixes,
even when an item looks one step from done and even after a compaction tells you to
resume. Record its exact state and next step in the brief instead. The resume and
open-ledger hooks detect a handoff in the transcript and say the same.

**A Slack or artifact input is a capture, not an ending.** It files briefs for those links
only; the session keeps its own open work and carries on. The hooks tell a capture from a
session handoff by the links in the invocation's `args`.

## Long-running sessions: capture, keep the ledger, keep running

First run `python3 ~/.claude/hooks/lib/long_running.py check`. Exit 0 means this session is on the
shortlist in `~/.claude/long-running-sessions.json`. For a long-running session a handoff is a capture
check, not an ending: do Steps 1 to 3 so every open item has a queue brief, but **never close a ledger
item** (record the brief path in its `metadata.brief` instead), and **keep working** the items
afterwards; the "never work an item" rule above does not apply. The Step 4 report names how many
briefs were filed or sharpened and that the session carries on.

Add a session to the shortlist with `long_running.py add <session-id> "<name>"`. There are only a few.

## Headroom check

When the input is this session, run `python3 ~/.claude/scripts/handoff_external.py check`.
Exit 0: hand off in-session. Exit 3: run `python3 ~/.claude/scripts/handoff_external.py launch`
(add `--direction "<target>"` when the user gave one, e.g. "to parked/"), report in one line that
the handoff runs externally with the log path, and stop: no briefs, no ledger closes. If the
script is missing or errors, carry on in-session. A session whose post-compact floor plus the
handoff's own needs exceed its autocompact window thrashes mid-handoff, so it hands off from
outside instead.

## Input: this session, an artifact, Slack links, or a session digest

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

When her message carries Slack permalinks (`https://<workspace>.slack.com/archives/...`) and
asks to hand them off or add them as tasks, each link is one task and the text she put beside
it is what she is thinking about it:

- **Split:** pipe her whole message to `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slack_links.py"`.
  It returns `direction` (her text before the first link, applied to every item through
  Directed handoff below) and one `links` entry per permalink with `channel`, `message_ts`,
  `thread_ts`, `note` (her text after that link, up to the next one) and `parked`. Exit 2
  means no permalink was found: say so and stop.
- **Parked option:** `parked` is true when the direction or that link's own note mentions
  parking, so "park these <links>" parks the batch and "<link> as a task, parked" parks only
  that link; file it with `--status parked`. Ignore the flag when her words negate parking or
  only mention it ("don't park this", "why was this parked?"). A note that is only the park
  instruction counts as no note.
- **Read:** `slack_read_thread` with `channel` and `thread_ts`, and anchor on the message at
  `message_ts`. Read that thread only; never browse the channel around it.
- **Items:** one brief per link. Her note is the intent: it sets the title and goal and
  outranks your reading of the thread. With no note, the task is what the message asks of
  her; if that is unclear, file with `--autonomy needs-input`.
- **Name:** the channel name is the `<session name>` for `router.py which` in Step 0, with
  her note and the message text as `--text`.
- **No ledger, no judgement layer:** Step 1 files the links without closing anything and
  Step 2 is skipped. Step 3 greps the queue for the permalink, so a re-pasted link sharpens
  its existing brief.
- **Provenance:** each brief's `--context` opens with
  `From Slack <url> (#<channel>, <message date>); re-check the thread before acting.`, then
  `Her note: <note verbatim>`, then a summary of the thread.
- **Unreadable link** (private channel, deleted message): file the brief from her note alone
  with `--autonomy needs-input` and say the thread could not be read.
- **Customer data:** a thread may carry pasted customer security data (log extracts, asset
  rows); the brief carries conclusions, never those records.
- **Report:** `Filed N Slack handoffs (<count> <state>/ for each state used; M unreadable).`

A message that mixes inputs runs one handoff per input, artifacts and Slack links before the
session, each its own Skill call: the links alone in one call's `args`, then the session with
no link in its `args`.

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

All `add_task.py` calls below resolve the queue via `$CLAUDE_TASKS_DIR` → nearest
`.tasks/` → `~/tasks` (or pass `--root <path>`).

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

Sharpen the file the grep found, in the folder it is in now. Never append to a remembered
`inbox/<id>.md` path: if the brief moved meanwhile, the append re-creates it. `git_sync.py`
refuses to commit such a re-created id.

## Directed handoff (steer where the items go)

By default everything lands in `inbox/`. When the user gives a **direction**, interpret it
and route accordingly — pass `--status <state>` to `add_task.py` and/or a shared directive
note via `--context`. The target state is one of `inbox` (default), `ready`, or `parked`.

- **"hand off everything to parked"** → file every item with `--status parked`. Parked =
  consciously suspended; add a one-line directive note so future-you knows why and when to
  resume, e.g. `--context "Parked from session <date>: paused until <trigger>."`
- **"hand these off as ready to go"** → `--status ready` (they're actionable as-is). The script
  rejects a ready brief without `--goal`, `--criteria` (alias `--success`) and `--context`, so
  add `--goal "<end state>" --criteria "<checkable condition>"` to the command below.
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

For a session handoff (not an artifact, Slack or digest input), end the report with the
successor prompt in a fenced block, ready to paste into the next session, after the ledger
carries every `metadata.brief`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/resume_prompt.py"
```

It rebuilds the prompt from the live ledger and the long-running registry every time; never
write it by hand.

One short line, never the whole pile. Name the direction if one was given. e.g.
*"Parked 6 open items + 2 follow-ups under '<note>'; resume with `/tasks-next` when ready."*
or *"Filed 6 open items to the inbox; run `/enrich` to turn them into briefs."*

## Step 5: Verify it shipped, then close

A session handoff that shipped work ends with the session closed, not left idle.

1. List every sha and branch this session claims landed. Confirm each is on the default branch of
   origin: `git fetch origin`, then `git branch -r --contains <sha>` shows `origin/<default>`, or
   `git merge-base --is-ancestor <sha> origin/<default>` exits 0.
2. Anything not on main stays in its brief with its exact state; the session does not close.
3. When all are on main, end your last message with the newest sha or PR link and stop. The
   orphan-close sweep (`child_sessions.close_orphans`, run by `long-running-keepalive.py`) reads
   that evidence and stops the job with `claude stop` (`child_sessions.close`).
4. A pinned session (`long_running.PinnedSessions`, `jobs/pins.json`) never closes: skip this step,
   as in "Long-running sessions" above.

## Note

A `SessionStart(compact)` hook (`catch-compact-thrash.py`) launches the external handoff
automatically when a session thrashes mid-handoff.

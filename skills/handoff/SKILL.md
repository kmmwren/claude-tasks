---
name: handoff
description: "Use when the user wants to STOP a Claude Code session and hand its still-open work to the task queue: \"handoff\", \"I'm done with this session\", \"stop and queue what's left\", \"offload this session\", \"park this and pick it up later\", \"hand off everything to parked\", or pastes a claude.ai artifact link and asks to hand it off, or pastes Slack message links (each optionally with the user's notes) and asks to hand them off, add them as tasks or park them (pass the pasted links verbatim in `args`, so any hooks treat it as a capture and the session carries on). Captures the session's open work items into the queue (inbox/ by default, or a directed target like parked/ or ready/) AND adds a judgement layer of things discussed-but-never-tracked, so nothing is stranded when the session ends."
---

# handoff

Turn this session's still-open work into queue briefs so nothing is lost when the user
stops. Over-capture into `inbox/`; the `enrich` skill filters and structures later. Do
**not** make the user approve each item; capture quietly, then let enrichment shrink it.

**Never work an item during a handoff.** No commits, pushes, PR actions, CI waits or fixes,
even when an item looks one step from done and even after a compaction tells you to
resume. Record its exact state and next step in the brief instead. Any resume or
open-ledger hooks in your setup that detect a handoff say the same.

**A Slack or artifact input is a capture, not an ending.** It files briefs for those links
only; the session keeps its own open work and carries on. Hooks in your setup can tell a
capture from a session handoff by the links in the invocation's `args`.

## Extension point

If `~/.config/claude-tasks/handoff.md` exists (check with `test -f`), read it and follow its extra steps at the
points it names (before Step 1, an extra input, Step 1 to Step 5, the closing note). Where
it says a rule above does not apply, that rule does not apply. If it is absent, carry on
with the steps below unchanged.

## Input: this session, an artifact, or Slack links

When the user's message carries a claude.ai artifact link (`claude.ai/artifact/…` or
`claude.ai/code/artifact/…`) and asks to hand it off, the artifact is the input in place of
the session, and every step below runs the same way with these substitutions:

- Read it with the `Artifact` tool, `action: "read"`, never WebFetch.
- **Items:** its open work (tasks, decisions, testing still needed, follow-ups, plans) stands
  in for the ledger in Step 1, and the page stands in for the conversation in Step 2.
- **Name:** its title is the session name.
- **No ledger to close:** its items were never on this session's ledger, so Step 1 files
  them without closing anything.
- **Provenance:** each brief's `--context` opens with
  `From artifact <url> (snapshot <page date>); re-check before acting.`

When the user's message carries Slack permalinks (`https://<workspace>.slack.com/archives/...`)
and asks to hand them off or add them as tasks, each link is one task and the text the user put
beside it is what they are thinking about it:

- **Split:** pipe the user's whole message to `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slack_links.py"`.
  It returns `direction` (the user's text before the first link, applied to every item through
  Directed handoff below) and one `links` entry per permalink with `channel`, `message_ts`,
  `thread_ts`, `key` (`slack:<channel>/<thread_ts>`, the thread's one identity), `note` (the user's
  text after that link, up to the next one) and `parked`. Exit 2
  means no permalink was found: say so and stop.
- **Parked option:** `parked` is true when the direction or that link's own note mentions
  parking, so "park these <links>" parks the batch and "<link> as a task, parked" parks only
  that link; file it with `--status parked`. Ignore the flag when the user's words negate parking or
  only mention it ("don't park this", "why was this parked?"). A note that is only the park
  instruction counts as no note.
- **Read:** `slack_read_thread` with `channel` and `thread_ts`, and anchor on the message at
  `message_ts`. Read that thread only; never browse the channel around it.
- **Items:** one brief per link. The user's note is the intent: it sets the title and goal and
  outranks your reading of the thread. With no note, the task is what the message asks of
  the user. When the user asked for it or approved it, file with `--autonomy full`; use
  `--autonomy needs-input` only when a real undecided question remains, and write that
  question in the brief's context.
- **Name:** the channel name is the session name, with the user's note and the message text
  as the item text.
- **No ledger, no judgement layer:** Step 1 files the links without closing anything and
  Step 2 is skipped. Step 3 greps the queue for the `key`, so a re-paste or an intake-watched
  thread sharpens its existing brief.
- **Filing:** each brief is filed with `--tags <key>` and `--source-ref <permalink>`; never
  build the key by hand.
- **Provenance:** each brief's `--context` opens with
  `From Slack <url> (#<channel>, <message date>); re-check the thread before acting.`, then
  `User note: <note verbatim>`, then a summary of the thread.
- **Unreadable link** (private channel, deleted message): file the brief from the user's note alone
  and say the thread could not be read. It is `--autonomy needs-input` only when the note
  leaves the task itself undecided; a note that states the task is `--autonomy full`.
- **Sensitive data:** a thread may carry pasted sensitive data (log extracts,
  records); the brief carries conclusions, never those records.
- **Report:** `Filed N Slack handoffs (<count> <state>/ for each state used; M unreadable).`

A message that mixes inputs runs one handoff per input, artifacts and Slack links before the
session, each its own Skill call: the links alone in one call's `args`, then the session with
no link in its `args`.

All `add_task.py` calls below resolve the queue via `$CLAUDE_TASKS_DIR` → nearest
`.tasks/` → `~/tasks` (or pass `--root <path>`).

## Step 1: The tracked-but-open items

List everything still open in this session's task state: your current TodoWrite /
Task list items whose status is `pending` or `in_progress` (skip anything `completed`
or `cancelled`). For each, file an inbox brief:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/add_task.py" \
  --title "<lead with the noun>" \
  --context "<what + why + where; note it came from this session's open work>" \
  --no-deps | --blocked-by <id|external:reason> [--probe "N: <command|human>"] \
  --status inbox --source claude --commit
```

Every brief declares its dependencies; silence is refused at promotion and reported by
`check_deps.py`. Take them from the ledger item's `blockedBy` and `metadata.deps`, and from
any PR, approval or login the item waits on:

- **Waits on another brief:** `--blocked-by <brief-id>`, one flag per blocker.
- **Waits on something no brief completes** (a PR to merge, an approval, a login):
  `--blocked-by external:<reason without commas>` with `--probe "N: <command>"` that exits 0
  once it clears (`gh pr view <n> --repo <o/r> --json state -q .state | grep -qx MERGED`), or
  `--probe "N: human"`. N is the blocker's position in the `--blocked-by` list.
- **Waits on nothing:** `--no-deps`.

`--status inbox` keeps the brief in inbox/ for `enrich`; without it, `--blocked-by` files to
blocked/ and then also needs `--goal` and `--criteria`.

Set autonomy from what the user said, not from caution: an item the user asked for or
approved is `--autonomy full`. Use `--autonomy needs-input` only for a real undecided
question, stated in the context.

Then close that ledger item (`TaskUpdate` status `completed`, `metadata.evidence` = the brief's path).
A handoff ends this session's ownership; an item left open makes the session keep
claiming and chasing work that now lives in the queue.

### Running agents and background tasks

Clearing the session loses any background `Agent` or `run_in_background` Bash command that is
still running, so hand those off too. List every one this session launched that has no
completion notification yet. Never wait for it, stop it or work its result during the handoff.
For each, add a `## Running background work` section to the brief of the item it serves (the
brief's `--context` when you file it now, a sharpen of the existing brief otherwise; an agent
that serves no item becomes its own brief). The section says:

- **kind and name:** `agent` or `task`, and its description or agent id.
- **asked:** one line of what it was asked to do.
- **output:** the output file path from its launch result.
- **collect:** how the successor gets the result: read the output file; message the agent by
  id if it may still be alive; otherwise re-run it with the same prompt.

Also record the same four fields on the ledger item, so the successor prompt prints them
under it: `TaskUpdate` with `metadata.agents` = a list of
`{"kind": "agent", "name": "...", "asked": "...", "output": "...", "collect": "..."}`
(set it in the same call that closes the item, or on the open item for a session recorded as
long-running, see `resume_prompt.py`).

## Step 2: The judgement layer (discussed but never tracked)

Now re-read the conversation and capture the **follow-ups that never made it into a todo**,
the things the user would wish had been captured: a fix flagged in passing, a "we
should also…", a decision deferred, a bug noticed out of scope. File each the same way,
with its dependency declaration.
Be generous (over-capture); leave them all in `inbox/` (no `--ready`) for `enrich` to weigh.

## Step 3: Dedup as you go

Before each capture, check for an existing brief and sharpen it instead of duplicating:
```bash
grep -rl "<keyword>" "$CLAUDE_TASKS_DIR"/{inbox,ready,in-progress,done} 2>/dev/null
```

For a Slack link grep for its `key` instead of a keyword: a brief that carries it is the same
thread however the link was written, so sharpen that brief and file nothing.

Sharpen the file the grep found, in the folder it is in now. Never append to a remembered
`inbox/<id>.md` path: if the brief moved meanwhile, the append re-creates it. `git_sync.py`
refuses to commit such a re-created id.

## Before filing: dependencies in fields, never only in prose

- A wait written only in the context ("after #942 merges", "once the design is approved")
  is a defect: `check_deps.py` reports it as `prose-dependency`. Put each wait in
  `--blocked-by`, and keep the prose as explanation only.
- An item the user approved and left at `needs-input` is a defect too: `check_deps.py`
  reports it as `approved-needs-input`.
- After filing, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/check_deps.py" --root <queue>`
  and fix every finding on the briefs you filed before ending the handoff.

## Directed handoff (steer where the items go)

By default everything lands in `inbox/`. When the user gives a **direction**, interpret it
and route accordingly: pass `--status <state>` to `add_task.py` and/or a shared directive
note via `--context`. The target state is one of `inbox` (default), `ready`, or `parked`.

- **"hand off everything to parked"** → file every item with `--status parked`. Parked =
  consciously suspended; add a one-line directive note so future-you knows why and when to
  resume, e.g. `--context "Parked from session <date>: paused until <trigger>."`
- **"leave these until <date>"** is a dependency on the day, not a park: `--status blocked
  --blocked-by date:YYYY-MM-DD`. The daily release sweep returns them to `ready/` on that day.
- **"hand these off as ready to go"** → `--status ready` (they're actionable as-is). The script
  rejects a ready brief without `--goal`, `--criteria` (alias `--success`) and `--context`, so
  add `--goal "<end state>" --criteria "<checkable condition>"` to the command below.
- **"park the X work, queue the rest"** → split: the X items `--status parked`, the others
  default. Use your judgement on which item is which.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/add_task.py" \
  --title "<lead with the noun>" \
  --context "<what + why + the handoff direction>" \
  --no-deps | --blocked-by <id|external:reason> [--probe "N: <command|human>"] \
  --status parked --source claude --commit
```

A ready brief that waits on something is refused: file it with `--blocked-by` and no
`--status`, which lands it in blocked/ until the blocker clears.

If no direction is given, behave as the default sections above (everything to `inbox/`).

## Step 4: Report (quiet, tiny)

For a session handoff (not an artifact or Slack input), end the report with the
successor prompt in a fenced block, ready to paste into the next session, after the ledger
carries every `metadata.brief`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/resume_prompt.py"
```

It rebuilds the prompt from the live ledger and its session registry every time, listing
each item's running agents and background tasks beneath it; never write it by hand. It lists
open items, plus a `/claude-tasks:action-task <brief-id>` line for every item this handoff closed
into a still-open brief (its `metadata.handed_to`, else `metadata.brief`), so the successor starts
that work instead of seeding from nothing.

One short line, never the whole pile. Name the direction if one was given. e.g.
*"Parked 6 open items + 2 follow-ups under '<note>'; resume with `/tasks-next` when ready."*
or *"Filed 6 open items to the inbox; run `/enrich` to turn them into briefs."*

## Step 5: Verify it shipped, then close

A session handoff that shipped work ends with the session closed, not left idle.

1. List every sha and branch this session claims landed. Confirm each is on the default branch of
   origin: `git fetch origin`, then `git branch -r --contains <sha>` shows `origin/<default>`, or
   `git merge-base --is-ancestor <sha> origin/<default>` exits 0.
2. Anything not on main stays in its brief with its exact state; the session does not close.
3. When all are on main, end your last message with the newest sha or PR link and stop.

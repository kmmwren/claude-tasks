---
name: handoff
description: Use when the user wants to STOP a Claude Code session and hand its still-open work to the task queue — "handoff", "I'm done with this session", "stop and queue what's left", "offload this session", "park this and pick it up later", "hand off everything to parked". Captures the session's open work items into the queue (inbox/ by default, or a directed target like parked/ or ready/) AND adds a judgement layer of things discussed-but-never-tracked, so nothing is stranded when the session ends.
---

# handoff

Turn this session's still-open work into queue briefs so nothing is lost when the user
stops. Over-capture into `inbox/`; the `enrich` skill filters and structures later. Do
**not** make the user approve each item — capture quietly, then let enrichment shrink it.

**Never work an item during a handoff.** No commits, pushes, PR actions, CI waits or fixes,
even when an item looks one step from done and even after a compaction tells you to
resume. Record its exact state and next step in the brief instead. The resume and
open-ledger hooks detect a handoff in the transcript and say the same.

All `add_task.py` calls below resolve the queue via `$CLAUDE_TASKS_DIR` → nearest
`.tasks/` → `~/tasks` (or pass `--root <path>`).

## Step 0 — Project mode (project work goes home, never into the generic queue)

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

Then close that ledger item (`TaskUpdate` status `completed`, `metadata.evidence` = the brief's path).
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

This is the model-driven handoff. A deterministic SessionEnd auto-capture hook (parsing
the session transcript for open Task-API/TodoWrite items, idempotent via a marker) is a
planned add-on — see the project roadmap. Until then, invoke this skill when you stop.

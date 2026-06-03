---
name: handoff
description: Use when the user wants to STOP a Claude Code session and hand its still-open work to the task queue — "handoff", "I'm done with this session", "stop and queue what's left", "offload this session", "park this and pick it up later". Captures the session's open work items into the queue's inbox/ AND adds a judgement layer of things discussed-but-never-tracked, so nothing is stranded when the session ends.
---

# handoff

Turn this session's still-open work into queue briefs so nothing is lost when the user
stops. Over-capture into `inbox/`; the `enrich` skill filters and structures later. Do
**not** make the user approve each item — capture quietly, then let enrichment shrink it.

All `add_task.py` calls below resolve the queue via `$CLAUDE_TASKS_DIR` → nearest
`.tasks/` → `~/tasks` (or pass `--root <path>`).

## `--parked` mode

If the user invoked handoff with `--parked` (e.g. `/handoff --parked`), the session is
being **intentionally suspended** because it is waiting on external input — answers to
clarifying questions, a decision from a third party, access being granted, etc. In that
case:

- Pass `--parked` to every `add_task.py` call below.
- Items land in `parked/` with `status: parked`, **not** `inbox/`.
- Do **not** add `--ready`; parked items skip `inbox/` entirely and are not unstructured
  raw captures — they are well-understood work that is blocked externally.

Without `--parked`, the default behaviour applies: all items go to `inbox/`.

## Step 1 — The tracked-but-open items

List everything still open in this session's task state — your current TodoWrite /
Task list items whose status is `pending` or `in_progress` (skip anything `completed`
or `cancelled`). For each, file a brief (add `--parked` if in parked mode):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/add_task.py" \
  --title "<lead with the noun>" \
  --context "<what + why + where; note it came from this session's open work>" \
  --source claude --commit
# add --parked when invoked as /handoff --parked
```

## Step 2 — The judgement layer (discussed but never tracked)

Now re-read the conversation and capture the **follow-ups that never made it into a todo**
— the things the user would wish had been captured: a fix flagged in passing, a "we
should also…", a decision deferred, a bug noticed out of scope. File each the same way.
Be generous (over-capture); in normal mode leave them all in `inbox/` (no `--ready`); in
parked mode, add `--parked` so they land alongside the tracked items.

## Step 3 — Dedup as you go

Before each capture, check for an existing brief and sharpen it instead of duplicating:
```bash
grep -rl "<keyword>" "$CLAUDE_TASKS_DIR"/{inbox,ready,in-progress,done,parked} 2>/dev/null
```

## Step 4 — Report (quiet, tiny)

One short line, never the whole pile. e.g. *"Filed 6 open items + 2 discussed follow-ups
to the inbox; run `/enrich` to turn them into briefs."* In parked mode: *"Parked 6 items
in parked/; resume with `/action-task` once the blocker is resolved."*

## Note

This is the model-driven handoff. A deterministic SessionEnd auto-capture hook (parsing
the session transcript for open Task-API/TodoWrite items, idempotent via a marker) is a
planned add-on — see the project roadmap. Until then, invoke this skill when you stop.

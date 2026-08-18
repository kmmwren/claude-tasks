---
name: enrich
description: Process the queue's inbox/ items into full ready/ briefs. Human-initiated (never automatic). For each item — dedup check, gather context, fill all frontmatter fields, write goal + criteria + context, commit to ready/, and archive the raw file to inbox/.processed/ (never delete — audit trail). Flag if the inbox grows past 20 items.
---

# enrich

Turns raw `inbox/` captures into executable `ready/` briefs. **Run on demand only** —
human-initiated, never automatic.

## When to run

- The user runs `/enrich` or says "enrich the inbox" / "clear the inbox".
- When `inbox/` has 5+ unprocessed items and there's a natural gap.

## Process (one item at a time)

For each file in `inbox/` (whether `raw: true` or partial frontmatter):

### 1. Dedup check first
```bash
grep -rl "<keyword>" "$CLAUDE_TASKS_DIR"/{inbox,ready,in-progress,done} 2>/dev/null
```
If a match exists, fold this capture's context into the existing brief and archive the
raw file — don't create a duplicate.

### 2. Gather context
Pull from every source available before enriching: linked repos/PRs/docs, related prior
briefs, the current session, anything the capture references. Don't ask the user for
context that's already discoverable.

### 3. Fill the brief fully

| Field | Guidance |
|---|---|
| `title` | Scannable one-liner, lead with the noun |
| `goal` | End state, not "do X" |
| `context` | Everything a zero-context agent needs (paths, repos, links, why) |
| `criteria` | Checkable conditions — if you can't check it, rewrite it |
| `importance` | 1–4 (default 3 unless clearly urgent/strategic) |
| `autonomy` | Honest: `full` ONLY if a future agent can complete end-to-end with zero user input |
| `estimated-effort` | xs / s / m / l / xl |
| `domain` | one of the domains in `tasks.toml` |
| `tags` | free-form labels (e.g. `depends:<other-id>`) |
| `assignee` | leave empty — enrichment doesn't claim work; `action-task` sets it |
| `requires-local` | `true` if the brief needs local-machine files or creds |
| `requires-repo` | repo slug if the brief needs a repo other than this queue |

### 4. Write to ready/
Use `add_task.py --ready`, or write directly to `ready/` with `status: ready` and commit.

### 5. Archive the raw file
```bash
mkdir -p "$CLAUDE_TASKS_DIR/inbox/.processed"
mv "$CLAUDE_TASKS_DIR/inbox/<file>.md" "$CLAUDE_TASKS_DIR/inbox/.processed/<file>.md"
```
**Never delete.** `.processed/` is the audit trail for where briefs originated.

## Inbox hygiene
- `inbox/` items are **not briefs** — never present them as the actionable queue.
- If inbox grows to 20+ items, flag it: "Inbox has N items — consider an enrich session."
- After enrichment, commit all new `ready/` briefs and the `.processed/` archives together.

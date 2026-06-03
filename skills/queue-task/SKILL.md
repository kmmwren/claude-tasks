---
name: queue-task
description: 'Use whenever a task should be added to the claude-tasks queue — either because the user said "add a task / track this / remember to do X / put this on the list", OR because you (Claude), in the middle of other work, discovered something that should become a tracked task: a follow-up, a deferred fix, a TODO you left, a security issue, an improvement, a "we should also…". This is how Claude self-captures work into the queue from any session. Fill the brief out as meaningfully as you can from what you already know.'
---

# queue-task

Add a well-formed **brief** to the task queue. A brief is one markdown file rich enough
that a future agent can execute it with zero prior context. Add one from anywhere with
the bundled `add_task.py`.

## When to self-capture (without being asked)

Add a brief whenever you would otherwise leave a loose end:
- A follow-up you flagged ("we should also…", "next step is…", "worth doing later").
- A fix or improvement you noticed but it's out of scope right now.
- A discovered bug, security issue, or tech-debt item.
- Something the user mentioned in passing that is a real task, not just chat.

Do NOT capture: things you're doing right now, pure conversation, or duplicates of briefs
that already exist (check first — see Dedup).

## How to add one

Run the script. Fill out **as much as you meaningfully can** from context — don't leave
fields blank that you can infer:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/add_task.py" \
  --title "<scannable one-liner, lead with the noun>" \
  --goal "<the end state, not 'do X'>" \
  --context "<everything a zero-context agent needs: repos, paths, links, why>" \
  --criteria "<checkable condition>" --criteria "<another>" \
  --type todo|project|research|decision \
  --importance 1|2|3|4 \
  --autonomy full|needs-input|blocked \
  --effort xs|s|m|l|xl --due YYYY-MM-DD \
  --domain "<one of the domains in tasks.toml>" \
  --source claude --commit
```

- **`--ready`**: add it if you can ALSO fill out goal + criteria + context well enough to
  execute. Then it lands in `ready/`. Otherwise (default) it lands in `inbox/` for later
  enrichment (the `enrich` skill turns inbox items into ready briefs).
- **`--autonomy full`** ONLY if a future agent could complete it end-to-end with zero
  user input. Be honest — over-tagging `full` makes the queue lie about what's delegable.
- **`--commit`** writes, commits, and pushes. Use it unless you have a reason to batch.

The script finds the queue via `$CLAUDE_TASKS_DIR` → nearest `.tasks/` → `~/tasks`, or
pass `--root <path>` explicitly. If it reports "No task queue", run `/tasks-init` first.

## Dedup first

Before adding, check for an existing brief on the same thing:
```bash
grep -rl "<keyword>" "$CLAUDE_TASKS_DIR"/{inbox,ready,in-progress,done} 2>/dev/null
```
If one exists, sharpen it (add a note to its body) instead of duplicating. Fewer, richer
briefs beat many thin ones.

## Honesty rules

- Set `--source claude` so a review can show what Claude self-generated.
- Don't inflate `importance`. Default 3 unless it's genuinely urgent/strategic.
- If you can't fill out the goal, that's fine — drop it in `inbox/` and let enrichment
  handle it. Capturing badly beats losing the idea.

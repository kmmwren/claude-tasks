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

A pasted Slack permalink goes through the `handoff` skill's Slack input instead, with the user's
message (links and notes verbatim) as `args`. It reads the thread, keeps the user's note as the intent,
can file the link to `parked/`, and leaves this session's own work open.

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
  --no-deps | --blocked-by <id|external:reason> [--probe "N: <command|human>"] \
  --source claude --commit
```

- **Dependencies (required):** every brief declares what it waits on, in fields and never
  only in prose; `check_deps.py` reports a prose-only wait as `prose-dependency`.
  - `--no-deps` when it waits on nothing (writes `blocked_by: []`).
  - `--blocked-by <brief-id>` (repeatable) for each brief that must finish first.
  - `--blocked-by external:<reason without commas>` for a PR merge, approval or login, with
    `--probe "N: <command>"` that exits 0 once it clears, or `--probe "N: human"` plus
    `--confirmed "N: <date> <who>"`. N is the blocker's position in the `--blocked-by` list.

  `--blocked-by` files the brief to blocked/ (it returns to ready/ when the blocker clears)
  unless `--status` says otherwise, and `--ready` with `--blocked-by` is refused.

- **`--ready`**: add it if you can ALSO fill out goal + criteria + context well enough to
  execute. The script refuses `--ready` (and `--status ready`) unless `--goal`, `--criteria`
  (alias `--success`) and `--context` are all given, plus `--no-deps`, so the brief never carries placeholder stubs or an unstated dependency. Then it lands in `ready/`. Otherwise (default) it lands in `inbox/` for later
  enrichment (the `enrich` skill turns inbox items into ready briefs).
- **`--autonomy full`** ONLY if a future agent could complete it end-to-end with zero
  user input. Be honest: over-tagging `full` makes the queue lie about what's delegable.
  Work the user asked for or approved is `full`; `needs-input` needs a real undecided
  question in the context (`check_deps.py` reports an approved one as `approved-needs-input`).
- **`--blocks <brief-id>`** (repeatable): name each open brief this new work is blocking, so the
  prioritiser ranks it higher. Use the same ids as `blockers:`/`blocked_by:`. Omit it when the work blocks nothing.
- **`--commit`** writes, commits, and pushes. Use it unless you have a reason to batch.

The script finds the queue via `$CLAUDE_TASKS_DIR` → nearest `.tasks/` → `~/tasks`, or
pass `--root <path>` explicitly. If it reports "No task queue", run `/tasks-init` first.

## Dedup first

add_task.py checks for you: a capture with the same normalised title and cited sources as a brief
already in the queue is merged into it (a log line, plus a note when the context is new) and the
output says `merged into <folder>/<id>.md`. `--on-duplicate refuse` files nothing and exits 1;
`--on-duplicate file` captures it anyway. A same-title brief citing different sources is a different
task. Fewer, richer briefs beat many thin ones.

## Honesty rules

- Set `--source claude` for a brief you generate yourself, so a review can show what Claude self-generated.
- A brief extracted from an external page or message is `--source intake --source-ref <url>`, so its origin stays on the brief.
- Don't inflate `importance`. Default 3 unless it's genuinely urgent/strategic.
- If you can't fill out the goal, that's fine — drop it in `inbox/` and let enrichment
  handle it. Capturing badly beats losing the idea.

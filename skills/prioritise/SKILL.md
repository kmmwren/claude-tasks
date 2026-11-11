---
name: prioritise
description: "Use when the user asks what to do next or how the queue is prioritised — \"what's next\", \"what should I work on\", \"what's the priority\", \"what's blocked\", \"what's the most important thing\", \"what can you pick up\". Ranks the ready/ briefs (dependency-aware: a brief stays blocked until its dependencies are done; briefs that unblock others rank higher) and shows the top of the queue with blocked / needs-you / unlock flags."
---

# prioritise — rank the queue, dependency-aware

Answer "what should I do next?" by ranking the `ready/` briefs. The score is transparent
and generic: due urgency + importance + quick-win (low effort) + lead-time + a bonus for
briefs that **unblock others**, plus any priority tag weights defined in `tasks.toml`.

Dependencies are real: a brief lists `blockers:` (brief ids or external text) and/or
`depends:<id>` tags. A dependency counts as met only once its target brief is `done`, so
the queue self-unblocks as work completes.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prioritise.py"               # ranked, all of ready/
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prioritise.py" --top 5       # top 5
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prioritise.py" --actionable  # only what Claude can run now
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prioritise.py" --json        # machine-readable
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prioritise.py" --context cloud  # skip requires-local / requires-repo
```

The script resolves the queue via `$CLAUDE_TASKS_DIR` → nearest `.tasks/` → `~/tasks`.

## How to use it

- **"What should I do next?"** → run it (optionally `--top 5`), then state the top few back
  in your own words: id, title, why it's ranked there (due / importance / unblocks N), and
  whether it's actionable now or blocked / needs them.
- **"What can you pick up?"** → `--actionable` (only `autonomy: full`, unblocked, runnable).
  This is also what an autonomous loop would call (`--actionable --context cloud --json`).
- **"What's blocked?"** → run it and read the `⛔blocked(<unmet deps>)` flags; offer to
  action the blocker first (a brief that unblocks others is ranked higher for exactly this).

## Ranking against her priorities (rank.py)

`prioritise.py` filters: blocked briefs drop out and due, importance, effort and unlocks become
facts. `rank.py` then orders the top candidates against the queue's taste profile
(`profile/priorities.md`) with one reason each. Use it when a person will read the list. Automations
keep the `prioritise.py` order unless they opt in.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/seed_profile.py" --north-stars org/repo:path/north-stars.md  # once: draft the profile
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rank.py" --scope all --top 10        # or tag:<t>, project:<slug>, session:<id>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rank.py" --scope project:lite --json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/log_pick.py" skip <id>               # pick and bump are logged automatically
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/learn.py"                            # propose profile weight changes from picks
```

- The seeded profile is a DRAFT: it says so in its frontmatter and in every ranked list's header
  until the owner edits it and sets `draft: false`.
- When the profile is missing or unreadable, or the judge fails, the list falls back to filter order
  and the header says why.
- Report `(low confidence)` items as uncertain: the two judge passes disagreed about them.
- `learn.py` only proposes changes by default. Apply one with `--apply <id>` and undo it with
  `--revert <version>`.

## Flags in the output

- `⛔blocked(...)` — not actionable; the listed dependencies aren't `done` yet.
- `🙋needs-input` / `🙋blocked` — waiting on the user, not auto-runnable.
- `→N` — this brief unblocks N others (clearing it has leverage).
- `⏱lead` — the effort lead time means it must start now to hit its due date.

## Defining your own priority tags

To weight specific tags (e.g. a "frog"/"urgent" convention), add to `tasks.toml`:

```toml
[priority]
tag_weights = { urgent = 500, frog = 400 }
```

Briefs carrying those tags get the weight added to their score. Empty by default — the
base ranking stays purely mechanical (due / importance / effort / unlocks).

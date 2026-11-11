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

## WSJF scorer (opt-in)

`additive` (above) is the default and what every consumer reads. A second scorer, `wsjf`,
ranks by cost of delay divided by job size, plus an aging term:

- **cost of delay** = importance value + time criticality (due curve, plus lead time) +
  milestone weight + tag weights + leverage (longest chain of live briefs waiting on it).
- **job size** = `estimated-effort` points (xs 1, s 2, m 3, l 5, xl 8, blank 3).
- **inheritance**: a blocker scores at least as high as the highest brief waiting on it.
- **lanes** from a `cos:` field or tag: `expedite` (capped), `fixed-date` (joins near its
  due date), `standard` (default), `chore` (a reserved share of slots).
- **move**: `execute`, `de-fang` (make it safe to run), `ask`, or `blocked`.

Pick it per call with `--scorer wsjf`, or per queue in `tasks.toml`:

```toml
[priority]
scorer = "wsjf"            # default "additive"
expedite_cap = 2
chore_share = 0.2
importance_values = { "1" = 2500, "2" = 1000, "3" = 300, "4" = 100, "5" = 30 }
```

Every weight has a default in `PRIORITY_DEFAULTS`; a partial table merges over it. `--json`
always carries both scorers (`score_additive`, `score_wsjf`, `rank_additive`, `rank_wsjf`)
plus `lane`, `move`, `leverage`, a per-factor `why` and `schema_version`. The pre-v2 fields
keep their meaning, and `score` follows the chosen scorer. In text output the trailing
`[wsjf cod/size +age N +inh N]` is the compact why.

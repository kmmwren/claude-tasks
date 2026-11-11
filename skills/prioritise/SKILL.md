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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prioritise.py" --scope project:lite   # one project, project weights
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prioritise.py" --scope session:<id> --ids a,b --root <queue>
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

## Fleet blockers (work that clears a blocker affecting every session)

A brief declares the blocker it clears with `clears: <blocker-id>` (or `[id, id]`), the reverse of
`blockers:`. A blocker is a JSON record, one `<id>.json` per blocker, in the directory `tasks.toml` names:

```toml
fleet_blockers_dir = "fleet-blockers"   # relative to the directory holding the queue, or absolute / ~
```

```json
{"id": "hooks-down", "summary": "Hooks block every session", "raised_at": "...", "raised_by": "...",
 "owner": "...", "probe": "...", "evidence": "...", "status": "active", "checked_at": "..."}
```

While `status` is `active` or `unverified`, a brief that clears it goes in the `fleet-blocker` lane, ahead of
every other lane under either scorer, and is not held to `expedite_cap`. `--json` shows `lane:
fleet-blocker` and `why.fleet_blocker` (`<id>: <summary>`); text output ends the row with `clears <id>: ...`.
A `cleared` or missing record, a blocked brief, or an unset `fleet_blockers_dir` leaves the brief in its normal
lane with no error; an unreadable directory or record warns once on stderr and is skipped. `rank.py` pins these
briefs above the judged list, which fills whatever room `--top` leaves. When asked what is next, name the blocker
first. `cos: fleet-blocker` is not a valid declaration; the lane comes only from an open record.

## Defining your own priority tags

To weight specific tags (e.g. a "frog"/"urgent" convention), add to `tasks.toml`:

```toml
[priority]
tag_weights = { urgent = 500, frog = 400 }
```

Briefs carrying those tags get the weight added to their score. Empty by default — the
base ranking stays purely mechanical (due / importance / effort / unlocks).

## Scopes and work signals

`--scope` (`all`, `tag:<t>`, `project:<slug>`, `session:<id>`; the same parser `rank.py` uses)
filters the briefs ranked and picks the signal weights. Three typed signals add to the score
under both scorers: `multiplier`, `ships-pr` and `clears-blocker`. `[priority.signals]` says
how a brief earns each (any of `tags`, `types`, `fields`, `min_leverage`), and
`[priority.scope_weights]` weights them per scope kind. The defaults:

```toml
[priority.signals.multiplier]
tags = ["multiplier", "tax", "fleet"]
[priority.signals.ships-pr]
tags = ["tier:quick", "tier:smoke", "pr"]
fields = ["pr"]
[priority.signals.clears-blocker]
tags = ["blocker"]
fields = ["clears"]
min_leverage = 1           # something live waits on it; 0 turns this matcher off

[priority.scope_weights]
all = { multiplier = 1500, ships-pr = 800, clears-blocker = 800 }      # also tag and session
project = { ships-pr = 2000, clears-blocker = 2000, multiplier = 500 }
```

Each matcher and weight merges over its default, so `tags = []` drops one matcher. `--ids`
ranks just the named briefs wherever they are live (ready/, in-progress/, blocked/), which is
how a session ranks its own open ledger. `why.signals` and `why.scope` in `--json`, and
`+<signal> N` in text output, show what each signal added. The `fleet-blocker` lane still sorts
above every signal.

## WSJF scorer (opt-in)

`additive` (above) is the default and what every consumer reads. A second scorer, `wsjf`,
ranks by cost of delay divided by job size, plus an aging term:

- **cost of delay** = importance value + time criticality (due curve, plus lead time) +
  milestone weight + tag weights + leverage (longest chain of live briefs waiting on it) +
  the scope's signal weights.
- **job size** = `estimated-effort` points (xs 1, s 2, m 3, l 5, xl 8, blank 3).
- **inheritance**: a blocker scores at least as high as the highest brief waiting on it.
- **lanes** from a `cos:` field or tag: `expedite` (capped), `fixed-date` (joins near its
  due date), `standard` (default), `chore` (a reserved share of slots). The `fleet-blocker` lane sits above all of them (see below).
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

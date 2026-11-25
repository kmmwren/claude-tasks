# claude-tasks — design

A Claude Code plugin that gives any Claude session a **git-backed task queue** it can
capture into, structure, see, execute, and hand off. Each task is a self-contained
markdown *brief* — rich enough that an agent can act on it with zero prior context.

This is a clean, generic extraction of a personal system that has been in daily use. The
personal instance is untouched; this repo is the reusable engine plus a thin plugin
wrapper.

## Why this exists

Most "task managers" store a title and a due date — too thin for an agent to act on. The
unit here is a **brief**: goal (the desired end state), context (everything a zero-context
agent needs), success criteria (the contract), constraints, and an execution log. Briefs
live as one markdown file per task in a git repo, so the queue is greppable, diffable,
syncable, and survives any tool.

Two actors share the queue: a human drops ideas in; Claude captures its own follow-ups,
enriches raw notes into briefs, executes the ones it can do end-to-end, and hands a
session's loose ends back to the queue when it stops.

## Architecture: engine vs. config

The single idea that makes this generic: **everything user-specific is a value, not code.**

- **Engine** (identical for everyone): the lifecycle, the scripts, the skills, the board.
  Shipped in the plugin.
- **Config** (per user): a `tasks.toml` at the queue root holding the project name, the
  list of domains, and the timezone. Optional, with sane defaults.

The personal instance's tuning (leverage weights, charge model, external routing to other
task systems, cloud worker) is deliberately **out of v1**. Those become optional add-ons
later, so a vanilla install and a tuned instance run the *exact same engine*.

## The queue

A directory (its location is resolved, see below) with one folder per lifecycle state:

```
inbox/        raw, un-enriched captures (pre-briefs)
ready/        enriched, executable briefs
in-progress/  briefs being worked
done/         completed briefs (kept — they are the training data)
parked/       suspended briefs
view/         generated board.html (read-only viewer; never hand-edited)
tasks.toml    per-queue config
_template.md  the brief schema
```

A brief's `status:` frontmatter field always matches the folder it lives in. The board is
a *view*, regenerated from the briefs; the briefs are the source of truth.

### Root resolution

Scripts live in the plugin, but operate on the user's queue, which is elsewhere. The queue
root is resolved in this order (the same "walk up for a marker" trick git uses):

1. `$CLAUDE_TASKS_DIR` environment variable, if set.
2. The nearest ancestor directory containing a `.tasks/` queue (project-scoped queues).
3. `~/tasks` (the global default).

A resolved root must be an *initialised* queue (contains `tasks.toml`); if not, the scripts
fail with a clear message pointing to `/tasks-init`. This gives two modes for free: a global
personal queue, and project-scoped queues checked into a repo.

## The brief schema

YAML frontmatter (the queryable surface) + markdown body (the context). Required fields:
`id`, `title`, `created`, `updated`, `status`, `type`, `importance`, `autonomy`. Optional:
`estimated-effort`, `due`, `not_before`, `domain`, `tags`, `parent`, `assignee`, `source`,
`source_ref`, `blockers`, `clears`, `related`, `requires-repo`, `requires-local`, and the wake
fields `wake_probe`, `wake_generation`, `valid_until`, `needs`, `escalate_after` (plus the
`lease_*` and `wake_key` fields `maintain.py` writes when it claims a brief). `assignee` is who
has claimed the brief (empty = unclaimed): advisory coordination for a shared queue, not a lock
across clones.

### Waking due briefs (`maintain.py`)

`maintain.py --tier laptop|cloud` is the one trigger-agnostic entry point; a LaunchAgent
(`launchd/`) calls it every five minutes and a cloud routine can call it hourly. Each tier keeps
a last-run record in the queue (`maintain/last-run/<tier>.json`); a tick younger than the tier's
interval exits at once without touching git.

A `ready/` or `inbox/` brief is due when `not_before` has passed or `wake_probe` exits 0
(10 = not yet, anything else is logged and not due). A due brief still waits for its unmet
`blocked_by` entries, including `date:` days. A brief past `valid_until` is reported
`overdue` and not run. A brief whose `needs:` the tier lacks is skipped, and reported
`overdue-needs-<cap>` once `escalate_after` has passed.

A due brief is claimed by compare-and-set on origin: the lease (holder, expiry, attempts), the move
to `in-progress/` and the wake's run record go in one commit, pushed without rebasing. A rejected
push means another runner won and the claim is dropped; an expired lease can be reclaimed. Only a
published claim is handed to a detached headless run through the configured launcher. The run
record `maintain/runs/<key>.json` (brief id, trigger, generation) stops any tier running the same
wake twice. Settings live in `tasks.toml` under `[maintain]`, `[maintain.laptop]` and
`[maintain.cloud]`.
Body sections: Goal, Context, Success criteria,
Constraints, Notes / open questions, Execution log. Full reference in `_template.md`.

Schema and `_template.md` must change together — drift between them is the failure mode
that kills these systems.

## What ships in v1

Skills (each wraps a tested script or is pure prompt logic):

| Skill / command | Backed by | Purpose |
|---|---|---|
| `queue-task` | `add_task.py` | mint a full brief from anywhere |
| `raw-capture` | `raw_capture.py` | zero-friction unstructured drop into `inbox/` |
| `enrich` | prompt | turn a raw inbox item into a real brief |
| `action-task` | `transition.py` | execute a brief end-to-end through the lifecycle |
| `handoff` | `capture_session.py` | offload a session's open items to the queue |
| `recap` | prompt | quick in-chat status summary |
| `session-browser` | scanner + render | browse/resume sessions; one session as a board |
| `/tasks-init` | `init_queue.py` | scaffold a new queue (folders, config, template) |
| `/tasks-board` | `build_board.py` | regenerate + open the HTML board |
| SessionEnd hook | `capture_session.py` | auto-capture loose ends when a session ends |

Shared plumbing (untouched, pure-generic): `_concurrency.py` (flock + atomic writes,
making the queue safe for many concurrent Claude sessions) and `git_sync.py` (serialised
commits through the same lock). New generic modules: `_paths.py` (root resolution) and
`config.py` (`tasks.toml` loader).

## The queue engine: one source of truth, kept current

claude-tasks holds every brief, and `maintain` keeps the queues true without a person. Four rules set
the shape, each from a recorded decision (file names in claude-config `decisions/`):

- One store. `2026-11-04-generic-tasks-fold-into-projects.md`: "one single source of truth".
- Capture stays fast. `2026-11-17-capture-stays-fast-hygiene-runs-async.md`: "I don't want to add
  lots of slowness at the time of doing handoff into inbox".
- Anything triggers it. `2026-11-17-queue-engine-runs-anywhere-trigger-agnostic.md`: "I'd like to have
  anything run the engine".
- Nothing is lost. `2026-11-17-triage-never-loses-work.md`: "the most important thing is that I don't
  lose things".

### One store across queues

The git sync, the `wip/tasks-<slug>` branch of a queue inside another repo and the parked-conflict
branches are in [queue-sync.md](queue-sync.md). On top of that:

- A brief lives in one queue. A wait across queues is a `<queue>:<id>` blocker, resolved against the
  roots in `$CLAUDE_TASKS_ROOTS`; `check_deps.py` reports a reference to an unknown queue or brief as
  `dangling`.
- Moving a brief from the generic queue into the project that owns it is the router, personal tooling
  outside this repo. It scores each brief against the registry's project names and `route_terms`,
  moves the unambiguous ones into `direct` queues, and leaves `routed/<id>.md` as a pointer in the
  brief's place, never a copy. Hygiene runs it as `router.py tick --no-judge` for the generic queue
  only, so an ambiguous brief waits for a judged pass.
- A session project folded back into its parent leaves a `moved_to:` stub (`fold_session.py`).

### Triggers, phases and the lease

Any trigger calls `maintain.py --tier laptop|cloud`. After the wake step above, the tick runs
**hygiene**: ordered steps in two phases, each with its own freshness record.

| Step | Phase | Does |
|---|---|---|
| `unblock` | deterministic | releases blocked briefs whose waits are done |
| `dep-check` | deterministic | `check_deps.py`; findings are reported, not a failure |
| `blocker-recheck` | deterministic | re-tests external blockers (personal tooling, generic queue only) |
| `route` | deterministic | the router above |
| `triage-mechanical` | deterministic | `triage.py promote --apply`: promote, merge duplicates, close on evidence |
| `board` | deterministic | regenerates the board |
| `triage-judge` | judgement | `triage.py promote --apply --llm`: model verdicts, drop alerts |
| `board-after-judge` | judgement | regenerates the board |

- The deterministic phase needs no Claude credits and always runs. The judgement phase runs only when
  the usage-limit reader (`limit_state`) shows an account with capacity, or is absent; otherwise the
  phase is deferred untouched and the next tick retries.
- A phase is due when its record is older than `hygiene_interval_minutes` (default 240). A step whose
  script is absent on this machine is recorded as skipped, and a runner that has the script treats the
  phase as due again, so a cloud tick without the personal tooling does not hide the work from a laptop.
- One runner at a time holds `maintain/hygiene-lease.json`. Taking and releasing it are commits pushed
  from a scratch worktree cut at origin's tip; a push that lost a race restarts from the new tip, up to
  eight times. Releasing writes the finished phases' records in the same commit, so a record reaches
  origin exactly when the lease is let go. A failed phase is not recorded and is due again.
- A lease that expires mid-run starts no further step and records nothing. A refused push prints
  `ERROR` and reports `hygiene-refused`; other failures warn and the tick carries on.

### What keeps each thing current

- **Dependencies.** `unblock` moves a blocked brief to `ready/` once every `blocked_by` entry is done,
  including `<queue>:<id>` and `date:` entries. `transition.py move <id> done` also logs what landed on
  every dependent in every known queue (`cascade.py`); a close by any other route, triage included,
  releases its dependents at the next `unblock` step. `dep-check` reports cycles, dangling ids,
  undeclared dependencies and a wait described only in prose.
- **Ordering.** Briefs released together land in `ready/` in priority order, which is the order to work
  them.
- **Priority.** Never stored. `prioritise.py` scores every brief on each call: the default scorer adds
  150 per brief that waits on it, and the `wsjf` scorer adds age and lets a blocker inherit the score of
  the brief waiting on it. The ranker above sits on top. Nothing needs refreshing.
- **Done-ness.** Triage, below.

### Triage: promotion is the gate

Capture writes to `inbox/` and the sync commits it. Promotion to `ready/` is where `triage.py` checks
a brief. The owner's rule: "Inbox is kind of a dumping section but by the time they get in ready, they
should have been deduplicated or they could be marked straight as done". One run, in order: collapse
bulk filings, declare empty dependencies, find evidence closes, classify and promote, merge
duplicates, close, alert, commit.

- **Evidence closes.** In `inbox/` and `ready/`, a brief moves to `done/` when every success criterion
  is ticked, or when a PR its title or goal names is merged or closed and nothing says more follows. An
  unreadable PR is no evidence. The brief records `closed_reason`, `closed_evidence` and `closed_from`.
- **Duplicates.** A brief with the same normalised title and cited sources as an earlier one is
  appended to the survivor under `## Merged from <id>`, then moved to `done/` with `duplicate_of`.
  A survivor that is closed or claimed takes no merge, so the duplicate stays put.
- **Judgement alerts.** A needs-info brief untouched past `stale_days` (21) and a model-called `drop`
  raise one aggregated alert per run, repeated no sooner than `stale_days` later. The brief does not
  move. Nothing is parked or closed on a timer or on a model's say-so.
- **Refusals alert.** A close refused because criteria are unticked, or not carried by the survivor,
  raises an `UNCLOSABLE` alert; a duplicate's merge already made is kept.
- **Undo.** Every close is a row in `closed.jsonl`; `triage.py reopen <id>` puts the brief back and
  marks it so triage never closes it again. At most `max_moves` (200) closes run per run, evidence first.

### One brief through the engine

1. A session hands off "Add retry to the export job" and the sync pushes it to `inbox/`.
2. At the next hygiene run `route` matches its project's `route_terms`, moves it into that project's
   queue and leaves `routed/<id>.md` behind. The project queue's own tick runs its triage next.
3. A second session captures the same title citing the same PR. Triage fingerprints it as a repeat,
   appends its body to the first brief, and moves it to `done/` with `duplicate_of` set.
4. The first brief passes the gate and is promoted to `ready/`.
5. The PR merges. The next run reads the PR as merged, moves the brief to `done/` with
   `closed_evidence: <repo>#<n> MERGED`, and `unblock` releases the briefs that waited on it.
6. Had the duplicate in step 3 carried an open criterion the first brief lacks, its close would be
   refused, its merged body kept, and an `UNCLOSABLE` alert would name it.

### Not built yet

- A cloud routine that calls `maintain --tier cloud`, and a trigger that picks accounts by remaining
  credits. Only the laptop LaunchAgent template ships. The wake step's last-run record is per tier, so
  a cloud tick does not yet skip on a fresh laptop tick; hygiene phases do skip, as their records are
  shared.
- Judged routing as a hygiene step: `route` runs without the judge.
- A health metric (inflow against outflow, share stale) on the board.
- A separate store for peer messages and agent reports.
- Capture without checks: `add_task.py` still merges an exact repeat into the brief it matches, and
  `--ready` still files straight to `ready/` after its field checks. The rule above would move both
  into triage.

## The predictor head: ranking against a taste profile

`prioritise.py` is the filter. `rank.py` is the ranker, and only human-facing lists use it.

- **Profile.** `profile/priorities.md` holds typed sections in a file the owner edits: `Goals` (each
  `name | weight w | match tag, tag | note`), `Values`, `Dislikes` and `Patterns`.
  `seed_profile.py` drafts it from goal files, a north-stars document and recent decision titles,
  and marks it `draft: true`.
- **Retrieval, then judgement.** The scope's unblocked briefs in filter order are cut to
  `[rank] candidates`. The judge ranks that set against the profile. Swapping retrieval for
  embeddings later touches only `candidates()`.
- **Judge.** One setwise call ranks all candidates, and a second call ranks them in reverse order.
  A Bradley-Terry fit merges the two orderings, and agreement between the passes becomes each item's
  confidence. The backend is `claude -p` with tools disabled, brief text escaped and delimited as
  data, and output constrained by a JSON schema. Anything other than an exact, reasoned permutation
  is rejected.
- **Launcher.** The judge and the triage calls (`headless.py`) resolve one launcher the same way:
  the first of `[rank] launcher`, `$CLAUDE_TASKS_LAUNCHER` and the deprecated `$CLAUDE_ANY_BIN` that
  is an executable file. When one is named but none is usable, a single stderr line says so and the
  call uses plain `claude -p` on `PATH`. A refused or failed call falls back like any other judge
  error.
- **Launcher contract.** A launcher is an executable that adds `-p`, accepts claude's flags, takes
  the prompt on stdin and prints claude's JSON envelope. It is run with `CLAUDE_TASKS_STDIN=1`,
  meaning forward stdin, and `CLAUDE_TASKS_LOCAL_REASON=<reason>`, meaning run here and not on an
  asynchronous cloud route that cannot return an answer. `[rank] local_reason` sets the judge's
  reason. The legacy `CLAUDE_ANY_STDIN` and `CLAUDE_LOCAL_OK` (`read-only-research:<reason>`) are
  set too, so launchers written against them keep working.
- **Fallback.** A missing or unreadable profile, fewer than two candidates, or two invalid passes
  return filter order with the reason in the header.
- **Cache and state.** Verdicts are cached by scope, profile version, brief content hashes and
  model, so an unchanged list costs nothing. Cache, pick log, proposals and profile snapshots live
  under `~/.claude-tasks/predictor/<queue hash>/` (`$CLAUDE_TASKS_STATE`), never in brief
  frontmatter or the queue's git history.
- **Pick log.** Each shown list is appended to `picks.jsonl` with the profile version, the filter
  order, each item's matched goals and the raw judge output for replay. Moving a brief to
  `in-progress` logs a pick, `bump.py` logs a bump, and `log_pick.py skip` logs a skip. Each choice
  links to the latest list that showed that brief within a day.
- **Learning.** `learn.py` turns choices into skip-above preference pairs: a pick beats the briefs
  shown above it, and a skip loses to the next one. It nudges the most-preferred and most-avoided
  goal weights, each by at most `max_change`, and replays the newest picks as a held-out set. A
  reorder must replay better and a nudge no worse. Proposals are versioned diffs, applied only on
  `--apply` after the old profile is snapshotted, and revertible. Auto-apply covers nudges only, and
  only once `[learn] auto_apply = true` and `min_runs` lists have been shown.

## Explicitly deferred (optional add-ons, not v1)

Strategic/leverage prioritisation, the overwhelm-gated personal surface, the twice-daily
check-in, external routing to other task systems, and the autonomous cloud worker. These
are either personal calibration or not-yet-stable. They are designed to bolt on without
changing the engine.

## Quality bar

Enforced in CI, blocking: `ruff` (lint), `mypy --strict` (types), `pytest` with a
**100%-coverage gate on our own code**, and **mutmut** mutation testing. Line coverage
proves code ran; mutation score proves the tests actually assert behaviour.

## Genericisation performed during extraction

- Hardcoded `ROOT = __file__.parent.parent` → `_paths.resolve_root()` in `add_task`,
  `raw_capture`, `git_sync`, `build_board`.
- Board title (a hardcoded repo slug) → project name from `tasks.toml` (generic default).
- `_template.md`: dropped a personal "enjoyed" routing field; neutralised the charge-model
  tag vocabulary to a generic "tags are free-form" note; generic example domains.
- `source:` values changed from a personal name to generic `user|claude|intake`; `source_ref`
  records where an intake brief came from.
- Removed references to personal external doc paths in script docstrings/context.

## Distribution

Installed as a Claude Code plugin. The plugin ships skills + commands + hooks + scripts;
`/tasks-init` scaffolds the user's own queue wherever they choose. The queue itself is the
user's private data and is never part of the plugin. The repo is built to be published
under an OSI licence when the owner chooses; no public push happens as part of the build.

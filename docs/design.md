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
`estimated-effort`, `due`, `domain`, `tags`, `parent`, `assignee`, `source`, `blockers`,
`related`, `requires-repo`, `requires-local`. `assignee` is who has claimed the brief
(empty = unclaimed): advisory coordination for a shared queue, not a lock across clones.
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
- `source:` values changed from a personal name to generic `user|claude`.
- Removed references to personal external doc paths in script docstrings/context.

## Distribution

Installed as a Claude Code plugin. The plugin ships skills + commands + hooks + scripts;
`/tasks-init` scaffolds the user's own queue wherever they choose. The queue itself is the
user's private data and is never part of the plugin. The repo is built to be published
under an OSI licence when the owner chooses; no public push happens as part of the build.

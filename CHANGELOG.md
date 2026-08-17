# Changelog

## Unreleased

### Added
- **`assignee:` frontmatter field** — optional, free-form (person or agent), empty means
  unclaimed. Lets several people/agents share one queue without silently picking the same
  brief: pull, then `transition.py move <id> in-progress --assignee <who>`, which claims and
  moves in one locked commit that lands before the work starts. Advisory coordination only,
  **not** a lock across clones. Rendered as a chip on the board and included in its search.
  Briefs without the field keep working unchanged, and transitions without `--assignee`
  leave an existing claim untouched (`--assignee ""` releases it).
- **Directed handoff** — `add_task.py` gains `--status {inbox,ready,parked}` (default
  inbox; `--ready` kept as an alias). The `handoff` skill can now route a session's items to
  a chosen lifecycle state per the user's direction (e.g. "hand off everything to parked"),
  optionally with a shared directive note.
- **`prioritise.py` + `prioritise` skill + `/tasks-next` command** — a dependency-aware
  ranking of the `ready/` queue ("what's next?"). Generic, transparent score: due urgency
  + importance + quick-win effort + lead time + a bonus for briefs that unblock others,
  plus optional `[priority] tag_weights` from `tasks.toml`.
- **Real dependency handling** — `blockers:` ids and `depends:<id>` tags are resolved
  against the queue; a brief stays blocked until its dependency reaches `done/`, and
  briefs that unblock more others rank higher.

## 0.1.0 — initial release

First generic, open-sourceable extraction of a personal task-queue system into a Claude
Code plugin.

### Added
- **Engine** (pure-stdlib Python): git-backed brief lifecycle with cross-process locking
  (`_concurrency.py`), serialised commits (`git_sync.py`), queue-root resolution
  (`_paths.py`), per-queue config (`config.py`), and queue scaffolding (`init_queue.py`).
- **Scripts**: `add_task.py` (mint a brief), `raw_capture.py` (inbox dump),
  `transition.py` (resolve + move briefs through the lifecycle), `build_board.py`
  (self-contained HTML board).
- **Skills**: `queue-task`, `raw-capture`, `enrich`, `action-task`, `handoff`, `recap`.
- **Commands**: `/tasks-init`, `/tasks-board`.
- **Quality**: ruff, `mypy --strict`, 100% test coverage (110 tests), and mutation testing
  (zero surviving mutants on the resolution/config core) — all gated in CI.

### Notes
- Root resolution: `$CLAUDE_TASKS_DIR` → nearest `.tasks/` → `~/tasks`.
- Config (`tasks.toml`) is the only per-user surface; the engine is identical for everyone.

See [docs/design.md](docs/design.md) for the design and what was deferred to later add-ons.

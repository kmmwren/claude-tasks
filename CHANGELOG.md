# Changelog

## Unreleased

### Changed
- **Breaking: the default wrapper launcher is removed.** Set `[rank] launcher` or
  `$CLAUDE_TASKS_LAUNCHER` to run the judge and the triage calls through a launcher script; with
  neither, both call plain `claude -p`. A named launcher that is missing or not executable prints one
  notice and falls back. To keep a custom launcher, set `CLAUDE_TASKS_LAUNCHER` to it.
- **One launch model.** `Launch`, `DirectLaunch` and `ScriptLaunch` live in `headless.py` (`WrapperLaunch`
  is now `ScriptLaunch`, and it no longer carries the reason); `resolve_launch` builds the launch the
  judge, `rank.py`, the triage and `headless.command` all use, and `rank_table` is the one reader of
  `[rank]`. A launcher is run with `CLAUDE_TASKS_STDIN` and `CLAUDE_TASKS_LOCAL_REASON` (the legacy
  `CLAUDE_ANY_STDIN` and `CLAUDE_LOCAL_OK` are still set).
- **`[rank] local_reason` replaces `local_ok`.** It is the reason a launcher is told the run stays
  local. `local_ok` and `$CLAUDE_ANY_BIN` still work as deprecated aliases with a notice; with
  `local_ok` set and no launcher configured, the notice says to set `[rank] launcher` or
  `$CLAUDE_TASKS_LAUNCHER` because no default launcher is assumed.
- **`handoff` is generic.** An optional `~/.config/claude-tasks/handoff.md` adds personal steps; Step 0
  project routing, the headroom check and the long-running-session branch are removed.

### Added
- **Triage stops calling `claude` for the rest of a run once no account has capacity.** After a
  wrapper exit of 2, 3, 4 or 10, later batches return nothing without spawning; their briefs get
  no verdict, are not logged, stay untouched and are counted as `unclassified` in the report. Each
  `triage.run` starts unlatched. A failure prints its exit status and reason, never the command.
- **A brief's milestone membership lives only in its `milestone:` frontmatter, and a milestone can
  name its `release`.** `milestones.members` is the one function every reader uses (progress, the
  board strip and the project page); a `briefs = [...]` list on a `project.toml` milestone is
  ignored, not an error. An optional `release` string per milestone, in the milestones file or
  `project.toml`, is read into `MilestoneSpec` and shown in the CLI, the board strip and the
  project page; absent means unset.
- **`add_task.py --source intake --source-ref <url|path>` records where a brief came from.** An
  intake brief must carry a ref; the ref is written as `source_ref:` after `source:`, a repeat
  capture logs its own ref on the brief it merges into, and a ref that would not read back as one
  frontmatter line (a newline, or a ` #` comment) is rejected. The queue template and design doc
  list the field.
- **Every frontmatter flag is written as a value that reads back whole.** `--title`, `--domain`,
  `--due`, `--effort` and `--parent` fold any whitespace run, line breaks included, into one space
  and join a ` #` to its word (`Fix PR #12` is filed as `Fix PR#12` instead of reading back as
  `Fix PR`); each `--tags`, `--requirements` and `--blocks` value splits on `,` into items with
  `[` and `]` replaced, so no flag can inject a key or close a list early. A changed value is
  reported once on stderr.
- **`resume_prompt.py` prints the successor prompt for a long-running session.** At the end of a
  session handoff it rebuilds the paste-ready resume prompt from the live ledger (open items, in
  progress first, each with its `metadata.brief` and blocker) and the long-running registry's
  mission; an unreadable registry, job state or ledger file warns and is skipped.
- **`handoff` also hands off running agents and background tasks.** Step 1 lists the session's
  background `Agent` and `run_in_background` work with no completion yet, writes each one's brief
  (what it was asked, its output file, how to collect or re-run it) into the item it serves, and
  records them in the ledger item's `metadata.agents`; `resume_prompt.py` prints them under the item.
- **`rollup.py` projects one or more queues' projects for readers above the engineer.** `pm` gives
  each project's health, ask, now/next/done and what shipped in the last 14 days; `cto` gives a table
  of health, milestones done of total, next due and missed. Output is JSON (schema
  `claude-tasks.rollup/v1`) plus Markdown, written to a folder for GitHub (`write --out <dir>`) or served on a
  loopback port (`serve`). Sources come from `~/.config/claude-tasks/rollup.toml` (or `--config`,
  `$CLAUDE_TASKS_ROLLUP_CONFIG`) and default to the current repository. Project text is escaped, never
  becomes a link or script; one bad queue file is skipped with a reason, and a queue or source that
  cannot be read, or one that holds no project, is reported as a "Not shown" row rather than
  stopping the run (the JSON also gains `source_projects`, a project count per source). A bad `[[source]]`
  row or a malformed default config warns and the run carries on; only a config you named is an
  error. A source that is a git work tree behind its upstream gets a "N commits behind" warning
  (no fetch) in `sources`, the Markdown and HTML footers and the JSON `warnings`. `write` also
  generates a `README.md` saying how someone else gets their own view. A pm milestone's `waiting` key counts
  its briefs waiting on an unmet blocker. `serve` reuses each source's behind-upstream count for 30 seconds
  rather than running git on every request. A destination `write` cannot create is a one-line error.
  A milestone's percent is the one the project page shows (from ticked criteria). An absent default
  config falls back to the current repository without a warning; a repeated source label, a `[[milestones]]`
  row without a text `id` and a `[project] slug` that is not text each warn and skip just that row
  (`--config` given with `--source` warns that it is ignored). Bare URLs and emails in project text are
  escaped so GitHub does not link them; `serve` also sends `X-Content-Type-Options: nosniff` and
  `frame-ancestors 'none'`. A source whose projects another source already shows is named under "Not
  shown". The `rollup` skill drives it.
- **Project views warn and skip a malformed milestones file**, and a `[[milestones]]` row without a text `id`,
  instead of stopping.
- **One shared `warn` in `_paths.py`** replaces the copies in `project_view`, `seed_profile`, `headless`
  and `fleet_blockers`; each keeps its output prefix.
- **`prioritise.py` weights typed work signals per scope.** `--scope all|tag:<t>|project:<slug>|session:<id>`
  filters and weights the ranking; the scope parser moved from `rank.py` to a shared `scope.py`, and
  `rank.py` passes its scope through. Signals `multiplier`, `ships-pr` and `clears-blocker` are matched by
  `[priority.signals]` (tags, types, fields, min_leverage) and weighted by `[priority.scope_weights]`:
  the whole queue favours multipliers (1500) over shipping and unblocking (800 each), a project favours
  shipping and unblocking (2000 each) over multipliers (500). Both scorers add them; `--json` (schema 3)
  adds `status`, `why.signals` and `why.scope`. `--ids` ranks named briefs across the live folders, and
  `--root` names the queue. With the defaults, a brief with a `clears:` field or a live waiter now ranks
  higher than before.
- **A brief can declare the fleet blocker it clears, and `prioritise.py` ranks it first.** A new optional
  `clears: <blocker-id>` (or `[ids]`) names a blocker whose record sits in `fleet_blockers_dir` (a new
  `tasks.toml` key, unset by default so the feature is off). While the record's `status` is `active` or
  `unverified`, the brief takes a new `fleet-blocker` lane that sorts ahead of every other lane under both
  scorers and is outside `expedite_cap`; `--json` gives `lane: fleet-blocker` and `why.fleet_blocker`
  (the blocker id and summary). A cleared or missing record, or an unset directory, leaves the brief in its
  normal lane. An unreadable directory or record warns once on stderr and is skipped. A blocked brief does
  not take the lane. `rank.py` pins these briefs above the judged list and never sends them to the judge.
- **`rank.py`: rank a scope's candidates against a taste profile, with a reason each.** It filters
  through `prioritise.py`, then makes two setwise `claude -p` passes (forward and reversed, tools off,
  schema-validated). Bradley-Terry merges the passes, and their agreement sets each item's confidence.
  Results are cached by brief and profile hash. Any failure falls back to filter order and says why.
  `--json` gives machine-readable output.
- **`seed_profile.py` drafts `profile/priorities.md`** from goal files, a north-stars document
  (`gh api`) and recent decision titles, marked `draft: true` until the owner edits it.
- **Pick log and learning loop.** Shown lists and picks, bumps and skips go to a local JSONL log
  outside the queue repo. `transition.py` to `in-progress` and `bump.py` record a choice;
  `log_pick.py` records the rest. `learn.py` proposes capped goal-weight changes, validated on
  held-out picks, applied and reverted by profile version. Auto-apply is off by default.
- **`prioritise.py` gains an opt-in WSJF scorer.** Cost of delay (importance, due curve and lead
  time, milestone, tag weights, chain leverage) over job size, plus aging; blockers inherit their
  waiters' score; `cos:` lanes (expedite cap, fixed-date, chore share) and a `move` per brief.
  Chosen by `[priority] scorer` (default `additive`, unchanged) or `--scorer`. `--json` adds both
  scores and ranks, `lane`, `move`, `leverage`, `why` and `schema_version`; existing fields are
  unchanged.

### Changed
- **The project view's rollout table headers show each stage's own label.** The `STAGE_WORDS`
  mapping that renamed stage labels is removed, so a header reads exactly as the project declares it.

### Fixed
- **Adjacent Execution-log appends no longer wedge `git_sync.py`.** When the rebase of local commits
  onto `origin/main` conflicts only because two writers each appended to one brief's Execution log
  (typically `append_log.py` on origin against a local move to `done/`), the file resolves as the base
  entries unchanged followed by both sides' new entries in date order, and the push goes ahead. A union
  built on an `origin/main` that has since moved is refused by the fast-forward-only push and reported
  as a deferral. Any other conflict, including a side that rewrote or reordered an existing entry,
  still aborts and reports as before.
- **`append_log.py` retries a push GitHub refused because another writer was updating main.** A
  server-side ref-lock clash reports `[remote rejected] HEAD -> main (failed)` (or `cannot lock ref`,
  `incorrect old value`), which the retry loop did not count as a lost race, so the line was dropped.
  These now retry from fresh origin/main like `[rejected]`; a declined hook still fails at once.
- **`append_log.py` no longer hangs when GitHub does.** Its fetch and push each stop after 20 s
  (`git_sync.NETWORK_TIMEOUT`); it then exits 2 naming the line it could not log.
- **Queue writes no longer fork a `git push` each.** `git_sync.sync_commit` still commits
  synchronously, then marks the clone dirty and returns; one detached pusher per clone (a flock on
  `.git/.tasks-push.lock`) waits for in-flight writers, pushes once, and pushes again only if a write
  landed meanwhile. Twenty concurrent writes cost at most two pushes. Its output, including any
  divergence report, goes to `.git/.tasks-push.log`, which the next write prints and clears.
  `sync_commit_and_push` pushes before returning, which `append_log.py` uses because it reads the
  brief back from `origin/main`. `triage.py`'s report field `synced` is now `committed`. A write never runs `git fetch`: the shadow
  check reads `origin/main` as the last sweep, push or rejected-push fetch left it. The sweep fetches
  unless `FETCH_HEAD` is under 45 s old, so a dispatch-time sweep reuses the timer's fetch, and a rejected push still fetches and rebases. The 60 s `git_sync.py --sweep` job is the catch-up for a push that failed. `--sweep` pushes
  before it exits, after any running pusher, so `dispatch-cloud.sh` sees a fresh brief on `origin/main`. A sweep finishes inside 110 s, under dispatch's 120 s flush timeout: its fetch is cut off at 20 s and its wait for a running pusher shrinks by whatever the fetch and commit took. A sweep with
  nothing ahead of `origin/main` starts no push. The queue's sync lock also moved to the clone's
  common git dir, so every worktree of one clone shares it.
- **The readiness gate also covers `parked/` (and `done/`) sources.** `transition.py move` from any folder
  outside `ready`, `in-progress` and `blocked` into one of them refuses an un-enriched brief, so inbox to
  parked to ready is no longer a way round it. Landing in `parked` and closing to `done` stay unconditional.
- **`transition.py move` promotes an inbox file only once it carries a goal, success criteria and
  context.** Moving it to `ready`, `in-progress` or `blocked` runs the same check `add_task.py --ready`
  applies (now `readiness.py`), treating add_task's placeholder stubs as missing, and refuses naming
  the missing fields. `done` and `parked` are unconditional, so a folded brief can still be closed.
- **`git_sync.py` commits only what the caller names.** It no longer runs `git add --ignore-removal .`,
  which swept another writer's uncommitted edit into an unrelated brief's commit. `ids` stage that
  brief's file at any depth (moves included); `paths` stage explicit pathspecs (`fold_session.py`
  passes `.`). A lock-held deferral now says to re-run with the same ids.
- **No autostash on a diverged push.** The local commits are rebased onto `origin/main` in a scratch
  worktree and pushed; the checkout advances with `reset --keep`, so uncommitted edits are never
  stashed or dropped. A rebase conflict leaves the local commits in place and prints the conflicting
  paths plus the unpushed and unintegrated commits.
- **A closed brief is no longer re-created.** `git_sync.py` fetches `origin/main` and refuses to
  commit a new brief file whose id is already filed in another state dir (`done/` included), on
  disk or on origin. The file stays untracked and a message names it, so a late note aimed at a
  moved brief cannot leave the id in two states.
- **`append_log.py` finds a brief that is uncommitted in the main checkout.** A brief filed on disk
  but never committed (a peer-message brief, say) was invisible to the temporary worktree and
  failed with "no brief with id". It is now committed through `git_sync` first, staging only that
  brief, so the log line lands and the checkout's next pull is not blocked by an untracked copy.
- **`append_log.py` no longer gives up while other writers keep pushing.** Local callers take turns
  on the queue lock, and a push that loses a race retries from fresh `origin/main` with jittered
  backoff (8 attempts). A push refused for any other reason fails at once with git's message, and
  every failure prints the line that was not logged.

### Added
- **`append_log.py`** appends a dated execution-log line to a brief from a temporary worktree
  off `origin/main`, commits, pushes and removes the worktree, so a subagent refused writes in
  the queue's main checkout can still log: `python scripts/append_log.py <id> "<line>"`.
- **Slack handoffs can go to `parked/`.** `slack_links.py` flags each link `parked` when the
  batch direction or that link's own note says park; `handoff` files flagged links with
  `--status parked`, other directions still route through Directed handoff, and `queue-task`
  sends pasted Slack links there. A Slack or artifact handoff captures only those links: the
  session keeps its open work and carries on.
- **Slack links as a handoff input.** Paste Slack permalinks, each optionally followed by a
  note, and `handoff` files one brief per link; `slack_links.py` splits the batch, with text
  before the first link as a shared direction.
- **A first-class `blocked/` lifecycle state.** `transition.py move <id> blocked --blocked-by
  <ids|external:reason>` records what a brief waits for; moving it anywhere else clears the
  list. Unknown ids and self-blocks are refused.
- **`unblock.py`** returns blocked briefs to `ready/` once every blocker is `done`, in priority
  order, automatically after `move ... done` and on demand. `unblock.py resolve` clears an
  `external:` blocker.
- **`check_deps.py`** reports dependency cycles, dangling ids, in-progress briefs with unmet
  dependencies, reasonless blocked briefs and releasable ones.
- **Optional, project-owned milestones.** `milestones = "<path>"` in `tasks.toml` validates a
  brief's `milestone:` against that file and groups board progress by it; unset, nothing
  changes. The board also gains a Blocked column showing each brief's reasons.

### Fixed
- **Claiming no longer overwrites someone else's claim.** `move --assignee` now reads the
  current `assignee` under the same move lock and refuses a *different* non-empty claimant
  instead of replacing it, closing a race where two local sessions could both report success
  and the first could commit the second's claim. Re-claiming with the same assignee and
  claiming an unclaimed brief are unchanged; a plain `move` still preserves the claim
  silently; and `--assignee ""` still releases whoever holds it, which stays the documented
  way to free a stuck brief.
- **`--assignee` is validated as a single-line value.** A newline or carriage return in it is
  rejected with a clear error instead of being written into the frontmatter, where it could
  have injected further keys. A whitespace-only value normalises to a release.
- **Inline frontmatter comments are no longer parsed as values.** Every parser
  (`transition.py`, `prioritise.py`, `build_board.py`) shared the same gap: `assignee: #
  OPTIONAL who has claimed this…` read back as that whole comment, so a brief copied
  straight from `_template.md` looked *claimed by a comment*, and even populated fields like
  `status: ready # inbox | ready | …` carried their comment into the value. Cleaning is now
  one shared `_paths.strip_comment`. A `#` that opens the value or is spaced off from it
  starts a comment; a `#` inside a value (`issue#42`, `[a#b]`) is kept.

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

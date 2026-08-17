---
name: action-task
description: Use when the user wants Claude to actually DO / execute / action / run / work on / pick up / start / tackle a SPECIFIC task that already lives in the claude-tasks queue — e.g. "action the staging task", "do the onboarding brief", "work on the billing one", "pick up X from the queue", "start the migration task". Resolves the phrase to a single brief, moves it to in-progress, executes it end-to-end against its success criteria, and marks it done (or escalates if it hits something only the user can decide). NOT for adding a task (use queue-task / raw-capture) or offloading the current session's loose ends (use handoff).
---

# action-task — execute one specific brief from the queue

The user names a task; you find it, do it, and close it out. The must-be-consistent
mechanics (frontmatter edits, the file move, the locked commit, the `ESCALATED —` token)
live in the tested `scripts/transition.py` — **always go through it; never hand-edit
`status:` or run raw git** in the queue (that bypasses the cross-process lock and can
corrupt concurrent writes).

All commands below resolve the queue via `$CLAUDE_TASKS_DIR` → nearest `.tasks/` →
`~/tasks`, or pass `--root <path>`.

## The flow

### 1. Resolve the phrase to ONE brief
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transition.py" resolve "<what the user said>"
```
Ranks actionable briefs (ready / in-progress / parked — never inbox) by relevance; exact
id wins outright. Then:
- **One clear top hit** → use it, name it back ("Actioning `<id>` — <title>"), continue.
- **Several close hits** → show the top 3–4 (id + title + status) and ask which.
- **No hits** → say so, show the closest if any, and offer to add it (`queue-task`). Don't
  invent a brief and execute it.

### 2. Load the full brief + its context
Read the resolved file in full, then pull the context it points to (parent/related briefs,
linked repos, PRs, docs) so you execute with zero guesswork. The **Success criteria are
the contract** — that list is what "done" means.

### 3. Gate on autonomy + blockers (before touching anything)
- `autonomy: full`, no unmet `blockers:` → proceed.
- `autonomy: needs-input` or an open question in Notes → this brief is waiting on the
  user. Surface the specific decision and ask it; do not silently upgrade and charge ahead.
- `autonomy: blocked` or listed blockers unmet → name the blocker; offer to action the
  blocker first, or do the unblocked part. Don't start the blocked work.

### 4. Claim it and move it to in-progress
On a shared queue, `git pull` first so you see other people's claims, then claim and move in
one locked, committed step with `--assignee`:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transition.py" move <id> in-progress \
  --assignee "<who is doing the work>" --log "started"
```
(Commits via the shared lock automatically. Pass `--no-sync` only if you'll batch the
commit yourself with `scripts/git_sync.py`. Omit `--assignee` on later transitions and the
existing claim is left alone; pass `--assignee ""` to release it.)

That commit **is** the claim: it lands before any of the actual work, so everyone else sees
the brief is taken. `assignee` is advisory, not a lock — if someone else already claimed it,
pick a different brief rather than working it in parallel.

### 5. Do the work — properly
Execute against the success criteria using the right approach (TDD for code, etc.). Tick
`- [ ]` → `- [x]` in the brief as criteria land, so the board shows real progress.

### 6. Close it out
- **Done** (all criteria met, *verified* — run the tests/build, don't assume):
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transition.py" move <id> done --log "<one-line outcome + evidence>"
  ```
- **Escalate** (hit something only the user can decide or access): append the load-bearing
  token and pause, leaving the brief in in-progress so the work isn't lost —
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transition.py" move <id> in-progress --log "ESCALATED — <exact decision/access needed>"
  ```
  then edit the brief to set `autonomy: needs-input`, write the precise question under
  `## Notes / open questions`, commit with `scripts/git_sync.py`, and stop. The
  `ESCALATED —` spelling is exact (an autonomy tracker can grep for it).
- **Park** (no longer worth doing now): `transition.py move <id> parked --log "<why>"`.

## Safety rails (these override "just finish it")
- **Destructive or outward-facing actions get confirmed first.** Never delete/overwrite
  in an external system, send an email, or post publicly without explicit confirmation,
  unless the brief explicitly authorises it. Prefer additive/reversible operations.
- **No charges without explicit sign-off.** Any action that could cost money stops, states
  exactly what would be charged, and waits for the user to confirm.
- **One brief at a time.** If you discover adjacent work, capture it with `queue-task`
  rather than scope-creeping the current brief.

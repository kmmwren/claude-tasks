---
name: requirements-extract
description: Use when a message is a project brain dump ("I want to build", "project", many distinct asks, a long pasted spec), when later messages add asks to a large project, or when a hook says to run requirements-extract. Not for one-off fixes or questions.
---

# requirements-extract

Turns what the user said into an exhaustive, id-stable requirements register next to the
project's queue, then covers every requirement with a brief. A requirement that is not in
the register does not get built, and one that is not in a brief does not get verified.

## Is it large enough?

Large = at least 5 atomic requirements, or at least 3 that span several sessions or several
repos. Large gets a queue, a register and briefs. Anything smaller stays on the session
ledger with no brief. Count after splitting (step 2), not by message length.

## Steps

1. **Find or create the queue.** Project queue is `<repo>/.tasks/` (team: `~/code/ai-knowledge-mm/.tasks`). If it has no `tasks.toml`, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/init_queue.py" <dir> --name <project> --prefix <PREFIX>`; that also writes `requirements.md`. Always pass `--root`/explicit paths; never rely on `$CLAUDE_TASKS_DIR`.
2. **Split.** Read every message (the brain dump plus later ones) and cut it into atomic requirements, one testable statement each. Kinds: `capability`, `constraint`, `quality`, `user`, `flow`, `integration`, `data-rule`, `hosting-rule`, `test-rule`, `never`. Every "never", "always", "must", "don't want" and "ideally" is its own entry. Split compound sentences: "X, and also Y, ideally Z" is three entries, and a list or a run of "and/also/plus/then" clauses gets one entry per clause. Splitting too coarsely is how asks get lost: 30 sub-asks vanished from one project when long messages were summarised into a few items.
3. **Quote.** `quote` is the shortest verbatim span of her words that carries the ask (at most 240 characters, no paraphrase). `source` is the ISO timestamp of the message it came from (transcript record `timestamp`).
4. **Append, never edit ids.** Write the items as a JSON list of `{kind, title, quote, source}` and run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/requirements_register.py" append <queue> --json items.json`. It assigns `<PREFIX>-R001...`, skips anything already registered (same quote and source), and never renumbers. Re-run it on each later message that adds asks. To withdraw a requirement, add a `- withdrawn: <reason>` line under it; never delete or reuse an id.
   **Sentence sweep.** After appending, re-read the source message sentence by sentence and name the id that holds each sentence's ask. A sentence with no id is either pure context (say so) or an ask you missed; append it.
5. **Cover.** Every requirement gets a brief in `ready/` (via `add_task.py --ready --goal ... --criteria ... --context ...`, all required with `--ready`) with `requirements: [ids]` in the frontmatter and concrete `- [ ]` criteria under `## Success criteria`, each checkable by a command, a test or a visible result. Group closely related requirements into one brief; one brief may cover several ids, but every id must appear in at least one.
6. **Prove it.** `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/requirements_check.py" <queue>` must exit 0 before you start building. Exit 1 lists the uncovered ids, unknown ids, criteria-less briefs and unticked `done/` briefs.
7. **Link the ledger.** Session ledger items for the project carry the brief id in their metadata; a brief moves to `done/` only with every criterion ticked.

## Red flags

| Thought | Reality |
|---|---|
| "The brief summarises it, no need to register each ask" | The register is the exhaustive layer; briefs group it. Register first. |
| "This ask is obvious, skip it" | Obvious asks are the ones that get dropped. Register it. |
| "Reword the quote so it reads cleanly" | Verbatim is the audit trail. Shorten the span, never rewrite it. |
| "Renumber to close the gap" | Ids are permanent; gaps are fine. |
| "It is only 4 asks but multi-repo" | Below 3 or without spread, it is a ledger item. Apply the threshold as written. |

## Triggers

The `require-requirements-extract` gate in the katherine-gates mod injects a reminder to run this skill when a prompt looks like a brain dump and no register exists, and to append when a later prompt adds asks to a project that has one. A Stop gate notices when neither happened.

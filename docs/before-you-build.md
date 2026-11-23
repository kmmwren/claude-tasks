## 2026-11-23 every brief declares its dependencies

1. **Outcome check.** `check_deps.py --root <queue>` exits 0 on every queue; it reports `undeclared` for a ready, in-progress or parked brief whose frontmatter has no `blocked_by:` key, no non-empty `blockers:` and no `depends:` tag. A `blocked_by: []` on disk always means someone or something judged the brief to wait on nothing.
2. **True constraints.** Every task carries an explicit dependency declaration in structured fields, never only in prose (decisions/2026-11-23-dependencies-always-set.md, in claude-config). The key's presence is the declaration, so no script may write `blocked_by: []` as a side effect. Briefs written before the rule keep working: legacy `blockers:` and `depends:` tags count.
3. **Check first.** `check_deps` `undeclared` and `prose_dependency` (a brief whose body describes a wait while `blocked_by` is empty) are the checks; both run daily across every queue.
4. **Already exists.** `deps.py` (`Brief`, `parse_list`), `transition.py` (`blocked_by` on moves to blocked, the completion cascade), `triage_enrich.Fill`. Each is extended, not paralleled.
5. **Cheapest experiment.** Run `check_deps` over the real queues before and after the backfill and compare counts.
6. **Waiting.** None; claude-tasks lands by direct push to main.
7. **Interface.** `add_task.py --blocked-by` / `--no-deps`, `transition.py --no-deps`, and the `blocked_by` field in the triage fill schema. Additive flags; existing callers that already declare are unaffected.
8. **Cost.** Triage adds one array field to an existing batched `claude -p` call; nothing else calls a model.
9. **Risk.** Low: gates refuse a move or a filing, never rewrite a brief's content. Full autonomy.
10. **Capture.** The `undeclared` finding, the transition and add_task refusals, and the daily check that alerts Claude.

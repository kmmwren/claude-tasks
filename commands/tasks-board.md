---
description: Regenerate the read-only HTML board from the briefs and open it
---

Regenerate the task board and open it.

The board is a *view* — it is rebuilt from the briefs every time, never hand-edited.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/build_board.py"
```

This reads every brief in the queue and writes `view/board.html`: a self-contained,
dependency-free, Kanban-style board (columns = lifecycle states, project briefs shown as
collapsible epics with their sub-tasks, per-brief success-criteria progress bars,
search + autonomy filters).

The script resolves the queue via `$CLAUDE_TASKS_DIR` → nearest `.tasks/` → `~/tasks`.
If it reports "No task queue", the user needs to run `/tasks-init` first.

After it runs, tell the user the path to `view/board.html` and offer to open it
(`open` on macOS, `xdg-open` on Linux).

A queue that holds a `project.toml` also renders as a per-project overview (outcome, health,
milestones, rollout, structure, sources): `python scripts/project_view.py <queue-dir> > page.html`.

---
description: Print the prompt that starts a session on a project, milestone or tag slice; alone, the menu of slices
argument-hint: "[PROJECT|all] [--milestone m1,m2] [--tag ux,memory]"
---

Print a session-start prompt for a slice of the queues, or the menu of slices on offer.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/start_prompt.py" $ARGUMENTS
```

The queues are the current one plus each initialised queue in `$CLAUDE_TASKS_ROOTS` (separated
like PATH). `PROJECT` is a queue's folder name or a short form of it (a word, its initials or a
prefix); `all` or a `--tag` alone takes every queue. `--milestone` takes milestone ids or their
short forms (`m2` for `web-m2`) and needs one project. Several values in one flag take any of
them; `--milestone` and `--tag` together take briefs matching both.

Show the output in one fenced block so it can be copied into a new session as it stands. With
no arguments the output is the menu: relay it and offer the command for the slice the user
seems to want. Pass on any `warning:` line from stderr, such as a clone behind origin, since the
ranking it prints is only as current as that clone.

---
name: rollup
description: "Use when someone above the engineer wants a roll-up of project progress: a product manager or CTO asks how projects are going, what shipped, what is next, what is at risk, or says \"roll-up\", \"portfolio status\", \"project health across everything\". Works in Claude Code (runs the generator over local queues) and in Claude Desktop or claude.ai chat (reads a published roll-up from GitHub)."
---

# rollup

A roll-up answers "how are my projects going?" for a reader who does not work in the queues.
It is generated from project queues (a queue with a `project.toml`) and has one level per reader:

- **pm**: per project, the owner's health and ask, milestones grouped Now, Next and Done, and
  what shipped in the last 14 days.
- **cto**: one table of every project: health, milestones done of total, next due, missed.

Only titles, states and dates, the project's outcome, the owner's own health and ask, and the text
of the automatic checks appear. Brief bodies and blocker text never do, so check that those fields
are safe for the audience before sharing a roll-up.

## Pick the mode

- **Claude Code, or the Code tab in Claude Desktop, with the plugin installed**: generate it (below).
  These run commands on the user's machine, so `serve` works here too.
- **Desktop Chat tab or claude.ai chat**: read a published copy (further below). The skill loads
  there, but chat cannot run the plugin's scripts on the user's machine, so never try `rollup.py` in chat.

Ask the user which level they want only if they have not said; "how are my projects" is pm,
"across everything" or "portfolio" is cto.

## Claude Code: generate

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" markdown --level pm
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" json --level cto
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" html --level pm
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" sources
```

Show the Markdown as it comes out; do not summarise it into a different shape. If a project is
listed under "Not shown", or a source is reported there as having no projects, say why in one line (the reason is printed) so the owner can fix it.
When health says "Not set by owner", the colour shown is the automatic floor; say so. A "Source
warnings" section means a source is behind its upstream (no fetch is made); tell the user to pull
before relying on the status. Warnings on stderr (a skipped config row, a milestone left out) are
worth repeating in one line.

### Setting up sources

Sources are directories that contain project queues, searched four levels deep; symlinked
folders are not followed and a queue reached twice counts once. The first match wins, in this order:

1. `--source LABEL=PATH`, repeated for several.
2. The file named by `--config PATH` or `$CLAUDE_TASKS_ROLLUP_CONFIG`; naming a file that does not
   exist or cannot be read is an error. A `[[source]]` row without a label and a path is skipped
   with a warning.
3. `~/.config/claude-tasks/rollup.toml`, when it exists (absent is silent); if it is malformed or has
   no usable rows, a warning is printed and the current repository is used instead. A row whose label
   repeats an earlier one is skipped with a warning.
4. The current repository, labelled "this repository": the queue in use when it is not a `.tasks`
   folder, otherwise the folder holding the `.tasks` queue. A home folder is never scanned by default.

A config file lists the sources, each with a unique label:

```toml
[[source]]
label = "Work"
path = "~/code/team-notes"

[[source]]
label = "Home"
path = "~/projects"
```

One run can override them: `--source Work=~/code/team-notes --source Home=~/projects`; a `--config`
given with `--source` is ignored, with a warning.
Run `rollup.py sources` to check which projects each source finds.

## Desktop or chat: read a published copy

`rollup.py write --out <dir>` writes `README.md` (what the files are and how someone else gets their own
view), `pm.md`, `pm.json`, `cto.md` and `cto.json`. `--out` belongs to `write` alone. It only
writes into a new or empty folder or one that already holds a roll-up (a `pm.json` with this
schema), and leaves a file alone when only its generated-on line would change. Teams commit that
folder to a GitHub repository so anyone can read it.

1. Ask once for the repository, branch and folder holding the roll-up, unless the user has
   already said.
2. Read `<folder>/<level>.md` and show it. Use `<level>.json` when the user wants to compute
   something (counts, filters, comparisons); a pm milestone carries `waiting`, the number of its
   briefs still waiting on an unmet blocker. If the file is not already in the conversation, ask
   the user to attach it with **+ > Add from GitHub** (a private repository needs the Claude GitHub
   App). A file kept in a Project is a snapshot: ask them to press **Sync now** first.
3. Mention the generated date at the top of the file, so the reader knows how fresh it is.

Never invent project status. If the files cannot be read, say what failed and stop.

## Sharing a roll-up

Three ways, simplest first:

1. **GitHub**: commit the `write` folder; GitHub renders `pm.md` and `cto.md` as pages, and the
   JSON is a stable API (`schema: claude-tasks.rollup/v1`) for anyone's own tool or page.
2. **Ask Claude**: anyone who adds this plugin uses this skill, in Claude Code, Desktop or claude.ai
   chat; in chat they attach the published files from GitHub.
3. **Local browser**: `rollup.py serve [--port N]` serves `/pm`, `/cto` and their `.json` on
   `http://localhost:8788/` (default port 8788), rebuilt on every request. It answers only to the
   host names `localhost` and `127.0.0.1`. Each source's behind-upstream count is reused for 30 seconds. `rollup.py html` prints the same page once, as HTML.

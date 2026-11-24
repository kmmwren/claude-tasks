---
name: rollup
description: "Use when someone wants a roll-up of project progress at work, project or portfolio level: how projects are going, what shipped, what is next, what is at risk, what to work on, or says \"roll-up\", \"portfolio status\", \"project health across everything\". Works in Claude Code (runs the generator over local queues) and in Claude Desktop or claude.ai chat (reads a published roll-up from GitHub)."
---

# rollup

A roll-up answers "how are my projects going?" at work, project or portfolio level.
It is generated from project queues (a queue with a `project.toml`) as they stand on each
repository's GitHub default branch, and has one view per level of detail. Every view opens each project
with one "Source: <label> @ <commit>, <date>" line, and a gap is written out ("No open bugs.",
"Nothing shipped in the last 14 days.", "No date") rather than left blank:

- **work**: per project, three lists. Ready briefs, most urgent first (numbered), each with its
  id, title, due date, estimate and the blockers it waits on; open bugs (numbered from 1); and the
  requirements no brief covers.
- **project**: three layers. Projects: one tile per project, red first, then amber, then stale or unset,
  then green, with slip breaking ties; a tile carries the health and who set it, the outcome, the
  milestone counts, the next due date, the ask, the latest shipped brief and the latest build.
  Milestones: a table per project of milestone, status, due, slip, briefs, evidence and flags.
  Detail: collapsed per project, with the automatic checks, milestones by stage, what shipped by
  release, and the backlog by milestone.
- **portfolio**: projects grouped by initiative, each with one sentence of status (health, milestones
  done, how many are past their date, what is next), its health and one line per milestone.

A milestone row in `project.toml` with `parked = true` (and optional `parked_on` and `parked_reason`)
shows as Parked: it has its own count on the tile, the portfolio sentence and the json, is listed
last in the detail, and is never late, missed or next due; a parked milestone whose briefs are all
done shows as Done.

Only these fields appear: each project's slug, title, initiative, source label, commit and `outcome`;
the owner's health colour, reason and date (never the owner's name); the project's `ask` and its
deadline; the text of the automatic checks; milestone ids, titles, due dates, slip, progress, counts,
evidence and feature-flag state per environment; brief ids, titles, types, due dates, estimates and
the ids or names of what each brief waits on; shipped brief ids, titles, notes, dates and releases;
the ids and quoted words of requirements no brief covers; the project's links and latest build; and
the "Not shown" and "Source warnings" lines. Brief bodies never appear, so check that the `outcome`,
the health reason, the ask, the automatic-check text, the names of blockers, the shipped notes and the
requirement words are safe for the audience before sharing a roll-up.

## Pick the mode

- **Claude Code, or the Code tab in Claude Desktop, with the plugin installed**: generate it (below).
  The Desktop Code tab runs `json`, `markdown`, `html`, `sources`, `write` and `serve` on the user's
  machine like the terminal does.
- **Desktop Chat tab or claude.ai chat**: read the published files (further below), attached with
  **+ > Add from GitHub**. Chat cannot run the plugin's scripts on the user's machine, so never try
  `rollup.py` in chat. This split is current as of 2026-11-19; recheck it after 2026-11-30.

Ask the user which view they want only if they have not said; "how are my projects" is project,
"across everything" or "portfolio" is portfolio, and "what should we work on" or "what is broken"
is work.

## Claude Code: generate

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" markdown --level project
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" markdown --level work
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" json --level portfolio
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" html --level project
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rollup.py" sources
```

`--level` takes `work`, `project` or `portfolio` and defaults to `project`.

Show the Markdown as it comes out; do not summarise it into a different shape. If a project is
listed under "Not shown", or a source is reported there as having no projects, say why in one line (the reason is printed) so the owner can fix it.
When health says "Not set by owner", the colour shown is the automatic floor; say so. A "Source
warnings" section means a source could not be reached, so its last cached copy is shown, as at the
time printed; tell the user the status may be out of date. Warnings on stderr (a skipped config row,
a milestone left out) are worth repeating in one line.

### Setting up sources

A source is one project: a repository and the folder in it that holds the queue. The roll-up reads
that queue as the repository's default branch holds it on GitHub, through a local cache, never from a
working copy; work that is not pushed does not appear. Offline, the newest cached copy is shown with
a "Source warnings" line; with no cached copy the source is listed under "Not shown". The first
match wins, in this order:

1. `--source LABEL=REPO[#QUEUE]`, repeated for several. `REPO` is a URL or a path git can read (a
   clone counts as its origin); `QUEUE` is the folder in it, `.tasks` when omitted.
2. The file named by `--config PATH` or `$CLAUDE_TASKS_ROLLUP_CONFIG`; naming a file that does not
   exist or cannot be read is an error. A `[[source]]` row without a label and a repo is skipped
   with a warning.
3. `~/.config/claude-tasks/rollup.toml`, when it exists (absent is silent); if it is malformed or has
   no usable rows, a warning is printed and the current repository is used instead. A row whose label
   repeats an earlier one is skipped with a warning.
4. The current repository, labelled "this repository": the repository holding the `.tasks` queue in
   use, or the queue itself when it is not a `.tasks` folder. A home folder is never used by default.

A config file lists the sources, each with a unique label:

```toml
[[source]]
label = "Work"
repo = "https://github.com/example/team-notes.git"

[[source]]
label = "Home"
repo = "~/projects/garden"
queue = "docs/tasks"
```

One run can override them: `--source Work=https://github.com/example/team-notes.git
--source Home=~/projects/garden#docs/tasks`; a `--config` given with `--source` is ignored, with a
warning. Run `rollup.py sources` to check what each source gives.

## What every project records

Each project names an `initiative` in `project.toml`, and each milestone in flight or queued (not done or parked) has a due
date. `rollup.py gaps` lists every project that falls short, each with the command or line that fixes
it, and exits 1 while anything is missing, so a scheduled job or CI step can fail on it. Every view's
JSON carries the same list as `gaps`. A new rule is one more `Expectation` subclass in
`rollup_gaps.EXPECTATIONS`.

## Desktop or chat: read a published copy

`rollup.py write --out <dir>` writes `README.md` (what the files are and how someone else gets their own
view), then `work.md`, `project.md`, `portfolio.md` and a `.json` beside each. `--out` belongs to `write` alone. It only
writes into a new or empty folder or one that already holds a roll-up (a view's `.json` with this
schema), and leaves a file alone when only its generated-on line would change. Teams commit that
folder to a GitHub repository so anyone can read it.

1. Ask once for the repository, branch and folder holding the roll-up, unless the user has
   already said.
2. Read `<folder>/<view>.md` and show it. Use `<view>.json` when the user wants to compute
   something (counts, filters, comparisons); a project milestone carries `waiting`, the number of its
   briefs still waiting on an unmet blocker, and the work view carries `ready`, `bugs` and
   `uncovered_requirements`. If the file is not already in the conversation, ask
   the user to attach it with **+ > Add from GitHub** (a private repository needs the Claude GitHub
   App). A file kept in a Project is a snapshot: ask them to press **Sync now** first.
3. Mention the generated date at the top of the file, so the reader knows how fresh it is.

Never invent project status. If the files cannot be read, say what failed and stop.

## Sharing a roll-up

Three ways, simplest first:

1. **GitHub**: commit the `write` folder; GitHub renders `work.md`, `project.md` and `portfolio.md` as
   pages, and the JSON is a stable API (`schema: claude-tasks.rollup/v1`) for anyone's own tool or
   page. Keys keep their names and meaning within the version; new keys may be added.
2. **Ask Claude**: anyone who adds this plugin uses this skill, in Claude Code, Desktop or claude.ai
   chat; in chat they attach the published files from GitHub.
3. **Local browser**: `rollup.py serve [--port N]` serves `/work`, `/project`, `/portfolio` and their `.json` on
   `http://localhost:8788/` (default port 8788), rebuilt on every request from each source's default
   branch. It answers only to the host names `localhost` and `127.0.0.1`. `rollup.py html` prints the
   same page once, as HTML.

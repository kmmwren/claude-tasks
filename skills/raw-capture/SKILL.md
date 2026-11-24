---
name: raw-capture
description: Zero-friction capture of a raw idea into the queue's inbox/. Use when the user dumps a thought mid-flow and you want to capture it without structural overhead — no domain, no importance, no enrichment needed. Just get it in the queue. For a fully-structured ready-to-execute brief, use queue-task instead.
---

# raw-capture

One-shot inbox capture with zero friction. The idea lands in `inbox/` with a timestamp
slug and raw text. A future `enrich` session turns it into a proper brief.

## When to use

- The user says "capture this", "add this to inbox", "remember this", or dumps a raw
  thought mid-flow.
- Claude discovers a loose end during other work and wants to park it without
  interrupting the current task.
- Speed > completeness: the idea is worth keeping but structuring it now would break flow.

## How to capture

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/raw_capture.py" "<title>" [--body "<raw text>"]
```

This writes `inbox/YYYY-MM-DD-HHMM-<slug>.md` with minimal frontmatter (`raw: true`,
`status: inbox`) and the title + body exactly as given. The script finds the queue via
`$CLAUDE_TASKS_DIR` → nearest `.tasks/` → `~/tasks` (or pass `--root <path>`).

## Rules

- **No enrichment at capture time.** Don't ask about domain, importance, autonomy, effort.
- **No dependency declaration at capture time.** A raw capture has no `blocked_by:` line, so
  it stays in inbox/: promotion to ready/ refuses it until `enrich` (or the inbox triage)
  sets `blocked_by`. Keep any wait the user mentions ("after #942 merges") in the raw text;
  enrich turns it into a `blocked_by` entry.
- **Title only required.** Body is optional if the title says enough.
- **Preserve the raw text exactly** — it's the input for enrichment; don't clean it up.
- **Check for near-duplicates** with a quick grep before writing — don't capture what's
  already there.
- For a fully-structured, immediately-executable brief instead, use the `queue-task` skill.

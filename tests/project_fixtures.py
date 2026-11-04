"""Queue fixture shared by the project view tests (no pytest import, so any interpreter can use it)."""
import pathlib

BRIEF = """---
id: {id}
title: {title}
status: {status}
type: feature
domain: {domain}
updated: {updated}
---

# {title}

## Success criteria
{criteria}
"""

PROJECT = """
[project]
slug = "demo"
title = "Demo Project"
outcome = "Customers can do the demo thing."

[health]
colour = "{colour}"
reason = "Going to plan."
set_by = "Owner"
set_on = "2026-10-22"

[[milestones]]
id = "m1"
title = "First"
plain = "The first thing works"
briefs = ["b1"]
{due}

[[milestones]]
id = "m2"
title = "Second"
plain = "The second thing works"
briefs = ["b2"]

[[prs]]
repo = "Panaseer/demo"
number = 7
workstream = "w"

[[workstreams]]
id = "w"
title = "Work"
domain = "demo"

[[sessions]]
id = "abc12345"
name = "Demo session"
"""


def make_queue(root: pathlib.Path, colour="green", due="", updated="2026-10-22") -> pathlib.Path:
    q = root / ".tasks"
    for d in ("inbox", "ready", "in-progress", "done", "parked"):
        (q / d).mkdir(parents=True)
    (q / "tasks.toml").write_text('name = "Demo"\n')
    (q / "done" / "b1.md").write_text(BRIEF.format(
        id="b1", title="One", status="done", domain="demo", updated=updated, criteria="- [x] works"))
    (q / "in-progress" / "b2.md").write_text(BRIEF.format(
        id="b2", title="Two", status="in-progress", domain="demo", updated=updated,
        criteria="- [x] a\n- [ ] b"))
    (q / "project.toml").write_text(PROJECT.format(colour=colour, due=due))
    return q


RICH = """
[project]
slug = "rich"
title = "Rich Project"
outcome = "Everything is exercised."

[health]
colour = "amber"
reason = "Owner set this a while ago."
set_by = "Owner"
set_on = "2026-09-01"

[[milestones]]
id = "m1"
title = "Only"
briefs = ["b-done", "b-open"]

[[workstreams]]
id = "w"
title = "Work"
summary = "All of it."
briefs = ["b-active", "b-queued", "b-parked", "b-unverified"]

[[workstreams]]
id = "empty"
title = "Nothing mapped"

[[prs]]
repo = "Panaseer/demo"
number = 1
workstream = "w"

[[prs]]
repo = "Panaseer/demo"
number = 2
workstream = "w"
label = "Fallback title"

[rollout]
[[rollout.stages]]
id = "built"
label = "Built"
[[rollout.stages]]
id = "live"
[[rollout.targets]]
id = "env"
label = "Env"
url = "https://example.test/env"
[[rollout.targets.evidence]]
stage = "built"
kind = "brief_done"
brief = "b-done"
url = "https://example.test/e"
[[rollout.targets.evidence]]
stage = "live"
kind = "pr_merged"
repo = "Panaseer/demo"
number = 1
[[rollout.targets]]
id = "other"
[[rollout.targets.evidence]]
stage = "built"
kind = "pr_merged"
repo = "Panaseer/demo"
number = 2
[[rollout.targets.evidence]]
stage = "live"
kind = "claimed"
note = "owner says so"
[[rollout.targets]]
id = "third"
[[rollout.targets.evidence]]
stage = "built"
kind = "configcat"
flag = "some_flag"
[[rollout.targets.evidence]]
stage = "live"
kind = "brief_done"
brief = "missing"

[[architecture]]
id = "sys"
title = "System"
briefs = ["b-done"]
[[architecture.links]]
label = "Doc"
url = "https://example.test/doc"
[[architecture]]
id = "box"
parent = "sys"
title = "Container"
[[architecture]]
id = "part"
parent = "box"
title = "Component"

[[decisions]]
file = "2026-10-01-pick-a-thing.md"
[[decisions]]
file = "0001-adr.md"
[[decisions]]
question = "Inline question?"
date = "2026-10-05"
status = "open"
why = "Because."
url = "https://example.test/d"
[[decisions.options]]
label = "A"
effect = "does a"

[ask]
kind = "question"
question = "Pick one?"
recommendation = "A"
deadline = "2026-11-01"
[[ask.options]]
label = "A"
effect = "does a"

[[jira]]
key = "MM-1"
title = "A ticket"
url = "https://example.test/MM-1"

[[sessions]]
id = "abc12345"
name = "Seen"
role = "owner"
[[sessions]]
id = "zzz99999"
name = "Unseen"
role = "helper"

[[links]]
kind = "env"
label = "Env"
url = "https://example.test/env"
[[links]]
kind = "doc"
label = "Doc"
url = "https://example.test/doc"
[[links]]
kind = "repo"
label = "Repo"
url = "https://example.test/repo"
"""

REGISTER = """# Requirements register: Rich

<!-- requirements-register v1; prefix: RCH; ids are permanent, never renumber. -->

| ID | Class | Words | Briefs |
|---|---|---|---|
| RCH-R001 | capability | "Covered and done" | b-done |
| RCH-R002 | capability | "Covered but open" | b-active |
| RCH-R003 | capability | "Nobody covers this" |  |
"""


def make_rich(root: pathlib.Path) -> pathlib.Path:
    q = root / ".tasks"
    for d in ("inbox", "ready", "in-progress", "done", "parked"):
        (q / d).mkdir(parents=True)
    (q / "tasks.toml").write_text('name = "Rich"\n')
    spec = [("b-done", "done", "done", "- [x] ok"), ("b-unverified", "done", "done", "- [x] a\n- [ ] b"),
            ("b-open", "ready", "ready", "- [ ] later"), ("b-active", "in-progress", "in-progress", "- [x] a\n- [ ] b"),
            ("b-queued", "ready", "ready", "- [ ] q"), ("b-parked", "parked", "parked", "- [ ] p")]
    for bid, folder, status, crit in spec:
        text = BRIEF.format(id=bid, title=bid.upper(), status=status, domain="demo", updated="2026-10-22",
                            criteria=crit.replace("\\\n", "\n"))
        if bid in ("b-done", "b-active"):
            text = text.replace("domain: demo", "domain: demo\nrequirements: [RCH-R00%d]" % (1 if bid == "b-done" else 2))
        (q / folder / f"{bid}.md").write_text(text)
    (q / "project.toml").write_text(RICH)
    (q / "requirements.md").write_text(REGISTER)
    adr = root / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-adr.md").write_text("# 1. Use an ADR\n\n| Date | 2026-09-30 |\n| Status | Proposed |\n\n### Option B, the thing (chosen)\n")
    dec = root / "decisions"
    dec.mkdir()
    (dec / "2026-10-01-pick-a-thing.md").write_text("# Pick a thing\n\n**Decision:**\n- Take the first one\n")
    return q

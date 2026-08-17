#!/usr/bin/env python3
"""
build_board.py — render the briefs as a self-contained, read-only HTML board.

Stdlib only (runs anywhere). Reads inbox/ ready/ in-progress/ done/ parked/, parses
frontmatter + Goal + Success criteria, writes view/board.html. The board is a *view*:
the briefs are the source of truth, so it is regenerated, never hand-edited.

Usage: python scripts/build_board.py
"""
import datetime
import html
import pathlib
import re
from typing import Any

from _concurrency import atomic_write
from _paths import require_queue, resolve_root, strip_comment
from config import load_config

DIRS = ["inbox", "ready", "in-progress", "done", "parked"]
COL_TITLE = {"inbox": "Inbox (raw)", "ready": "Ready", "in-progress": "In progress",
             "done": "Done", "parked": "Parked"}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    body = m.group(2)
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = strip_comment(v)
    return fm, body


def section(body: str, name: str) -> str:
    m = re.search(rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)", body, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def load_briefs(root: pathlib.Path) -> list[dict[str, Any]]:
    briefs: list[dict[str, Any]] = []
    for d in DIRS:
        for f in sorted((root / d).glob("*.md")):
            raw = f.read_text()
            fm, body = parse_frontmatter(raw)
            if not fm.get("id"):
                if d == "inbox":
                    # raw capture: synthesise minimal fields, preview the body
                    fm = {"id": f.stem, "title": fm.get("title", f.stem.replace("-", " ")),
                          "type": "todo", "autonomy": "needs-input", "importance": "3"}
                else:
                    continue
            goal = section(body, "Goal")
            if not goal and d == "inbox":
                goal = body.strip()[:240]
            crit = section(body, "Success criteria")
            done = len(re.findall(r"^\s*-\s*\[[xX]\]", crit, re.MULTILINE))
            total = len(re.findall(r"^\s*-\s*\[[ xX]\]", crit, re.MULTILINE))
            briefs.append({
                "dir": d, "id": fm["id"], "title": fm.get("title", fm["id"]),
                "type": fm.get("type", "todo"), "importance": fm.get("importance", "3"),
                "autonomy": fm.get("autonomy", "needs-input"), "effort": fm.get("estimated-effort", ""),
                "domain": fm.get("domain", ""), "parent": fm.get("parent", "") or "",
                "assignee": fm.get("assignee", ""),
                "goal": goal if goal != "# TODO" else "", "crit": crit,
                "crit_done": done, "crit_total": total,
            })
    return briefs


AUTONOMY = {
    "full": ("auto", "Claude can do this end-to-end", "ok"),
    "needs-input": ("needs you", "Needs human input", "warn"),
    "blocked": ("blocked", "Blocked", "muted"),
}


def search_text(b: dict[str, Any]) -> str:
    """Lowercased haystack for the board's client-side filter, skipping empty fields."""
    parts = (b["title"], b["domain"], b["goal"], b["assignee"])
    return " ".join(p for p in parts if p).lower()


def card_html(b: dict[str, Any], child_of_epic: bool = False) -> str:
    pid = html.escape(b["id"])
    p = b["importance"] if b["importance"] in {"1", "2", "3", "4"} else "3"
    a_label, a_tip, a_cls = AUTONOMY.get(b["autonomy"], AUTONOMY["needs-input"])
    crit = b["crit"]
    # render success criteria as a checklist
    crit_html = ""
    for line in crit.splitlines():
        mm = re.match(r"^\s*-\s*\[([ xX])\]\s*(.*)$", line)
        if mm:
            checked = mm.group(1).lower() == "x"
            crit_html += f'<li class="{"done" if checked else ""}">{"✓" if checked else "○"} {html.escape(mm.group(2))}</li>'
    if not crit_html and crit and crit != "# TODO":
        crit_html = f"<li>{html.escape(crit[:200])}</li>"
    prog = ""
    if b["crit_total"]:
        pct = round(100 * b["crit_done"] / b["crit_total"])
        prog = f'<div class="prog"><div class="bar" style="width:{pct}%"></div></div><span class="pct">{b["crit_done"]}/{b["crit_total"]}</span>'
    goal = f'<p class="goal">{html.escape(b["goal"])}</p>' if b["goal"] else ""
    body = ""
    if goal or crit_html:
        body = f'<div class="body">{goal}{("<ul class=crit>"+crit_html+"</ul>") if crit_html else ""}</div>'
    effort = f'<span class="chip eff">{html.escape(b["effort"])}</span>' if b["effort"].strip() else ""
    domain = f'<span class="chip dom">{html.escape(b["domain"])}</span>' if b["domain"] else ""
    who = f'<span class="chip who">{html.escape(b["assignee"])}</span>' if b["assignee"] else ""
    return f'''<div class="card {('child' if child_of_epic else '')}" data-autonomy="{b['autonomy']}" data-text="{html.escape(search_text(b))}">
  <div class="head" onclick="this.parentNode.classList.toggle('open')">
    <span class="pri p{p}">P{p}</span>
    <span class="title">{html.escape(b['title'])}</span>
    <span class="auto {a_cls}" title="{a_tip}">{a_label}</span>
  </div>
  <div class="meta"><span class="chip id">{pid}</span>{who}{domain}{effort}{prog}</div>
  {body}
</div>'''


def build(root: pathlib.Path, project: str = "tasks") -> None:
    briefs = load_briefs(root)
    cols = ""
    for d in DIRS:
        col = [b for b in briefs if b["dir"] == d]
        epics = [b for b in col if b["type"] == "project"]
        epic_ids = {e["id"] for e in epics}
        loose = [b for b in col if not b["parent"] and b["type"] != "project"]
        cards = ""
        for e in epics:
            children = [c for c in col if c["parent"] == e["id"]]
            kids = "".join(card_html(c, True) for c in children)
            cards += f'<div class="epic">{card_html(e)}<div class="kids">{kids or "<span class=empty>no sub-tasks in this column</span>"}</div></div>'
        for b in loose:
            cards += card_html(b)
        # children whose parent epic lives in a different column (shown here, not nested)
        orphan_children = [b for b in col if b["parent"] and b["parent"] not in epic_ids and b["type"] != "project"]
        for b in orphan_children:
            cards += card_html(b)
        cols += f'<section class="col"><h2>{COL_TITLE[d]} <span class="n">{len(col)}</span></h2>{cards or "<p class=empty>nothing here</p>"}</section>'

    n_auto = sum(1 for b in briefs if b["autonomy"] == "full" and b["dir"] in {"ready", "in-progress"})
    n_needs = sum(1 for b in briefs if b["autonomy"] == "needs-input" and b["dir"] in {"ready", "in-progress"})
    stamp = datetime.date.today().isoformat()
    out_dir = root / "view"
    out_dir.mkdir(exist_ok=True)
    atomic_write(out_dir / "board.html", PAGE.format(
        cols=cols, stamp=stamp, total=len(briefs), n_auto=n_auto, n_needs=n_needs,
        project=html.escape(project)))
    print(f"Wrote {out_dir/'board.html'} — {len(briefs)} briefs, {n_auto} autonomy:full open, {n_needs} need you")


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{project} — work queue</title>
<style>
:root{{--bg:#0f1115;--panel:#171a21;--card:#1e222b;--ink:#e8eaed;--mut:#9aa3b0;--line:#2a2f3a;--ok:#3fb950;--warn:#d29922;--muted:#6e7681;--accent:#7aa2f7}}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}}
header{{position:sticky;top:0;z-index:5;background:linear-gradient(180deg,#0f1115,#0f1115ee);backdrop-filter:blur(6px);padding:18px 24px 12px;border-bottom:1px solid var(--line)}}
header h1{{margin:0;font-size:18px;font-weight:650;letter-spacing:.2px}}
header .sub{{color:var(--mut);font-size:12.5px;margin-top:3px}}
.controls{{display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap}}
.controls input{{background:var(--card);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:7px 11px;font-size:13px;min-width:220px}}
.btn{{background:var(--card);border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:7px 11px;font-size:12.5px;cursor:pointer;user-select:none}}
.btn.on{{color:var(--ink);border-color:var(--accent);background:#1f2740}}
.board{{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;padding:18px 24px 60px;align-items:start}}
@media(max-width:1300px){{.board{{grid-template-columns:repeat(3,1fr)}}}}
@media(max-width:1100px){{.board{{grid-template-columns:1fr 1fr}}}}
@media(max-width:680px){{.board{{grid-template-columns:1fr}}}}
.col h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:0 0 10px;display:flex;gap:8px;align-items:center}}
.col h2 .n{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:1px 8px;font-size:11px;color:var(--ink)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:10px 12px;margin-bottom:10px;transition:border-color .15s}}
.card:hover{{border-color:#3a4256}}
.card.child{{margin-bottom:7px}}
.epic{{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:9px;margin-bottom:12px}}
.epic > .card{{background:#222838;border-color:#313a52}}
.kids{{padding:8px 4px 2px 10px;border-left:2px solid #2c3342;margin:6px 0 2px 8px}}
.head{{display:flex;gap:8px;align-items:flex-start;cursor:pointer}}
.title{{flex:1;font-weight:560;font-size:13.5px}}
.pri{{font-size:11px;font-weight:700;border-radius:6px;padding:1px 6px;height:fit-content}}
.p1{{background:#3d1620;color:#ff7b8a}}.p2{{background:#3a2a12;color:#e0a458}}.p3{{background:#1d2c3a;color:#79b8ff}}.p4{{background:#222;color:#8b949e}}
.auto{{font-size:10.5px;font-weight:650;border-radius:20px;padding:2px 9px;white-space:nowrap;height:fit-content}}
.auto.ok{{background:#10331b;color:var(--ok)}}.auto.warn{{background:#33280c;color:var(--warn)}}.auto.muted{{background:#23262d;color:var(--muted)}}
.meta{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:7px}}
.chip{{font-size:10.5px;color:var(--mut);background:#20242d;border:1px solid var(--line);border-radius:6px;padding:1px 7px}}
.chip.id{{font-family:ui-monospace,Menlo,monospace;color:#7d8694}}
.chip.who{{color:var(--accent);border-color:#33406b;background:#1b2138}}
.body{{display:none;margin-top:9px;padding-top:9px;border-top:1px dashed var(--line)}}
.card.open .body{{display:block}}
.goal{{margin:0 0 8px;color:#c9d1d9;font-size:12.5px}}
ul.crit{{margin:0;padding-left:2px;list-style:none}}
ul.crit li{{font-size:12px;color:var(--mut);padding:1px 0}}
ul.crit li.done{{color:var(--ok)}}
.prog{{flex:1;min-width:50px;max-width:90px;height:5px;background:#20242d;border-radius:6px;overflow:hidden}}
.prog .bar{{height:100%;background:var(--ok)}}
.pct{{font-size:10.5px;color:var(--mut)}}
.empty{{color:var(--muted);font-size:12px;font-style:italic}}
.up{{color:var(--accent)!important}}
footer{{color:var(--muted);font-size:11.5px;padding:0 24px 30px;text-align:center}}
.hide{{display:none!important}}
</style></head><body>
<header>
  <h1>{project} <span style="color:var(--mut);font-weight:400;font-size:13px">· {total} briefs</span></h1>
  <div class="sub">Read-only view of the task queue. Source of truth is the git briefs, not this page. Generated {stamp}.</div>
  <div class="controls">
    <input id="q" placeholder="Search title, domain, goal, assignee…" oninput="filter()">
    <span class="btn" id="fAuto" onclick="tog('fAuto');filter()">⚡ Claude can do ({n_auto})</span>
    <span class="btn" id="fNeed" onclick="tog('fNeed');filter()">🙋 Needs you ({n_needs})</span>
    <span class="btn" onclick="document.querySelectorAll('.card').forEach(c=>c.classList.add('open'))">Expand all</span>
    <span class="btn" onclick="document.querySelectorAll('.card').forEach(c=>c.classList.remove('open'))">Collapse all</span>
  </div>
</header>
<div class="board">{cols}</div>
<footer>Pull-based: nothing here nags you. Ask Claude "what can you handle without me?" · Regenerate with <code>python scripts/build_board.py</code></footer>
<script>
let S={{fAuto:false,fNeed:false}};
function tog(id){{S[id]=!S[id];document.getElementById(id).classList.toggle('on',S[id]);}}
function filter(){{
  let q=document.getElementById('q').value.toLowerCase().trim();
  document.querySelectorAll('.card').forEach(c=>{{
    let t=c.dataset.text||'', a=c.dataset.autonomy;
    let ok=(!q||t.includes(q));
    if(S.fAuto&&a!=='full')ok=false;
    if(S.fNeed&&a!=='needs-input')ok=false;
    c.classList.toggle('hide',!ok);
  }});
  document.querySelectorAll('.epic').forEach(e=>{{
    let vis=[...e.querySelectorAll('.card')].some(c=>!c.classList.contains('hide'));
    e.classList.toggle('hide',!vis);
  }});
}}
</script></body></html>"""


def main(argv: list[str] | None = None) -> None:
    root = require_queue(resolve_root())
    cfg = load_config(root)
    build(root, str(cfg["name"]))


if __name__ == "__main__":
    main()  # pragma: no cover

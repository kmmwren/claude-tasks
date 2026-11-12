#!/usr/bin/env python3
"""
project_view.py - one project page, drawn from a queue and its `project.toml`.

Extends build_board: the briefs come from `build_board.load_briefs`, the requirements from the
register beside the queue, and everything the queue cannot hold (workstreams, rollout targets,
architecture nodes, links, Jira epics, sessions, owner health) from `<queue>/project.toml`.

The page is for an executive: health, one progress picture, at most one ask, then tabbed detail
(plan with rollout, how it works, decisions, links). Counts, progress and the health floor are
computed on every build and never stored.

This module is pure: network and clock inputs arrive as `Signals`, so one function renders both
the live page and the published copy.

Usage: python scripts/project_view.py [queue-dir] > page.html
"""
import datetime as dt
import hashlib
import html
import pathlib
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import build_board
from _paths import is_queue
from requirements_check import check as check_queue
from requirements_register import parse_register, register_path

GREEN, AMBER, RED = "green", "amber", "red"
RANK = {GREEN: 0, AMBER: 1, RED: 2}
LABEL = {GREEN: "Green", AMBER: "Amber", RED: "Red"}
MEANING = {
    GREEN: "On track for the stated outcome. Nothing is needed from the reader.",
    AMBER: "At risk. The owner is acting and a slip is possible, but no decision is needed yet.",
    RED: "Off track. The outcome will be missed unless a decision or help arrives.",
}
STALE_DAYS = 7
OWNER_STALE_DAYS = 14
BASELINE_DAYS = 7
CI_RED_FLOOR = AMBER
STALE_FLOOR = AMBER
MISSED_FLOOR = RED


class ProjectConfigError(ValueError):
    """project.toml is missing or breaks the schema; the message names the field."""


@dataclass(frozen=True)
class PrState:
    key: str  # owner/repo#number
    state: str  # OPEN | MERGED | CLOSED | UNKNOWN
    ci: str = "unknown"  # green | red | pending | unknown
    updated: dt.datetime | None = None
    url: str = ""
    title: str = ""


@dataclass
class Signals:
    """Everything the clock and the network contribute, injected so tests can force each."""

    now: dt.datetime
    prs: dict[str, PrState] = field(default_factory=dict)
    sessions: dict[str, dt.datetime] = field(default_factory=dict)
    queue_activity: dt.datetime | None = None


# --- loading -----------------------------------------------------------------------------


def load_project(queue: pathlib.Path) -> dict[str, Any]:
    path = queue / "project.toml"
    if not path.is_file():
        raise ProjectConfigError(f"{path} does not exist")
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    for table in ("project", "health"):
        if not isinstance(cfg.get(table), dict):
            raise ProjectConfigError(f"[{table}] is required")
    for key in ("title", "outcome"):
        if not cfg["project"].get(key):
            raise ProjectConfigError(f"[project] {key} is required")
    if cfg["health"].get("colour") not in RANK:
        raise ProjectConfigError("[health] colour must be green, amber or red")
    if not cfg["health"].get("reason"):
        raise ProjectConfigError("[health] reason is required: one line saying why")
    if "ask" in cfg and not isinstance(cfg["ask"], dict):
        raise ProjectConfigError("[ask] is a single table: a page carries at most one ask")
    return cfg


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return dt.date.fromisoformat(value.strip())
    return None


def _utc(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc)


# --- derived values ----------------------------------------------------------------------


def brief_state(b: dict[str, Any]) -> str:
    """done only counts with every criterion ticked; a done/ brief with gaps is `unverified`."""
    if b["dir"] == "done":
        return "done" if b["crit_done"] == b["crit_total"] else "unverified"
    return {"in-progress": "active", "parked": "parked"}.get(b["dir"], "queued")


def _select(briefs: list[dict[str, Any]], ids: Iterable[str], domain: str = "") -> list[dict[str, Any]]:
    wanted = set(ids)
    return [b for b in briefs if b["id"] in wanted or (domain and b["domain"] == domain)]


def _rollup(chosen: list[dict[str, Any]]) -> dict[str, Any]:
    ticked = sum(b["crit_done"] for b in chosen)
    total = sum(b["crit_total"] for b in chosen)
    states = [brief_state(b) for b in chosen]
    if not chosen:
        status = "unmapped"
    elif all(s == "done" for s in states):
        status = "done"
    elif ticked or any(s in ("active", "unverified") for s in states):
        status = "active"
    else:
        status = "not-started"
    return {"ticked": ticked, "total": total, "status": status,
            "pct": round(100 * ticked / total) if total else (100 if status == "done" else 0)}


def _titles(chosen: list[dict[str, Any]], states: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"id": b["id"], "title": b["title"]} for b in chosen if brief_state(b) in states]


def _criteria(b: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for line in b["crit"].splitlines():
        m = re.match(r"^\s*[-*]\s*\[([ xX])\]\s*(.*)$", line)
        if m:
            out.append({"done": m[1].lower() == "x", "text": m[2]})
    return out


def _evidence_cell(rules: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
                   prs: dict[str, PrState]) -> dict[str, Any]:
    """Verified only when every rule is machine-checked true; claimed and unchecked say so."""
    verdicts: list[str] = []
    notes: list[str] = []
    url = ""
    for rule in rules:
        url = url or str(rule.get("url", ""))
        kind = rule.get("kind")
        if kind == "brief_done":
            b = by_id.get(rule.get("brief", ""))
            ok = bool(b) and brief_state(b) == "done"  # type: ignore[arg-type]
            verdicts.append("verified" if ok else "pending")
            notes.append(f"brief {rule.get('brief')}")
        elif kind == "pr_merged":
            key = f"{rule.get('repo')}#{rule.get('number')}"
            pr = prs.get(key)
            if pr is None or pr.state == "UNKNOWN":
                verdicts.append("unchecked")
            else:
                verdicts.append("verified" if pr.state == "MERGED" else "pending")
            notes.append(f"PR {key}")
        elif kind == "claimed":
            verdicts.append("claimed")
            notes.append(str(rule.get("note", "owner's word")))
        else:
            verdicts.append("unchecked")
            notes.append(str(rule.get("note") or rule.get("flag") or "not machine-checked"))
    if not verdicts:
        state = "pending"
    elif all(v == "verified" for v in verdicts):
        state = "verified"
    elif "pending" in verdicts:
        state = "pending"
    elif "unchecked" in verdicts:
        state = "unchecked"
    else:
        state = "claimed"
    return {"state": state, "note": "; ".join(notes), "url": url}


def _rollout(cfg: dict[str, Any], by_id: dict[str, dict[str, Any]], prs: dict[str, PrState]) -> dict[str, Any]:
    spec = cfg.get("rollout") or {}
    stages = [{"id": s["id"], "label": s.get("label", s["id"])} for s in spec.get("stages", [])]
    targets = []
    for t in spec.get("targets", []):
        cells = {}
        for stage in stages:
            rules = [e for e in t.get("evidence", []) if e.get("stage") == stage["id"]]
            cells[stage["id"]] = _evidence_cell(rules, by_id, prs)
        targets.append({"id": t["id"], "label": t.get("label", t["id"]), "url": t.get("url", ""),
                        "cells": cells})
    return {"stages": stages, "targets": targets}


def _arch_status(node: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    chosen = [by_id[i] for i in node.get("briefs", []) if i in by_id]
    return _rollup(chosen)["status"] if chosen else "unmapped"


def _architecture(cfg: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = {n["id"]: dict(n, children=[], status=_arch_status(n, by_id)) for n in cfg.get("architecture", [])}
    roots: list[dict[str, Any]] = []
    for n in nodes.values():
        parent = nodes.get(n.get("parent", ""))
        (parent["children"] if parent else roots).append(n)
    return roots


def _requirements(queue: pathlib.Path, briefs: list[dict[str, Any]]) -> dict[str, Any]:
    path = register_path(queue)
    if not path.is_file():
        return {"items": [], "counts": {}, "problems": []}
    by_id = {b["id"]: b for b in briefs}
    covering: dict[str, list[str]] = {}
    for b in briefs:
        for r in b["requirements"]:
            covering.setdefault(r, []).append(b["id"])
    items = []
    for r in parse_register(path.read_text()):
        if r.withdrawn:
            continue
        ids = covering.get(r.id, [])
        states = [brief_state(by_id[i]) for i in ids]
        state = "uncovered" if not ids else "done" if all(s == "done" for s in states) else "active"
        items.append({"id": r.id, "words": r.quote or r.title, "class": r.kind, "state": state,
                      "briefs": [{"id": i, "title": by_id[i]["title"], "state": brief_state(by_id[i]),
                                  "criteria": _criteria(by_id[i])} for i in ids]})
    counts: dict[str, int] = {}
    for it in items:
        state = str(it["state"])
        counts[state] = counts.get(state, 0) + 1
    return {"items": items, "counts": counts, "problems": check_queue(queue) if items else []}


_DECISION_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")


def _read_decision(path: pathlib.Path) -> dict[str, str]:
    text = path.read_text()
    title = re.search(r"^#\s+(?:\d+\.\s+)?(.+)$", text, re.MULTILINE)
    day = (re.search(r"\|\s*Date\s*\|\s*(\d{4}-\d{2}-\d{2})", text)
           or re.search(r"\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})", text)
           or _DECISION_DATE.match(path.name))
    status = re.search(r"\|\s*Status\s*\|\s*([A-Za-z]+)", text)
    chosen = (re.search(r"\*\*Decision:\*\*\s*\n?\s*(?:-\s*)?(.+)", text)
              or re.search(r"^###\s+Option [^\n]*?,?\s*(.+?)\s*\(chosen\)", text, re.MULTILINE))
    word = (status[1].upper() if status else "ACCEPTED")
    rec = (re.search(r"^###\s+Option [^\n]*?,\s*(.+?)\s*\((?:proposed|recommended)\)", text, re.MULTILINE | re.I)
           or re.search(r"^\*{0,2}Recommend(?:ed|ation)\b[^:\n]*:\*{0,2}\s*(.+)", text, re.MULTILINE))
    return {"recommended": rec[1].strip()[:300] if rec else "",
            "question": title[1].strip() if title else path.stem,
            "date": day[1] if day else "",
            "chosen": chosen[1].strip()[:300] if chosen else "",
            "status": {"PROPOSED": "open", "SUPERSEDED": "superseded"}.get(word, "decided")}


def _decisions(cfg: dict[str, Any], dirs: list[pathlib.Path]) -> list[dict[str, Any]]:
    out = []
    for d in cfg.get("decisions", []):
        entry: dict[str, Any] = {"question": d.get("question", ""), "chosen": d.get("chosen", ""),
                                 "why": d.get("why", ""), "date": str(d.get("date", "")),
                                 "url": d.get("url", ""), "options": d.get("options", [])}
        found = next((p / d["file"] for p in dirs if "file" in d and (p / d["file"]).is_file()), None)
        parsed = _read_decision(found) if found else {}
        for key in ("question", "date", "chosen"):
            entry[key] = entry[key] or parsed.get(key, "")
        entry["recommended"] = d.get("recommended") or parsed.get("recommended", "")
        entry["status"] = d.get("status") or parsed.get("status", "decided")
        out.append(entry)
    return sorted(out, key=lambda e: e["date"], reverse=True)


# --- health ------------------------------------------------------------------------------


def _last_movement(briefs: list[dict[str, Any]], prs: list[PrState], signals: Signals,
                   sessions: list[str]) -> dt.datetime | None:
    stamps = [signals.queue_activity] if signals.queue_activity else []
    stamps += [_utc(d) for b in briefs if (d := _date(b["updated"]))]
    stamps += [p.updated for p in prs if p.updated]
    stamps += [signals.sessions[s] for s in sessions if s in signals.sessions]
    return max(stamps) if stamps else None


def _floor(milestones: list[dict[str, Any]], prs: list[PrState], last: dt.datetime | None,
           signals: Signals) -> list[dict[str, str]]:
    found = []
    failing = sorted(p.key for p in prs if p.state == "OPEN" and p.ci == "red")
    if failing:
        found.append({"signal": "red CI", "floor": CI_RED_FLOOR,
                      "detail": f"{len(failing)} open pull request(s) with failing checks: {', '.join(failing)}"})
    finished = bool(milestones) and all(m["status"] == "done" for m in milestones)
    if last is not None and not finished:
        idle = (signals.now - last).days
        if idle >= STALE_DAYS:
            found.append({"signal": "no movement", "floor": STALE_FLOOR,
                          "detail": f"nothing has moved for {idle} days (limit {STALE_DAYS})"})
    missed = [m for m in milestones if m["missed"]]
    if missed:
        found.append({"signal": "missed milestone date", "floor": MISSED_FLOOR,
                      "detail": "past its date and not done: " + ", ".join(m["id"] for m in missed)})
    return found


def _health(cfg: dict[str, Any], signals_found: list[dict[str, str]], now: dt.datetime) -> dict[str, Any]:
    owner = cfg["health"]
    floor = max((s["floor"] for s in signals_found), key=RANK.__getitem__, default=GREEN)
    effective = max(owner["colour"], floor, key=RANK.__getitem__)
    set_on = _date(owner.get("set_on"))
    age = (now.date() - set_on).days if set_on else None
    return {"owner": owner["colour"], "reason": owner["reason"], "set_by": owner.get("set_by", ""),
            "set_on": set_on.isoformat() if set_on else "", "age_days": age,
            "owner_stale": age is not None and age >= OWNER_STALE_DAYS,
            "floor": floor, "signals": signals_found, "effective": effective,
            "overridden": RANK[floor] > RANK[owner["colour"]],
            "capping": [s for s in signals_found if RANK[s["floor"]] > RANK[owner["colour"]]]}


# --- change since last time --------------------------------------------------------------


def snapshot(model: dict[str, Any]) -> dict[str, Any]:
    return {"at": model["generated"], "health": model["health"]["effective"],
            "milestones_done": sorted(m["id"] for m in model["milestones"] if m["status"] == "done"),
            "briefs_done": sorted(b["id"] for b in model["briefs"] if b["state"] == "done"),
            "criteria": [model["totals"]["ticked"], model["totals"]["total"]],
            "prs_merged": sorted(p["key"] for p in model["prs"] if p["state"] == "MERGED")}


def pick_baseline(snapshots: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any] | None:
    """The oldest snapshot inside the last week; failing that the newest older one."""
    ordered = sorted(snapshots, key=lambda s: s["at"])
    cutoff = (now - dt.timedelta(days=BASELINE_DAYS)).isoformat()
    recent = [s for s in ordered if s["at"] >= cutoff]
    return recent[0] if recent else (ordered[-1] if ordered else None)


def delta(model: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    if baseline is None:
        return {"since": "", "lines": [], "note": "First report: there is no earlier snapshot to compare with."}
    now = snapshot(model)
    lines = []
    plain = {m["id"]: m["plain"] for m in model["milestones"]}
    for m in sorted(set(now["milestones_done"]) - set(baseline["milestones_done"])):
        lines.append(f"Milestone reached: {plain.get(m, m)}")
    gained = now["criteria"][0] - baseline["criteria"][0]
    if gained > 0:
        lines.append(f"{gained} more acceptance criteria met ({now['criteria'][0]} of {now['criteria'][1]})")
    elif gained < 0:
        lines.append(f"{-gained} acceptance criteria no longer met ({now['criteria'][0]} of {now['criteria'][1]})")
    for key in sorted(set(now["prs_merged"]) - set(baseline["prs_merged"])):
        lines.append(f"Merged: {key}")
    if now["health"] != baseline["health"]:
        lines.append(f"Health moved from {baseline['health']} to {now['health']}")
    since = baseline["at"][:10]
    return {"since": since, "lines": lines,
            "note": "" if lines else f"Nothing has moved since {since}."}


# --- model -------------------------------------------------------------------------------


def build_model(queue: pathlib.Path, signals: Signals, baseline: dict[str, Any] | None = None,
                decision_dirs: Iterable[pathlib.Path] = ()) -> dict[str, Any]:
    if not is_queue(queue):
        raise ProjectConfigError(f"{queue} is not an initialised queue (no tasks.toml)")
    cfg = load_project(queue)
    raw = build_board.load_briefs(queue)
    briefs = [b for b in raw if b["dir"] != "inbox" or b["requirements"] or b["domain"]]
    by_id = {b["id"]: b for b in briefs}
    today = signals.now.date()

    milestones = []
    for m in cfg.get("milestones", []):
        picked = _select(briefs, m.get("briefs", []))
        roll = _rollup(picked)
        due = _date(m.get("due"))
        milestones.append({"id": m["id"], "title": m.get("title", m["id"]),
                           "plain": m.get("plain") or m.get("title", m["id"]),
                           "due": due.isoformat() if due else "", "briefs": m.get("briefs", []),
                           "criteria": [c for b in picked for c in _criteria(b)],
                           "missed": bool(due and due < today and roll["status"] != "done"), **roll})

    prs = []
    for p in cfg.get("prs", []):
        key = f"{p['repo']}#{p['number']}"
        got = signals.prs.get(key, PrState(key, "UNKNOWN"))
        prs.append({"key": key, "workstream": p.get("workstream", ""), "state": got.state, "ci": got.ci,
                    "url": got.url or f"https://github.com/{p['repo']}/pull/{p['number']}",
                    "title": got.title or p.get("label", ""), "updated": got.updated})
    pr_states = [signals.prs.get(p["key"], PrState(p["key"], "UNKNOWN")) for p in prs]

    workstreams = []
    for w in cfg.get("workstreams", []):
        chosen = _select(briefs, w.get("briefs", []), w.get("domain", ""))
        roll = _rollup(chosen)
        own = [p for p in prs if p["workstream"] == w["id"]]
        workstreams.append({
            "id": w["id"], "title": w.get("title", w["id"]), "summary": w.get("summary", ""),
            "brief_ids": [b["id"] for b in chosen],
            "now": _titles(chosen, ("active", "unverified")), "next": _titles(chosen, ("queued",)),
            "later": _titles(chosen, ("parked",)), "prs": own,
            "ci_red": any(p["state"] == "OPEN" and p["ci"] == "red" for p in own), **roll})

    session_ids = []
    sessions = []
    for s in cfg.get("sessions", []):
        full = next((sid for sid in signals.sessions if sid.startswith(s["id"])), s["id"])
        session_ids.append(full)
        sessions.append({"id": s["id"], "name": s.get("name", ""), "role": s.get("role", ""),
                         "last": signals.sessions.get(full)})

    last = _last_movement(briefs, pr_states, signals, session_ids)
    totals = {"ticked": sum(b["crit_done"] for b in briefs), "total": sum(b["crit_total"] for b in briefs)}
    found = _floor(milestones, pr_states, last, signals)
    model: dict[str, Any] = {
        "slug": cfg["project"].get("slug", queue.parent.name),
        "project": cfg["project"], "generated": signals.now.isoformat(timespec="seconds"),
        "milestones": milestones, "workstreams": workstreams, "prs": prs,
        "rollout": _rollout(cfg, by_id, signals.prs),
        "architecture": _architecture(cfg, by_id),
        "requirements": _requirements(queue, briefs),
        "decisions": _decisions(cfg, [*map(pathlib.Path, decision_dirs), queue.parent / "docs" / "adr"]),
        "briefs": [{"id": b["id"], "title": b["title"], "state": brief_state(b), "dir": b["dir"]} for b in briefs],
        "counts": {d: sum(1 for b in briefs if b["dir"] == d) for d in build_board.DIRS},
        "totals": totals, "jira": cfg.get("jira", []), "sessions": sessions,
        "links": cfg.get("links", []), "ask": cfg.get("ask"),
        "last_movement": last.isoformat(timespec="seconds") if last else "",
        "unreadable_ci": sorted(p["key"] for p in prs if p["state"] == "UNKNOWN"),
        "health": _health(cfg, found, signals.now),
    }
    model["delta"] = delta(model, baseline)
    return model


# --- rendering ---------------------------------------------------------------------------
# One reader in mind: the CEO. Header, one progress picture, two cards, then tabs for the detail.

e = html.escape

STATUS_WORD = {"done": "Done", "active": "In progress", "unverified": "In progress"}
STATUS_CLASS = {"Done": "done", "In progress": "prog", "Not started": "todo"}
HEALTH_WORD = {GREEN: "On track", AMBER: "At risk", RED: "Off track"}
TABS = (("plan", "Plan"), ("how-it-works", "How it works"), ("decisions", "Decisions"), ("links", "Links"))
STAGE_WORDS = {"Enabled on C00 production": "Live on C00", "Enabled for every customer": "Live everywhere"}

_ID_NOTE = re.compile(r"\s*\((?:[A-Z]{2,}-\d+(?:[,;]\s*[A-Z]{2,}-\d+)*)\)")
_TRAIL_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
_JIRA_KEY = re.compile(r"\b[A-Z]{2,}-\d+\b")


def _plain(text: str) -> str:
    """Drop ticket-id asides such as (MM-2177) so the page reads as sentences."""
    return re.sub(r"\s{2,}", " ", _ID_NOTE.sub("", text)).strip()


def _name(title: str) -> str:
    """A node's plain name: the repository aside in brackets moves to the tooltip."""
    return _TRAIL_PAREN.sub("", _plain(title)).strip() or title


def _word(state: str) -> str:
    return STATUS_WORD.get(state, "Not started")


def _chip(state: str) -> str:
    word = _word(state)
    return f'<span class="chip {STATUS_CLASS[word]}">{word}</span>'


def _link(text: str, url: str) -> str:
    return f'<a href="{e(url, quote=True)}" target="_blank" rel="noopener">{e(text)}</a>' if url else e(text)


def _day(iso: str, year: bool = False) -> str:
    try:
        d = dt.date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


def _ago(stamp: dt.datetime | None, now: str) -> str:
    if stamp is None:
        return "no activity found"
    days = (dt.datetime.fromisoformat(now) - stamp).days
    return "today" if days <= 0 else f"{days} day{'s' if days != 1 else ''} ago"


def _days_ago(days: int) -> str:
    return "today" if days <= 0 else f"{days} day{'s' if days != 1 else ''} ago"


def _since_line(m: dict[str, Any]) -> str:
    d = m["delta"]
    if not d["since"]:
        return '<p class="since">First report: there is nothing earlier to compare with.</p>'
    if not d["lines"]:
        return f'<p class="since">No change since {e(_day(d["since"]))}.</p>'
    items = []
    for line in d["lines"]:
        line = re.sub(r"^Milestone reached: ", "Milestone done: ", line)
        line = re.sub(r"^(\d+) more acceptance criteria met.*", r"\1 more checks done", line)
        line = re.sub(r"^(\d+) acceptance criteria no longer met.*", r"\1 checks reopened", line)
        line = re.sub(r"Health moved from (\w+) to (\w+)",
                      lambda g: f"Health moved from {HEALTH_WORD.get(g[1], g[1])} to {HEALTH_WORD.get(g[2], g[2])}", line)
        items.append(line)
    lead = f"Since {_day(d['since'])}: "
    if len(items) == 1:
        return f'<p class="since">{e(lead + items[0])}.</p>'
    return f'<div class="since"><p>{e(lead.strip())}</p><ul>{"".join(f"<li>{e(i)}</li>" for i in items)}</ul></div>'


def _signal_text(s: dict[str, str], m: dict[str, Any]) -> str:
    if s["signal"] == "missed milestone date":
        late = [x["plain"] for x in m["milestones"] if x["missed"]]
        return "milestones past their date: " + "; ".join(late)
    if s["signal"] == "red CI":
        return s["detail"].replace("pull request(s) with failing checks", "pull requests with failing checks")
    return s["detail"]


def _header(m: dict[str, Any]) -> str:
    h, p = m["health"], m["project"]
    eff = h["effective"]
    meta = []
    if h["age_days"] is not None:
        meta.append(f"Updated {_days_ago(h['age_days'])}")
    meta.append(f"set by {h['set_by'] or 'the owner'}")
    if h["owner_stale"]:
        meta.append("not reconfirmed recently")
    note = ""
    if h["overridden"]:
        why = "; ".join(_signal_text(s, m) for s in h["capping"])
        note = (f'<p class="override">Automatic checks lowered this from {e(HEALTH_WORD[h["owner"]])} '
                f'to {e(HEALTH_WORD[eff])}: {e(why)}.</p>')
    elif h["signals"]:
        note = ('<p class="flag">Automatic checks also flagged: '
                + e("; ".join(_signal_text(s, m) for s in h["signals"])) + ".</p>")
    return (f'<header class="top"><h1>{e(p["title"])}</h1><p class="goal">{e(p["outcome"])}</p>'
            f'<div class="health"><span class="pill {eff}">{e(HEALTH_WORD[eff])}</span>'
            f'<div class="reason"><p>{e(h["reason"])}</p><p class="meta">{e(" · ".join(meta))}</p>{note}</div></div>'
            f'{_since_line(m)}</header>')


def _groups(m: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Each milestone belongs to the workstream sharing most of its work; the rest go under one heading."""
    mine: dict[str, list[dict[str, Any]]] = {w["id"]: [] for w in m["workstreams"]}
    rest: list[dict[str, Any]] = []
    for ms in m["milestones"]:
        best, score = None, 0
        for w in m["workstreams"]:
            overlap = len(set(ms["briefs"]) & set(w.get("brief_ids", [])))
            if overlap > score:
                best, score = w, overlap
        (mine[best["id"]] if best else rest).append(ms)
    groups = [(w, mine[w["id"]]) for w in m["workstreams"] if mine[w["id"]]]
    if rest:
        title = "Milestones" if not groups else "Other milestones"
        groups.append(({"id": "other", "title": title, "summary": "", "prs": []}, rest))
    return groups


def _group_state(items: list[dict[str, Any]]) -> str:
    if items and all(x["status"] == "done" for x in items):
        return "done"
    return "active" if any(x["status"] in ("done", "active", "unverified") for x in items) else "not-started"


def _progress(m: dict[str, Any], groups: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> str:
    total = len(m["milestones"])
    done = sum(1 for x in m["milestones"] if x["status"] == "done")
    going = sum(1 for x in m["milestones"] if x["status"] in ("active", "unverified"))
    if not total:
        return '<section class="where"><h2>Where we are</h2><p class="sentence">No milestones are defined yet.</p></section>'
    segs = ""
    for w, items in groups:
        n_done = sum(1 for x in items if x["status"] == "done")
        state = _group_state(items)
        pct = round(100 * n_done / len(items))
        segs += (f'<li class="seg {STATUS_CLASS[_word(state)]}"><div class="segbar"><i style="width:{pct}%"></i></div>'
                 f'<b>{e(w["title"])}</b><span class="count">{n_done} of {len(items)} done</span>'
                 f'<span class="state">{_word(state)}</span></li>')
    sentence = f"{done} of {total} milestones done" + (f", {going} in progress." if going else ".")
    return (f'<section class="where"><h2>Where we are</h2><ol class="track">{segs}</ol>'
            f'<p class="sentence">{e(sentence)}</p></section>')


def _open_decisions(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Open decisions for the tab, with the project's single ask folded in when no entry already carries it."""
    found = [d for d in m["decisions"] if d["status"] == "open"]
    a = m["ask"]
    if a and not any(_plain(d["question"]) == _plain(a.get("question", "")) for d in found):
        found.insert(0, {"question": a.get("question", ""), "date": "", "chosen": "", "why": "",
                         "url": "", "status": "open", "options": a.get("options", []),
                         "recommended": a.get("recommendation", "")})
    return found


def _recommended(d: dict[str, Any], m: dict[str, Any]) -> str:
    a = m["ask"]
    if a and _plain(a.get("question", "")) == _plain(d["question"]) and a.get("recommendation"):
        return str(a["recommendation"])
    if d.get("recommended"):
        return str(d["recommended"])
    pick = next((o for o in d.get("options", []) if "recommended" in o.get("label", "").lower()), None)
    if not pick:
        return ""
    label = re.sub(r"\s*\(recommended\)\.?", "", pick["label"], flags=re.I).rstrip(".")
    return f"{label}. {pick.get('effect', '')}".strip()


def _cards(m: dict[str, Any]) -> str:
    cards = ""
    opens = _open_decisions(m)
    if opens:
        d = opens[0]
        rec = _recommended(d, m)
        extra = f' <span class="sub">and {len(opens) - 1} more</span>' if len(opens) > 1 else ""
        cards += (f'<section class="card ask"><h2>Needs your decision</h2><p class="q">{e(_plain(d["question"]))}</p>'
                  + (f'<p>Recommended: {e(_plain(rec))}</p>' if rec else "")
                  + f'<p><a href="#decisions" data-tab="decisions">See the options on the Decisions tab</a>{extra}</p></section>')
    live = [x for x in m["milestones"] if x["status"] in ("active", "unverified")]
    upcoming = live or [x for x in m["milestones"] if x["status"] != "done"]
    rows = "".join(f'<li><span>{e(x["plain"])}</span></li>' for x in upcoming[:3])
    cards += ('<section class="card next"><h2>Next up</h2>'
              + (f'<ul>{rows}</ul>' if rows else '<p>Every milestone is done.</p>') + '</section>')
    return f'<div class="cards{" one" if not opens else ""}">{cards}</div>'


def _jira_for(ms: dict[str, Any], m: dict[str, Any]) -> list[dict[str, Any]]:
    by_key = {j["key"]: j for j in m["jira"]}
    titles = [b["title"] for b in m["briefs"] if b["id"] in ms["briefs"]]
    text = " ".join([ms["plain"], *titles, *[c["text"] for c in ms.get("criteria", [])]])
    return [by_key[k] for k in dict.fromkeys(_JIRA_KEY.findall(text)) if k in by_key]


def _pr_state_text(p: dict[str, Any]) -> str:
    state = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}.get(p["state"], "")
    ci = {"green": "checks passing", "red": "checks failing", "pending": "checks running"}.get(p["ci"], "")
    return ", ".join(x for x in (state, ci if p["state"] == "OPEN" else "") if x)


def _milestone(ms: dict[str, Any], w: dict[str, Any], m: dict[str, Any]) -> str:
    when = ""
    if ms["due"]:
        when = f' <span class="due{" late" if ms["missed"] else ""}">Due {e(_day(ms["due"], True))}{" (overdue)" if ms["missed"] else ""}</span>'
    crit = ms.get("criteria", [])
    body = ""
    if crit:
        ticked = sum(1 for c in crit if c["done"])
        items = "".join(f'<li class="{"yes" if c["done"] else ""}"><span aria-hidden="true">{"✓" if c["done"] else "○"}</span> {e(_plain(c["text"]))}</li>' for c in crit)
        body += f'<p class="sub">{ticked} of {len(crit)} checks done</p><ul class="checks">{items}</ul>'
    refs = []
    if ms["status"] != "done":
        refs += [f'<li>{_link(p["key"], p["url"])} <span class="sub">{e(_pr_state_text(p))}</span></li>' for p in w.get("prs", [])]
    refs += [f'<li>{_link(j["key"], j.get("url", ""))} <span class="sub">{e(j.get("title", ""))}</span></li>' for j in _jira_for(ms, m)]
    if refs:
        body += f'<ul class="refs">{"".join(refs)}</ul>'
    inner = body or '<p class="sub">No checks written yet.</p>'
    return (f'<li><details class="ms"><summary>{_chip(ms["status"])}'
            f'<span class="t">{e(ms["plain"])}{when}</span></summary><div class="msbody">{inner}</div></details></li>')


def _rollout_table(m: dict[str, Any]) -> str:
    r = m["rollout"]
    if not r["targets"]:
        return ""
    head = "".join(f'<th scope="col">{e(STAGE_WORDS.get(s["label"], s["label"]))}</th>' for s in r["stages"])
    rows = ""
    for t in r["targets"]:
        cells, seen_open = "", False
        for s in r["stages"]:
            c = t["cells"][s["id"]]
            if c["state"] in ("verified", "claimed"):
                cells += '<td class="yes"><span aria-hidden="true">✓</span><span class="sr">Done</span></td>'
            elif not seen_open:
                seen_open = True  # the first unfinished stage is where the work is happening now
                cells += '<td class="prog">In progress</td>'
            else:
                cells += '<td class="todo">Not yet</td>'
        rows += f'<tr><th scope="row">{_link(_plain(t["label"]), t["url"])}</th>{cells}</tr>'
    return (f'<h3>Rollout</h3><div class="scroll"><table class="rollout"><thead><tr><th scope="col"></th>{head}</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def _requirements_list(m: dict[str, Any]) -> str:
    rq = m["requirements"]
    if not rq["items"]:
        return ""
    done = sum(1 for i in rq["items"] if i["state"] == "done")
    items = ""
    for it in rq["items"]:
        state = "not-started" if it["state"] == "uncovered" else it["state"]
        work = "".join(
            f'<li>{e(b["title"])} {_chip(b["state"])}<ul class="checks">'
            + "".join(f'<li class="{"yes" if c["done"] else ""}"><span aria-hidden="true">{"✓" if c["done"] else "○"}</span> {e(_plain(c["text"]))}</li>' for c in b["criteria"])
            + "</ul></li>" for b in it["briefs"]) or '<li class="sub">No work is planned for this yet.</li>'
        items += (f'<li><details class="ms" title="{e(it["id"], quote=True)}"><summary>{_chip(state)}'
                  f'<span class="t">{e(it["words"])}</span></summary><div class="msbody"><ul class="refs">{work}</ul></div></details></li>')
    return (f'<h3>Requirements</h3><details class="ws"><summary><span class="t">All requirements</span>'
            f'<span class="count">{done} of {len(rq["items"])} done</span></summary>'
            f'<ul class="plain">{items}</ul></details>')


def _plan(m: dict[str, Any], groups: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> str:
    out = ""
    for w, items in groups:
        n_done = sum(1 for x in items if x["status"] == "done")
        state = _group_state(items)
        out += (f'<details class="ws"{" open" if state == "active" else ""}><summary><span class="t">{e(w["title"])}</span>'
                f'<span class="count">{n_done} of {len(items)} done</span>{_chip(state)}</summary>'
                + (f'<p class="wsum">{e(_plain(w["summary"]))}</p>' if w["summary"] else "")
                + f'<ul class="plain">{"".join(_milestone(x, w, m) for x in items)}</ul></details>')
    return out or '<p class="sub">No milestones are defined yet.</p>'


def _node(n: dict[str, Any]) -> str:
    kids = "".join(_node(c) for c in n["children"])
    links = " ".join(_link(x.get("label", x.get("url", "")), x.get("url", "")) for x in n.get("links", []))
    chip = _chip(n["status"]) if n["status"] != "unmapped" else ""
    raw = n.get("title", n["id"])
    summary = _plain(n.get("summary", ""))
    return (f'<li><div class="node" title="{e(raw, quote=True)}"><b>{e(_name(raw))}</b> {chip}'
            f'{f"<p>{e(summary)}</p>" if summary else ""}{f"<p class=sub>{links}</p>" if links else ""}</div>'
            f'{f"<ul>{kids}</ul>" if kids else ""}</li>')


def _how(m: dict[str, Any]) -> str:
    tree = "".join(_node(n) for n in m["architecture"])
    return f'<ul class="tree">{tree}</ul>' if tree else '<p class="sub">No architecture has been described yet.</p>'


def _decision(d: dict[str, Any], m: dict[str, Any]) -> str:
    is_open = d["status"] == "open"
    answer = _recommended(d, m) if is_open else (d["chosen"] or d["why"])
    lead = "Recommended: " if is_open and answer else ""
    word = {"open": "Open", "superseded": "Superseded"}.get(d["status"], "Decided")
    more = ""
    if d.get("options"):
        more += "<ul>" + "".join(f"<li><b>{e(_plain(o.get('label', '')))}</b> {e(o.get('effect', ''))}</li>" for o in d["options"]) + "</ul>"
    if d["chosen"] and d["why"]:
        more += f'<p>{e(d["why"])}</p>'
    if d["chosen"]:
        more = f'<p>{e(d["chosen"])}</p>' + more
    if d.get("url"):
        more += f'<p>{_link("Open the full record", d["url"])}</p>'
    detail = f'<details class="full"><summary>Full text</summary>{more}</details>' if more else ""
    date = f'<span class="date">{e(_day(d["date"], True))}</span>' if d["date"] else ""
    return (f'<li class="dec{" open" if is_open else ""}"><div class="dhead">{date}'
            f'<span class="chip {"prog" if is_open else "todo" if word == "Superseded" else "done"}">{word}</span></div>'
            f'<h4>{e(_plain(d["question"]))}</h4>'
            + (f'<p class="answer">{e(lead + _plain(answer))}</p>' if answer else "") + detail + "</li>")


def _decisions_tab(m: dict[str, Any]) -> str:
    opens = _open_decisions(m)
    rest = [d for d in m["decisions"] if d["status"] != "open"]
    rest.sort(key=lambda d: d["date"], reverse=True)
    items = "".join(_decision(d, m) for d in [*opens, *rest])
    return f'<ul class="plain decs">{items}</ul>' if items else '<p class="sub">No decisions are recorded yet.</p>'


def _links(m: dict[str, Any]) -> str:
    cols = []

    def col(title: str, rows: list[str]) -> None:
        if rows:
            cols.append(f'<div><h3>{e(title)}</h3><ul class="plain">{"".join(f"<li>{r}</li>" for r in rows)}</ul></div>')

    col("Pull requests", [f'{_link(p["key"], p["url"])} {e(p["title"])}'
                          + (f' <span class="sub">{e(_pr_state_text(p))}</span>' if _pr_state_text(p) else "") for p in m["prs"]])
    col("Jira", [f'{_link(j["key"], j.get("url", ""))} <span class="sub">{e(j.get("title", ""))}</span>' for j in m["jira"]])
    group: dict[str, list[dict[str, Any]]] = {}
    for x in m["links"]:
        group.setdefault(x.get("kind", "doc"), []).append(x)
    names = {"env": "Live environments", "doc": "Documents"}
    for kind, items in group.items():
        col(names.get(kind, kind.title()), [_link(x.get("label", x.get("url", "")), x.get("url", "")) for x in items])
    col("Claude sessions", [f'{e(s["name"] or "Session")} <span class="sub">{e(s["role"] + "; " if s["role"] else "")}last active {e(_ago(s["last"], m["generated"]))}</span>' for s in m["sessions"]])
    return f'<div class="linkgrid">{"".join(cols)}</div>' if cols else '<p class="sub">No links have been added yet.</p>'


def _rules(m: dict[str, Any]) -> str:
    h = m["health"]
    firing = "".join(f"<li>{e(_signal_text(s, m))}</li>" for s in h["signals"]) or "<li>No automatic check is firing.</li>"
    unread = (f'<p>Check results could not be read for: {e(", ".join(m["unreadable_ci"]))}. That is not counted against the project.</p>'
              if m["unreadable_ci"] else "")
    return (f'<details class="foot"><summary>How health is computed</summary>'
            f'<p>The owner sets the status and gives the reason. The page never shows a status better than its worst automatic check: '
            f'failing checks on an open pull request (At risk), no movement for {STALE_DAYS} or more days (At risk), '
            f'or a milestone past its date and not done (Off track).</p>'
            f'<p>On track: {e(MEANING[GREEN])} At risk: {e(MEANING[AMBER])} Off track: {e(MEANING[RED])}</p>'
            f'<p>Right now:</p><ul>{firing}</ul>{unread}</details>')


CSS = """
/* Layout: one header, one progress track, two cards, then tabbed detail. Accent blue; status colours only for state. */
:root{--bg:#f4f6f9;--panel:#fff;--ink:#17202b;--mut:#566273;--line:#dde2ea;--accent:#1d5e9c;--accent-bg:#e5eef8;
--done:#1b7a46;--done-bg:#dcf1e4;--prog:#1d5e9c;--prog-bg:#e5eef8;--prog-bar:#9fc0e3;--todo:#5f6b7c;--todo-bg:#eceff3;--bad:#a3262a;--bad-bg:#f9e0e1;
--g:#1a7d47;--on-g:#fff;--a:#e0a21a;--on-a:#2a1d00;--r:#b5303a;--on-r:#fff;--warn-bg:#fbefd0;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0f141b;--panel:#171e28;--ink:#e6ebf2;--mut:#9aa6b6;--line:#2a3544;--accent:#8db4e0;--accent-bg:#1d2d41;
--done:#6fd09a;--done-bg:#173326;--prog:#8db4e0;--prog-bg:#1d2d41;--prog-bar:#3a5c85;--todo:#9aa6b6;--todo-bg:#232c39;--bad:#ff8d91;--bad-bg:#40191b;
--g:#3fb877;--on-g:#04180d;--a:#e0a928;--on-a:#2a1d00;--r:#e5565c;--on-r:#2a0507;--warn-bg:#382c10;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f141b;--panel:#171e28;--ink:#e6ebf2;--mut:#9aa6b6;--line:#2a3544;--accent:#8db4e0;--accent-bg:#1d2d41;
--done:#6fd09a;--done-bg:#173326;--prog:#8db4e0;--prog-bg:#1d2d41;--prog-bar:#3a5c85;--todo:#9aa6b6;--todo-bg:#232c39;--bad:#ff8d91;--bad-bg:#40191b;
--g:#3fb877;--on-g:#04180d;--a:#e0a928;--on-a:#2a1d00;--r:#e5565c;--on-r:#2a0507;--warn-bg:#382c10;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5;
padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:8px 56px}
.wrap{max-width:960px;margin:0 auto}
a{color:var(--accent)}a:focus-visible,summary:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
h1{font-size:32px;line-height:1.15;margin:20px 0 6px;text-wrap:balance}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 10px;font-weight:600}
h3{font-size:16px;margin:28px 0 8px}h4{font-size:15px;margin:6px 0 2px}
p{margin:0}.sub,.meta{color:var(--mut);font-size:13px}
.goal{font-size:18px;max-width:62ch;text-wrap:pretty;margin-bottom:18px}
.health{display:flex;gap:14px;align-items:flex-start}
.reason{min-width:0;flex:1}.reason p+p{margin-top:2px}
.pill{flex:none;font-weight:700;font-size:14px;border-radius:999px;padding:4px 14px;white-space:nowrap}
.pill.green{background:var(--g);color:var(--on-g)}.pill.amber{background:var(--a);color:var(--on-a)}.pill.red{background:var(--r);color:var(--on-r)}
.override{margin-top:8px;background:var(--warn-bg);border-radius:6px;padding:6px 10px;font-size:13.5px}
.flag{margin-top:6px;font-size:13px;color:var(--mut)}
.since{margin-top:14px;color:var(--mut);font-size:14px}.since ul{margin:4px 0 0;padding-left:20px}
.where{margin-top:32px}
.track{list-style:none;margin:0;padding:0;display:flex;gap:6px}
.seg{flex:1 1 0;min-width:0;display:flex;flex-direction:column;gap:2px;font-size:13px;line-height:1.35}
.seg b{font-size:13.5px;margin-top:6px;text-wrap:balance}
.segbar{height:8px;border-radius:4px;background:var(--todo-bg);overflow:hidden}.segbar i{display:block;height:100%}
.seg.done .segbar{background:var(--done)}.seg.done .segbar i{background:var(--done)}
.seg.prog .segbar{background:var(--prog-bar)}.seg.prog .segbar i{background:var(--prog)}
.seg.todo .segbar{background:var(--todo-bg)}
.seg .count{color:var(--mut)}.seg .state{font-size:12px;font-weight:600}
.seg.done .state{color:var(--done)}.seg.prog .state{color:var(--prog)}.seg.todo .state{color:var(--todo)}
.sentence{margin-top:12px;font-size:16px;font-weight:600}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:28px}.cards.one{grid-template-columns:1fr}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;min-width:0}
.card.ask{border-color:var(--accent)}.card.ask h2{color:var(--accent)}
.card .q{font-weight:600;font-size:16px;margin-bottom:6px}.card p+p{margin-top:6px}
.card ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:8px}
.card.next li{padding-left:14px;position:relative}.card.next li::before{content:"";position:absolute;left:0;top:.6em;width:6px;height:6px;border-radius:50%;background:var(--prog)}
.tabs{display:flex;gap:4px;margin-top:36px;border-bottom:1px solid var(--line);overflow-x:auto}
.tabs button{font:inherit;font-weight:600;color:var(--mut);background:none;border:0;border-bottom:3px solid transparent;padding:10px 14px;cursor:pointer;white-space:nowrap}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent)}
.tabs button:hover{color:var(--ink)}
[role="tabpanel"]{padding-top:18px;min-width:0}
.chip{display:inline-block;flex:none;font-size:12px;font-weight:600;border-radius:999px;padding:1px 9px;white-space:nowrap;line-height:1.5}
.chip.done{background:var(--done-bg);color:var(--done)}.chip.prog{background:var(--prog-bg);color:var(--prog)}.chip.todo{background:var(--todo-bg);color:var(--todo)}
ul.plain{list-style:none;margin:0;padding:0}
details.ws{border-top:1px solid var(--line)}details.ws:last-of-type{border-bottom:1px solid var(--line)}
summary{cursor:pointer;display:flex;gap:10px;align-items:baseline;list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"";flex:none;width:7px;height:7px;border-right:2px solid var(--mut);border-bottom:2px solid var(--mut);transform:rotate(-45deg);margin-right:4px;align-self:center}
details[open]>summary::before{transform:rotate(45deg)}
details.ws>summary{padding:12px 2px;font-weight:600;font-size:16px}
details.ws>summary .t{flex:1;min-width:0}.count{color:var(--mut);font-size:13px;font-weight:400;white-space:nowrap}
.wsum{color:var(--mut);padding:0 2px 8px 21px;max-width:70ch}
details.ms{margin-left:21px;border-top:1px solid var(--line)}
details.ms>summary{padding:9px 2px;font-size:14.5px;align-items:flex-start}
details.ms>summary::before{margin-top:.6em;align-self:flex-start}
details.ms>summary .t{flex:1;min-width:0}
.due{display:block;color:var(--mut);font-size:12.5px}.due.late{color:var(--bad);font-weight:600}
.msbody{padding:0 2px 12px 21px;font-size:14px}.msbody .sub{margin-bottom:4px}
.checks{list-style:none;margin:0;padding:0}.checks li{display:flex;gap:8px;padding:2px 0;color:var(--mut)}.checks li.yes{color:var(--done)}
.checks li span{flex:none;width:1.1em}
.refs{list-style:none;margin:8px 0 0;padding:0}.refs li{padding:2px 0}
.scroll{overflow-x:auto}
table.rollout{border-collapse:collapse;width:100%;font-size:13.5px;min-width:0}
.rollout th,.rollout td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:break-word}
.rollout thead th{font-size:12px;color:var(--mut);font-weight:600}.rollout tbody th{font-weight:600;width:28%}
.rollout td.yes{color:var(--done);font-weight:700}.rollout td.prog{color:var(--prog);font-weight:600}.rollout td.todo{color:var(--todo)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
ul.tree,ul.tree ul{list-style:none;margin:0;padding:0}ul.tree ul{margin-left:14px;padding-left:16px;border-left:2px solid var(--line)}
ul.tree li{margin-top:12px}.node p{color:var(--mut);font-size:13.5px;max-width:70ch}
.dec{padding:14px 0;border-top:1px solid var(--line)}
.dec.open{background:var(--accent-bg);border:1px solid var(--accent);border-radius:8px;padding:12px 14px;margin-bottom:12px}
.dhead{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--mut)}
.answer{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;max-width:75ch}
details.full{margin-top:6px;font-size:13.5px}details.full summary{color:var(--accent);display:block}details.full summary::before{display:none}
details.full ul{padding-left:20px;margin:6px 0}details.full p{margin-top:6px}
.linkgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px 32px}.linkgrid>*{min-width:0}
.linkgrid h3{margin-top:12px}.linkgrid li{padding:3px 0;overflow-wrap:anywhere}
details.foot{margin-top:48px;border-top:1px solid var(--line);padding-top:12px;font-size:13.5px;color:var(--mut)}
details.foot summary{font-weight:600}details.foot p,details.foot ul{margin-top:8px;max-width:70ch}details.foot ul{padding-left:20px}
footer{margin-top:20px;color:var(--mut);font-size:12.5px}
@media (max-width:700px){.cards{grid-template-columns:1fr}.track{flex-direction:column;gap:12px}
.seg{display:grid;grid-template-columns:1fr auto;column-gap:10px}.seg .segbar{grid-column:1/-1}.seg b{margin-top:2px}
.seg .count{grid-column:2;grid-row:2}.seg .state{grid-column:1;grid-row:3}h1{font-size:26px}.health{flex-direction:column;gap:8px}
details.ws>summary{flex-wrap:wrap}.rollout th,.rollout td{padding:8px 6px;font-size:12.5px}}
@media (prefers-reduced-motion:no-preference){.tabs button{transition:color .15s}}
"""

SCRIPT = """
(function(){
var tabs=[].slice.call(document.querySelectorAll('[role="tab"]')),panels=[].slice.call(document.querySelectorAll('[role="tabpanel"]'));
if(!tabs.length)return;
function show(id,focus){
  if(!tabs.some(function(t){return t.dataset.tab===id}))id='plan';
  tabs.forEach(function(t){var on=t.dataset.tab===id;t.setAttribute('aria-selected',on);t.tabIndex=on?0:-1;if(on&&focus)t.focus()});
  panels.forEach(function(p){p.hidden=p.id!=='panel-'+id});
}
function fromHash(){show((location.hash||'').slice(1))}
tabs.forEach(function(t,i){
  t.addEventListener('click',function(){show(t.dataset.tab);try{history.replaceState(null,'','#'+t.dataset.tab)}catch(x){}});
  t.addEventListener('keydown',function(ev){var d=ev.key==='ArrowRight'?1:ev.key==='ArrowLeft'?-1:0;
    if(d){ev.preventDefault();var n=tabs[(i+d+tabs.length)%tabs.length];show(n.dataset.tab,true);try{history.replaceState(null,'','#'+n.dataset.tab)}catch(x){}}});
});
[].slice.call(document.querySelectorAll('a[data-tab]')).forEach(function(a){
  a.addEventListener('click',function(ev){ev.preventDefault();show(a.dataset.tab);try{history.replaceState(null,'','#'+a.dataset.tab)}catch(x){}
    var t=document.getElementById('tabs');if(t&&t.scrollIntoView)t.scrollIntoView()})});
window.addEventListener('hashchange',fromHash);
fromHash();
})();
"""

NOSCRIPT = ('<noscript><style>.tabs{display:none}[role="tabpanel"][hidden]{display:block!important}'
            '[role="tabpanel"]{border-top:1px solid var(--line);margin-top:24px}</style></noscript>')


def render_fragment(model: dict[str, Any], surface: str = "live", stamp: bool = True) -> str:
    """The page body, shared by both surfaces. `stamp=False` drops the clock so a hash only moves on content."""
    groups = _groups(model)
    when = "Generated " + model["generated"].replace("T", " ")[:16] + " UTC. " if stamp else ""
    where = "Live view, computed when opened." if surface == "live" else "Updated automatically when the project changes."
    buttons = "".join(
        f'<button type="button" role="tab" id="tab-{k}" data-tab="{k}" aria-controls="panel-{k}" '
        f'aria-selected="{"true" if k == "plan" else "false"}">{label}</button>' for k, label in TABS)
    panels = {
        "plan": f'{_plan(model, groups)}{_rollout_table(model)}{_requirements_list(model)}',
        "how-it-works": _how(model), "decisions": _decisions_tab(model), "links": _links(model)}
    body = "".join(
        f'<div role="tabpanel" id="panel-{k}" aria-labelledby="tab-{k}"{"" if k == "plan" else " hidden"}>{panels[k]}</div>'
        for k, _ in TABS)
    return (f'<title>{e(model["project"]["title"])} overview</title><meta name="claude:viewport-fit-sides" content="cover">'
            f'<style>{CSS}</style>{NOSCRIPT}'
            f'<div class="wrap">{_header(model)}{_progress(model, groups)}{_cards(model)}'
            f'<div class="tabs" id="tabs" role="tablist" aria-label="Project detail">{buttons}</div>{body}'
            f'{_rules(model)}<footer>{e(when + where)}</footer></div><script>{SCRIPT}</script>')


def render_document(model: dict[str, Any], surface: str = "live") -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>'
            + render_fragment(model, surface) + "</body></html>")


def content_hash(model: dict[str, Any], surface: str = "published") -> str:
    return hashlib.sha256(render_fragment(model, surface, stamp=False).encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    queue = pathlib.Path(args[0] if args else ".tasks").expanduser()
    signals = Signals(now=dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
    print(render_document(build_model(queue, signals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover

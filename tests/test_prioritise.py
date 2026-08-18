"""Tests for prioritise.py — ranking the ready queue, dependency-aware.

Covers: dependency parsing, the queue index (status + unlock counts), blocked detection
(a dependency only counts as met once it is `done`), the generic score (due urgency,
importance, quick-win, lead time, config tag weights, unlock bonus), the ranked view, and
the CLI.
"""
import datetime

import init_queue
import prioritise

TODAY = datetime.date(2026, 6, 2)


def _brief(bid, *, status="ready", autonomy="full", importance="3", effort="",
           due="", blockers="[]", tags="[]", title="T"):
    return f"""---
id: {bid}
title: {title}
status: {status}
type: todo
importance: {importance}
autonomy: {autonomy}
estimated-effort: {effort}
due: {due}
tags: {tags}
blockers: {blockers}
related: []
---

## Goal

g
"""


def _queue(tmp_path, briefs):
    """briefs: list of (folder, filename, text). Returns an initialised queue root."""
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    for folder, name, text in briefs:
        (root / folder / name).write_text(text)
    return root


# ── dependency parsing ────────────────────────────────────────────────────────

def test_parse_deps_from_blockers_and_depends_tag():
    b = {"blockers": "[task-a, task-b]", "tags": "[depends:task-c, urgent]"}
    assert prioritise.parse_deps(b) == {"task-a", "task-b", "task-c"}


def test_parse_deps_empty():
    assert prioritise.parse_deps({"blockers": "[]", "tags": "[]"}) == set()


def test_parse_deps_missing_fields():
    assert prioritise.parse_deps({}) == set()


# ── blocked detection ─────────────────────────────────────────────────────────

def test_not_blocked_when_dependency_is_done():
    b = {"autonomy": "full", "blockers": "[task-a]", "tags": "[]"}
    blocked, unmet = prioritise.is_blocked(b, done_ids={"task-a"})
    assert blocked is False
    assert unmet == []


def test_blocked_when_dependency_not_done():
    b = {"autonomy": "full", "blockers": "[task-a]", "tags": "[]"}
    blocked, unmet = prioritise.is_blocked(b, done_ids=set())
    assert blocked is True
    assert unmet == ["task-a"]


def test_blocked_when_autonomy_blocked_even_with_no_deps():
    b = {"autonomy": "blocked", "blockers": "[]", "tags": "[]"}
    blocked, unmet = prioritise.is_blocked(b, done_ids=set())
    assert blocked is True


def test_external_free_text_blocker_blocks():
    # an external blocker (not a brief id, never 'done') keeps the brief blocked
    b = {"autonomy": "full", "blockers": "[waiting-on-vendor]", "tags": "[]"}
    blocked, _ = prioritise.is_blocked(b, done_ids=set())
    assert blocked is True


# ── queue index ───────────────────────────────────────────────────────────────

def test_index_queue_status_and_unlock_counts(tmp_path):
    root = _queue(tmp_path, [
        ("ready", "a.md", _brief("a", blockers="[c]")),
        ("ready", "b.md", _brief("b", tags="[depends:c]")),
        ("done", "c.md", _brief("c", status="done")),
    ])
    status_by_id, unlocks = prioritise.index_queue(root)
    assert status_by_id["c"] == "done"
    assert status_by_id["a"] == "ready"
    assert unlocks["c"] == 2  # both a and b depend on c
    assert unlocks.get("a", 0) == 0


# ── score ─────────────────────────────────────────────────────────────────────

def test_score_no_due_importance_and_effort():
    b = {"importance": "1", "estimated-effort": "xs"}
    assert prioritise.score(b, TODAY) == 440  # 0 + (5-1)*100 + 40


def test_score_overdue_bonus():
    b = {"due": "2026-06-01", "importance": "2", "estimated-effort": "xs"}
    assert prioritise.score(b, TODAY) >= 1000


def test_score_due_today_is_lead_urgent():
    b = {"due": "2026-06-02", "importance": "3", "estimated-effort": "xs"}
    # 500 (due<=3) + 200 (imp3) + 40 (xs) + 300 (lead urgent) = 1040
    assert prioritise.score(b, TODAY) == 1040


def test_score_bad_importance_defaults_to_3():
    assert prioritise.score({"importance": "oops"}, TODAY) == prioritise.score({"importance": "3"}, TODAY)


def test_score_bad_due_ignored():
    assert prioritise.score({"due": "not-a-date"}, TODAY) == prioritise.score({}, TODAY)


def test_score_unlock_bonus_adds():
    base = prioritise.score({"importance": "3"}, TODAY)
    boosted = prioritise.score({"importance": "3"}, TODAY, unlocks=2)
    assert boosted == base + 2 * prioritise.UNLOCK_WEIGHT


def test_score_config_tag_weights():
    b = {"importance": "3", "tags": "[frog, misc]"}
    s = prioritise.score(b, TODAY, tag_weights={"frog": 400, "panic": 1500})
    assert s == prioritise.score({"importance": "3"}, TODAY) + 400


# ── lead_time_urgent ──────────────────────────────────────────────────────────

def test_lead_time_urgent_true_when_gap_within_effort():
    assert prioritise.lead_time_urgent({"due": "2026-06-07", "estimated-effort": "l"}, TODAY)


def test_lead_time_urgent_false_with_no_due():
    assert not prioritise.lead_time_urgent({"estimated-effort": "xs"}, TODAY)


def test_lead_time_urgent_false_on_bad_due():
    assert not prioritise.lead_time_urgent({"due": "nope", "estimated-effort": "xs"}, TODAY)


# ── context filter ────────────────────────────────────────────────────────────

def test_context_excludes_requires_local_in_cloud():
    assert prioritise.is_runnable_in_context({"requires-local": "true"}, "cloud") is False


def test_context_allows_everything_with_no_context():
    assert prioritise.is_runnable_in_context({"requires-local": "true"}, "") is True


# ── rank ──────────────────────────────────────────────────────────────────────

def test_rank_orders_by_score_and_flags_actionable(tmp_path):
    root = _queue(tmp_path, [
        ("ready", "high.md", _brief("high", importance="1", due="2026-06-01")),  # overdue
        ("ready", "low.md", _brief("low", importance="4")),
        ("ready", "needs.md", _brief("needs", importance="1", autonomy="needs-input")),
    ])
    ranked = prioritise.rank(root, today=TODAY)
    ids = [b["id"] for b in ranked]
    assert ids[0] == "high"  # overdue + importance 1 ranks first
    by_id = {b["id"]: b for b in ranked}
    assert by_id["high"]["_actionable"] is True
    assert by_id["needs"]["_actionable"] is False  # needs-input is not auto-actionable


def test_rank_marks_blocked_and_unlocks(tmp_path):
    root = _queue(tmp_path, [
        ("ready", "blocker.md", _brief("blocker", importance="3")),
        ("ready", "blocked.md", _brief("blocked", blockers="[blocker]")),
    ])
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["blocked"]["_blocked"] is True
    assert ranked["blocked"]["_actionable"] is False
    assert ranked["blocker"]["_unlocks"] == 1
    # the blocker outranks an equivalent brief because it unblocks one
    assert ranked["blocker"]["_score"] > prioritise.score({"importance": "3"}, TODAY)


def test_rank_unblocks_once_dependency_done(tmp_path):
    root = _queue(tmp_path, [
        ("done", "dep.md", _brief("dep", status="done")),
        ("ready", "ok.md", _brief("ok", blockers="[dep]")),
    ])
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["ok"]["_blocked"] is False
    assert ranked["ok"]["_actionable"] is True


def test_rank_reads_tag_weights_from_config(tmp_path):
    root = _queue(tmp_path, [("ready", "f.md", _brief("f", tags="[frog]"))])
    (root / "tasks.toml").write_text('name = "demo"\n[priority]\ntag_weights = {frog = 400}\n')
    ranked = prioritise.rank(root, today=TODAY)
    assert ranked[0]["_score"] == prioritise.score({}, TODAY) + 400


def test_rank_context_filters(tmp_path):
    root = _queue(tmp_path, [
        ("ready", "local.md", _brief("local")),
    ])
    # mark it requires-local by appending the field
    p = root / "ready" / "local.md"
    p.write_text(p.read_text().replace("related: []", "related: []\nrequires-local: true"))
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY, context="cloud")}
    assert ranked["local"]["_actionable"] is False


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_main_text_output(tmp_path, monkeypatch, capsys):
    root = _queue(tmp_path, [("ready", "a.md", _brief("a", importance="1"))])
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main([])
    assert "a" in capsys.readouterr().out


def test_main_json_and_top(tmp_path, monkeypatch, capsys):
    root = _queue(tmp_path, [
        ("ready", "a.md", _brief("a", importance="1")),
        ("ready", "b.md", _brief("b", importance="4")),
    ])
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main(["--json", "--top", "1"])
    import json
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == "a"


def test_main_actionable_filter_hides_blocked(tmp_path, monkeypatch, capsys):
    root = _queue(tmp_path, [
        ("ready", "blocker.md", _brief("blocker")),
        ("ready", "blocked.md", _brief("blocked", blockers="[blocker]")),
    ])
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main(["--actionable", "--json"])
    import json
    ids = [b["id"] for b in json.loads(capsys.readouterr().out)]
    assert "blocked" not in ids
    assert "blocker" in ids


def test_main_empty_actionable_message(tmp_path, monkeypatch, capsys):
    root = _queue(tmp_path, [("ready", "n.md", _brief("n", autonomy="needs-input"))])
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main(["--actionable"])
    assert "No " in capsys.readouterr().out


def test_main_text_renders_all_flags(tmp_path, monkeypatch, capsys):
    today = datetime.date.today().isoformat()
    root = _queue(tmp_path, [
        ("ready", "blk.md", _brief("blk")),                              # unlocks → 1
        ("ready", "bd.md", _brief("bd", blockers="[blk]")),             # ⛔ blocked
        ("ready", "nd.md", _brief("nd", autonomy="needs-input")),       # 🙋 needs you
        ("ready", "ld.md", _brief("ld", due=today, effort="xs")),       # ⏱ lead time
    ])
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main([])
    out = capsys.readouterr().out
    assert "⛔" in out          # blocked flag
    assert "🙋" in out          # needs-you flag
    assert "⏱" in out          # lead-time flag
    assert "→1" in out          # blk unlocks one brief


def test_fm_strips_an_inline_comment_from_a_populated_value():
    assert prioritise.fm("---\nstatus: ready # ready | done\n---\n")["status"] == "ready"


def test_fm_reads_a_comment_only_value_as_empty():
    assert prioritise.fm("---\nautonomy: # full | blocked\n---\n")["autonomy"] == ""


def test_fm_keeps_a_hash_that_is_part_of_the_value():
    assert prioritise.fm("---\ntitle: fix issue#42\n---\n")["title"] == "fix issue#42"


def test_fm_parses_the_shipped_template_without_comment_pollution():
    import init_queue

    parsed = prioritise.fm(init_queue.QUEUE_TEMPLATE)
    assert parsed["assignee"] == ""
    assert parsed["status"] == "ready"
    assert parsed["importance"] == ""
    assert parsed["autonomy"] == ""

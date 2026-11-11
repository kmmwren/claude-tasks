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


def _brief(
    bid,
    *,
    status="ready",
    autonomy="full",
    importance="3",
    effort="",
    due="",
    blockers="[]",
    tags="[]",
    title="T",
):
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
    root = _queue(
        tmp_path,
        [
            ("ready", "a.md", _brief("a", blockers="[c]")),
            ("ready", "b.md", _brief("b", tags="[depends:c]")),
            ("done", "c.md", _brief("c", status="done")),
        ],
    )
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
    assert prioritise.score({"importance": "oops"}, TODAY) == prioritise.score(
        {"importance": "3"}, TODAY
    )


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
    root = _queue(
        tmp_path,
        [
            ("ready", "high.md", _brief("high", importance="1", due="2026-06-01")),  # overdue
            ("ready", "low.md", _brief("low", importance="4")),
            ("ready", "needs.md", _brief("needs", importance="1", autonomy="needs-input")),
        ],
    )
    ranked = prioritise.rank(root, today=TODAY)
    ids = [b["id"] for b in ranked]
    assert ids[0] == "high"  # overdue + importance 1 ranks first
    by_id = {b["id"]: b for b in ranked}
    assert by_id["high"]["_actionable"] is True
    assert by_id["needs"]["_actionable"] is False  # needs-input is not auto-actionable


def test_rank_marks_blocked_and_unlocks(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("ready", "blocker.md", _brief("blocker", importance="3")),
            ("ready", "blocked.md", _brief("blocked", blockers="[blocker]")),
        ],
    )
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["blocked"]["_blocked"] is True
    assert ranked["blocked"]["_actionable"] is False
    assert ranked["blocker"]["_unlocks"] == 1
    # the blocker outranks an equivalent brief because it unblocks one
    assert ranked["blocker"]["_score"] > prioritise.score({"importance": "3"}, TODAY)


def test_rank_unblocks_once_dependency_done(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("done", "dep.md", _brief("dep", status="done")),
            ("ready", "ok.md", _brief("ok", blockers="[dep]")),
        ],
    )
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert ranked["ok"]["_blocked"] is False
    assert ranked["ok"]["_actionable"] is True


def test_rank_reads_tag_weights_from_config(tmp_path):
    root = _queue(tmp_path, [("ready", "f.md", _brief("f", tags="[frog]"))])
    (root / "tasks.toml").write_text('name = "demo"\n[priority]\ntag_weights = {frog = 400}\n')
    ranked = prioritise.rank(root, today=TODAY)
    assert ranked[0]["_score"] == prioritise.score({}, TODAY) + 400


def test_rank_context_filters(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("ready", "local.md", _brief("local")),
        ],
    )
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
    root = _queue(
        tmp_path,
        [
            ("ready", "a.md", _brief("a", importance="1")),
            ("ready", "b.md", _brief("b", importance="4")),
        ],
    )
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main(["--json", "--top", "1"])
    import json

    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["id"] == "a"


def test_main_actionable_filter_hides_blocked(tmp_path, monkeypatch, capsys):
    root = _queue(
        tmp_path,
        [
            ("ready", "blocker.md", _brief("blocker")),
            ("ready", "blocked.md", _brief("blocked", blockers="[blocker]")),
        ],
    )
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
    root = _queue(
        tmp_path,
        [
            ("ready", "blk.md", _brief("blk")),  # unlocks → 1
            ("ready", "bd.md", _brief("bd", blockers="[blk]")),  # ⛔ blocked
            ("ready", "nd.md", _brief("nd", autonomy="needs-input")),  # 🙋 needs you
            ("ready", "ld.md", _brief("ld", due=today, effort="xs")),  # ⏱ lead time
        ],
    )
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main([])
    out = capsys.readouterr().out
    assert "⛔" in out  # blocked flag
    assert "🙋" in out  # needs-you flag
    assert "⏱" in out  # lead-time flag
    assert "→1" in out  # blk unlocks one brief


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


# ── blocked_by (first-class dependency form) ──────────────────────────────────


def _blocked_by_brief(bid, blocked_by):
    return _brief(bid).replace("related: []", f"blocked_by: {blocked_by}\nrelated: []")


def test_parse_deps_includes_blocked_by_ids_but_not_externals():
    b = {"blocked_by": "[task-a, external:login]"}
    assert prioritise.parse_deps(b) == {"task-a"}


def test_external_blocker_keeps_a_brief_blocked_forever():
    blocked, unmet = prioritise.is_blocked(
        {"autonomy": "full", "blocked_by": "[external:login]"}, done_ids={"external:login", "x"}
    )
    assert blocked is True and unmet == ["external:login"]


def test_blocked_by_dependency_counts_as_unlock_and_is_met_when_done(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("done", "dep.md", _brief("dep", status="done")),
            ("ready", "w.md", _blocked_by_brief("w", "[dep]")),
        ],
    )
    ranked = prioritise.rank(root, today=TODAY)
    assert ranked[0]["_blocked"] is False


def test_blocked_folder_briefs_are_indexed_for_unlock_counts(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("ready", "dep.md", _brief("dep")),
            ("blocked", "w.md", _blocked_by_brief("w", "[dep]")),
        ],
    )
    status, unlocks = prioritise.index_queue(root)
    assert status["w"] == "blocked" and unlocks["dep"] == 1


# ── wsjf: weights ─────────────────────────────────────────────────────────────

W = prioritise.DEFAULT_WEIGHTS


def test_weights_default_to_the_additive_scorer():
    assert W.scorer is prioritise.Scorer.ADDITIVE


def test_weights_merge_a_partial_table_over_the_defaults():
    w = prioritise.Weights.from_priority({"scorer": "wsjf", "importance_values": {"1": 9000}})
    assert w.scorer is prioritise.Scorer.WSJF
    assert w.importance[1] == 9000 and w.importance[3] == 300


def test_importance_value_falls_back_to_level_3_for_bad_or_unknown_levels():
    assert W.importance_value("oops") == W.importance_value("") == W.importance_value("9") == 300
    assert W.importance_value("1") == 2500


def test_size_of_an_unknown_effort_is_the_blank_default():
    assert W.size("huge") == W.size("") == 3 and W.size("xs") == 1


def test_due_value_follows_the_curve_and_ignores_no_date():
    assert [W.due_value(d) for d in (None, -2, 0, 5, 30)] == [0, 1000, 500, 200, 50]
    assert W.due_value(10**6) == 0


# ── wsjf: dates ───────────────────────────────────────────────────────────────


def test_age_reads_created_then_the_id_date_prefix():
    assert prioritise.age_days({"created": "2026-05-23"}, TODAY) == 10
    assert prioritise.age_days({"id": "2026-05-30-thing"}, TODAY) == 3
    assert prioritise.age_days({"id": "no-date"}, TODAY) == 0
    assert prioritise.age_days({"created": "2026-07-01"}, TODAY) == 0


def test_days_until_due_is_none_for_a_bad_date():
    assert prioritise.days_until_due({"due": "soon"}, TODAY) is None


# ── wsjf: factors and score ───────────────────────────────────────────────────


def test_factors_divide_cost_of_delay_by_job_size_and_add_age():
    f = prioritise.factors(
        {"importance": "2", "estimated-effort": "s", "milestone": "m1", "created": "2026-05-31"},
        TODAY,
        leverage=1,
    )
    assert f.cost_of_delay == 1000 + 200 + 150
    assert f.total == round(1350 / 2 + 4, 1)
    assert f.why()["cost_of_delay"] == 1350 and f.why()["size"] == 2


def test_wsjf_keeps_the_pr_tier_order_and_lets_a_bump_jump_it():
    w = prioritise.Weights.from_priority({"tag_weights": {"tier:quick": 100, "tier:smoke": 50}})
    pr = {"importance": "3"}
    quick = prioritise.wsjf_score(
        {**pr, "estimated-effort": "xs", "tags": "[tier:quick]"}, TODAY, weights=w
    )
    smoke = prioritise.wsjf_score(
        {**pr, "estimated-effort": "m", "tags": "[tier:smoke]"}, TODAY, weights=w
    )
    human = prioritise.wsjf_score(
        {**pr, "estimated-effort": "l", "tags": "[tier:human]"}, TODAY, weights=w
    )
    bumped = prioritise.wsjf_score({"importance": "1", "estimated-effort": "l"}, TODAY, weights=w)
    assert quick > smoke > human and bumped > quick


def test_wsjf_spreads_what_additive_ties():
    a = {"importance": "3", "estimated-effort": "s"}
    b = {"importance": "3", "estimated-effort": "s", "created": "2026-05-01"}
    assert prioritise.score(a, TODAY) == prioritise.score(b, TODAY)
    assert prioritise.wsjf_score(a, TODAY) != prioritise.wsjf_score(b, TODAY)


def test_lead_time_counts_as_time_criticality():
    f = prioritise.factors({"due": "2026-06-02", "estimated-effort": "xs"}, TODAY)
    assert f.time_criticality == 500 + 300


# ── wsjf: wait graph ──────────────────────────────────────────────────────────


def _graph(edges):
    return prioritise.WaitGraph({k: tuple(v) for k, v in edges.items()})


def test_depth_is_the_longest_downstream_chain():
    depth = _graph({"a": ["b", "x"], "b": ["c"]}).depth()
    assert (depth("a"), depth("b"), depth("c")) == (2, 1, 0)


def test_depth_survives_a_cycle():
    assert _graph({"a": ["b"], "b": ["a"]}).depth()("a") == 1


def test_inheritance_takes_the_highest_waiter():
    value = _graph({"a": ["b"], "b": ["c"]}).inherited({"a": 10, "b": 5, "c": 90})
    assert value("a") == 90 and value("b") == 90 and value("zz") == 0


def test_graph_counts_only_live_waiters(tmp_path):
    from deps import load_queue

    root = _queue(
        tmp_path,
        [
            ("ready", "dep.md", _brief("dep")),
            ("blocked", "w.md", _blocked_by_brief("w", "[dep]")),
            ("parked", "p.md", _blocked_by_brief("p", "[dep]")),
        ],
    )
    assert prioritise.WaitGraph.build(load_queue(root)).waiters == {"dep": ("w",)}


# ── wsjf: lanes and moves ─────────────────────────────────────────────────────


def test_lane_comes_from_the_cos_field_or_tag():
    assert prioritise.declared_lane({"cos": "expedite"}) is prioritise.Lane.EXPEDITE
    assert prioritise.declared_lane({"tags": "[cos:chore]"}) is prioritise.Lane.CHORE
    assert prioritise.declared_lane({"cos": "whenever"}) is prioritise.Lane.STANDARD


def test_fixed_date_lane_waits_until_the_due_date_is_near():
    near = {"cos": "fixed-date", "due": "2026-06-05"}
    far = {"cos": "fixed-date", "due": "2026-08-01"}
    assert prioritise.lane_of(near, TODAY, W) is prioritise.Lane.FIXED_DATE
    assert prioritise.lane_of(far, TODAY, W) is prioritise.Lane.STANDARD


def test_move_names_what_to_do_with_the_brief():
    M = prioritise.Move
    assert prioritise.move_of({"autonomy": "full"}, True, W) is M.BLOCKED
    assert prioritise.move_of({"autonomy": "needs-input"}, False, W) is M.ASK
    assert prioritise.move_of({"autonomy": "full", "tags": "[tier:human]"}, False, W) is M.DEFANG
    assert prioritise.move_of({"autonomy": "autonomous"}, False, W) is M.DEFANG
    assert prioritise.move_of({"autonomy": "full"}, False, W) is M.EXECUTE


def test_interleave_reserves_a_share_of_slots_for_chores():
    main, chores = list("abcdefgh"), ["x", "y", "z"]
    assert prioritise._interleave(main, chores, 0.2) == list("abcdxefghy") + ["z"]
    assert prioritise._interleave(main, chores, 0) == main + chores


def test_expedite_over_the_cap_drops_to_standard():
    E, S = prioritise.Lane.EXPEDITE, prioritise.Lane.STANDARD
    items = [
        {"id": i, "_score_wsjf": s, "_lane": E, "_actionable": True}
        for i, s in (("a", 3), ("b", 2), ("c", 1))
    ]
    items.append({"id": "d", "_score_wsjf": 9, "_lane": S, "_actionable": True})
    w = prioritise.Weights.from_priority({"expedite_cap": 2})
    assert [b["id"] for b in prioritise.order_lanes(items, w)] == ["a", "b", "d", "c"]


def test_a_blocked_expedite_brief_holds_no_expedite_slot():
    E = prioritise.Lane.EXPEDITE
    items = [
        {"id": i, "_score_wsjf": s, "_lane": E, "_actionable": ok}
        for i, s, ok in (("x", 9, False), ("y", 8, False), ("a", 3, True), ("b", 2, True))
    ]
    w = prioritise.Weights.from_priority({"expedite_cap": 2})
    assert [b["id"] for b in prioritise.order_lanes(items, w)][:2] == ["a", "b"]


def test_a_zero_job_size_is_a_bad_setting(tmp_path, capsys):
    root = _queue(tmp_path, [("ready", "a.md", _brief("a"))])
    (root / "tasks.toml").write_text('name = "demo"\n[priority]\njob_size = { "" = 0 }\n')
    assert prioritise.Weights.for_queue(root).size("") == 3
    assert prioritise.rank(root, today=TODAY)[0]["id"] == "a"
    assert "job_size values must be positive" in capsys.readouterr().err


# ── wsjf: rank ────────────────────────────────────────────────────────────────


def test_rank_emits_both_scores_and_ranks_and_orders_by_the_chosen_scorer(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("ready", "big.md", _brief("big", importance="2", effort="xl")),
            ("ready", "quick.md", _brief("quick", importance="3", effort="xs")),
        ],
    )
    additive = prioritise.rank(root, today=TODAY)
    wsjf = prioritise.rank(root, today=TODAY, scorer=prioritise.Scorer.WSJF)
    assert [b["id"] for b in additive] == ["big", "quick"]
    assert [b["id"] for b in wsjf] == ["quick", "big"]
    assert wsjf[0]["_rank_wsjf"] == 1 and wsjf[0]["_rank_additive"] == 2
    assert wsjf[0]["_score"] == wsjf[0]["_score_wsjf"]


def test_config_scorer_key_switches_the_default(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("ready", "big.md", _brief("big", importance="2", effort="xl")),
            ("ready", "quick.md", _brief("quick", importance="3", effort="xs")),
        ],
    )
    (root / "tasks.toml").write_text('name = "demo"\n[priority]\nscorer = "wsjf"\n')
    assert prioritise.rank(root, today=TODAY)[0]["id"] == "quick"


def test_a_blocker_inherits_the_score_of_what_waits_on_it(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("ready", "chore.md", _brief("chore", importance="4", effort="l")),
            ("ready", "other.md", _brief("other", importance="3", effort="m")),
            (
                "blocked",
                "vip.md",
                _blocked_by_brief("vip", "[chore]").replace("importance: 3", "importance: 1"),
            ),
        ],
    )
    ranked = prioritise.rank(root, today=TODAY, scorer=prioritise.Scorer.WSJF)
    chore = next(b for b in ranked if b["id"] == "chore")
    assert ranked[0]["id"] == "chore" and chore["_why"]["inherited"] > 0
    assert chore["_leverage"] == 1


# ── CLI: scorer ───────────────────────────────────────────────────────────────


def test_main_scorer_flag_and_json_fields(tmp_path, monkeypatch, capsys):
    import json

    root = _queue(
        tmp_path,
        [
            ("ready", "a.md", _brief("a", importance="1")),
            ("ready", "b.md", _blocked_by_brief("b", "[a]")),
        ],
    )
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main(["--json", "--scorer", "wsjf"])
    row = json.loads(capsys.readouterr().out)[0]
    assert {
        "score_additive",
        "score_wsjf",
        "rank_additive",
        "rank_wsjf",
        "why",
        "lane",
        "move",
        "leverage",
        "schema_version",
    } <= row.keys()
    assert row["score"] == row["score_wsjf"]


def test_main_text_shows_age_and_inheritance(tmp_path, monkeypatch, capsys):
    root = _queue(
        tmp_path,
        [
            ("ready", "old.md", _brief("2020-01-01-old", importance="4")),
            (
                "blocked",
                "vip.md",
                _blocked_by_brief("vip", "[2020-01-01-old]").replace(
                    "importance: 3", "importance: 1"
                ),
            ),
        ],
    )
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    prioritise.main(["--scorer", "wsjf"])
    out = capsys.readouterr().out
    assert "+age" in out and "+inh" in out


def test_a_bad_priority_setting_warns_and_keeps_tag_weights(tmp_path, capsys):
    root = _queue(tmp_path, [("ready", "a.md", _brief("a"))])
    (root / "tasks.toml").write_text(
        'name = "demo"\n[priority]\nscorer = "wsfj"\ntag_weights = { urgent = 7 }\n'
    )
    w = prioritise.Weights.for_queue(root)
    assert w.scorer is prioritise.Scorer.ADDITIVE and w.tags == {"urgent": 7}
    assert "ignoring [priority] settings" in capsys.readouterr().err


# ── ids ───────────────────────────────────────────────────────────────────────


def _no_id(text):
    return "\n".join(line for line in text.splitlines() if not line.startswith("id:")) + "\n"


def test_a_brief_with_no_id_field_takes_its_file_stem_as_id(tmp_path, monkeypatch, capsys):
    root = _queue(tmp_path, [("ready", "stemmed-brief.md", _no_id(_brief("x", importance="1")))])
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    assert [b["id"] for b in prioritise.rank(root, today=TODAY)] == ["stemmed-brief"]
    prioritise.main(["--json"])
    import json

    assert [row["id"] for row in json.loads(capsys.readouterr().out)] == ["stemmed-brief"]


def test_an_empty_id_field_also_falls_back_to_the_file_stem(tmp_path):
    root = _queue(tmp_path, [("ready", "blank.md", _brief("x").replace("id: x", "id:"))])
    assert [b["id"] for b in prioritise.rank(root, today=TODAY)] == ["blank"]


def test_an_id_less_brief_is_a_dependency_target_under_its_stem(tmp_path):
    root = _queue(
        tmp_path,
        [
            ("ready", "stemmed.md", _no_id(_brief("x"))),
            ("ready", "waiter.md", _brief("waiter", blockers="[stemmed]")),
        ],
    )
    ranked = {b["id"]: b for b in prioritise.rank(root, today=TODAY)}
    assert (ranked["stemmed"]["_unlocks"], ranked["stemmed"]["_leverage"]) == (1, 1)

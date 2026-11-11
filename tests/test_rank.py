"""Tests for rank.py: scopes, filter-first candidates, judged order, cache, fallback, pick logging."""
import json

import pytest
import rank
import taste
from judge import ClaudeBackend
from picks import PickLog
from queue_helpers import make_queue
from rank import AllScope, ProjectScope, Ranker, SessionScope, TagScope

PROFILE = "---\ndraft: false\nupdated: 2026-11-01\n---\n## Goals\n- FDO | weight 1 | match fdo\n"


def reply(*ids):
    return json.dumps({"ranking": [{"id": i, "reason": f"serves {i}"} for i in ids]})


class FakePredict:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def queue(tmp_path, profile=PROFILE):
    root = make_queue(tmp_path, {"a": {"tags": "[fdo]"}, "b": {"tags": "[project:lite]"}, "c": {}})
    if profile is not None:
        taste.profile_path(root).parent.mkdir()
        taste.profile_path(root).write_text(profile)
    return root


def rows(*ids, blocked=()):
    return lambda root: [{"id": i, "title": f"Title of {i}", "file": f"{i}.md", "importance": "3", "due": "",
                          "effort": "m", "unlocks": ["x"] if i == "a" else [], "blocked": i in blocked}
                         for i in ids]


def briefs(root, *ids, scope=None):
    return rank.candidates(root, scope or AllScope(""), 15, rows(*ids))


@pytest.mark.parametrize("text, cls, key", [
    ("all", AllScope, "all"), ("tag:fdo", TagScope, "tag:fdo"), ("project:lite", ProjectScope, "project:lite"),
    ("session:abc", SessionScope, "session:abc"),
])
def test_parse_scope(text, cls, key):
    scope = rank.parse_scope(text)
    assert (type(scope), scope.key) == (cls, key)


@pytest.mark.parametrize("text", ["tag", "tag:", "milestone:m1"])
def test_parse_scope_rejects_unknown_or_empty_scopes(text):
    with pytest.raises(SystemExit, match="--scope must be"):
        rank.parse_scope(text)


def test_scopes_admit_by_tag_and_by_project_tag_or_field(tmp_path):
    root = queue(tmp_path)
    (root / "ready" / "c.md").write_text((root / "ready" / "c.md").read_text().replace("tags:", "project: lite\ntags:"))
    assert [b.id for b in briefs(root, "a", "b", "c", scope=TagScope("fdo"))] == ["a"]
    assert [b.id for b in briefs(root, "a", "b", "c", scope=ProjectScope("lite"))] == ["b", "c"]


def test_session_scope_uses_the_bound_queue(tmp_path, monkeypatch):
    root = queue(tmp_path)
    monkeypatch.setenv("CLAUDE_TASKS_SESSIONS", str(tmp_path / "sessions"))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s1.toml").write_text(f'root = "{root}"\n')
    assert SessionScope("s1").root(tmp_path) == root
    with pytest.raises(SystemExit, match="session s2 is not bound to a queue"):
        SessionScope("s2").root(tmp_path)


def test_candidates_drop_blocked_briefs_and_keep_filter_order_up_to_the_limit(tmp_path):
    root = queue(tmp_path)
    found = rank.candidates(root, AllScope(""), 2, rows("c", "b", "a", blocked=("b",)))
    assert [(b.id, b.title, b.tags) for b in found] == [("c", "Title of c", frozenset()),
                                                       ("a", "Title of a", frozenset({"fdo"}))]
    assert found[1].facts == "importance 3; due none; effort m; unlocks 1 briefs"


def test_prioritise_rows_reads_the_filter_cli_for_the_queue(tmp_path):
    assert sorted(r["id"] for r in rank.prioritise_rows(queue(tmp_path))) == ["a", "b", "c"]


def test_a_candidate_carries_the_body_after_the_frontmatter_trimmed(tmp_path):
    brief = briefs(queue(tmp_path), "a")[0]
    assert brief.candidate(7) == rank.Candidate("a", "Title of a", brief.facts, "## Goal")


def test_the_judge_orders_the_candidates_with_reasons(tmp_path):
    root = queue(tmp_path)
    ranking = Ranker(root, FakePredict(reply("c", "a", "b"), reply("c", "a", "b"))).rank(
        AllScope(""), briefs(root, "a", "b", "c"), 2)
    assert [(i.id, i.reason, i.confidence, i.filter_position) for i in ranking.items] == [
        ("c", "serves c", 1.0, 3), ("a", "serves a", 1.0, 1)]
    assert (ranking.source, ranking.agreement, ranking.total, ranking.draft) == ("judge", 1.0, 3, False)


def test_an_unchanged_list_is_served_from_the_cache_and_a_changed_brief_is_not(tmp_path):
    root = queue(tmp_path)
    fake = FakePredict(reply("c", "a", "b"), reply("c", "a", "b"), reply("b", "a", "c"), reply("b", "a", "c"))
    ranker = Ranker(root, fake)
    first = ranker.rank(AllScope(""), briefs(root, "a", "b", "c"), 3)
    assert ranker.rank(AllScope(""), briefs(root, "a", "b", "c"), 3) == first
    assert len(fake.prompts) == 2
    (root / "ready" / "b.md").write_text((root / "ready" / "b.md").read_text() + "now urgent\n")
    assert ranker.rank(AllScope(""), briefs(root, "a", "b", "c"), 1).items[0].id == "b"


@pytest.mark.parametrize("profile, outputs, ids, reason", [
    (None, (), ("a", "b"), "no profile/priorities.md (run seed_profile.py)"),
    ("## Goals\n- X | weight lots\n", (), ("a", "b"), "profile unreadable: goal 'X': weight 'lots' is not a number"),
    (PROFILE, (), ("a",), "fewer than two candidates"),
    (PROFILE, ("bad", "worse"), ("a", "b"), "judge output was not a valid ranking of the candidates"),
])
def test_any_failure_falls_back_to_filter_order_and_says_why(tmp_path, profile, outputs, ids, reason):
    root = queue(tmp_path, profile)
    ranking = Ranker(root, FakePredict(*outputs)).rank(AllScope(""), briefs(root, *ids), 5)
    assert (ranking.source, ranking.fallback_reason, [i.id for i in ranking.items]) == ("filter", reason, list(ids))
    assert ranking.items[0].reason == "importance 3; due none; effort m; unlocks 1 briefs"


def test_text_output_flags_a_draft_profile_and_low_confidence(tmp_path):
    root = queue(tmp_path, PROFILE.replace("draft: false", "draft: true"))
    ranking = Ranker(root, FakePredict(reply("a", "b"), reply("b", "a"))).rank(AllScope(""), briefs(root, "a", "b"), 2)
    version = taste.load_profile(root).version
    assert ranking.text(0.75) == (f"Top 2 of 2 (all; profile {version} DRAFT; judge, agreement 0.00)\n"
                                  " 1. a  Title of a\n    serves a  (low confidence)\n"
                                  " 2. b  Title of b\n    serves b  (low confidence)")


def test_text_output_names_one_pass_and_fallback_sources(tmp_path):
    root = queue(tmp_path)
    one_pass = Ranker(root, FakePredict(reply("b", "a"), "x")).rank(AllScope(""), briefs(root, "a", "b"), 1)
    fallback = Ranker(queue(tmp_path / "q2", None), FakePredict()).rank(TagScope("t"), [], 3)
    assert (one_pass.how(), one_pass.text(0.75).splitlines()[-1]) == ("judge, one pass", "    serves b  (low confidence)")
    assert fallback.text(0.75) == "Top 0 of 0 (tag:t; profile none; filter order: no profile/priorities.md (run seed_profile.py))"


def test_json_output_omits_the_raw_judge_text(tmp_path):
    root = queue(tmp_path)
    ranking = Ranker(root, FakePredict(reply("b", "a"), reply("b", "a"))).rank(AllScope(""), briefs(root, "a", "b"), 2)
    out = json.loads(ranking.json())
    assert ("raw" in out, out["source"], [i["id"] for i in out["items"]]) == (False, "judge", ["b", "a"])


def test_run_logs_the_shown_list_with_each_items_goals(tmp_path):
    root = queue(tmp_path)
    rank.run(root, AllScope(""), 2, FakePredict(reply("a", "c", "b"), reply("a", "c", "b")), rows("a", "b", "c"))
    rank.run(root, AllScope(""), 2, FakePredict(), rows("a", "b", "c"), log=False)
    [shown] = PickLog(root).shown()
    assert ([(i.id, i.goals) for i in shown.items], shown.filter_order, shown.source) == (
        [("a", ("FDO",)), ("c", ())], ("a", "b", "c"), "judge")
    assert shown.judge_output == (reply("a", "c", "b"), reply("a", "c", "b"))


def test_a_fallback_list_with_an_unreadable_profile_is_still_logged(tmp_path):
    root = queue(tmp_path, "## Goals\n- X | weight lots\n")
    rank.run(root, AllScope(""), 2, FakePredict(), rows("a", "b"))
    assert [(i.id, i.goals) for i in PickLog(root).shown()[0].items] == [("a", ()), ("b", ())]


def test_rank_config_overrides_the_defaults(tmp_path):
    root = queue(tmp_path)
    (root / "tasks.toml").write_text((root / "tasks.toml").read_text() + "\n[rank]\ncandidates = 2\n")
    assert (rank.settings(root)["candidates"], rank.settings(root)["top"]) == (2, 10)


def test_backend_runs_claude_from_a_neutral_dir_with_the_configured_model(tmp_path):
    root = queue(tmp_path)
    built = rank.backend(root, {**rank.DEFAULTS, "model": "haiku"})
    assert (type(built), built.model, built.cwd.is_dir(), built.cwd.name) == (ClaudeBackend, "haiku", True, "judge")


def test_main_prints_the_ranked_text_and_json(tmp_path, monkeypatch, capsys):
    root = queue(tmp_path)
    monkeypatch.setattr(rank, "backend", lambda root, config: FakePredict(reply("c", "a", "b"), reply("c", "a", "b")))
    rank.main(["--root", str(root), "--top", "1"])
    rank.main(["--root", str(root), "--json", "--no-log"])
    text, payload = capsys.readouterr().out.split("\n{", 1)
    assert text.splitlines()[1:] == [" 1. c  Title of c", "    serves c"]
    assert [i["id"] for i in json.loads("{" + payload)["items"]] == ["c", "a", "b"]
    assert len(PickLog(root).shown()) == 1


def test_main_json_lists_every_candidate_for_a_session_scope(tmp_path, monkeypatch, capsys):
    root = queue(tmp_path)
    monkeypatch.setenv("CLAUDE_TASKS_SESSIONS", str(tmp_path / "sessions"))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "s1.toml").write_text(f'root = "{root}"\n')
    monkeypatch.setattr(rank, "backend", lambda root, config: FakePredict(reply("b", "c", "a"), reply("b", "c", "a")))
    rank.main(["--scope", "session:s1", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert (out["scope"], [i["id"] for i in out["items"]]) == ("session:s1", ["b", "c", "a"])

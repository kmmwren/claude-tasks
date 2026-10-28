"""check_deps.py — each defect class is injected into an otherwise clean queue and must fire."""
import json

import check_deps
import pytest

from tests.queue_helpers import MILESTONES, make_queue


def _kinds(root):
    return sorted((p.kind, p.brief) for p in check_deps.check(root))


def test_clean_queue_has_no_problems(tmp_path):
    root = make_queue(tmp_path, {
        "a": {"status": "done"}, "b": {"blocked_by": "[a]"},
        "c": {"status": "in-progress", "blocked_by": "[a]"}})
    assert check_deps.check(root) == []


def test_injected_cycle_is_detected_and_named_once(tmp_path):
    root = make_queue(tmp_path, {
        "a": {"status": "blocked", "blocked_by": "[b]"},
        "b": {"status": "blocked", "blocked_by": "[c]"},
        "c": {"status": "blocked", "blocked_by": "[a]"},
        "free": {}})
    problems = [p for p in check_deps.check(root) if p.kind == "cycle"]
    assert [p.detail for p in problems] == ["a -> b -> c -> a"]


def test_self_dependency_is_a_cycle(tmp_path):
    root = make_queue(tmp_path, {"a": {"status": "blocked", "tags": "[depends:a]"}})
    assert ("cycle", "a") in _kinds(root)


def test_legacy_blockers_participate_in_cycles(tmp_path):
    root = make_queue(tmp_path, {"a": {"blockers": "[b]"}, "b": {"blockers": "[a]"}})
    assert ("cycle", "a") in _kinds(root)


def test_cycle_reached_through_an_acyclic_branch_is_found_once(tmp_path):
    root = make_queue(tmp_path, {
        "entry": {"blocked_by": "[a]"},
        "a": {"blocked_by": "[b]"}, "b": {"blocked_by": "[a]"}})
    assert [k for k in _kinds(root) if k[0] == "cycle"] == [("cycle", "a")]


def test_injected_dangling_dependency_is_detected(tmp_path):
    root = make_queue(tmp_path, {
        "a": {"status": "blocked", "blocked_by": "[ghost, external:fine]"},
        "b": {"tags": "[depends:phantom]"}})
    assert _kinds(root) == [("dangling", "a"), ("dangling", "b")]


def test_legacy_prose_in_blockers_is_not_dangling(tmp_path):
    root = make_queue(tmp_path, {"a": {"blockers": "[waiting for the vpn]"}})
    assert check_deps.check(root) == []


def test_in_progress_brief_with_unmet_dependency_is_flagged(tmp_path):
    root = make_queue(tmp_path, {
        "dep": {"status": "ready"},
        "w": {"status": "in-progress", "blocked_by": "[dep, external:login]"}})
    problem = next(p for p in check_deps.check(root) if p.kind == "in-progress-unmet")
    assert problem.brief == "w" and problem.detail == "dep, external:login"


def test_blocked_without_reason_and_releasable_are_flagged(tmp_path):
    root = make_queue(tmp_path, {
        "dep": {"status": "done"},
        "empty": {"status": "blocked"},
        "stale": {"status": "blocked", "blocked_by": "[dep]"},
        "fine": {"status": "blocked", "blocked_by": "[external:x]"}})
    assert _kinds(root) == [("blocked-no-reason", "empty"), ("releasable", "stale")]


def test_unknown_milestone_is_flagged_only_when_milestones_are_configured(tmp_path):
    briefs = {"a": {"milestone": "M1"}, "b": {"milestone": "M9"}, "c": {}}
    configured = make_queue(tmp_path / "cfg", briefs, milestones=MILESTONES)
    assert _kinds(configured) == [("unknown-milestone", "b")]
    unconfigured = make_queue(tmp_path / "plain", briefs)
    assert check_deps.check(unconfigured) == []


def test_unreadable_milestones_file_is_reported_not_raised(tmp_path):
    root = make_queue(tmp_path, {"a": {}}, milestones="not = [valid")
    assert [p.kind for p in check_deps.check(root)] == ["milestones-file"]


def test_find_cycles_on_empty_graph():
    assert check_deps.find_cycles({}) == []


def test_cli_exit_codes_and_output(tmp_path, capsys):
    good = make_queue(tmp_path / "g", {"a": {}})
    with pytest.raises(SystemExit) as e:
        check_deps.main(["--root", str(good)])
    assert e.value.code == 0 and "dependency graph OK" in capsys.readouterr().out
    bad = make_queue(tmp_path / "b", {"a": {"blocked_by": "[ghost]"}})
    with pytest.raises(SystemExit) as e:
        check_deps.main(["--root", str(bad)])
    assert e.value.code == 1 and "dangling" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        check_deps.main(["--root", str(bad), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "dangling"

"""check_deps.py — each defect class is injected into an otherwise clean queue and must fire."""

import json

import check_deps
import pytest

from tests.queue_helpers import MILESTONES, make_queue


def _kinds(root):
    return sorted((p.kind, p.brief) for p in check_deps.check(root))


def test_clean_queue_has_no_problems(tmp_path):
    root = make_queue(
        tmp_path,
        {
            "a": {"status": "done"},
            "b": {"blocked_by": "[a]"},
            "c": {"status": "in-progress", "blocked_by": "[a]"},
        },
    )
    assert check_deps.check(root) == []


def test_injected_cycle_is_detected_and_named_once(tmp_path):
    root = make_queue(
        tmp_path,
        {
            "a": {"status": "blocked", "blocked_by": "[b]"},
            "b": {"status": "blocked", "blocked_by": "[c]"},
            "c": {"status": "blocked", "blocked_by": "[a]"},
            "free": {},
        },
    )
    problems = [p for p in check_deps.check(root) if p.kind == "cycle"]
    assert [p.detail for p in problems] == ["a -> b -> c -> a"]


def test_self_dependency_is_a_cycle(tmp_path):
    root = make_queue(tmp_path, {"a": {"status": "blocked", "tags": "[depends:a]"}})
    assert ("cycle", "a") in _kinds(root)


def test_legacy_blockers_participate_in_cycles(tmp_path):
    root = make_queue(tmp_path, {"a": {"blockers": "[b]"}, "b": {"blockers": "[a]"}})
    assert ("cycle", "a") in _kinds(root)


def test_cycle_reached_through_an_acyclic_branch_is_found_once(tmp_path):
    root = make_queue(
        tmp_path,
        {"entry": {"blocked_by": "[a]"}, "a": {"blocked_by": "[b]"}, "b": {"blocked_by": "[a]"}},
    )
    assert [k for k in _kinds(root) if k[0] == "cycle"] == [("cycle", "a")]


def test_injected_dangling_dependency_is_detected(tmp_path):
    root = make_queue(
        tmp_path,
        {
            "a": {"status": "blocked", "blocked_by": "[ghost, external:fine]"},
            "b": {"tags": "[depends:phantom]"},
        },
    )
    assert _kinds(root) == [("dangling", "a"), ("dangling", "b")]


def test_legacy_prose_in_blockers_is_not_dangling(tmp_path):
    root = make_queue(tmp_path, {"a": {"blockers": "[waiting for the vpn]"}})
    assert check_deps.check(root) == []


def test_in_progress_brief_with_unmet_dependency_is_flagged(tmp_path):
    root = make_queue(
        tmp_path,
        {
            "dep": {"status": "ready"},
            "w": {"status": "in-progress", "blocked_by": "[dep, external:login]"},
        },
    )
    problem = next(p for p in check_deps.check(root) if p.kind == "in-progress-unmet")
    assert problem.brief == "w" and problem.detail == "dep, external:login"


def test_blocked_without_reason_and_releasable_are_flagged(tmp_path):
    root = make_queue(
        tmp_path,
        {
            "dep": {"status": "done"},
            "empty": {"status": "blocked"},
            "stale": {"status": "blocked", "blocked_by": "[dep]"},
            "fine": {"status": "blocked", "blocked_by": "[external:x]"},
        },
    )
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


def test_blocked_brief_waiting_only_on_a_finished_depends_tag_is_releasable(tmp_path):
    root = make_queue(
        tmp_path, {"dep": {"status": "done"}, "w": {"status": "blocked", "tags": "[depends:dep]"}}
    )
    kinds = {(p.kind, p.brief) for p in check_deps.check(root)}
    assert ("releasable", "w") in kinds
    assert ("blocked-no-reason", "w") not in kinds


def test_blocked_brief_with_external_blocker_and_no_probe_is_flagged(tmp_path):
    root = make_queue(
        tmp_path, {"a": {"status": "blocked", "blocked_by": "[external:x]", "probed": False}}
    )
    assert [p.kind for p in check_deps.check(root)] == ["blocked-no-probe"]


# --------------------------------------------------------------------------- #
# facts lint
# --------------------------------------------------------------------------- #
def _facts(root, stem, text):
    (root / "facts").mkdir(exist_ok=True)
    (root / "facts" / f"{stem}.toml").write_text(text)


def test_a_facts_file_that_matches_its_brief_is_clean(tmp_path):
    root = make_queue(tmp_path, {"a": {}})
    _facts(root, "a", 'brief = "a"\n')
    assert check_deps.check(root) == []


def test_a_queue_without_a_facts_folder_is_clean(tmp_path):
    assert check_deps.check(make_queue(tmp_path, {"a": {}})) == []


def test_a_facts_file_with_no_brief_of_that_id_is_an_orphan(tmp_path):
    root = make_queue(tmp_path, {"a": {}})
    _facts(root, "gone", 'brief = "gone"\n')
    assert _kinds(root) == [("facts-orphan", "gone")]


def test_a_facts_file_naming_another_brief_is_a_mismatch(tmp_path):
    root = make_queue(tmp_path, {"a": {}, "b": {}})
    _facts(root, "a", 'brief = "b"\n')
    problems = check_deps.check(root)
    assert [(p.kind, p.brief) for p in problems] == [("facts-brief-mismatch", "a")]
    assert "'b'" in problems[0].detail


def test_a_facts_file_naming_no_brief_is_a_mismatch(tmp_path):
    root = make_queue(tmp_path, {"a": {}})
    _facts(root, "a", 'shipped = "yes"\n')
    assert _kinds(root) == [("facts-brief-mismatch", "a")]


def test_a_facts_file_that_is_not_toml_is_unreadable(tmp_path):
    root = make_queue(tmp_path, {"a": {}})
    _facts(root, "a", "this is = = not toml\n")
    assert _kinds(root) == [("facts-unreadable", "a")]


def test_an_orphan_that_is_also_unreadable_reports_both(tmp_path):
    root = make_queue(tmp_path, {"a": {}})
    _facts(root, "gone", "= bad\n")
    assert _kinds(root) == [("facts-orphan", "gone"), ("facts-unreadable", "gone")]


def test_a_parked_brief_with_no_blocked_by_is_undeclared(tmp_path):
    root = make_queue(tmp_path, {"p": {"status": "parked"}})
    path = root / "parked" / "p.md"
    path.write_text(
        "".join(ln for ln in path.read_text().splitlines(True) if not ln.startswith("blocked_by:"))
    )
    assert _kinds(root) == [("undeclared", "p")]


def _without_declaration(root, status, bid):
    path = root / status / f"{bid}.md"
    path.write_text(
        "".join(ln for ln in path.read_text().splitlines(True) if not ln.startswith("blocked_by:"))
    )


@pytest.mark.parametrize("status", ["ready", "in-progress"])
def test_a_live_brief_with_no_blocked_by_is_undeclared(tmp_path, status):
    root = make_queue(tmp_path, {"a": {"status": status}})
    _without_declaration(root, status, "a")
    assert _kinds(root) == [("undeclared", "a")]


@pytest.mark.parametrize("status", ["ready", "in-progress"])
def test_an_empty_blocked_by_declares_depending_on_nothing(tmp_path, status):
    root = make_queue(tmp_path, {"a": {"status": status, "blocked_by": "[]"}})
    assert check_deps.check(root) == []


def test_a_legacy_blockers_entry_counts_as_a_declaration(tmp_path):
    root = make_queue(tmp_path, {"b": {}, "a": {"blockers": "[b]"}})
    _without_declaration(root, "ready", "a")
    assert ("undeclared", "a") not in _kinds(root)

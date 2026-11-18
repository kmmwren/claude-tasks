"""Tests for resume_prompt.py: the successor prompt rebuilt from a session's ledger and mission."""

import json
import pathlib

import pytest
import resume_prompt

SID = "new-id"


def ledger(root: pathlib.Path, *rows: dict[str, object]) -> pathlib.Path:
    folder = root / SID
    folder.mkdir(parents=True)
    for row in rows:
        (folder / f"{row['id']}.json").write_text(json.dumps(row))
    return root


def item(id_: str, status: str, **meta: str) -> dict[str, object]:
    return {"id": id_, "subject": f"task {id_}", "status": status, "metadata": meta}


@pytest.fixture
def rows(tmp_path: pathlib.Path) -> pathlib.Path:
    return ledger(
        tmp_path / "tasks",
        item("1", "completed", brief="done/a.md"),
        item("2", "pending", brief="inbox/b.md"),
        item("3", "in_progress", brief="inbox/c.md", blocker="waits on CI"),
        item("10", "in_progress", brief="ready/d.md"),
        item("4", "pending"),
    )


def test_open_items_put_in_progress_first_unblocked_first_then_ledger_order(
    rows: pathlib.Path,
) -> None:
    assert [i.id for i in resume_prompt.open_items(SID, rows)] == ["10", "3", "2", "4"]


def test_line_names_the_brief_blocker_and_a_missing_brief(rows: pathlib.Path) -> None:
    lines = [i.line() for i in resume_prompt.open_items(SID, rows)]
    assert "- #3 [in_progress] task 3; brief inbox/c.md; blocked: waits on CI" in lines
    assert "- #4 [pending] task 4; no brief yet: file one first" in lines


def test_mission_is_found_by_a_previous_session_id(tmp_path: pathlib.Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "id": "current",
                        "name": "Checkout",
                        "mission": "Ship checkout.",
                        "previous_ids": [SID],
                    }
                ]
            }
        )
    )
    found = resume_prompt.Mission.lookup(SID, registry, tmp_path / "jobs")
    assert found == resume_prompt.Mission("Checkout", "Ship checkout.")


def test_mission_is_found_by_the_roster_entrys_job_short_id(tmp_path: pathlib.Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"sessions": [{"id": "x", "name": "PRs", "mission": "Zero.", "job": SID[:8]}]})
    )
    found = resume_prompt.Mission.lookup(SID, registry, tmp_path / "jobs")
    assert found == resume_prompt.Mission("PRs", "Zero.")


def test_pinned_session_off_the_roster_takes_its_job_name(tmp_path: pathlib.Path) -> None:
    job = tmp_path / "jobs" / SID[:8]
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({"name": "Checkout"}))
    mission = resume_prompt.Mission.lookup(SID, tmp_path / "missing.json", tmp_path / "jobs")
    assert (
        resume_prompt.header(SID, mission) == f'Resume long-running session "Checkout" (was {SID}).'
    )


def test_prompt_without_a_registry_entry_still_lists_the_work(
    rows: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    mission = resume_prompt.Mission.lookup(SID, tmp_path / "missing.json", tmp_path / "jobs")
    prompt = resume_prompt.render(SID, mission, resume_prompt.open_items(SID, rows))
    assert prompt.startswith(f"Resume the open work of session {SID}.")
    assert "run /claude-tasks:handoff" in prompt and "task 1;" not in prompt


def test_corrupt_registry_and_ledger_file_degrade_instead_of_crashing(
    rows: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    (rows / SID / "bad.json").write_text("{not json")
    registry = tmp_path / "registry.json"
    registry.write_text("[]")
    assert resume_prompt.Mission.lookup(SID, registry, tmp_path / "jobs") is None
    assert [i.id for i in resume_prompt.open_items(SID, rows)] == ["10", "3", "2", "4"]


def test_main_reads_session_id_from_the_environment(
    rows: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("CLAUDE_TASKS_LEDGER_ROOT", str(rows))
    monkeypatch.setenv("LONG_RUNNING_REGISTRY", str(tmp_path / "none.json"))
    monkeypatch.setenv("LONG_RUNNING_JOBS", str(tmp_path / "no-jobs"))
    assert resume_prompt.main([]) == 0
    assert "- #10 [in_progress] task 10; brief ready/d.md" in capsys.readouterr().out


def test_a_live_subagent_is_named_under_the_item_it_serves(tmp_path: pathlib.Path) -> None:
    agent = {
        "kind": "agent",
        "name": "wc-8572 slim",
        "asked": "slim the PR down to the module list",
        "output": "/tmp/agents/slim.output",
        "collect": "Read the output file; re-run with the same prompt if it is missing",
    }
    served = item("5", "in_progress", brief="ready/e.md")
    served["metadata"] = {"brief": "ready/e.md", "agents": [agent, "junk", {"kind": "task"}]}
    rows = ledger(tmp_path / "tasks", served, item("6", "pending", brief="inbox/f.md"))
    prompt = resume_prompt.render(SID, None, resume_prompt.open_items(SID, rows))
    assert prompt.splitlines()[-4:] == [
        "- #5 [in_progress] task 5; brief ready/e.md",
        "  - running agent wc-8572 slim; asked: slim the PR down to the module list; "
        "output /tmp/agents/slim.output; "
        "collect: Read the output file; re-run with the same prompt if it is missing",
        "  - running task (unnamed)",
        "- #6 [pending] task 6; brief inbox/f.md",
    ]
    assert "junk" not in prompt


def test_an_item_with_no_agents_or_malformed_agents_prints_one_line(tmp_path: pathlib.Path) -> None:
    rows = ledger(
        tmp_path / "tasks",
        {
            **item("7", "pending", brief="inbox/g.md"),
            "metadata": {"brief": "inbox/g.md", "agents": "x"},
        },
    )
    (only,) = resume_prompt.open_items(SID, rows)
    assert only.lines() == ["- #7 [pending] task 7; brief inbox/g.md"]


def test_main_without_a_session_id_exits_with_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with pytest.raises(SystemExit) as raised:
        resume_prompt.main([])
    assert raised.value.code == 2

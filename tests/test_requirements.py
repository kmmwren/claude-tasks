"""Tests for requirements_register.py and requirements_check.py (the requirements-extract mechanism)."""
import json

import init_queue
import pytest
import requirements_check
import requirements_register as reg


def item(quote="build one feature at a time", source="2026-10-23T10:09:21Z", kind="constraint", title="One feature at a time"):
    return {"kind": kind, "title": title, "quote": quote, "source": source}


@pytest.fixture
def queue(tmp_path):
    return init_queue.init_queue(tmp_path / "q", name="panaseer-lite")


def brief(queue, folder, name, ids, criteria):
    marks = "\n".join(criteria)
    text = (
        f"---\nid: {name}\ntitle: {name}\nrequirements: [{', '.join(ids)}]\n---\n\n"
        f"## Goal\nx\n\n## Success criteria\n{marks}\n\n## Constraints\n- none\n"
    )
    (queue / folder / f"{name}.md").write_text(text)


# --- register -----------------------------------------------------------------

def test_stub_scaffolded_with_default_prefix(queue):
    text = (queue / "requirements.md").read_text()
    assert reg.parse_prefix(text) == "PL"
    assert reg.parse_register(text) == []


def test_force_never_clobbers_an_existing_register(queue):
    reg.append_items(queue, [item()])
    init_queue.init_queue(queue, name="panaseer-lite", force=True)
    assert len(reg.parse_register((queue / "requirements.md").read_text())) == 1


@pytest.mark.parametrize("name,expected", [("panaseer-lite", "PL"), ("tasks", "TASK"), ("3d-app", "A"), ("---", "REQ"), ("x", "X")])
def test_default_prefix(name, expected):
    assert init_queue.default_prefix(name) == expected


def test_explicit_prefix_and_invalid_prefix(tmp_path):
    q = init_queue.init_queue(tmp_path / "a", prefix="lite")
    assert reg.parse_prefix((q / "requirements.md").read_text()) == "LITE"
    with pytest.raises(SystemExit, match="--prefix"):
        init_queue.init_queue(tmp_path / "b", prefix="bad prefix")
    assert not (tmp_path / "b").exists()


def test_append_assigns_sequential_ids_with_verbatim_quote_and_source(queue):
    added, skipped = reg.append_items(queue, [item(), item(quote="never share credentials", source="2026-10-23T10:10:00Z", kind="never", title="No shared creds")])
    assert added == ["PL-R001", "PL-R002"] and skipped == []
    first, second = reg.parse_register((queue / "requirements.md").read_text())
    assert (first.id, first.kind, first.quote, first.source) == ("PL-R001", "constraint", "build one feature at a time", "2026-10-23T10:09:21Z")
    assert second.kind == "never" and second.number == 2


def test_rerun_is_idempotent_byte_for_byte(queue):
    reg.append_items(queue, [item()])
    before = (queue / "requirements.md").read_text()
    added, skipped = reg.append_items(queue, [item(quote="  Build ONE feature   at a time ")])
    assert added == [] and skipped == ["PL-R001"]
    assert (queue / "requirements.md").read_text() == before


def test_rerun_on_new_messages_appends_and_never_renumbers(queue):
    reg.append_items(queue, [item(), item(quote="two", source="s2")])
    path = queue / "requirements.md"
    text = path.read_text()
    # delete R001 by hand: R002 keeps its id and the next new one is R003, not R002
    path.write_text(text.replace("## PL-R001", "<!-- gone -->\n## XX", 1).replace("## XX [constraint] One feature at a time", ""))
    added, _ = reg.append_items(queue, [item(quote="three", source="s3")])
    assert added == ["PL-R003"]
    assert [r.id for r in reg.parse_register(path.read_text())] == ["PL-R002", "PL-R003"]


def test_same_quote_from_a_later_message_is_a_new_requirement(queue):
    reg.append_items(queue, [item()])
    added, _ = reg.append_items(queue, [item(source="2026-10-24T09:00:00Z")])
    assert added == ["PL-R002"]


def test_register_header_prefix_wins_over_argument_and_missing_prefix_needs_one(queue, tmp_path):
    added, _ = reg.append_items(queue, [item()], prefix="OTHER")
    assert added == ["PL-R001"]
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "requirements.md").write_text("# no header\n")
    with pytest.raises(SystemExit, match="prefix"):
        reg.append_items(bare, [item()])
    assert reg.append_items(bare, [item()], prefix="BARE")[0] == ["BARE-R001"]


def test_append_without_register_fails(tmp_path):
    with pytest.raises(SystemExit, match="No register"):
        reg.append_items(tmp_path, [item()])


@pytest.mark.parametrize("bad,match", [
    ({"kind": "constraint", "title": "t", "quote": "", "source": "s"}, "missing quote"),
    ({"kind": "wish", "title": "t", "quote": "q", "source": "s"}, "unknown kind"),
    ({"kind": "never", "title": "t", "quote": "q" * 241, "source": "s"}, "over 240"),
])
def test_invalid_items_rejected_and_nothing_written(queue, bad, match):
    before = (queue / "requirements.md").read_text()
    with pytest.raises(SystemExit, match=match):
        reg.append_items(queue, [item(), bad])
    assert (queue / "requirements.md").read_text() == before


def test_register_without_trailing_newline_still_appends_cleanly(queue):
    path = queue / "requirements.md"
    path.write_text(path.read_text().rstrip("\n"))
    reg.append_items(queue, [item()])
    assert len(reg.parse_register(path.read_text())) == 1


def test_parse_ignores_fields_before_any_entry_and_keeps_inner_quotes():
    text = '- quote: "orphan"\n## A-R001 [never] t\n- quote: "say "hi" now"\n- source: s\n- withdrawn: superseded\n'
    (r,) = reg.parse_register(text)
    assert r.quote == 'say "hi" now' and r.withdrawn == "superseded"


def test_cli_appends_from_file_and_stdin(queue, tmp_path, capsys, monkeypatch):
    f = tmp_path / "items.json"
    f.write_text(json.dumps([item()]))
    reg.main(["append", str(queue), "--json", str(f)])
    assert "added 1: PL-R001" in capsys.readouterr().out
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([item()])))
    reg.main(["append", str(queue)])
    assert "already registered 1: PL-R001" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO('{"not": "a list"}'))
    with pytest.raises(SystemExit, match="list"):
        reg.main(["append", str(queue)])


# --- coverage check -----------------------------------------------------------

def two_requirements(queue):
    reg.append_items(queue, [item(), item(quote="two", source="s2")])


def test_check_passes_when_every_requirement_is_covered(queue, capsys):
    two_requirements(queue)
    brief(queue, "ready", "a", ["PL-R001"], ["- [ ] one"])
    brief(queue, "done", "b", ["PL-R002"], ["- [x] two", "- [X] three"])
    assert requirements_check.main([str(queue)]) == 0
    assert "2 requirement(s) covered" in capsys.readouterr().out


def test_check_fails_on_requirement_with_no_brief(queue, capsys):
    two_requirements(queue)
    brief(queue, "ready", "a", ["PL-R001"], ["- [ ] one"])
    assert requirements_check.main([str(queue)]) == 1
    assert "PL-R002 has no brief" in capsys.readouterr().out


def test_a_brief_in_any_lifecycle_folder_covers(queue):
    two_requirements(queue)
    brief(queue, "inbox", "a", ["PL-R001"], ["- [ ] one"])
    brief(queue, "parked", "b", ["PL-R002"], ["- [ ] one"])
    assert requirements_check.check(queue) == []


def test_check_fails_on_unknown_id(queue):
    reg.append_items(queue, [item()])
    brief(queue, "ready", "a", ["PL-R001", "PL-R099"], ["- [ ] one"])
    assert requirements_check.check(queue) == ["a names unknown requirement PL-R099"]


def test_check_fails_on_unticked_criterion_in_done_only(queue):
    reg.append_items(queue, [item()])
    brief(queue, "in-progress", "a", ["PL-R001"], ["- [x] one", "- [ ] two"])
    assert requirements_check.check(queue) == []
    brief(queue, "done", "b", ["PL-R001"], ["- [x] one", "- [ ] two"])
    assert requirements_check.check(queue) == ["b is in done/ with 1 unticked criteria"]


def test_unticked_checkbox_anywhere_in_a_done_brief_fails(queue):
    reg.append_items(queue, [item()])
    brief(queue, "done", "a", ["PL-R001"], ["- [x] one"])
    path = queue / "done" / "a.md"
    path.write_text(path.read_text().replace("- none", "- [ ] stray"))
    assert requirements_check.check(queue) == ["a is in done/ with 1 unticked criteria"]


def test_brief_with_requirements_but_no_criteria_fails(queue):
    reg.append_items(queue, [item()])
    (queue / "ready" / "a.md").write_text("---\nid: a\nrequirements: [PL-R001]\n---\n\n## Goal\nx\n")
    assert requirements_check.check(queue) == ["a cites requirements but has no `- [ ]` criteria"]


def test_withdrawn_requirement_needs_no_brief(queue):
    reg.append_items(queue, [item()])
    path = queue / "requirements.md"
    path.write_text(path.read_text() + "- withdrawn: superseded by PL-R009\n")
    assert requirements_check.check(queue) == []


def test_block_list_requirements_and_briefs_without_frontmatter(queue):
    two_requirements(queue)
    (queue / "ready" / "a.md").write_text(
        "---\nid: a\nrequirements: # both\n  - PL-R001\n  - PL-R002 # second\nnext: x\n---\n\n## Success criteria\n- [ ] c\n"
    )
    (queue / "ready" / "plain.md").write_text("no frontmatter at all\n")
    assert requirements_check.check(queue) == []


def test_brief_without_requirements_field_cites_nothing(queue):
    reg.append_items(queue, [item()])
    (queue / "ready" / "x.md").write_text("---\nid: x\ntitle: y\n---\n\n## Success criteria\n- [ ] c\n")
    assert requirements_check.check(queue) == ["PL-R001 has no brief: add `requirements: [PL-R001]` to a brief with criteria"]


def test_main_exit_codes(queue, tmp_path, capsys):
    assert requirements_check.main([]) == 2
    assert requirements_check.main([str(tmp_path / "nope")]) == 2
    assert requirements_check.main([str(queue)]) == 0  # empty register: nothing to cover
    (queue / "requirements.md").unlink()
    assert requirements_check.main([str(queue)]) == 2


def test_main_reads_argv_when_not_given(queue, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["requirements_check.py", str(queue)])
    assert requirements_check.main() == 0


# --- ported from the Lite audit's own check (table register, project-root layout) ---

TABLE = (
    "| Id | When | Words | Class | Briefs |\n|---|---|---|---|---|\n"
    '| LITE-R001 | t | "a" | IN-FLIGHT | one |\n'
)


def project(tmp_path, brief_text, folder="in-progress", register=TABLE):
    """The Lite layout: requirements.md beside a `.tasks/` queue, briefs without `---` fences."""
    (tmp_path / "requirements.md").write_text(register)
    queue = init_queue.init_queue(tmp_path / ".tasks", name="lite", prefix="LITE")
    (queue / "requirements.md").unlink()
    (queue / folder / "one.md").write_text(brief_text)
    return queue


def test_table_register_beside_the_queue_agrees(tmp_path):
    queue = project(tmp_path, "requirements: [LITE-R001]\n- [ ] x\n")
    assert reg.register_path(queue) == tmp_path / "requirements.md"
    assert requirements_check.check(queue) == []
    assert requirements_check.main([str(queue)]) == 0


def test_table_requirement_with_no_brief_fails(tmp_path):
    assert "LITE-R001 has no brief: add `requirements: [LITE-R001]` to a brief with criteria" in requirements_check.check(project(tmp_path, "no field\n"))


def test_table_done_brief_with_unticked_criterion_fails(tmp_path):
    problems = requirements_check.check(project(tmp_path, "requirements: [LITE-R001]\n- [ ] x\n", folder="done"))
    assert problems == ["one is in done/ with 1 unticked criteria"]


def test_table_unknown_requirement_fails(tmp_path):
    problems = requirements_check.check(project(tmp_path, "requirements: [LITE-R001, LITE-R999]\n- [ ] x\n"))
    assert problems == ["one names unknown requirement LITE-R999"]


def test_table_row_and_briefs_must_name_each_other(tmp_path):
    register = TABLE.replace("| one |", "| one, two |")
    problems = requirements_check.check(project(tmp_path, "requirements: [LITE-R001]\n- [ ] x\n", register=register))
    assert problems == ["LITE-R001 lists two but that brief does not name it"]
    other = tmp_path / "b"
    other.mkdir()
    problems = requirements_check.check(project(other, "requirements: [LITE-R001]\n- [ ] x\n", register=TABLE.replace("| one |", "|  |")))
    assert problems == ["one names LITE-R001 but the register row omits it"]


def test_table_append_adds_a_row_after_the_last_one_with_no_brief_and_skips_duplicates(tmp_path):
    register = TABLE + "\n## Notes\nstays below the table\n"
    queue = project(tmp_path, "requirements: [LITE-R001]\n- [ ] x\n", register=register)
    added, skipped = reg.append_items(queue, [item(quote="a"), item(quote="new ask", source="2026-10-24T09:00:00Z")])
    assert (added, skipped) == (["LITE-R002"], ["LITE-R001"])
    text = (tmp_path / "requirements.md").read_text()
    assert text.index("LITE-R002") < text.index("## Notes")
    assert '| LITE-R002 | 2026-10-24T09:00:00Z | "new ask" | UNTRACKED |  |' in text
    assert requirements_check.check(queue) == ["LITE-R002 has no brief: add `requirements: [LITE-R002]` to a brief with criteria"]
    assert reg.append_items(queue, [item(quote="new ask", source="2026-10-24T09:00:00Z")]) == ([], ["LITE-R002"])


def test_table_row_too_short_to_hold_a_briefs_column_is_not_a_requirement():
    assert reg.parse_register("| LITE-R001 | only | three |\n") == []


# --- add_task writes the link the check reads ---------------------------------

def test_add_task_requirements_flag_covers_the_register(queue, capsys):
    import add_task

    reg.append_items(queue, [item()])
    assert requirements_check.main([str(queue)]) == 1
    add_task.main(["--root", str(queue), "--title", "Cover it", "--status", "ready",
                   "--criteria", "a test proves it", "--requirements", "PL-R001",
                   "--goal", "PL-R001 holds", "--context", "from the register"])
    assert "requirements: [PL-R001]" in next((queue / "ready").glob("*.md")).read_text()
    assert requirements_check.main([str(queue)]) == 0


def test_add_task_without_flag_writes_an_empty_requirements_field(queue):
    import add_task

    add_task.main(["--root", str(queue), "--title", "Plain", "--status", "parked"])
    assert "requirements: []" in next((queue / "parked").glob("*.md")).read_text()

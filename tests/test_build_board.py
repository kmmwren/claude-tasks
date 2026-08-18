"""Tests for build_board.py — rendering briefs into the read-only HTML board.

One rich fixture queue exercises the structural branches (epics, children, orphan
children, loose cards, empty columns, raw inbox items, progress bars), plus targeted
cases for the frontmatter/section parsers and the main() entrypoint.
"""
import build_board
import init_queue


def _write(root, folder, name, text):
    (root / folder / name).write_text(text)


def _brief(bid, *, title="T", status="ready", btype="todo", autonomy="full",
           importance="2", parent="", domain="", effort="", criteria=None, goal="A goal.",
           assignee=None):
    crit = "\n".join(criteria) if criteria else ""
    # assignee is omitted entirely unless given, so the default brief doubles as the
    # backwards-compatibility fixture for briefs written before the field existed.
    claim = "" if assignee is None else f"\nassignee: {assignee}"
    return f"""---
id: {bid}
title: {title}
status: {status}
type: {btype}
importance: {importance}
autonomy: {autonomy}
estimated-effort: {effort}
domain: {domain}
parent: {parent}{claim}
---

## Goal

{goal}

## Success criteria

{crit}
"""


# ── parse_frontmatter / section ───────────────────────────────────────────────

def test_parse_frontmatter_splits_body():
    fm, body = build_board.parse_frontmatter(_brief("x"))
    assert fm["id"] == "x"
    assert "## Goal" in body


def test_parse_frontmatter_no_frontmatter():
    fm, body = build_board.parse_frontmatter("just body")
    assert fm == {}
    assert body == "just body"


def test_parse_frontmatter_skips_lines_without_colon():
    fm, _ = build_board.parse_frontmatter("---\nid: x\nstray line no colon\n---\nbody")
    assert fm == {"id": "x"}  # the colon-less line is ignored, not crashed on


def test_section_extracts_and_stops_at_next_heading():
    _, body = build_board.parse_frontmatter(_brief("x", goal="Only this."))
    assert build_board.section(body, "Goal") == "Only this."
    assert build_board.section(body, "Nonexistent") == ""


# ── load_briefs ───────────────────────────────────────────────────────────────

def test_load_briefs_skips_non_inbox_file_without_id(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "ready", "no-id.md", "---\ntitle: headless\n---\n\nbody\n")
    ids = [b["id"] for b in build_board.load_briefs(root)]
    assert "no-id" not in ids


def test_load_briefs_synthesises_raw_inbox_item(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "inbox", "raw-thought.md", "---\nraw: true\n---\n\n# Raw thought\n\nsome body\n")
    briefs = {b["id"]: b for b in build_board.load_briefs(root)}
    assert "raw-thought" in briefs
    assert briefs["raw-thought"]["goal"].startswith("# Raw thought")


# ── assignee (advisory claim) ─────────────────────────────────────────────────

def test_load_briefs_reads_assignee(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "in-progress", "claimed.md", _brief("claimed", assignee="alice"))
    briefs = {b["id"]: b for b in build_board.load_briefs(root)}
    assert briefs["claimed"]["assignee"] == "alice"


def test_load_briefs_defaults_assignee_to_empty_when_field_absent(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "ready", "legacy.md", _brief("legacy"))
    briefs = {b["id"]: b for b in build_board.load_briefs(root)}
    assert briefs["legacy"]["assignee"] == ""


def test_load_briefs_defaults_assignee_for_raw_inbox_item(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "inbox", "raw.md", "---\nraw: true\n---\n\nsome body\n")
    briefs = {b["id"]: b for b in build_board.load_briefs(root)}
    assert briefs["raw"]["assignee"] == ""


def test_search_text_joins_only_non_empty_parts():
    b = {"title": "t", "domain": "", "goal": "", "assignee": "alice"}
    assert build_board.search_text(b) == "t alice"


def test_search_text_lowercases_and_orders_all_parts():
    b = {"title": "Ttl", "domain": "Dom", "goal": "Goal", "assignee": "Alice"}
    assert build_board.search_text(b) == "ttl dom goal alice"


def test_search_text_of_unclaimed_brief_omits_assignee():
    b = {"title": "t", "domain": "d", "goal": "g", "assignee": ""}
    assert build_board.search_text(b) == "t d g"


def test_card_renders_assignee_chip_when_claimed(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "in-progress", "claimed.md", _brief("claimed", assignee="agent-2"))
    build_board.build(root, "demo")
    page = (root / "view" / "board.html").read_text()
    assert '<span class="chip who">agent-2</span>' in page


def test_card_omits_assignee_chip_when_unclaimed(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "ready", "free.md", _brief("free", title="Unclaimed"))
    build_board.build(root, "demo")
    page = (root / "view" / "board.html").read_text()
    assert "chip who" not in page
    assert "Unclaimed" in page  # the unclaimed card still renders


def test_card_escapes_assignee(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "ready", "x.md", _brief("x", assignee="<script>"))
    build_board.build(root, "demo")
    page = (root / "view" / "board.html").read_text()
    assert '<span class="chip who">&lt;script&gt;</span>' in page


def test_card_data_text_includes_assignee(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="demo")
    _write(root, "ready", "x.md", _brief("x", title="Ttl", domain="", assignee="Alice"))
    build_board.build(root, "demo")
    page = (root / "view" / "board.html").read_text()
    assert 'data-text="ttl a goal. alice"' in page


# ── build(): the structural fixture ───────────────────────────────────────────

def _rich_queue(tmp_path):
    root = init_queue.init_queue(tmp_path / "q", name="My Project")
    # an epic with a child in the same column (kids branch + progress bar)
    _write(root, "ready", "epic-a.md", _brief("epic-a", title="Epic A", btype="project"))
    _write(root, "ready", "child-1.md", _brief(
        "child-1", title="Child One", parent="epic-a", domain="work", effort="xs",
        criteria=["- [x] done bit", "- [ ] todo bit"]))
    # a loose card with non-checklist criteria text + an invalid importance + odd autonomy
    _write(root, "ready", "loose-1.md", _brief(
        "loose-1", title="Loose", autonomy="weird", importance="9",
        goal="# TODO", criteria=["free text criterion"]))
    # an orphan child whose parent epic lives in a DIFFERENT column
    _write(root, "ready", "orphan.md", _brief(
        "orphan", title="Orphan", parent="epic-b", autonomy="needs-input"))
    # the parent epic, in another column, with no children of its own → "no sub-tasks"
    _write(root, "in-progress", "epic-b.md", _brief(
        "epic-b", title="Epic B", status="in-progress", btype="project", autonomy="blocked"))
    return root


def test_build_writes_board_with_all_structures(tmp_path):
    root = _rich_queue(tmp_path)
    build_board.build(root, "My Project")
    page = (root / "view" / "board.html").read_text()
    assert "My Project" in page                 # project name in the title
    assert "Epic A" in page and "Child One" in page
    assert "no sub-tasks" in page               # epic-b has none in its column
    assert "Orphan" in page                     # orphan child still rendered
    assert "nothing here" in page               # done/ and parked/ are empty
    assert "1/2" in page                        # child-1 progress (1 of 2 criteria)
    assert "free text criterion" in page        # non-checklist criterion rendered


def test_build_counts_autonomy_in_header(tmp_path):
    root = _rich_queue(tmp_path)
    build_board.build(root, "My Project")
    page = (root / "view" / "board.html").read_text()
    # epic-a + child-1 are autonomy:full in ready → "Claude can do (2)"
    assert "Claude can do (2)" in page


def test_main_uses_resolver_and_config(tmp_path, monkeypatch):
    root = init_queue.init_queue(tmp_path / "q", name="Resolved Name")
    _write(root, "ready", "one.md", _brief("one", title="Just one"))
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(root))
    build_board.main()
    page = (root / "view" / "board.html").read_text()
    assert "Resolved Name" in page
    assert "Just one" in page


def test_parse_frontmatter_strips_an_inline_comment():
    fm, _ = build_board.parse_frontmatter("---\nstatus: ready # ready | done\n---\nbody")
    assert fm["status"] == "ready"


def test_parse_frontmatter_reads_a_comment_only_value_as_empty():
    fm, _ = build_board.parse_frontmatter("---\nassignee: # OPTIONAL who claimed it\n---\nb")
    assert fm["assignee"] == ""


def test_parse_frontmatter_keeps_a_hash_that_is_part_of_the_value():
    fm, _ = build_board.parse_frontmatter("---\ntitle: fix issue#42\n---\nbody")
    assert fm["title"] == "fix issue#42"


def test_parse_frontmatter_handles_the_shipped_template():
    fm, _ = build_board.parse_frontmatter(init_queue.QUEUE_TEMPLATE)
    assert fm["assignee"] == ""
    assert fm["status"] == "ready"
    assert fm["importance"] == ""

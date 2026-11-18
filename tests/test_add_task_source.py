"""Tests for add_task.py provenance: `--source intake` and the `--source-ref` frontmatter line."""

import add_task
import init_queue
import pytest
from deps import fm

PAGE = "https://panaseer.atlassian.net/wiki/spaces/SMH/pages/123/A-call"


@pytest.fixture
def queue(tmp_path):
    return init_queue.init_queue(tmp_path / "q", name="demo")


def filed(queue, *extra):
    add_task.main(
        ["--title", "Answer a Lite prospect question", "--root", str(queue), "--no-sync", *extra]
    )
    (path,) = (queue / "inbox").glob("*.md")
    return path.read_text()


def test_source_ref_is_written_after_source(queue):
    text = filed(queue, "--source", "intake", "--source-ref", PAGE)
    lines = text.split("---")[1].strip().splitlines()
    assert lines[lines.index("source: intake") + 1] == f"source_ref: {PAGE}"
    assert fm(text)["source_ref"] == PAGE


def test_source_ref_defaults_to_empty(queue):
    text = filed(queue)
    assert fm(text)["source"] == "claude"
    assert fm(text)["source_ref"] == ""


def test_the_execution_log_names_intake_and_its_page(queue):
    text = filed(queue, "--source", "intake", "--source-ref", PAGE)
    assert f"captured by intake from {PAGE} via add_task.py" in text


def test_intake_without_a_ref_is_rejected(queue):
    with pytest.raises(SystemExit):
        filed(queue, "--source", "intake")


@pytest.mark.parametrize(
    "ref",
    [
        f"{PAGE}\nstatus: done",
        f"{PAGE}\rautonomy: full",
        "~/notes/call #3.md",
        "#top",
        f"{PAGE}\u2028x",
        f"{PAGE}\x85x",
    ],
)
def test_a_ref_that_would_not_read_back_is_rejected(queue, ref):
    with pytest.raises(SystemExit):
        filed(queue, "--source", "intake", "--source-ref", ref)
    assert not list((queue / "inbox").glob("*.md"))


def test_a_ref_with_spaces_and_a_fragment_reads_back_whole(queue):
    ref = "~/notes/call notes.md#section"
    assert fm(filed(queue, "--source", "intake", "--source-ref", ref))["source_ref"] == ref


def test_a_repeat_capture_logs_its_ref_on_the_existing_brief(queue):
    filed(queue, "--source", "intake", "--source-ref", PAGE)
    other = PAGE.replace("123", "456")
    (path,) = (queue / "inbox").glob("*.md")
    add_task.main(
        ["--title", "Answer a Lite prospect question", "--root", str(queue), "--no-sync"]
        + ["--source", "intake", "--source-ref", other]
    )
    merged = path.read_text()
    assert len(list((queue / "inbox").glob("*.md"))) == 1
    assert fm(merged)["source_ref"] == PAGE
    assert f"repeat capture by intake from {other} merged here." in merged


def test_an_unknown_source_is_still_rejected(queue):
    with pytest.raises(SystemExit):
        filed(queue, "--source", "robot")


def test_the_queue_template_documents_source_ref(tmp_path):
    template = (init_queue.init_queue(tmp_path / "t") / "_template.md").read_text()
    assert "source_ref:" in template and "user | claude | intake" in template


TITLE = ["--title", "Answer a Lite prospect question", "--no-sync"]


def capture(queue, *extra):
    add_task.main([*TITLE, "--root", str(queue), *extra])
    (path,) = (queue / "inbox").glob("*.md")
    return fm(path.read_text())


@pytest.mark.parametrize("flag", ["--title", "--domain", "--due", "--effort", "--parent"])
@pytest.mark.parametrize("brk", ["\n", "\r\n", "\u2028", "\x85", "\x0b"])
def test_a_line_break_in_a_frontmatter_flag_is_folded_not_injected(queue, flag, brk):
    meta = capture(queue, flag, f"x{brk}assignee: someone")
    assert meta["status"] == "inbox" and not meta.get("assignee")
    assert meta[{"--effort": "estimated-effort"}.get(flag, flag[2:])] == "x assignee: someone"


@pytest.mark.parametrize("flag", ["--tags", "--requirements", "--blocks"])
@pytest.mark.parametrize(
    "hostile", ["x\nstatus: done", "x\u2028status: done", "x], status: [done", "x #y"]
)
def test_a_list_item_cannot_close_the_list_or_inject_a_key(queue, flag, hostile):
    meta = capture(queue, flag, hostile)
    assert meta["status"] == "inbox"
    listed = meta[flag.lstrip("-")]
    assert listed.startswith("[x") and listed.endswith("]") and listed.count("]") == 1


def test_a_title_with_page_text_whitespace_and_joiners_is_filed_intact(queue):
    meta = capture(
        queue, "--title", "Lite\u00a0prospect\tasks about \U0001f468\u200d\U0001f4bb setup"
    )
    assert meta["title"] == "Lite prospect asks about \U0001f468\u200d\U0001f4bb setup"


def test_a_title_naming_an_issue_number_reads_back_whole(queue):
    assert capture(queue, "--title", "Fix PR #12 flake")["title"] == "Fix PR#12 flake"

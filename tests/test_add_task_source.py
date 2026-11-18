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


@pytest.mark.parametrize("flag", ["--title", "--domain", "--due", "--effort", "--parent"])
def test_a_frontmatter_flag_cannot_inject_a_key(queue, flag):
    args = ["--title", "Answer a Lite prospect question", "--root", str(queue), "--no-sync"]
    with pytest.raises(SystemExit):
        add_task.main([*args, flag, "x\nassignee: someone"])
    assert not list((queue / "inbox").glob("*.md"))


def test_a_title_may_still_name_an_issue_number(queue):
    add_task.main(["--title", "Fix PR #12 flake", "--root", str(queue), "--no-sync"])
    assert len(list((queue / "inbox").glob("*.md"))) == 1

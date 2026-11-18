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
    add_task.main(["--title", "Answer a Lite prospect question", "--root", str(queue), "--no-sync", *extra])
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


def test_the_execution_log_names_intake_as_the_filer(queue):
    assert "captured by intake via add_task.py" in filed(queue, "--source", "intake")


def test_an_unknown_source_is_still_rejected(queue):
    with pytest.raises(SystemExit):
        filed(queue, "--source", "robot")


def test_the_queue_template_documents_source_ref(tmp_path):
    template = (init_queue.init_queue(tmp_path / "t") / "_template.md").read_text()
    assert "source_ref:" in template and "user | claude | intake" in template

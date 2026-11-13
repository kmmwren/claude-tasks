"""Tests for the `blocks:` field: add_task.py writes it, and the prioritiser weighs it as an unlock."""

import add_task
import init_queue
import prioritise
import pytest
from deps import load_queue

TITLE = ["--title", "Fix the flaky fixture", "--context", "Found while shipping the importer."]


@pytest.fixture
def queue(tmp_path):
    return init_queue.init_queue(tmp_path / "q", name="demo")


def captured_frontmatter(queue):
    (path,) = (queue / "inbox").glob("*.md")
    return path.read_text().split("---")[1]


def capture(queue, *extra):
    add_task.main([*TITLE, "--root", str(queue), "--no-sync", *extra])


def brief(bid, *, blocks="[]", blockers="[]", status="ready"):
    return f"---\nid: {bid}\nstatus: {status}\nimportance: 3\nautonomy: full\nblockers: {blockers}\nblocks: {blocks}\n---\n"


def queue_with(queue, briefs):
    for folder, bid, text in briefs:
        (queue / folder / f"{bid}.md").write_text(text)
    return queue


def test_blocks_flag_writes_the_id_into_frontmatter(queue):
    capture(queue, "--blocks", "ship-the-importer")
    assert "\nblocks: [ship-the-importer]\n" in captured_frontmatter(queue)


def test_blocks_flag_repeats_into_one_list(queue):
    capture(queue, "--blocks", "a", "--blocks", " b ", "--blocks", "")
    assert "\nblocks: [a, b]\n" in captured_frontmatter(queue)


def test_omitting_blocks_writes_an_empty_list(queue):
    capture(queue)
    assert "\nblocks: []\n" in captured_frontmatter(queue)


def test_an_open_brief_a_blocker_blocks_counts_as_an_unlock(queue):
    queue_with(queue, [("ready", "fix", brief("fix", blocks="[ship]")), ("ready", "ship", brief("ship"))])
    assert prioritise.index_queue(queue)[1] == {"fix": 1}


def test_a_done_brief_a_blocker_blocks_adds_no_unlock(queue):
    queue_with(queue, [("ready", "fix", brief("fix", blocks="[ship]")), ("done", "ship", brief("ship", status="done"))])
    assert prioritise.index_queue(queue)[1] == {}


def test_blocks_and_the_waiters_blockers_naming_the_same_pair_count_once(queue):
    queue_with(
        queue,
        [("ready", "fix", brief("fix", blocks="[ship]")), ("ready", "ship", brief("ship", blockers="[fix]"))],
    )
    assert prioritise.index_queue(queue)[1] == {"fix": 1}


def test_the_wait_graph_has_a_blocks_edge_once(queue):
    queue_with(
        queue,
        [("ready", "fix", brief("fix", blocks="[ship]")), ("ready", "ship", brief("ship", blockers="[fix]"))],
    )
    assert prioritise.WaitGraph.build(load_queue(queue)).waiters == {"fix": ("ship",)}


def test_the_wait_graph_ignores_a_blocks_edge_to_a_parked_brief(queue):
    queue_with(queue, [("ready", "fix", brief("fix", blocks="[ship]")), ("parked", "ship", brief("ship"))])
    assert prioritise.WaitGraph.build(load_queue(queue)).waiters == {}

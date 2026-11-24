"""cascade.py: a brief going done tells every brief that waited on it what landed, then releases."""

import cascade
import pytest
from deps import fm

from tests.queue_helpers import brief_text, make_queue

TODAY = "2026-06-02"


def _text(root, folder, bid):
    return (root / folder / f"{bid}.md").read_text()


def _done_with_log(root, bid, line):
    path = root / "done" / f"{bid}.md"
    path.write_text(path.read_text() + f"2026-06-02: {line}\n")


def _shipped(root, bid, line):
    (root / "facts").mkdir(exist_ok=True)
    (root / "facts" / f"{bid}.toml").write_text(f'brief = "{bid}"\nshipped = "{line}"\n')


def _waiting(root, bid, wait, body="## Goal\n\ng\n"):
    (root / "blocked" / f"{bid}.md").write_text(
        brief_text(bid, status="blocked", blocked_by=wait, body=body)
    )


@pytest.fixture
def root(tmp_path):
    return make_queue(tmp_path, {"dep": {"status": "done"}})


def test_every_dependent_is_told_what_landed_released_or_still_waiting(root):
    _shipped(root, "dep", "Records API shipped (https://github.com/o/r/pull/12)")
    _waiting(root, "freed", "[dep]")
    _waiting(root, "held", "[dep, external:vendor reply]")
    touched = cascade.cascade_done(root, "dep", [root], today=TODAY)
    assert [(t.released, sorted(t.updated)) for t in touched] == [(("freed",), ["freed", "held"])]
    for folder, bid in (("ready", "freed"), ("blocked", "held")):
        assert "landed: dep done: Records API shipped (https://github.com/o/r/pull/12)" in _text(
            root, folder, bid
        )


def test_without_facts_the_done_log_line_and_its_links_are_what_landed(root):
    _done_with_log(root, "dep", "shipped in o/r#5449 as abc1234")
    _waiting(root, "w", "[dep]")
    cascade.cascade_done(root, "dep", [root], today=TODAY)
    assert "landed: dep done: 2026-06-02: shipped in o/r#5449 as abc1234" in _text(
        root, "ready", "w"
    )


def test_a_link_only_the_done_log_carries_is_added_to_the_facts_summary(root):
    _shipped(root, "dep", "Records API shipped")
    _done_with_log(root, "dep", "merged https://github.com/o/r/pull/12")
    _waiting(root, "w", "[dep]")
    cascade.cascade_done(root, "dep", [root], today=TODAY)
    assert "Records API shipped [https://github.com/o/r/pull/12]" in _text(root, "ready", "w")


@pytest.mark.parametrize(
    "prose", ["Build Y after dep merges.", "This waits on dep.", "Do Y once #5449 lands."]
)
def test_prose_that_still_waits_on_the_done_brief_is_flagged_stale(root, prose):
    _done_with_log(root, "dep", "merged o/r#5449")
    _waiting(root, "w", "[dep]", body=f"## Goal\n\n{prose}\n")
    cascade.cascade_done(root, "dep", [root], today=TODAY)
    assert "stale prose" in _text(root, "ready", "w")


def test_prose_about_something_else_is_not_flagged(root):
    _waiting(root, "w", "[dep]", body="## Goal\n\nBuild Y after other-brief merges.\n")
    cascade.cascade_done(root, "dep", [root], today=TODAY)
    assert "stale prose" not in _text(root, "ready", "w")


def test_a_brief_that_does_not_name_the_done_brief_is_left_untouched(root):
    _waiting(root, "bystander", "[external:vendor reply]")
    before = _text(root, "blocked", "bystander")
    assert cascade.cascade_done(root, "dep", [root], today=TODAY) == []
    assert _text(root, "blocked", "bystander") == before


def test_running_the_cascade_twice_logs_each_line_once(root):
    _waiting(root, "held", "[dep, external:vendor reply]")
    for _ in range(2):
        cascade.cascade_done(root, "dep", [root], today=TODAY)
    assert _text(root, "blocked", "held").count("landed: dep done") == 1


def test_a_brief_in_another_queue_is_updated_and_released_by_its_qualified_id(tmp_path):
    alpha = make_queue(tmp_path, {"dep": {"status": "done"}}, project_name="alpha")
    beta = make_queue(tmp_path, {}, project_name="beta")
    _waiting(beta, "far", "[alpha:dep]", body="## Goal\n\nStart after alpha:dep merges.\n")
    touched = cascade.cascade_done(alpha, "dep", [alpha, beta], today=TODAY)
    assert [(t.root, t.released) for t in touched] == [(beta, ("far",))]
    text = _text(beta, "ready", "far")
    assert "landed: alpha:dep done" in text and "stale prose" in text
    assert fm(text)["blocked_by"] == "[]"


def test_a_cross_queue_waiter_stays_blocked_while_another_blocker_is_open(tmp_path):
    alpha = make_queue(tmp_path, {"dep": {"status": "done"}}, project_name="alpha")
    beta = make_queue(tmp_path, {"other": {"status": "in-progress"}}, project_name="beta")
    _waiting(beta, "far", "[alpha:dep, other]")
    touched = cascade.cascade_done(alpha, "dep", [alpha, beta], today=TODAY)
    assert [(t.released, t.updated) for t in touched] == [((), ("far",))]
    assert (beta / "blocked" / "far.md").exists()

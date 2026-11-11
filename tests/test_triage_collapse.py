"""Tests for triage_collapse.py: reversible folding of bulk filings into a parent brief."""
import pytest
from queue_helpers import inbox_text, make_inbox
from readiness import brief_missing_fields
from triage_brief import load_inbox
from triage_collapse import apply_collapse, plan_collapses, stem, title_shape


@pytest.mark.parametrize("bid, expected", [
    ("peer-memory-07b268fe", "peer-memory"), ("land-x-63-64", "land-x"), ("plain", "plain"), ("-12", "-12"),
])
def test_stem_drops_numeric_and_hash_suffixes(bid, expected):
    assert stem(bid) == expected


def test_title_shape_ignores_numbers_and_hashes():
    assert title_shape("Peer message from Memory (07b268fe)") == "Peer message from Memory"


def _peer(h, **kw):
    return inbox_text(f"peer-memory-{h}", title=f"Peer message from Memory ({h})",
                      tags="[peer]", context=f"Merged o/r#{len(h)}.", **kw)


PEERS = {f"peer-memory-{h}": _peer(h) for h in ("0a0a0a0a", "1b1b1b1b", "2c2c2c2c")}


def test_briefs_sharing_stem_tags_and_title_shape_form_one_group(tmp_path):
    root = make_inbox(tmp_path, {**PEERS, "other-1": inbox_text("other-1")})
    [plan] = plan_collapses(load_inbox(root), root)
    assert plan.parent_id == "peer-memory-batch" and len(plan.children) == 3


def test_a_group_below_the_minimum_or_already_parented_is_left(tmp_path):
    root = make_inbox(tmp_path, {"peer-memory-0a0a0a0a": _peer("0a0a0a0a"),
                                 "peer-memory-1b1b1b1b": _peer("1b1b1b1b", parent="elsewhere")})
    assert plan_collapses(load_inbox(root), root) == []


def test_a_group_whose_parent_id_is_taken_is_left(tmp_path):
    root = make_inbox(tmp_path, {**PEERS, "peer-memory-batch": inbox_text("peer-memory-batch")})
    assert plan_collapses(load_inbox(root), root) == []


def test_the_parent_is_ready_material_and_takes_the_most_important_rank(tmp_path):
    root = make_inbox(tmp_path, {**PEERS, "peer-memory-3d3d3d3d": _peer("3d3d3d3d", importance="1")})
    [plan] = plan_collapses(load_inbox(root), root)
    text = plan.parent_text("2026-11-11")
    assert brief_missing_fields(text) == []
    assert "importance: 1" in text and "title: Peer message from Memory (4 filed)" in text


def test_importance_defaults_when_no_child_has_a_number(tmp_path):
    root = make_inbox(tmp_path, {k: v.replace("importance: 3", "importance: ") for k, v in PEERS.items()})
    [plan] = plan_collapses(load_inbox(root), root)
    assert "importance: 3" in plan.parent_text("2026-11-11")


def test_apply_retires_children_to_done_with_a_pointer(tmp_path):
    root = make_inbox(tmp_path, PEERS)
    [plan] = plan_collapses(load_inbox(root), root)
    touched = apply_collapse(plan, root, "2026-11-11")
    assert touched[0] == "peer-memory-batch" and len(touched) == 4
    assert [p.name for p in (root / "inbox").glob("*.md")] == ["peer-memory-batch.md"]
    child = (root / "done" / "peer-memory-0a0a0a0a.md").read_text()
    assert "parent: peer-memory-batch" in child and "collapsed into peer-memory-batch" in child

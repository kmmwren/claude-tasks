"""Tests for triage_promote.py: the gate from inbox/ to ready/ and the write that crosses it."""
from tests.queue_helpers import inbox_text, make_inbox
from triage_brief import InboxBrief, load_inbox
from triage_enrich import Fill
from triage_promote import as_filed, enrich_all, enriched, needs_fill, promote, with_cited_context

READY = dict(goal="Ship it.", criteria=("It ships.",))


def _brief(tmp_path, **kw):
    return InboxBrief(tmp_path / "b.md", inbox_text("b", **kw))


def test_a_complete_brief_citing_a_source_promotes_as_filed(tmp_path):
    assert as_filed(_brief(tmp_path, context="Per o/r#2.", **READY)).reason == "ready as filed"


def test_a_context_without_a_source_gains_the_brief_s_own_sources(tmp_path):
    b = _brief(tmp_path, title="Fix ABC-1", context="Because.", **READY)
    assert "Sources cited in this brief: ABC-1" in with_cited_context(b).context
    assert as_filed(b) is not None


def test_cited_context_is_unchanged_when_nothing_to_add(tmp_path):
    for b in (_brief(tmp_path, context="Per o/r#2."), _brief(tmp_path, title="Fix ABC-1"),
              _brief(tmp_path, context="Because.")):
        assert with_cited_context(b) is b


def test_a_brief_with_no_source_anywhere_stays(tmp_path):
    assert as_filed(_brief(tmp_path, context="Because.", **READY)) is None


def test_a_grounded_fill_promotes_and_a_fabricated_one_does_not(tmp_path):
    b = _brief(tmp_path, title="Fix ABC-1")
    assert enriched(b, Fill("Ship.", ("Done.",), "Start at ABC-1.")) is not None
    assert enriched(b, Fill("Ship.", ("Done.",), "Start at ABC-9.")) is None


def test_only_briefs_missing_fields_with_a_source_need_a_fill(tmp_path):
    assert needs_fill(_brief(tmp_path, title="Fix ABC-1"))
    assert not needs_fill(_brief(tmp_path))
    assert not needs_fill(_brief(tmp_path, context="Per o/r#2.", **READY))


def test_enrich_all_batches_and_keeps_passing_fills(tmp_path):
    briefs = [InboxBrief(tmp_path / f"{i}.md", inbox_text(i, title=f"Fix ABC-{n}"))
              for n, i in enumerate("abc", 1)]
    calls = []

    def predict(batch):
        calls.append(len(batch))
        return {b.id: Fill("Ship.", ("Done.",), f"Start at ABC-{1 if b.id == 'a' else 9}.") for b in batch}

    assert [p.original.id for p in enrich_all(briefs, predict, size=2, jobs=2)] == ["a"]
    assert sorted(calls) == [1, 2]


def test_promote_writes_the_candidate_and_moves_it_to_ready(tmp_path):
    root = make_inbox(tmp_path, {"b": inbox_text("b", title="Fix ABC-1", context="Because.", **READY)})
    [b] = load_inbox(root)
    assert promote(as_filed(b), root, "2026-11-11")
    text = (root / "ready" / "b.md").read_text()
    assert "status: ready" in text and "promoted by inbox-triage: ready as filed" in text


def test_promote_skips_a_brief_changed_or_gone_since_it_was_read(tmp_path):
    root = make_inbox(tmp_path, {"b": inbox_text("b", context="Per o/r#2.", **READY)})
    [b] = load_inbox(root)
    p = as_filed(b)
    b.path.write_text(b.text + "\nedited\n")
    assert not promote(p, root, "2026-11-11")
    b.path.unlink()
    assert not promote(p, root, "2026-11-11")

"""deps.py — the shared dependency model."""
import deps

from tests.queue_helpers import brief_text, make_queue


def _b(**kw):
    return deps.Brief("x", "ready", deps.fm(brief_text("x", **kw)))


def test_fm_parses_frontmatter_and_ignores_missing_block():
    assert deps.fm(brief_text("x"))["id"] == "x"
    assert deps.fm("no frontmatter") == {}


def test_parse_list_handles_empty_brackets_and_spacing():
    assert deps.parse_list("[a,  b , external:vendor reply]") == ["a", "b", "external:vendor reply"]
    assert deps.parse_list("[]") == []
    assert deps.parse_list("") == []


def test_external_prefix_detection():
    assert deps.is_external("external:login")
    assert not deps.is_external("some-brief")


def test_declared_ids_cover_blocked_by_and_depends_tags_but_not_externals():
    b = _b(blocked_by="[a, external:login]", tags="[depends:c, other]")
    assert b.declared_ids == {"a", "c"}
    assert b.external_blockers == ["external:login"]


def test_legacy_tokens_are_the_words_of_blockers():
    assert _b(blockers="[a, waiting for vpn]").legacy_tokens == {"a", "waiting", "for", "vpn"}


def test_milestone_property():
    assert _b(milestone="M1").milestone == "M1"


def test_unmet_lists_open_ids_and_always_lists_externals():
    b = _b(blocked_by="[a, b, external:login]")
    assert deps.unmet(b, {"a"}) == ["b", "external:login"]
    assert deps.unmet(b, {"a", "b"}) == ["external:login"]


def test_dependency_edges_keep_only_real_ids_from_legacy_prose():
    b = _b(blockers="[a, waiting]", blocked_by="[ghost]")
    assert deps.dependency_edges(b, {"a"}) == {"a", "ghost"}


def test_load_queue_reads_every_folder_and_tolerates_a_missing_one(tmp_path):
    root = make_queue(tmp_path, {"a": {}, "b": {"status": "blocked", "blocked_by": "[a]"}})
    loaded = {b.id: b.status for b in deps.load_queue(root)}
    assert loaded == {"a": "ready", "b": "blocked"}
    import shutil
    shutil.rmtree(root / "parked")
    assert {b.id for b in deps.load_queue(root)} == {"a", "b"}

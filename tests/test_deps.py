"""deps.py — the shared dependency model."""

import datetime as dt

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


def test_load_queue_names_an_id_less_or_blank_id_brief_by_its_file_stem(tmp_path):
    (tmp_path / "ready").mkdir()
    (tmp_path / "ready" / "no-id-here.md").write_text("---\ntitle: T\n---\n")
    (tmp_path / "ready" / "blank-id.md").write_text("---\nid:\ntitle: T\n---\n")
    assert [b.id for b in deps.load_queue(tmp_path)] == ["blank-id", "no-id-here"]


def test_architecture_is_the_named_value_or_none():
    assert deps.Brief("x", "ready", {"architecture": "lite-core"}).architecture == "lite-core"
    assert deps.Brief("x", "ready", {"architecture": ""}).architecture is None
    assert deps.Brief("x", "ready", {}).architecture is None


def test_evidence_is_required_only_when_the_brief_says_required():
    assert deps.Brief("x", "ready", {"evidence": "required"}).evidence_required
    assert not deps.Brief("x", "ready", {"evidence": "optional"}).evidence_required
    assert not deps.Brief("x", "ready", {}).evidence_required


def test_a_qualified_id_parses_into_queue_and_brief_and_plain_items_do_not():
    assert deps.QueueRef.parse("work-tasks:abc") == deps.QueueRef("work-tasks", "abc")
    assert deps.QueueRef.parse("/q/tasks:abc") == deps.QueueRef("/q/tasks", "abc")
    assert str(deps.QueueRef("work-tasks", "abc")) == "work-tasks:abc"
    for plain in ("abc", "external:vendor reply", "owner/repo#12", "split", ":abc", "queue:"):
        assert deps.QueueRef.parse(plain) is None


def test_a_queue_resolves_by_path_or_by_the_short_name_of_exactly_one_known_root(tmp_path):
    alpha = make_queue(tmp_path, {}, project_name="alpha")
    beta = make_queue(tmp_path, {}, project_name="beta")
    assert deps.queue_name(alpha) == "alpha"
    assert deps.resolve_queue("beta", [alpha, beta]) == beta
    assert deps.resolve_queue(str(alpha), []) == alpha
    assert deps.resolve_queue("gamma", [alpha, beta]) is None
    assert deps.resolve_queue(str(tmp_path / "nowhere"), [alpha]) is None
    twin = make_queue(tmp_path / "again", {}, project_name="alpha")
    assert deps.resolve_queue("alpha", [alpha, twin]) is None


def test_a_remote_blocker_counts_as_done_only_when_its_brief_is_done(tmp_path):
    alpha = make_queue(tmp_path, {"dep": {"status": "done"}, "open": {}}, project_name="alpha")
    beta = make_queue(
        tmp_path,
        {
            "w1": {"status": "blocked", "blocked_by": "[alpha:dep]"},
            "w2": {"blocked_by": "[alpha:open]"},
        },
        project_name="beta",
    )
    assert deps.done_ids(beta, [alpha, beta]) == {"alpha:dep"}
    assert deps.done_ids(beta, []) == set()


def test_remote_refs_and_local_ids_split_the_declared_blockers():
    brief = _b(blocked_by="[local, alpha:far, external:vendor]")
    assert brief.remote_refs == [deps.QueueRef("alpha", "far")]
    assert brief.local_ids == {"local"}


def test_a_ref_problem_names_an_unknown_queue_and_a_missing_brief(tmp_path):
    alpha = make_queue(tmp_path, {"dep": {"status": "done"}}, project_name="alpha")
    assert deps.ref_problem(deps.QueueRef("alpha", "dep"), [alpha]) is None
    assert "no single known queue" in deps.ref_problem(deps.QueueRef("zeta", "dep"), [alpha])
    assert "no brief 'gone'" in deps.ref_problem(deps.QueueRef("alpha", "gone"), [alpha])


def test_a_date_entry_is_neither_an_id_nor_a_queue_ref():
    b = _b(blocked_by="[a, date:2026-12-01]")
    assert b.declared_ids == {"a"}
    assert b.remote_refs == []
    assert b.date_blockers == ["date:2026-12-01"]
    assert deps.QueueRef.parse("date:2026-12-01") is None


def test_a_date_dependency_is_unmet_before_its_day_and_met_on_it():
    b = _b(blocked_by="[date:2026-12-01]")
    assert deps.unmet(b, set(), today=dt.date(2026, 11, 30)) == ["date:2026-12-01"]
    assert deps.unmet(b, set(), today=dt.date(2026, 12, 1)) == []
    assert deps.awaits_release(b, set(), today=dt.date(2026, 12, 2))


def test_a_malformed_date_never_counts_as_reached():
    b = _b(blocked_by="[date:2026-13-40]")
    assert deps.date_of("date:2026-13-40") is None
    assert deps.unmet(b, set(), today=dt.date(2099, 1, 1)) == ["date:2026-13-40"]

"""scope_binding.py: a session's binding is stored beside its `root` and says what it may pull."""

import datetime
import tomllib

import _paths
import init_queue
import prioritise
import pytest
import scope_binding
from _paths import QueueBriefs
from scope import AllScope, IdsScope, ProjectScope, TagScope, parse_scope
from scope_binding import (
    Addition,
    Binding,
    BindingError,
    FixedMode,
    NoneMode,
    OngoingMode,
    parse_mode,
)
from signals import Subject

from tests.queue_helpers import make_queue

TODAY = datetime.date(2026, 6, 2)
AT = "2026-06-02T10:00:00+00:00"


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setenv(_paths.SESSIONS_ENV, str(directory))
    return directory


@pytest.fixture
def queue(tmp_path):
    return make_queue(
        tmp_path,
        {
            "a": {"tags": "[project:checkout]"},
            "b": {"tags": "[project:checkout]", "status": "blocked", "blocked_by": "[a]"},
            "c": {"tags": "[other]"},
            "d": {"tags": "[project:checkout]", "status": "done"},
            "e": {"tags": "[project:checkout]", "status": "in-progress"},
        },
    )


def _fixed(*members, additions=()):
    return Binding("project:checkout", FixedMode(), tuple(members), tuple(additions))


def test_no_file_is_no_binding(sessions):
    assert scope_binding.read("s1") is None


def test_a_file_with_only_root_is_no_binding(sessions):
    sessions.mkdir()
    (sessions / "s1.toml").write_text('root = "/q"\n')
    assert scope_binding.read("s1") is None


def test_a_written_binding_reads_back(sessions):
    binding = _fixed("a", additions=[Addition("c", "needed by a", AT)])
    scope_binding.write("s1", binding)
    assert scope_binding.read("s1") == binding


def test_writing_keeps_root_and_other_keys(sessions):
    sessions.mkdir()
    (sessions / "s1.toml").write_text('root = "/q"\nnote = "x"\n')

    scope_binding.write("s1", _fixed("a"))

    stored = tomllib.loads((sessions / "s1.toml").read_text())
    assert stored["root"] == "/q" and stored["note"] == "x"
    assert stored[scope_binding.TABLE]["members"] == ["a"]


def test_the_session_queue_still_resolves_after_binding(sessions, tmp_path):
    root = init_queue.init_queue(tmp_path / "child")
    init_queue.SessionLink("s1", tmp_path / "child").bind(root)
    scope_binding.write("s1", _fixed("a"))
    env = {_paths.SESSION_ENV: "s1", _paths.SESSIONS_ENV: str(sessions)}
    assert _paths.session_root(env) == root.resolve()


def test_binding_a_session_again_keeps_its_scope(sessions, tmp_path):
    root = init_queue.init_queue(tmp_path / "child")
    scope_binding.write("s1", _fixed("a"))
    init_queue.SessionLink("s1", tmp_path / "child").bind(root)
    assert scope_binding.read("s1") == _fixed("a")


@pytest.mark.parametrize(
    "body",
    [
        "[scope_binding\n",
        'scope_binding = "x"\n',
        '[scope_binding]\nselector = "bogus"\nmode = "none"\n',
        '[scope_binding]\nmode = "none"\n',
        '[scope_binding]\nselector = 3\nmode = "none"\nmembers = []\nadditions = []\n',
        '[scope_binding]\nselector = "all"\nmode = "none"\nmembers = "a"\nadditions = []\n',
        '[scope_binding]\nselector = "all"\nmode = "sometimes"\nmembers = []\nadditions = []\n',
        '[scope_binding]\nselector = "all"\nmode = "fixed"\nmembers = ["a b"]\nadditions = []\n',
        '[scope_binding]\nselector = "all"\nmode = "fixed"\nmembers = []\n'
        '[[scope_binding.additions]]\nslug = "a"\nreason = "r"\n',
    ],
)
def test_a_malformed_binding_raises(sessions, body):
    sessions.mkdir()
    (sessions / "s1.toml").write_text(body)
    with pytest.raises(BindingError):
        scope_binding.read("s1")


def test_a_session_id_cannot_leave_the_sessions_directory(sessions):
    with pytest.raises(ValueError, match="invalid session id"):
        scope_binding.read("../x")


def test_none_pulls_nothing():
    assert Binding("all", NoneMode()).pull_scope() is None


def test_ongoing_pulls_what_the_selector_admits():
    scope = Binding("tag:docs", OngoingMode()).pull_scope()
    assert isinstance(scope, TagScope) and scope.key == "tag:docs"


def test_fixed_pulls_its_members_and_additions():
    binding = _fixed("a", "b", additions=[Addition("c", "why", AT)])
    scope = binding.pull_scope()
    assert isinstance(scope, IdsScope) and scope.slugs == {"a", "b", "c"}


def test_an_empty_fixed_set_admits_nothing():
    scope = _fixed().pull_scope()
    assert not scope.admits(Subject({"id": "a"}, frozenset(), 0))


def test_a_fixed_binding_records_what_the_selector_admits_now(queue):
    binding = Binding.bind("project:checkout", FixedMode(), queue)
    assert binding.members == ("a", "b")


def test_a_brief_filed_later_is_not_in_a_fixed_set(queue):
    binding = Binding.bind("project:checkout", FixedMode(), queue)
    (queue / "ready" / "late.md").write_text(
        (queue / "ready" / "a.md").read_text().replace("id: a", "id: late")
    )
    assert "late" not in binding.pull_scope().slugs


def test_a_brief_without_an_id_is_known_by_its_file_name(queue):
    (queue / "ready" / "nameless.md").write_text("---\ntitle: t\ntags: [project:checkout]\n---\n")
    assert "nameless" in Binding.bind("project:checkout", FixedMode(), queue).members


def test_ongoing_and_none_record_no_members(queue):
    assert Binding.bind("all", OngoingMode(), queue).members == ()
    assert Binding.bind("all", NoneMode(), queue).members == ()


def test_a_selector_that_is_not_a_scope_is_refused(queue):
    with pytest.raises(BindingError, match="--scope must be one of"):
        Binding.bind("nonsense", NoneMode(), queue)


def test_fixed_needs_a_queue_to_resolve_from(tmp_path):
    with pytest.raises(SystemExit, match="No task queue"):
        Binding.bind("all", FixedMode(), tmp_path / "nowhere")


def test_a_fixed_set_grows_with_a_reason():
    grown = _fixed("a").added("c", "needed by a", AT)
    assert grown.additions == (Addition("c", "needed by a", AT),)
    assert grown.members == ("a",)


@pytest.mark.parametrize("mode", [OngoingMode(), NoneMode()])
def test_only_a_fixed_binding_takes_additions(mode):
    with pytest.raises(BindingError, match=f"a {mode.name} binding takes no additions"):
        Binding("all", mode).added("c", "why", AT)


@pytest.mark.parametrize("reason", ["", "   "])
def test_an_addition_needs_a_reason(reason):
    with pytest.raises(BindingError, match="needs a reason"):
        _fixed("a").added("c", reason, AT)


@pytest.mark.parametrize("slug", ["a", "c"])
def test_a_slug_already_in_the_set_is_not_added_again(slug):
    binding = _fixed("a", additions=[Addition("c", "why", AT)])
    with pytest.raises(BindingError, match="already in the set"):
        binding.added(slug, "again", AT)


@pytest.mark.parametrize("slug", ["", "a,b", "a b"])
def test_a_slug_that_would_break_the_selector_is_refused(slug):
    with pytest.raises(BindingError, match="not a brief slug"):
        _fixed("a").added(slug, "why", AT)


def test_a_mode_is_its_kind_not_an_instance():
    assert FixedMode() == parse_mode("fixed") != OngoingMode()
    assert len({FixedMode(), FixedMode(), NoneMode()}) == 2


def test_an_unknown_mode_is_refused():
    with pytest.raises(BindingError, match="mode must be one of"):
        parse_mode("sometimes")


def test_now_is_an_iso_timestamp_in_utc():
    assert datetime.datetime.fromisoformat(scope_binding.now()).utcoffset() == datetime.timedelta(0)


@pytest.mark.parametrize(
    "text, scope_type, key",
    [
        ("ids:a,b,c", IdsScope, "ids:a,b,c"),
        ("all", AllScope, "all"),
        ("project:checkout", ProjectScope, "project:checkout"),
    ],
)
def test_parse_scope_knows_ids(text, scope_type, key):
    scope = parse_scope(text)
    assert isinstance(scope, scope_type) and scope.key == key


def test_ids_needs_at_least_one_slug():
    with pytest.raises(SystemExit):
        parse_scope("ids:")


def test_ids_tolerates_spaces_and_repeats():
    assert parse_scope("ids: a, b ,,a").slugs == {"a", "b"}


QUEUE_IDS = ("a", "b", "c", "d")


@pytest.fixture
def ranked_queue(tmp_path):
    return make_queue(tmp_path, {bid: {} for bid in QUEUE_IDS})


def _ranked(root, scope):
    return sorted(
        b["id"] for b in prioritise.rank(QueueBriefs.read(root), today=TODAY, scope=scope)
    )


def test_ids_scope_ranks_only_the_named_briefs(ranked_queue):
    assert _ranked(ranked_queue, IdsScope.of(["b", "d"])) == ["b", "d"]


def test_ids_scope_ignores_a_name_that_is_not_ready(ranked_queue):
    assert _ranked(ranked_queue, IdsScope.of(["b", "ghost"])) == ["b"]


def test_an_empty_ids_scope_ranks_nothing(ranked_queue):
    assert _ranked(ranked_queue, IdsScope.of([])) == []


def test_the_whole_queue_still_ranks_without_a_scope(ranked_queue):
    assert _ranked(ranked_queue, None) == list(QUEUE_IDS)


def test_ids_scope_weighs_signals_like_the_whole_queue(tmp_path):
    root = make_queue(tmp_path, {"m": {"tags": "[multiplier]"}})
    rows = prioritise.rank(QueueBriefs.read(root), today=TODAY, scope=IdsScope.of(["m"]))
    assert rows[0]["_why"]["signals"] == {"multiplier": 1500}


def test_the_fixed_scope_reaches_the_ranking_as_the_consumer_passes_it(sessions, queue):
    scope_binding.write("s1", Binding.bind("project:checkout", FixedMode(), queue))
    scope = scope_binding.read("s1").pull_scope()
    assert _ranked(queue, scope) == ["a"]


def test_a_rebind_round_trips_through_its_table():
    replaced = _fixed("a").replaced_by(_fixed("b"), "goal grew", AT)
    assert Binding.from_table(replaced.to_table()) == replaced


def test_a_rebind_missing_a_key_is_refused():
    table = {**_fixed("a").to_table(), "rebinds": [{"selector": "all", "mode": "fixed"}]}
    with pytest.raises(BindingError, match="rebind is missing"):
        Binding.from_table(table)


def test_a_binding_written_before_rebinds_reads_as_none():
    table = _fixed("a").to_table()
    del table["rebinds"]
    assert Binding.from_table(table).rebinds == ()


def test_a_binding_without_members_or_additions_reads_as_empty():
    binding = Binding.from_table({"selector": "all", "mode": "none"})
    assert (binding.members, binding.additions, binding.rebinds) == ((), (), ())


def test_a_rebind_keeps_what_the_fixed_set_admitted():
    old = _fixed("a", additions=[Addition("b", "r", AT)])
    rebind = old.replaced_by(_fixed("c"), "goal grew", AT).rebinds[0]
    assert rebind.members == ("a", "b")


def test_a_rebind_written_before_members_reads_as_none():
    table = {"selector": "all", "mode": "fixed", "reason": "r", "at": AT}
    assert scope_binding.Rebind.from_table(table).members == ()


def test_a_brief_whose_id_is_not_a_slug_is_left_out_of_a_fixed_set(tmp_path, capsys):
    root = make_queue(
        tmp_path, {"a": {"tags": "[project:checkout]"}, "b c": {"tags": "[project:checkout]"}}
    )
    assert Binding.bind("project:checkout", FixedMode(), root).members == ("a",)
    assert "leaving out 'b c'" in capsys.readouterr().err


def test_an_in_progress_brief_is_not_claimable():
    assert "in-progress" not in scope_binding.CLAIMABLE_STATUSES
    assert set(scope_binding.CLAIMABLE_STATUSES) == {"ready", "blocked"}


def test_require_brief_refuses_a_slug_the_queue_does_not_have(queue):
    scope_binding.require_brief("all", "a", queue)
    with pytest.raises(BindingError, match="typo is not a brief"):
        scope_binding.require_brief("all", "typo", queue)

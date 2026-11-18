"""Tests for route_inbox.py: briefs move into the project that owns them, on evidence only.

Every queue and registry is a temp directory; nothing reads the real ~/work-tasks or registry.
"""

import contextlib
import json
import pathlib

import init_queue
import pytest
import route_inbox
import route_registry
import transition
from route_inbox import Matcher, Outcome, Router
from route_registry import Project

TODAY = "2026-07-01"


def _queue(path: pathlib.Path) -> pathlib.Path:
    return init_queue.init_queue(path, name=path.parent.name)


def _brief(
    root: pathlib.Path,
    bid: str,
    title: str,
    *,
    folder: str = "inbox",
    extra: str = "",
    tags: str = "[]",
    body: str = "",
) -> pathlib.Path:
    path = root / folder / f"{bid}.md"
    path.write_text(
        f"---\nid: {bid}\ntitle: {title}\nstatus: {folder}\ntype: todo\ntags: {tags}\n{extra}---\n\n"
        f"## Goal\n\ng\n\n## Context\n\n{body}\n\n## Execution log\n\n2026-06-01: captured.\n"
    )
    return path


def _snapshot(top: pathlib.Path) -> dict[str, bytes]:
    return {str(p.relative_to(top)): p.read_bytes() for p in sorted(top.rglob("*")) if p.is_file()}


class World:
    """A generic queue plus project queues and a registry that names them."""

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.top = tmp_path
        self.generic = _queue(tmp_path / "generic" / ".tasks")
        self.queues = {
            slug: _queue(tmp_path / slug / ".tasks")
            for slug in ("alpha", "beta", "gamma", "delta", "prmode")
        }
        self.registry = tmp_path / "projects.toml"
        self.registry.write_text(self._toml())

    def _toml(self) -> str:
        q = {s: self.queues[s] for s in self.queues}
        return f"""
[[project]]
slug = "alpha"
queue = "{q["alpha"]}"
names = ["Alpha Product"]
route_terms = ["alphaterm", "ALP-12"]
repos = ["org/alpha-repo"]

[[project]]
slug = "beta"
queue = "{q["beta"]}"
route_terms = ["betaterm"]
jira_keys = ["BET-7"]

[[project]]
slug = "gamma"
queue = "{q["gamma"]}"
route_terms = ["gammaterm"]
status = "parked"

[[project]]
slug = "delta"
queue = "{q["delta"]}"
route_terms = ["deltaterm"]
lifecycle = "session"

[[project]]
slug = "prmode"
queue = "{q["prmode"]}"
sync_repo = "{self.top}"
queue_mode = "pr"
route_terms = ["prterm"]

[[project]]
slug = "noqueue"
queue = "{self.top}/nowhere/.tasks"
route_terms = ["noqueueterm"]

[[project]]
slug = "old"
queue = "{self.top}/old/.tasks"
route_terms = ["oldterm"]
lifecycle = "folded"

[[project]]
slug = "incomplete"
"""

    def router(
        self, dest_root: pathlib.Path | None = None, folders: tuple[str, ...] = ("inbox",)
    ) -> Router:
        projects = route_registry.load_projects(self.registry)
        return Router(self.generic, Matcher(tuple(projects)), dest_root, folders)

    def outcomes(self, router: Router | None = None) -> dict[str, Outcome]:
        return {d.brief_id: d.outcome for d in (router or self.router()).plan()}


@pytest.fixture
def world(tmp_path: pathlib.Path) -> World:
    return World(tmp_path)


# --------------------------------------------------------------------------- #
# The moves the work asks for
# --------------------------------------------------------------------------- #
def test_one_project_match_moves_the_brief_and_leaves_no_copy(world: World) -> None:
    _brief(world.generic, "fix-it", "Tidy the alphaterm importer")
    router = world.router()
    done = router.apply(TODAY)
    assert [(d.brief_id, d.outcome, d.slug) for d in done] == [("fix-it", Outcome.ROUTED, "alpha")]
    moved = world.queues["alpha"] / "inbox" / "fix-it.md"
    assert moved.exists()
    assert not (world.generic / "inbox" / "fix-it.md").exists()
    copies = [p for p in world.top.rglob("fix-it.md")]
    assert copies == [moved]
    text = moved.read_text()
    assert "status: inbox" in text
    assert f"{TODAY}: routed from {world.generic}/inbox/fix-it.md into project alpha" in text
    log = route_inbox.RouteLog(world.generic).rows()
    assert [(r.id, r.project, r.event, r.queue) for r in log] == [
        ("fix-it", "alpha", "moved", str(world.queues["alpha"]))
    ]


def test_an_ambiguous_brief_is_reported_unrouted_and_not_moved(world: World) -> None:
    path = _brief(world.generic, "both", "alphaterm meets betaterm")
    before = path.read_bytes()
    assert world.outcomes() == {"both": Outcome.AMBIGUOUS}
    world.router().apply(TODAY)
    assert path.read_bytes() == before


def test_an_unmatched_brief_is_reported_unrouted_and_not_moved(world: World) -> None:
    path = _brief(world.generic, "stray", "Something nobody owns")
    before = path.read_bytes()
    assert world.outcomes() == {"stray": Outcome.UNMATCHED}
    world.router().apply(TODAY)
    assert path.read_bytes() == before
    assert not route_inbox.RouteLog(world.generic).path.exists()


def test_a_dry_run_writes_nothing(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    _brief(world.generic, "fix-it", "alphaterm one")
    _brief(world.generic, "other", "nothing relevant")
    before = _snapshot(world.top)
    argv = ["route", "--root", str(world.generic), "--registry", str(world.registry), "--dry-run"]
    assert route_inbox.main(argv) == 0
    assert _snapshot(world.top) == before
    body = json.loads(capsys.readouterr().out)
    assert body["dry_run"] is True
    assert body["counts"]["routed"] == {"alpha": 1}
    assert body["counts"]["unmatched"] == 1
    assert [d["id"] for d in body["decisions"]] == ["fix-it", "other"]


def test_a_move_is_reversible_and_stays_put_afterwards(world: World) -> None:
    path = _brief(world.generic, "fix-it", "alphaterm one")
    original = path.read_text()
    world.router().apply(TODAY)
    undone = route_inbox.reverse(world.generic, "fix-it", "2026-07-02")
    assert undone.event == "reversed"
    assert path.read_text().startswith(original.rstrip("\n").split("## Execution log")[0])
    assert "returned from project alpha" in path.read_text()
    assert not (world.queues["alpha"] / "inbox" / "fix-it.md").exists()
    assert [p for p in world.top.rglob("fix-it.md")] == [path]
    assert world.outcomes() == {"fix-it": Outcome.ALREADY_ROUTED}
    world.router().apply(TODAY)
    assert path.exists()


def test_an_already_routed_brief_is_untouched(world: World) -> None:
    path = _brief(world.generic, "fix-it", "alphaterm one")
    route_inbox.RouteLog(world.generic).append(
        route_inbox.Route("fix-it", "alpha", str(world.queues["alpha"]), "inbox", TODAY, "moved")
    )
    before = _snapshot(world.top)
    done = world.router().apply(TODAY)
    assert [d.outcome for d in done] == [Outcome.ALREADY_ROUTED]
    assert _snapshot(world.top) == before
    assert path.exists()


def test_an_unknown_project_name_is_never_created(world: World) -> None:
    path = _brief(world.generic, "new-thing", "Fresh idea", extra="project: ghost\n")
    before = path.read_bytes()
    dirs = {p.name for p in world.top.iterdir()}
    done = world.router().apply(TODAY)
    assert [d.outcome for d in done] == [Outcome.UNKNOWN_PROJECT]
    assert path.read_bytes() == before
    assert {p.name for p in world.top.iterdir()} == dirs
    assert not list(world.top.rglob("ghost*"))


def test_routing_twice_moves_nothing_the_second_time(world: World) -> None:
    _brief(world.generic, "fix-it", "alphaterm one")
    world.router().apply(TODAY)
    again = world.router().apply(TODAY)
    assert again == []
    assert len(route_inbox.RouteLog(world.generic).rows()) == 1


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
def test_the_project_field_routes_by_slug_or_name(world: World) -> None:
    _brief(world.generic, "by-slug", "Plain title", extra="project: beta\n")
    _brief(world.generic, "by-name", "Plain title", extra="project: alpha product\n")
    assert world.outcomes() == {"by-name": Outcome.ROUTED, "by-slug": Outcome.ROUTED}
    slugs = {d.brief_id: d.slug for d in world.router().plan()}
    assert slugs == {"by-name": "alpha", "by-slug": "beta"}


def test_a_project_tag_routes_like_the_field(world: World) -> None:
    _brief(world.generic, "tagged", "Plain title", tags="[x, project:beta]")
    assert [(d.slug, d.outcome) for d in world.router().plan()] == [("beta", Outcome.ROUTED)]


def test_two_declared_projects_are_ambiguous(world: World) -> None:
    _brief(world.generic, "two", "Plain", extra="project: alpha\n", tags="[project:beta]")
    assert world.outcomes() == {"two": Outcome.AMBIGUOUS}


def test_a_known_and_an_unknown_declared_name_never_route(world: World) -> None:
    _brief(world.generic, "mixed", "Plain", extra="project: alpha\n", tags="[project:ghost]")
    assert world.outcomes() == {"mixed": Outcome.UNKNOWN_PROJECT}


def test_a_declared_project_that_takes_no_briefs_is_not_moved(world: World) -> None:
    _brief(world.generic, "parked", "Plain", extra="project: gamma\n")
    _brief(world.generic, "session", "Plain", extra="project: delta\n")
    assert world.outcomes() == {"parked": Outcome.NOT_ACCEPTING, "session": Outcome.NOT_ACCEPTING}


def test_declared_project_beats_keywords_for_another(world: World) -> None:
    _brief(world.generic, "decl", "betaterm title", extra="project: alpha\n")
    assert [(d.slug, d.outcome) for d in world.router().plan()] == [("alpha", Outcome.ROUTED)]


def test_a_registry_jira_key_in_the_body_routes(world: World) -> None:
    _brief(world.generic, "keyed", "Plain title", body="Tracked in ALP-12 and by BET-7x.")
    _brief(world.generic, "declared-key", "Plain title", body="See BET-7 for detail.")
    _brief(world.generic, "foreign-key", "Plain title", body="See ZZZ-1 for detail.")
    got = world.outcomes()
    assert got == {
        "declared-key": Outcome.ROUTED,
        "foreign-key": Outcome.UNMATCHED,
        "keyed": Outcome.ROUTED,
    }
    assert {d.brief_id: d.slug for d in world.router().plan() if d.slug} == {
        "declared-key": "beta",
        "keyed": "alpha",
    }


def test_two_projects_keys_in_one_brief_are_ambiguous(world: World) -> None:
    _brief(world.generic, "both", "Plain title", body="ALP-12 and BET-7")
    assert world.outcomes() == {"both": Outcome.AMBIGUOUS}


def test_requires_repo_that_a_project_owns_routes(world: World) -> None:
    _brief(world.generic, "repo", "Plain title", extra="requires-repo: Org/Alpha-Repo\n")
    _brief(world.generic, "other-repo", "Plain title", extra="requires-repo: org/else\n")
    assert world.outcomes() == {"other-repo": Outcome.UNMATCHED, "repo": Outcome.ROUTED}


def test_keywords_count_in_the_title_id_and_tags_but_not_the_body(world: World) -> None:
    _brief(world.generic, "in-title", "The alphaterm thing")
    _brief(world.generic, "betaterm-in-id", "Plain")
    _brief(world.generic, "in-tags", "Plain", tags="[Alpha Product]")
    _brief(world.generic, "in-body", "Plain", body="this mentions alphaterm only in passing")
    _brief(world.generic, "partial-word", "Plain alphatermite")
    assert world.outcomes() == {
        "betaterm-in-id": Outcome.ROUTED,
        "in-body": Outcome.UNMATCHED,
        "in-tags": Outcome.ROUTED,
        "in-title": Outcome.ROUTED,
        "partial-word": Outcome.UNMATCHED,
    }


def test_keywords_of_a_parked_project_never_route(world: World) -> None:
    _brief(world.generic, "quiet", "gammaterm work")
    assert world.outcomes() == {"quiet": Outcome.UNMATCHED}


def test_decisions_name_the_terms_that_matched(world: World) -> None:
    _brief(world.generic, "fix-it", "alphaterm and Alpha Product")
    (decision,) = world.router().plan()
    assert decision.terms == ("Alpha Product", "alpha", "alphaterm")
    assert decision.row()["terms"] == ["Alpha Product", "alpha", "alphaterm"]


# --------------------------------------------------------------------------- #
# Where a matched brief can go
# --------------------------------------------------------------------------- #
def test_a_pr_mode_project_is_reported_and_never_written_without_a_dest_root(world: World) -> None:
    path = _brief(world.generic, "pr", "prterm work")
    before = _snapshot(world.top)
    assert world.outcomes() == {"pr": Outcome.HELD_PR_MODE}
    world.router().apply(TODAY)
    assert _snapshot(world.top) == before
    assert path.exists()


def test_a_dest_root_worktree_takes_a_pr_mode_brief(world: World, tmp_path: pathlib.Path) -> None:
    worktree = tmp_path / "wt"
    mirrored = worktree / world.queues["prmode"].relative_to(world.top)
    _queue_at = init_queue.init_queue(mirrored, name="prmode")
    _brief(world.generic, "pr", "prterm work")
    router = world.router(dest_root=worktree)
    assert world.outcomes(router) == {"pr": Outcome.ROUTED}
    router.apply(TODAY)
    assert (_queue_at / "inbox" / "pr.md").exists()
    assert not (world.queues["prmode"] / "inbox" / "pr.md").exists()
    row = route_inbox.RouteLog(world.generic).rows()[0]
    assert row.queue == str(_queue_at)


def test_a_dest_root_does_not_remap_a_project_without_a_sync_repo(world: World) -> None:
    _brief(world.generic, "fix-it", "alphaterm one")
    router = world.router(dest_root=world.top / "elsewhere")
    assert world.outcomes(router) == {"fix-it": Outcome.ROUTED}
    router.apply(TODAY)
    assert (world.queues["alpha"] / "inbox" / "fix-it.md").exists()


def test_a_project_without_an_initialised_queue_is_held_not_created(world: World) -> None:
    _brief(world.generic, "nq", "noqueueterm work")
    assert world.outcomes() == {"nq": Outcome.HELD_NO_QUEUE}
    world.router().apply(TODAY)
    assert not (world.top / "nowhere").exists()


def test_an_id_already_in_the_project_queue_is_a_conflict_not_an_overwrite(world: World) -> None:
    path = _brief(world.generic, "dup", "alphaterm one")
    existing = _brief(world.queues["alpha"], "dup", "alphaterm theirs", folder="done")
    before = (path.read_bytes(), existing.read_bytes())
    assert world.outcomes() == {"dup": Outcome.HELD_CONFLICT}
    world.router().apply(TODAY)
    assert (path.read_bytes(), existing.read_bytes()) == before


def test_a_brief_claimed_after_planning_is_reported_as_a_conflict(world: World) -> None:
    path = _brief(world.generic, "fix-it", "alphaterm one")
    router = world.router()
    (decision,) = router.plan()
    path.unlink()
    assert router.move(decision, TODAY).outcome is Outcome.HELD_CONFLICT
    assert not route_inbox.RouteLog(world.generic).path.exists()


def test_the_ready_folder_routes_into_the_project_inbox_and_returns_there(world: World) -> None:
    ready = _brief(world.generic, "r", "alphaterm ready", folder="ready")
    _brief(world.generic, "i", "alphaterm inbox")
    assert world.outcomes() == {"i": Outcome.ROUTED}
    router = world.router(folders=("inbox", "ready"))
    router.apply(TODAY)
    assert (world.queues["alpha"] / "inbox" / "r.md").exists()
    assert not ready.exists()
    route_inbox.reverse(world.generic, "r", TODAY)
    assert ready.exists()


# --------------------------------------------------------------------------- #
# Reverse
# --------------------------------------------------------------------------- #
def test_reverse_refuses_a_brief_that_was_never_routed(world: World) -> None:
    with pytest.raises(ValueError, match="no route to reverse"):
        route_inbox.reverse(world.generic, "nope", TODAY)


def test_reverse_refuses_a_brief_already_reversed(world: World) -> None:
    _brief(world.generic, "fix-it", "alphaterm one")
    world.router().apply(TODAY)
    route_inbox.reverse(world.generic, "fix-it", TODAY)
    with pytest.raises(ValueError, match="no route to reverse"):
        route_inbox.reverse(world.generic, "fix-it", TODAY)


def test_reverse_refuses_once_the_brief_has_left_the_project_inbox(world: World) -> None:
    _brief(world.generic, "fix-it", "alphaterm one")
    world.router().apply(TODAY)
    moved = world.queues["alpha"] / "inbox" / "fix-it.md"
    moved.rename(world.queues["alpha"] / "done" / "fix-it.md")
    with pytest.raises(ValueError, match="claimed by another actor"):
        route_inbox.reverse(world.generic, "fix-it", TODAY)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _route_argv(world: World, *extra: str) -> list[str]:
    return ["route", "--root", str(world.generic), "--registry", str(world.registry), *extra]


def test_route_prints_counts_and_moves(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    _brief(world.generic, "fix-it", "alphaterm one")
    _brief(world.generic, "stray", "unowned")
    _brief(world.generic, "both", "alphaterm betaterm")
    _brief(world.generic, "ghost", "x", extra="project: ghost\n")
    _brief(world.generic, "pr", "prterm")
    assert route_inbox.main(_route_argv(world, "--counts-only", "--today", TODAY)) == 0
    body = json.loads(capsys.readouterr().out)
    assert "decisions" not in body
    assert body["counts"] == {
        "routed": {"alpha": 1},
        "matched": {"alpha": 1, "prmode": 1},
        "ambiguous": 1,
        "unmatched": 1,
        "unknown_project": 1,
        "not_accepting": 0,
        "already_routed": 0,
        "held_pr_mode": 1,
        "held_no_queue": 0,
        "held_conflict": 0,
    }
    assert (world.queues["alpha"] / "inbox" / "fix-it.md").exists()


def test_route_status_flag_scans_the_named_folders(world: World) -> None:
    _brief(world.generic, "r", "alphaterm", folder="ready")
    assert route_inbox.main(_route_argv(world, "--status", "ready", "--today", TODAY)) == 0
    assert (world.queues["alpha"] / "inbox" / "r.md").exists()


def test_route_sync_commits_the_moved_ids_and_the_log(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, pathlib.Path, list[str], list[str]]] = []
    monkeypatch.setattr(
        route_inbox, "sync_commit", lambda m, root, ids, paths: calls.append((m, root, ids, paths))
    )
    _brief(world.generic, "fix-it", "alphaterm one")
    assert route_inbox.main(_route_argv(world, "--sync")) == 0
    assert calls == [
        ("route_inbox: 1 brief(s) into projects", world.generic, ["fix-it"], [route_inbox.LOG_NAME])
    ]


def test_route_sync_commits_nothing_when_nothing_moved_or_on_a_dry_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(route_inbox, "sync_commit", lambda *a: pytest.fail("committed"))
    assert route_inbox.main(_route_argv(world, "--sync")) == 0
    _brief(world.generic, "fix-it", "alphaterm one")
    assert route_inbox.main(_route_argv(world, "--sync", "--dry-run")) == 0


def test_route_reports_an_unreadable_registry(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = ["route", "--root", str(world.generic), "--registry", str(world.top / "none.toml")]
    assert route_inbox.main(missing) == 2
    world.registry.write_text("[[project")
    assert route_inbox.main(_route_argv(world)) == 2
    assert capsys.readouterr().err.count("cannot read the project registry") == 2


def test_the_root_defaults_to_the_resolved_queue(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(world.generic))
    _brief(world.generic, "fix-it", "alphaterm one")
    assert route_inbox.main(["route", "--registry", str(world.registry), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["routed"] == {"alpha": 1}


def test_reverse_command_returns_the_brief_and_can_sync(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, list[str], list[str]]] = []
    monkeypatch.setattr(
        route_inbox, "sync_commit", lambda m, root, ids, paths: calls.append((m, ids, paths))
    )
    _brief(world.generic, "fix-it", "alphaterm one")
    world.router().apply(TODAY)
    assert route_inbox.main(["reverse", "fix-it", "--root", str(world.generic), "--sync"]) == 0
    assert "fix-it returned from alpha" in capsys.readouterr().out
    assert (world.generic / "inbox" / "fix-it.md").exists()
    assert calls == [("route_inbox: fix-it returned", ["fix-it"], [route_inbox.LOG_NAME])]


def test_reverse_command_reports_a_brief_it_cannot_return(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert route_inbox.main(["reverse", "nope", "--root", str(world.generic)]) == 1
    assert "no route to reverse" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_the_registry_skips_folded_and_incomplete_rows(world: World) -> None:
    slugs = [p.slug for p in route_registry.load_projects(world.registry)]
    assert slugs == ["alpha", "beta", "gamma", "delta", "prmode", "noqueue"]


def test_the_registry_path_honours_the_override_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(route_registry.REGISTRY_ENV, "~/from-env.toml")
    assert route_registry.registry_path().name == "from-env.toml"
    assert route_registry.registry_path("/x/explicit.toml") == pathlib.Path("/x/explicit.toml")
    monkeypatch.delenv(route_registry.REGISTRY_ENV)
    assert route_registry.registry_path().name == "projects.toml"


def test_a_project_owns_the_keys_its_names_and_terms_look_like() -> None:
    p = Project(
        "p",
        pathlib.Path("/q"),
        names=("P", "PRJ-1"),
        route_terms=("word", "prj-2", "PRJ-3"),
        jira_keys=("prj-9",),
    )
    assert p.keys == {"PRJ-1", "PRJ-3", "PRJ-9"}
    assert p.keywords == ("p", "P", "word", "prj-2")
    assert p.takes_briefs
    assert not Project("p", pathlib.Path("/q"), status="parked").takes_briefs
    assert not p.owns_repo("")


# --------------------------------------------------------------------------- #
# relocate_brief
# --------------------------------------------------------------------------- #
def test_relocate_refuses_a_destination_that_is_not_a_queue(
    world: World, tmp_path: pathlib.Path
) -> None:
    path = _brief(world.generic, "fix-it", "alphaterm one")
    with pytest.raises(ValueError, match="not an initialised queue"):
        transition.relocate_brief(
            "fix-it", world.generic, tmp_path / "plain", "inbox", "inbox", "moved", TODAY
        )
    assert path.exists()
    assert not (tmp_path / "plain").exists()


def test_relocate_refuses_to_overwrite_an_id_the_destination_holds(world: World) -> None:
    path = _brief(world.generic, "dup", "alphaterm one")
    theirs = _brief(world.queues["alpha"], "dup", "theirs", folder="parked")
    before = theirs.read_bytes()
    with pytest.raises(ValueError, match="already exists in .*/parked"):
        transition.relocate_brief(
            "dup", world.generic, world.queues["alpha"], "inbox", "inbox", "moved", TODAY
        )
    assert path.exists()
    assert theirs.read_bytes() == before


def test_relocate_defaults_the_date_and_stamps_it(world: World) -> None:
    _brief(world.generic, "fix-it", "alphaterm one")
    new = transition.relocate_brief(
        "fix-it", world.generic, world.queues["beta"], "inbox", "ready", "moved"
    )
    text = new.read_text()
    assert "status: ready" in text
    assert text.rstrip().endswith("moved")


def test_relocate_leaves_the_brief_when_the_lock_times_out(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _brief(world.generic, "fix-it", "alphaterm one")
    monkeypatch.setattr(transition, "repo_lock", lambda *a, **k: contextlib.nullcontext(False))
    with pytest.raises(RuntimeError, match="NOT moved"):
        transition.relocate_brief(
            "fix-it", world.generic, world.queues["alpha"], "inbox", "inbox", "moved", TODAY
        )
    assert path.exists()
    assert not (world.queues["alpha"] / "inbox" / "fix-it.md").exists()

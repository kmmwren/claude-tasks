"""start_prompt prints a ranked session-start prompt for a slice, or the menu of slices."""

import collections
import pathlib
import subprocess

import pytest
import start_prompt as sp

from tests.queue_helpers import make_queue

WEB_MILESTONES = """
[milestone.web-m1]
title = "One"

[milestone.web-m2]
title = "Two"

[milestone.web-m10]
title = "Ten"
"""


@pytest.fixture
def web(tmp_path) -> sp.Queue:
    briefs = {
        "cart": {"milestone": "web-m1", "tags": "[ux, memory]", "importance": "1"},
        "login": {"milestone": "web-m2", "tags": "[ux]"},
        "audit": {"milestone": "web-m2", "tags": "[ops]", "status": "in-progress"},
        "stuck": {"milestone": "web-m1", "tags": "[ux]", "status": "blocked"},
    }
    return sp.Queue(make_queue(tmp_path, briefs, WEB_MILESTONES, project_name="web-shop"))


@pytest.fixture
def search(tmp_path) -> sp.Queue:
    briefs = {"index": {"tags": "[memory]", "importance": "2"}, "facet": {"tags": "[ux]"}}
    return sp.Queue(make_queue(tmp_path, briefs, project_name="search-platform"))


@pytest.fixture
def idle(tmp_path) -> sp.Queue:
    return sp.Queue(make_queue(tmp_path, {}, project_name="docs"))


def git(repo: pathlib.Path, *args: str) -> str:
    flags = ("-c", "user.email=t@t.t", "-c", "user.name=t", "-c", "commit.gpgsign=false")
    done = subprocess.run(["git", *flags, "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_one_of_takes_the_first_test_with_a_single_hit():
    words = ["alpha", "alps", "beta"]
    assert sp.one_of("alps", "word", words, str, lambda w: w == "alps") == "alps"
    assert (
        sp.one_of("be", "word", words, str, lambda w: w == "be", lambda w: w[:2] == "be") == "beta"
    )


def test_one_of_names_the_choices_when_none_or_several_match():
    with pytest.raises(SystemExit, match="'al' matches alpha, alps: name one"):
        sp.one_of("al", "word", ["alpha", "alps"], str, lambda w: w.startswith("al"))
    with pytest.raises(SystemExit, match="no word 'z'; choose from alpha"):
        sp.one_of("z", "word", ["alpha"], str, lambda w: False)
    with pytest.raises(SystemExit, match=r"choose from \(none\)"):
        sp.one_of("z", "word", [], str, lambda w: True)


def test_split_and_counted_and_natural():
    assert sp.split(" m1, ,m2 ") == ("m1", "m2")
    assert sp.counted([("ux", 3), ("ops", 2)]) == "ux 3 · ops 2"
    assert sorted(["m10", "M2", "m1"], key=sp.natural) == ["m1", "M2", "m10"]


def test_milestones_answer_to_exact_case_and_short_forms():
    ms = sp.Milestones(("web-m1", "web-m2", "WEB-M3"))
    assert ms.resolve("web-m2") == "web-m2"
    assert ms.resolve("web-m3") == "WEB-M3"
    assert ms.resolve("M1") == "web-m1"
    with pytest.raises(SystemExit, match="no milestone 'm9'"):
        ms.resolve("m9")


def test_a_short_form_is_shown_only_when_it_names_one_milestone():
    ms = sp.Milestones(("web-m1", "api-m1", "web-m2"))
    assert ms.short("web-m2") == "m2"
    assert ms.short("web-m1") == "web-m1"


def test_milestones_order_by_file_then_naturally():
    ms = sp.Milestones(("web-m2", "web-m1"))
    counts = collections.Counter({"web-m1": 1, "x-m10": 1, "x-m9": 1, "web-m2": 3})
    assert [m for m, _ in ms.ordered(counts)] == ["web-m2", "web-m1", "x-m9", "x-m10"]


def test_a_project_queue_is_named_for_its_folder_and_answers_to_short_forms(search, tmp_path):
    assert search.name == "search-platform"
    assert search.aliases == {"search-platform", "search", "platform", "sp"}
    assert search.answers_to("sp") and search.answers_to("search-pl")
    assert not search.answers_to("web")
    assert sp.Queue(tmp_path / "work-tasks").name == "work-tasks"


def test_an_unreadable_milestones_file_warns_and_offers_none(tmp_path, capsys):
    queue = sp.Queue(make_queue(tmp_path, {}, "not = [toml", project_name="web-shop"))
    assert queue.milestones.ids == ()
    assert "cannot read milestones file" in capsys.readouterr().err


def test_a_queue_outside_git_has_no_checkout(web):
    assert web.checkout is None


def test_a_checkout_behind_origin_is_warned_once_for_all_its_queues(tmp_path, idle, capsys):
    seed, bare = tmp_path / "seed", tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "-b", "trunk", str(seed))
    git(seed, "commit", "-q", "--allow-empty", "-m", "c")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    for clone in (mine, theirs):
        git(tmp_path, "clone", "-q", str(bare), str(clone))
    for n in range(2):
        git(theirs, "commit", "-q", "--allow-empty", "-m", f"ahead {n}")
    git(theirs, "push", "-q", "origin", "trunk")
    git(mine, "fetch", "-q")
    queues = [
        sp.Queue(make_queue(mine, {}, project_name=n)) for n in ("web-shop", "search-platform")
    ]

    sp.warn_stale([*queues, idle])

    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1
    assert "is 2 commits behind origin; pull it to rank web-shop, search-platform" in err[0]
    git(mine, "pull", "-q")
    sp.warn_stale(queues)
    assert capsys.readouterr().err == ""


def test_a_checkout_with_no_default_branch_or_no_origin_counts_as_current(tmp_path):
    plain, fresh = tmp_path / "plain", tmp_path / "fresh"
    plain.mkdir()
    git(tmp_path, "init", "-q", str(fresh))
    assert sp.Checkout(plain).commits_behind() == 0
    assert sp.Checkout(fresh).commits_behind() == 0


def test_discover_lists_the_current_queue_then_the_listed_initialised_ones(
    web, search, tmp_path, monkeypatch
):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(web.root))
    monkeypatch.setenv("CLAUDE_TASKS_ROOTS", f"{search.root}:{tmp_path}")
    assert [q.name for q in sp.discover()] == ["web-shop", "search-platform"]


def test_a_pick_line_carries_the_command_title_queue_and_brief_path(web):
    pick = sp.Slice((web,)).picks()[0]
    assert pick.line() == (
        "- /claude-tasks:action-task cart  (Title of cart; web-shop; "
        f"brief {web.root / 'ready' / 'cart.md'})"
    )


def test_one_queue_weighs_as_a_project_and_several_as_the_whole_queue(web, search):
    assert sp.Slice((web,), ("web-m1",), ("ux",)).scope.name == "project"
    assert sp.Slice((web,), ("web-m1",), ("ux",)).scope.key == "milestone:web-m1+tag:ux"
    assert sp.Slice((web, search)).scope.name == "all"


def test_a_slice_describes_itself():
    one = sp.Slice((sp.Queue(pathlib.Path("/q/web-shop/.tasks")),), ("web-m1", "web-m2"), ("ux",))
    assert one.describe() == "web-shop, milestone web-m1 or web-m2, tagged ux"
    assert sp.Slice((one.queues[0], one.queues[0])).describe() == "every queue"


def test_picks_interleave_queues_by_score_and_respect_the_slice(web, search):
    picks = sp.Slice((web, search), tags=("ux",)).picks()
    assert {p.row["id"] for p in picks} == {"cart", "login", "facet"}
    scores = [p.score for p in picks]
    assert scores == sorted(scores, reverse=True)


def test_waiting_counts_in_scope_briefs_held_elsewhere(web):
    assert sp.Slice((web,)).waiting() == {"in-progress": 1, "blocked": 1}
    assert sp.Slice((web,), ("web-m2",)).waiting() == {"in-progress": 1}


def test_choose_resolves_a_queue_by_name_or_short_form_and_its_milestones(web, search):
    assert sp.choose([web, search], "sp", (), ()).queues == (search,)
    assert sp.choose([web, search], "web-shop", ("m2",), ()).milestones == ("web-m2",)
    assert sp.choose([web, search], "all", (), ("ux",)).queues == (web, search)


def test_an_unknown_queue_names_the_queues_on_offer(web, search):
    with pytest.raises(
        SystemExit, match="no queue 'billing'; choose from web-shop, search-platform"
    ):
        sp.choose([web, search], "billing", (), ())


def test_a_milestone_needs_one_project(web, search):
    with pytest.raises(SystemExit, match="a milestone belongs to one project"):
        sp.choose([web, search], "all", ("m1",), ())


def test_render_lists_picks_and_what_is_held_elsewhere(web):
    chosen = sp.Slice((web,), ("web-m1",))
    text = sp.render(chosen, chosen.picks(), chosen.waiting())
    assert text.startswith("Start a session on web-shop, milestone web-m1: 1 ready briefs")
    assert "/claude-tasks:handoff" in text
    assert "/claude-tasks:action-task cart" in text
    assert text.endswith("Not listed, held elsewhere: 1 blocked.\n")


def test_an_empty_slice_points_at_the_menu(web):
    text = sp.render(sp.Slice((web,), tags=("none",)), [], collections.Counter())
    assert "nothing ready in this slice: run /claude-tasks:start for the menu" in text
    assert "held elsewhere" not in text


def test_the_menu_offers_each_queue_its_milestones_and_shared_tags(web, search, idle):
    text = sp.menu([web, search, idle])
    assert "web-shop: 2 ready\n  /claude-tasks:start web-shop\n" in text
    assert "  milestone  web-m1 1 · web-m2 1\n" in text
    assert "/claude-tasks:start web-shop --milestone m1" in text
    assert "  tag        ux 2\n" in text and "memory 1" not in text.split("search-platform")[0]
    assert "all: 4 ready" in text and "/claude-tasks:start all --tag ux" in text
    assert "--milestone m1,m2" in text
    assert text.endswith("Nothing ready in: docs.\n")


def test_a_shelf_without_milestones_or_shared_tags_offers_only_its_command(search):
    shelf = sp.Shelf("search-platform", tuple(search.briefs("ready")), search.milestones)
    assert shelf.lines() == ["search-platform: 2 ready", "  /claude-tasks:start search-platform"]


def test_main_prints_the_menu_with_no_slice(web, monkeypatch, capsys):
    monkeypatch.setattr(sp, "discover", lambda: [web])
    assert sp.main([]) == 0
    assert capsys.readouterr().out.startswith("Start a session on any of these slices")


def test_main_takes_a_tag_alone_across_every_queue(web, search, monkeypatch, capsys):
    monkeypatch.setattr(sp, "discover", lambda: [web, search])
    assert sp.main(["--tag", "memory"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Start a session on every queue, tagged memory: 2 ready briefs")

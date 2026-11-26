"""The three roll-up views in three formats, from one queue that exercises every section."""

import dataclasses
import datetime as dt
import json
import os
import pathlib
from collections.abc import Callable, Mapping

import pytest
import rollup_model as rm
import rollup_views as rv

from tests.remote_queue import publish, snapshot

NOW = dt.datetime(2026, 11, 19, 12, 0, tzinfo=dt.timezone.utc)
GOLDEN = pathlib.Path(__file__).parent / "golden" / "rollup"
UPDATE_GOLDEN = "UPDATE_GOLDEN"
ALPHA, BETA, GONE, IDLE = "alpha-source", "beta-source", "gone-source", "idle-source"


def brief(
    id: str,
    folder: str,
    *,
    kind: str = "feature",
    milestone: str = "",
    effort: str = "",
    due: str = "",
    updated: str = "2026-11-18",
    unticked: int = 0,
    blocked_by: str = "",
    requirements: str = "",
) -> str:
    optional = {
        "milestone": milestone,
        "estimated-effort": effort,
        "due": due,
        "blocked_by": blocked_by,
        "requirements": requirements,
    }
    extra = "".join(f"{key}: {value}\n" for key, value in optional.items() if value)
    criteria = "- [x] done\n" + "- [ ] open\n" * unticked
    return (
        f"---\nid: {id}\ntitle: {id.upper()} title\nstatus: {folder}\ntype: {kind}\n"
        f"domain: demo\nupdated: {updated}\n{extra}---\n\n# {id}\n\n## Success criteria\n{criteria}"
    )


ATLAS = """initiative = "Platform"

[project]
slug = "atlas"
title = "Atlas"
outcome = "Teams see what shipped."

[health]
colour = "green"
reason = "On course."
set_by = "Owner"
set_on = "2026-11-02"

[links]
docs = "https://example.test/docs"

[[releases]]
id = "r1"
date = "2026-11-01"

[[releases]]
id = "r2"
date = "2026-12-01"

[ask]
question = "Ship it?"
deadline = "2026-11-25"

[[milestones]]
id = "m1"
title = "Shipped"
plain = "Shipping is done"
due = "2026-11-05"
release = "r1"

[[milestones]]
id = "m2"
title = "Active"
plain = "The active thing"
due = "2026-11-10"

[[milestones]]
id = "m3"
title = "Later"

[[milestones]]
id = "m4"
title = "Empty"
"""

FLAGS = """brief = "b3"

[[flag]]
name = "x"
environment = "prod"
state = "off"
changed_on = "2026-11-15"

[[flag]]
name = "x"
environment = "dev"
state = "on"
changed_on = "2026-11-11"

[[evidence]]
kind = "screenshot"
link = "https://example.test/shot.png"
caption = "The page"
access = "public"
recorded_on = "2026-11-14"
"""

ORG_VIDEO = """brief = "b4"

[[evidence]]
kind = "video"
link = "https://example.test/clip"
caption = "The demo"
access = "org"
recorded_on = "2026-11-15"
"""

PUBLIC_VIDEO = """brief = "b6"

[[evidence]]
kind = "video"
link = "https://example.test/clip.mp4"
caption = "Walkthrough"
access = "public"
recorded_on = "2026-11-16"
"""

REGISTER = """# Requirements register: Atlas

<!-- requirements-register v1; prefix: ATL; ids are permanent, never renumber. -->

| ID | Class | Words | Briefs |
|---|---|---|---|
| ATL-R001 | capability | "Covered and done" | b1 |
| ATL-R002 | capability | "Covered but open" | b3 |
| ATL-R003 | capability | "Nobody covers this" |  |
"""

ATLAS_FILES = {
    "tasks.toml": 'name = "Atlas"\n',
    "project.toml": ATLAS,
    "requirements.md": REGISTER,
    "done/b1.md": brief(
        "b1", "done", milestone="m1", updated="2026-11-12", requirements="[ATL-R001]"
    ),
    "done/b2.md": brief("b2", "done", milestone="m1", updated="2026-10-01"),
    "in-progress/b3.md": brief(
        "b3",
        "in-progress",
        milestone="m2",
        effort="m",
        unticked=1,
        blocked_by="[b4]",
        requirements="[ATL-R002]",
    ),
    "ready/b4.md": brief("b4", "ready", milestone="m2", due="2026-11-12", unticked=1),
    "ready/b5.md": brief("b5", "ready", milestone="m3", effort="l", due="2026-11-25", unticked=1),
    "done/b6.md": brief("b6", "done", milestone="m2", effort="s", unticked=1),
    "done/b7.md": brief("b7", "done"),
    "done/b8.md": brief("b8", "done", updated="2026-11-17"),
    "ready/b9.md": brief("b9", "ready", kind="bug", unticked=1),
    "ready/b10.md": brief("b10", "ready", due="2027-01-15", unticked=1, blocked_by="[b5]"),
    "parked/b12.md": brief("b12", "parked", milestone="m3", effort="s", unticked=1),
    "facts/b1.toml": 'brief = "b1"\nrelease = "r1"\nshipped = "Launched"\nshipped_on = "2026-11-13"\n',
    "facts/b7.toml": 'brief = "b7"\nrelease = "r9"\n',
    "facts/b3.toml": FLAGS,
    "facts/b4.toml": ORG_VIDEO,
    "facts/b6.toml": PUBLIC_VIDEO,
    "history/due/m2-2026-11-12-aaaaaaaa.toml": (
        'milestone = "m2"\ndue = "2026-11-30"\nset_on = "2026-11-12"\nreason = "Scope grew."\n'
    ),
    "history/ci/2026-11-18-bbbbbbbb.toml": (
        'state = "red"\nlink = "https://example.test/ci/2"\nset_on = "2026-11-18"\n'
    ),
}


def small_project(slug: str, initiative: str, health: str) -> str:
    head = f'initiative = "{initiative}"\n' if initiative else ""
    return (
        f'{head}\n[project]\nslug = "{slug}"\ntitle = "{slug.title()}"\n'
        f'outcome = "{slug.title()} works."\n\n{health}\n'
        '[[milestones]]\nid = "m1"\ntitle = "Only"\n'
    )


FRESH = '[health]\ncolour = "green"\nreason = "Fine."\nset_by = "Owner"\nset_on = "2026-11-18"\n'
# A queue as git keeps it: the empty ready/ directory is not there.
BETA_FILES = {
    "tasks.toml": 'name = "Beta"\n',
    "project.toml": small_project("beta", "", FRESH),
    "done/b1.md": brief("b1", "done", milestone="m1"),
}
GAMMA_FILES = {
    "tasks.toml": 'name = "Gamma"\n',
    "project.toml": small_project("gamma", "Growth", ""),
    "ready/g1.md": brief("g1", "ready", milestone="m1", unticked=1),
}


def writer(files: Mapping[str, str]) -> Callable[[pathlib.Path], None]:
    def write(clone: pathlib.Path) -> None:
        for relative, text in files.items():
            path = clone / ".tasks" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    return write


def entry(tmp_path: pathlib.Path, label: str, files: Mapping[str, str]) -> rv.Entry:
    tmp_path.mkdir(parents=True, exist_ok=True)
    snap = snapshot(publish(tmp_path, writer(files)), tmp_path / "cache")
    return rv.Entry.read(label, snap, NOW)


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> rv.Report:
    queues = ((ALPHA, ATLAS_FILES), (BETA, BETA_FILES), (BETA, GAMMA_FILES))
    entries = tuple(
        entry(tmp_path_factory.mktemp(f"q{n}"), label, files)
        for n, (label, files) in enumerate(queues)
    )
    return rv.Report(
        NOW,
        (ALPHA, BETA, IDLE),
        entries,
        omitted=(
            rv.Omission(GONE, "", "unreachable"),
            rv.Omission(GONE, ".tasks", "no project.toml"),
        ),
        notes=(rv.SourceNote(GONE, "Showing a cached copy."),),
    )


@pytest.fixture(scope="module")
def atlas(report: rv.Report) -> rv.Entry:
    return report.entries[0]


# --- golden files: one per view and format --------------------------------------------------


def normalised(text: str, report: rv.Report) -> str:
    """Commit ids change with the code a test repo copies in, so each one gets a stable name."""
    for n, sha in enumerate(dict.fromkeys(e.project.as_at.sha for e in report.entries), 1):
        text = text.replace(sha, f"FULLSHA{n}").replace(sha[:7], f"SHA{n:04d}")
    return text


def golden_name(view: str, fmt: rv.Format) -> str:
    return f"{view}.{fmt.name}"


@pytest.mark.parametrize("fmt", rv.FORMATS.values(), ids=lambda f: f.name)
@pytest.mark.parametrize("view", rv.VIEWS.values(), ids=lambda v: v.name)
def test_each_view_matches_its_golden_file_in_each_format(
    view: rv.View, fmt: rv.Format, report: rv.Report
) -> None:
    got = normalised(fmt.render(view, report), report)
    path = GOLDEN / golden_name(view.name, fmt)
    if os.environ.get(UPDATE_GOLDEN):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(got)
    assert got == path.read_text()


def test_no_golden_file_is_left_over_from_a_view_or_format_that_is_gone() -> None:
    expected = {golden_name(v, f) for v in rv.VIEWS for f in rv.FORMATS.values()}
    assert {p.name for p in GOLDEN.iterdir()} == expected


# --- what each view says --------------------------------------------------------------------


def test_ready_briefs_list_what_is_due_within_the_fortnight_first_then_the_rest_in_rank_order(
    atlas: rv.Entry,
) -> None:
    work = atlas.board.work
    assert [b.id for b in work[:2]] == ["b4", "b5"]
    assert [b.id for b in work[2:]] == ["b10"]
    assert (work[0].overdue, work[1].overdue, work[1].due_soon) == (True, False, True)
    assert (work[0].days_to_due, work[1].days_to_due) == (-7, 6)
    assert work[2].waiting_on == ("b5",)


def test_a_far_off_due_date_is_not_urgent() -> None:
    far = rv.RankedBrief(1, "x", "X", "", "", dt.date(2027, 1, 1), 43, ())
    assert (far.overdue, far.due_soon, far.urgency()) == (False, False, (1, 0))


def test_bugs_are_their_own_list(atlas: rv.Entry) -> None:
    assert [b.id for b in atlas.board.bugs] == ["b9"]
    assert "b9" not in [b.id for b in atlas.board.work]


def test_requirements_no_brief_cites_are_listed(atlas: rv.Entry) -> None:
    assert atlas.board.gaps == (rv.Gap("ATL-R003", "Nobody covers this"),)


def test_backlog_groups_by_milestone_and_puts_briefs_with_none_last(atlas: rv.Entry) -> None:
    groups = [(g.milestone, g.title, [b.id for b in g.briefs]) for g in atlas.board.backlog]
    assert groups == [
        ("m2", "The active thing", ["b4"]),
        ("m3", "Later", ["b5", "b12"]),
        ("", rv.NO_MILESTONE, ["b10", "b9"]),
    ]


def test_a_queue_without_a_ready_directory_has_nothing_ready(report: rv.Report) -> None:
    beta = report.entries[1].board
    assert (beta.ready, beta.gaps, beta.backlog) == ((), (), ())


def test_a_work_page_names_each_gap_instead_of_leaving_it_blank(report: rv.Report) -> None:
    text = rv.render("work", "markdown", report)
    assert (rv.NO_READY, rv.NO_BUGS, rv.NO_UNCOVERED) == (
        "No ready briefs.",
        "No open bugs.",
        "No uncovered requirements.",
    )
    assert text.count(rv.NO_READY) == 1
    assert text.count(rv.NO_BUGS) == 2
    assert text.count(rv.NO_UNCOVERED) == 2
    assert "None." not in text


def test_work_ranked_lists_are_numbered_and_bugs_count_from_one(atlas: rv.Entry) -> None:
    one = rv.Report(NOW, (ALPHA,), (atlas,))
    text = rv.render("work", "markdown", one)
    assert "1. B4 title (b4)" in text and "3. B10 title (b10)" in text
    assert "### Bugs\n\n1. B9 title (b9)" in text
    html = rv.render("work", "html", one)
    assert html.count("<ol") == 2 and "<ul" in html


def test_health_that_is_stale_and_capped_says_so_in_words(atlas: rv.Entry) -> None:
    text = rv.render("project", "markdown", rv.Report(NOW, (ALPHA,), (atlas,)))
    assert "`stale, set 17 days ago`" in text
    assert "`capped by automatic checks`" in text
    assert "`build red`" in text
    assert "Latest build, red on 2026-11-18" in text


def test_a_milestone_shows_its_slip_from_the_first_date_and_its_flags(atlas: rv.Entry) -> None:
    text = rv.render("project", "markdown", rv.Report(NOW, (ALPHA,), (atlas,)))
    assert (
        "Due 2026-11-30, first promised 2026-11-10, slipped 20 days, 9 days past the first date."
        in text
    )
    assert "**Flags in prod:** x off since 2026-11-15" in text
    assert "**Flags in dev:** x on since 2026-11-11" in text


def test_shipped_lines_group_by_release_with_those_without_one_last(atlas: rv.Entry) -> None:
    text = rv.render("project", "markdown", rv.Report(NOW, (ALPHA,), (atlas,)))
    assert (
        text.index("Release r1 (2026-11-01)") < text.index("Release r9") < text.index(rv.NO_RELEASE)
    )


def test_the_project_page_is_tiles_then_a_milestone_table_then_collapsed_detail(
    report: rv.Report,
) -> None:
    text = rv.render("project", "markdown", report)
    assert text.index("## Projects") < text.index("## Milestones") < text.index("## Detail")
    assert "| Milestone | Status | Due | Slip | Briefs | Evidence | Flags |" in text
    assert text.count("<details>") == 3 and text.count("</details>") == 3
    html = rv.render("project", "html", report)
    assert html.count("<table>") == 3 and html.count("<details>") == 3


def test_tiles_sort_red_then_unset_then_green_and_say_what_they_hold(report: rv.Report) -> None:
    text = rv.render("project", "markdown", report)
    tiles = text[text.index("## Projects") : text.index("## Milestones")]
    assert tiles.index("### Atlas") < tiles.index("### Gamma") < tiles.index("### Beta")
    assert "**Next:** The active thing due 2026-11-30 (slipped 20 days from 2026-11-10)" in tiles
    assert "**Ask:** Ship it? (by 2026-11-25)" in tiles
    assert "**Latest shipped:** B7 title (2026-11-18)" in tiles
    assert "**Milestones:** 1 of 4 milestones done" in tiles
    assert "**Milestones:** 1 of 1 milestone done" in tiles
    assert "**Next:** no open milestone has a date." in tiles
    assert "**Ask:** none." in tiles
    assert f"**Latest shipped:** {rv.NO_SHIPPED}" in tiles


def test_attention_orders_red_amber_unset_then_green_with_slip_breaking_ties(
    atlas: rv.Entry,
) -> None:
    def with_health(colour: str, stale: bool = False, slip: int = 0) -> rv.Entry:
        health = dataclasses.replace(atlas.project.health, effective=colour, stale=stale)
        milestones = tuple(dataclasses.replace(m, slip_days=slip) for m in atlas.project.milestones)
        tile = dataclasses.replace(atlas.project.tile, health=health)
        project = dataclasses.replace(atlas.project, tile=tile, milestones=milestones)
        return rv.Entry(project, atlas.board)

    ranked = [
        rv.attention(e)[0]
        for e in (
            with_health("red"),
            with_health("amber"),
            with_health("green", stale=True),
            with_health("green"),
        )
    ]
    assert ranked == [0, 1, 2, 3]
    assert rv.attention(with_health("red", slip=9)) < rv.attention(with_health("red", slip=1))


def test_slip_reads_in_words() -> None:
    assert rv.slip_phrase(1) == "slipped 1 day"
    assert rv.slip_phrase(20) == "slipped 20 days"
    assert rv.slip_phrase(-3) == "pulled in 3 days"


def test_the_portfolio_page_groups_projects_by_initiative_with_none_last(report: rv.Report) -> None:
    groups = rv.initiatives(report.entries)
    assert [(g.title, [e.project.slug for e in g.entries]) for g in groups] == [
        ("Platform", ["atlas"]),
        ("Growth", ["gamma"]),
        (rv.NO_INITIATIVE, ["beta"]),
    ]
    data = json.loads(rv.render("portfolio", "json", report))
    assert [g["initiative"] for g in data["initiatives"]] == ["Platform", "Growth", ""]


def test_the_source_line_appears_once_per_project_and_nowhere_else(report: rv.Report) -> None:
    for view in rv.view_names():
        text = rv.render(view, "markdown", report)
        assert text.count("**Source:**") == 3
        assert "As at" not in text
        assert rv.render(view, "html", report).count("<strong>Source:</strong>") == 3


# --- evidence -------------------------------------------------------------------------------


def evidence(kind: str, access: str, link: str) -> rm.BriefEvidence:
    import facts

    e = facts.Evidence(kind, link, "Caption", access, dt.date(2026, 11, 1))
    return rm.BriefEvidence("b1", e)


@pytest.mark.parametrize(
    "kind, access, link, expected",
    [
        ("screenshot", "public", "https://example.test/a.png", rv.Image),
        ("video", "public", "https://example.test/a.mp4", rv.Video),
        ("screenshot", "org", "https://example.test/a.png", rv.Link),
        ("video", "private", "https://example.test/a.mp4", rv.Link),
        ("screenshot", "public", "http://example.test/a.png", rv.Link),
    ],
)
def test_only_public_https_evidence_is_shown_inline(
    kind: str, access: str, link: str, expected: type
) -> None:
    item = evidence(kind, access, link)
    assert isinstance(rv.show(item.evidence), expected)
    assert rv.evidence_table(item)["inline"] is (expected is not rv.Link)


def test_evidence_is_embedded_or_linked_in_the_pages(atlas: rv.Entry) -> None:
    one = rv.Report(NOW, (ALPHA,), (atlas,))
    html = rv.render("project", "html", one)
    assert '<img src="https://example.test/shot.png" alt="The page" loading="lazy">' in html
    assert '<video src="https://example.test/clip.mp4" controls preload="none">' in html
    assert (
        '<a href="https://example.test/clip" rel="noopener noreferrer">The demo (video, org access)</a>'
        in html
    )
    markdown = rv.render("project", "markdown", one)
    assert "![The page](https://example.test/shot.png)" in markdown
    assert "[Walkthrough (video)](https://example.test/clip.mp4)" in markdown


def test_a_link_that_is_not_plain_https_shows_only_its_words() -> None:
    html, markdown = rv.FORMATS["html"], rv.FORMATS["markdown"]
    assert isinstance(html, rv.Markup) and isinstance(markdown, rv.Markup)
    link = rv.Link("Run it", 'javascript:alert("x")')
    assert link.render(html) == "Run it"
    assert link.render(markdown) == "Run it"
    assert not rv.is_https("https://a.test/x y")
    assert not rv.is_https("https://a.test/)")


# --- hostile text ---------------------------------------------------------------------------

HOSTILE = "<script>alert(1)</script> *bold* [x](https://evil.test) @someone www.evil.test | `tick`"


def hostile(atlas: rv.Entry) -> rv.Report:
    project = dataclasses.replace(atlas.project, title=HOSTILE, outcome=HOSTILE)
    return rv.Report(NOW, (ALPHA,), (rv.Entry(project, atlas.board),))


@pytest.mark.parametrize("view", rv.VIEWS)
def test_html_never_lets_queue_text_become_markup(view: str, atlas: rv.Entry) -> None:
    html = rv.render(view, "html", hostile(atlas))
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.parametrize("view", rv.VIEWS)
def test_markdown_never_lets_queue_text_become_markup(view: str, atlas: rv.Entry) -> None:
    text = rv.render(view, "markdown", hostile(atlas))
    assert "<script>" not in text
    assert "[x](https://evil.test)" not in text
    assert "\\<script\\>" in text


def test_markdown_escaping_stops_links_mentions_and_blocks() -> None:
    escaped = rv.escape_md("a\n\n# b @c https://d www.e")
    assert "\n" not in escaped
    assert "@\N{WORD JOINER}c" in escaped
    assert "https\\://d" in escaped and "www\\.e" in escaped


def test_a_chip_cannot_close_its_own_code_span() -> None:
    assert (
        rv.FORMATS["markdown"].render(rv.VIEWS["project"], rv.Report(NOW, (), ())).startswith("#")
    )
    chip = rv.Chip("a`b")
    markdown = rv.FORMATS["markdown"]
    assert isinstance(markdown, rv.Markup)
    assert chip.render(markdown) == "`a'b`"


# --- stamps, envelope and formats -----------------------------------------------------------


def test_a_cached_copy_is_marked_in_every_markup_format(atlas: rv.Entry) -> None:
    stale = dataclasses.replace(atlas.project.as_at, stale=True)
    cached = rv.Entry(dataclasses.replace(atlas.project, as_at=stale), atlas.board)
    one = rv.Report(NOW, (ALPHA,), (cached,))
    assert "`cached copy`" in rv.render("work", "markdown", one)
    assert "cached copy</span>" in rv.render("work", "html", one)


def test_the_source_line_names_the_repo_commit_and_time(atlas: rv.Entry) -> None:
    short = atlas.project.as_at.sha[:7]
    markdown = rv.render("project", "markdown", rv.Report(NOW, (ALPHA,), (atlas,)))
    assert f"**Source:** {ALPHA} @ {short}, 2026-11-19 09:00 UTC" in markdown


@pytest.mark.parametrize("view", rv.VIEWS)
def test_the_json_envelope_keeps_the_keys_readers_use(view: str, report: rv.Report) -> None:
    data = json.loads(rv.render(view, "json", report))
    assert data["schema"] == "claude-tasks.rollup/v1"
    assert data["level"] == view
    assert data["generated"] == "2026-11-19T12:00:00+00:00"
    assert data["sources"] == [ALPHA, BETA, IDLE]
    assert data["source_projects"] == [
        {"source": ALPHA, "projects": 1},
        {"source": BETA, "projects": 2},
        {"source": IDLE, "projects": 0},
    ]
    assert data["skipped"][0] == {"source": GONE, "queue": "", "reason": "unreachable"}
    assert data["warnings"] == [{"source": GONE, "behind": {}, "message": "Showing a cached copy."}]
    assert {"source", "slug", "title", "outcome", "as_at"} <= data["projects"][0].keys()


def test_the_project_json_keeps_its_old_keys_and_adds_the_new(report: rv.Report) -> None:
    project = json.loads(rv.render("project", "json", report))["projects"][0]
    assert {"health", "ask", "now", "next", "done", "shipped"} <= project.keys()
    assert project["now"][0].keys() >= {"id", "title", "percent_done", "due", "missed", "waiting"}
    assert project["shipped"][0].keys() == {"id", "title", "updated"}
    assert {"board", "tile", "shipped_by_release", "backlog", "initiative"} <= project.keys()
    assert project["tile"]["build"]["state"] == "red"
    assert project["now"][0]["flags"][0]["environment"] == "dev"


def test_the_portfolio_json_keeps_its_old_keys(report: rv.Report) -> None:
    project = json.loads(rv.render("portfolio", "json", report))["projects"][0]
    assert project["health"].keys() == {"effective", "owner", "reason"}
    assert project["milestones"] == {"done": 1, "total": 4}
    assert project["next_due"] == {"id": "m2", "title": "The active thing", "due": "2026-11-30"}
    assert project["missed"][0]["id"] == "m2"
    no_date = json.loads(rv.render("portfolio", "json", report))["projects"][1]
    assert no_date["next_due"] is None


def test_the_work_json_lists_ready_bugs_and_uncovered_requirements(report: rv.Report) -> None:
    project = json.loads(rv.render("work", "json", report))["projects"][0]
    assert [b["id"] for b in project["ready"]] == ["b4", "b5", "b10"]
    assert project["ready"][0]["overdue"] is True
    assert [b["id"] for b in project["bugs"]] == ["b9"]
    assert [r["id"] for r in project["uncovered_requirements"]] == ["ATL-R003"]


def test_a_report_with_no_projects_says_so_in_every_view_and_format(report: rv.Report) -> None:
    empty = rv.Report(NOW, (ALPHA,), ())
    for view in rv.view_names():
        assert rv.NO_PROJECTS in rv.render(view, "markdown", empty)
        assert rv.NO_PROJECTS in rv.render(view, "html", empty)
        assert json.loads(rv.render(view, "json", empty))["projects"] == []


def test_footnotes_name_what_was_left_out_and_why(report: rv.Report) -> None:
    text = rv.render("project", "markdown", report)
    assert "gone-source: unreachable" in text
    assert "gone-source / .tasks: no project.toml" in text
    assert "Showing a cached copy." in text


def test_each_format_names_itself_for_the_caller() -> None:
    got = {f.name: (f.content_type, f.suffix) for f in rv.FORMATS.values()}
    assert got["json"] == ("application/json", ".json")
    assert got["markdown"] == ("text/markdown; charset=utf-8", ".md")
    assert got["html"][0] == "text/html; charset=utf-8"
    assert list(rv.view_names()) == ["work", "project", "portfolio"]


def test_the_html_page_is_self_contained_and_adapts_to_phone_and_dark_mode(
    report: rv.Report,
) -> None:
    html = rv.render("project", "html", report)
    assert "<script" not in html
    assert "prefers-color-scheme:dark" in html
    assert 'name="viewport"' in html
    assert "img-src https:; media-src https:" in html
    assert 'src="http://' not in html


def test_the_page_csp_allows_only_what_evidence_needs_and_forbids_framing() -> None:
    assert rv.HTML_CSP == (
        "default-src 'none'; style-src 'unsafe-inline'; img-src https:; media-src https:; "
        "frame-ancestors 'none'"
    )


def test_the_plural_helper_and_blank_dates() -> None:
    assert (rv.plural(1, "day"), rv.plural(2, "day")) == ("1 day", "2 days")
    assert rv.date_text(None) == ""


def test_an_owner_who_set_nothing_is_not_quoted(report: rv.Report) -> None:
    gamma = report.entries[2].project
    assert (
        rv.owner_line(gamma.health).render(rv.MarkdownFormat())
        == "The owner has not set a health in project.toml."
    )


def health_of(
    owner: str, effective: str, *, set_on: str = "", stale: bool = False, age: int | None = None
) -> rm.Health:
    return rm.Health(owner, "On course.", set_on, effective, ("a check failed",), stale, age)


def says(h: rm.Health) -> tuple[list[str], str]:
    chips = [c.label for c in rv.health_chips(h)]
    return chips, rv.owner_line(h).render(rv.MarkdownFormat())


def test_a_capped_and_stale_statement_is_quoted_with_its_date_and_both_warnings() -> None:
    h = health_of("green", "red", set_on="2026-11-10", stale=True, age=9)
    assert says(h) == (
        ["Red", "stale, set 9 days ago", "capped by automatic checks"],
        "Owner said Green on 2026-11-10: On course.",
    )


def test_a_statement_that_stands_is_quoted_and_carries_only_its_colour() -> None:
    h = health_of("green", "green", set_on="2026-11-18")
    assert says(h) == (["Green"], "Owner said Green on 2026-11-18: On course.")


def test_an_undated_statement_says_the_date_was_not_recorded_capped_or_not() -> None:
    line = "Owner said Green on a date that was not recorded: On course."
    assert says(health_of("green", "red")) == (["Red", "capped by automatic checks"], line)
    assert says(health_of("green", "green")) == (["Green"], line)


def test_a_floor_with_no_owner_word_is_the_colour_alone_and_not_a_cap() -> None:
    assert says(health_of("", "red")) == (
        ["Red"],
        "The owner has not set a health in project.toml.",
    )


def test_with_no_owner_word_and_nothing_wrong_the_colour_chip_is_neutral() -> None:
    (chip,) = rv.health_chips(rm.Health("", "", "", "", ()))
    assert (chip.label, chip.tone) == ("Not set by owner", rv.Tone.NEUTRAL)


def test_a_due_today_brief_says_so() -> None:
    today = rv.RankedBrief(1, "x", "X", "", "", dt.date(2026, 11, 19), 0, ())
    chips = [p for p in rv.due_parts(today) if isinstance(p, rv.Chip)]
    assert [c.label for c in chips] == ["due today"]
    undated = rv.RankedBrief(1, "x", "X", "", "", None, None, ())
    assert rv.due_parts(undated) == ()


# --- the contents page and the guide -----------------------------------------------------------


def test_the_contents_page_links_every_view_and_its_data_in_each_format(report: rv.Report) -> None:
    md = rv.render_contents("markdown", report)
    html = rv.render_contents("html", report)
    for view in rv.view_names():
        assert f"[JSON]({view}.json)" in md and f'href="{view}.json"' in html
        assert f"]({view}.md)" in md and f'href="{view}"' in html
    assert "What these files are" not in md and "What these files are" not in html


def test_the_guide_is_added_on_request_with_its_code_spans_set_as_code(report: rv.Report) -> None:
    md = rv.render_contents("markdown", report, with_guide=True)
    html = rv.render_contents("html", report, with_guide=True)
    assert "`rollup.py write --out <dir> --source Label=repo`" in md.replace("\\", "")
    assert "<code>rollup.py write --out &lt;dir&gt; --source Label=repo</code>" in html
    assert f"`{rv.SCHEMA}`" in md.replace("\\", "") and "## The JSON is an API" in md
    assert "<code>work.md</code>, <code>project.md</code>" in html


def test_prose_keeps_the_spaces_around_a_code_span_and_drops_none_inside_words() -> None:
    md = rv.prose("a `b` c").line.render(rv.MarkdownFormat())
    assert md == "a `b` c"
    assert rv.prose("`b`").line.render(rv.MarkdownFormat()) == "`b`"
    assert rv.stretch(" ") == (rv.Space(),)
    assert rv.stretch("  ") == (rv.Space(), rv.Space())
    assert rv.stretch("") == ()
    assert rv.stretch(" x ") == (rv.Space(), rv.Text("x"), rv.Space())


def test_a_page_link_is_a_link_to_a_sibling_file_in_each_markup() -> None:
    page = rv.Page("Project", "project.md")
    assert page.render(rv.MarkdownFormat()) == "[Project](project.md)"
    assert (
        page.render(rv.HtmlFormat()) == '<a href="project.md" rel="noopener noreferrer">Project</a>'
    )


def test_a_project_without_milestones_says_so_in_its_stage_line_and_grid(atlas: rv.Entry) -> None:
    bare = dataclasses.replace(atlas.project, milestones=())
    assert rv.stage_line(bare) == rv.NO_MILESTONES
    assert rv.milestone_grid(bare) == rv.paragraph(rv.NO_MILESTONES)


def test_a_bad_tasks_toml_warns_once_per_queue_naming_the_source(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry(tmp_path, ALPHA, {**ATLAS_FILES, "tasks.toml": "name = [unterminated\n"})
    err = capsys.readouterr().err
    assert f"cannot read {ALPHA}/tasks.toml (" in err and err.count("cannot read") == 1


# --- feature rollout ------------------------------------------------------------------------

CODES = ("acme", "globex", "initech", "hooli", "umbrella", "wayne", "stark", "wonka", "tyrell")
# Nine customers: three have the feature on, five run a release that holds it.
ROLLOUT_ROWS = "".join(
    f'\n[[customer]]\ncode = "{code}"\nrelease = "{"r2" if n < 5 else ""}"\non = {str(n < 3).lower()}\n'
    for n, code in enumerate(CODES)
)
ROLLOUT_PROJECT = (
    small_project("delta", "", FRESH).replace(
        'title = "Only"', 'title = "Only"\nplain = "Ship KEY-12"'
    )
    + '\n[[jira]]\nkey = "KEY-12"\ntitle = "The feature"\nurl = "https://example.test/KEY-12"\n'
)
ROLLOUT_FILES = {
    "tasks.toml": 'name = "Delta"\n',
    "project.toml": ROLLOUT_PROJECT,
    "ready/d1.md": brief("d1", "ready", milestone="m1", unticked=1),
    "history/rollout/KEY-12-2026-11-10-aaaaaaaa.toml": (
        f'key = "KEY-12"\nrecorded_on = 2026-11-10\n{ROLLOUT_ROWS}'
    ),
}
CELL = "KEY-12 on: some 3/9, released 5/9"
MISSING = "hooli, stark, tyrell, umbrella, wayne, wonka"


def delta(tmp_path: pathlib.Path, files: Mapping[str, str] = ROLLOUT_FILES) -> rv.Report:
    return rv.Report(NOW, (BETA,), (entry(tmp_path, BETA, files),))


def test_a_milestone_with_a_rollout_shows_counts_in_a_rollout_column(
    tmp_path: pathlib.Path,
) -> None:
    text = rv.render("project", "markdown", delta(tmp_path))
    assert "| Milestone | Status | Due | Slip | Briefs | Evidence | Flags | Rollout |" in text
    assert f"| {CELL} |" in text


def test_the_milestone_details_name_the_customers_still_missing(tmp_path: pathlib.Path) -> None:
    text = rv.render("project", "markdown", delta(tmp_path))
    assert f"**KEY-12 not on:** {MISSING}" in text


def test_a_fully_rolled_out_key_lists_no_missing_customers(tmp_path: pathlib.Path) -> None:
    rows = ROLLOUT_ROWS.replace("on = false", "on = true")
    files = {
        **ROLLOUT_FILES,
        "history/rollout/KEY-12-2026-11-10-aaaaaaaa.toml": (
            f'key = "KEY-12"\nrecorded_on = 2026-11-10\n{rows}'
        ),
    }
    text = rv.render("project", "markdown", delta(tmp_path, files))
    assert "KEY-12 on: all 9/9, released 5/9" in text and "not on" not in text


def test_the_html_page_carries_the_same_cell_and_details(tmp_path: pathlib.Path) -> None:
    html = rv.render("project", "html", delta(tmp_path))
    assert "<th>Rollout</th>" in html and f"<td>{CELL}</td>" in html
    assert "KEY-12 not on:" in html


def test_the_project_json_lists_each_rollout_on_its_milestone(tmp_path: pathlib.Path) -> None:
    project = json.loads(rv.render("project", "json", delta(tmp_path)))["projects"][0]
    (milestone,) = (m for stage in ("now", "next", "done") for m in project[stage])
    (rollout,) = milestone["rollouts"]
    assert rollout == {
        "key": "KEY-12",
        "on": 3,
        "released": 5,
        "total": 9,
        "spread": "some",
        "missing": ["hooli", "stark", "tyrell", "umbrella", "wayne", "wonka"],
        "releases": {"r2": sorted(CODES[:5])},
        "recorded_on": "2026-11-10",
    }


def test_the_milestone_details_name_each_release_and_the_customers_on_it(
    tmp_path: pathlib.Path,
) -> None:
    text = rv.render("project", "markdown", delta(tmp_path))
    assert f"**KEY-12 released:** r2: {', '.join(sorted(CODES[:5]))}" in text


def test_a_rollout_with_pr_counts_shows_how_much_has_merged(tmp_path: pathlib.Path) -> None:
    counted = (
        f'key = "KEY-12"\nrecorded_on = 2026-11-10\nmerged_prs = 3\nopen_prs = 1\n{ROLLOUT_ROWS}'
    )
    files = {**ROLLOUT_FILES, "history/rollout/KEY-12-2026-11-10-aaaaaaaa.toml": counted}
    report = delta(tmp_path, files)
    assert "| KEY-12 merged 3/4 PRs, on: some 3/9, released 5/9 |" in rv.render(
        "project", "markdown", report
    )
    project = json.loads(rv.render("project", "json", report))["projects"][0]
    (milestone,) = (m for stage in ("now", "next", "done") for m in project[stage])
    assert (milestone["rollouts"][0]["merged_prs"], milestone["rollouts"][0]["open_prs"]) == (3, 1)


def test_without_rollout_history_the_table_json_and_details_gain_nothing(
    tmp_path: pathlib.Path,
) -> None:
    bare = {k: v for k, v in ROLLOUT_FILES.items() if not k.startswith("history/")}
    report = delta(tmp_path, bare)
    text = rv.render("project", "markdown", report)
    assert "Rollout" not in text and "KEY-12 on:" not in text and "not on:" not in text
    project = json.loads(rv.render("project", "json", report))["projects"][0]
    (milestone,) = (m for stage in ("now", "next", "done") for m in project[stage])
    assert "rollouts" not in milestone


def test_the_other_views_do_not_mention_rollouts(tmp_path: pathlib.Path) -> None:
    report = delta(tmp_path)
    for view in ("work", "portfolio"):
        text = rv.render(view, "markdown", report)
        assert "KEY-12 on:" not in text and "not on:" not in text

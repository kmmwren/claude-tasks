"""Roll-ups read text the project owner typed, so none of it may become markup, links or script."""

import http.client
import json
import re
import threading
from pathlib import Path

import pytest
import queue_snapshot as qs
import rollup
import rollup_views as rv

from tests.project_fixtures import make_queue
from tests.rollup_sources import NOW, projects, rollup_of, source_of, with_project_toml

SAFE_TARGET = re.compile(r"[\w./-]+")
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src https:; media-src https:; "
    "frame-ancestors 'none'"
)
INJECTED_PROJECT = """
[project]
title = "A\\n# Injected\\n| x | y |\\n- bullet"
outcome = "O\\n## Two"
"""
BENIGN_PROJECT = '[project]\ntitle = "A"\noutcome = "O"\n'


def text_in_every_field(z):
    """A queue whose every owner-typed field, and one brief title, holds `z`."""

    def build(clone):
        q = make_queue(clone)
        (q / "project.toml").write_text(
            f"[project]\ntitle = {json.dumps(z)}\noutcome = {json.dumps(z)}\n"
            f'[health]\ncolour = "amber"\nreason = {json.dumps(z)}\n'
            f"[ask]\nquestion = {json.dumps(z)}\ndeadline = {json.dumps(z)}\n"
            f'[[milestones]]\nid = "m1"\ntitle = {json.dumps(z)}\n'
        )
        done = q / "done" / "b1.md"
        done.write_text(done.read_text().replace("title: One", f"title: {json.dumps(z)}"))

    return build


# --- HTML --------------------------------------------------------------------------------

HOSTILE_TITLE = '<script>alert("x")</script> & "q" \'s\' [a](javascript:alert(1)) *b* `c` | d'
ESCAPED_TITLE = (
    "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;q&quot; &#x27;s&#x27; "
    "[a](javascript:alert(1)) *b* `c` | d"
)


def hostile_everywhere(tmp_path):
    return rollup_of(tmp_path, text_in_every_field(HOSTILE_TITLE), HOSTILE_TITLE)


ESCAPED_AT_LEAST = {"work": 2, "project": 6, "portfolio": 3}


@pytest.mark.parametrize("view", rv.VIEWS)
def test_hostile_text_never_becomes_markup_in_html(tmp_path, view):
    page = rv.HTML.render(rv.VIEWS[view], hostile_everywhere(tmp_path))
    assert "<script" not in page and "onmouseover" not in page.replace(ESCAPED_TITLE, "")
    assert page.count(ESCAPED_TITLE) >= ESCAPED_AT_LEAST[view]
    assert "<a " not in page


def test_html_on_the_server_front_page_links_only_to_fixed_targets(tmp_path):
    got = rollup.route("/", hostile_everywhere(tmp_path))
    assert got is not None
    hrefs = re.findall(r'<a href="([^"]*)"', got[1])
    assert hrefs and all(SAFE_TARGET.fullmatch(href) for href in hrefs)


def hostile_notes() -> rv.Report:
    return rv.Report(
        NOW,
        ("S",),
        (),
        (rv.Omission("S", "q", "<b>bad</b>"),),
        (rv.SourceNote("<i>S</i>", "<i>S</i> could not be reached"),),
    )


@pytest.mark.parametrize("view", rv.VIEWS)
def test_skip_reasons_and_warnings_are_escaped_in_html(view):
    page = rv.HTML.render(rv.VIEWS[view], hostile_notes())
    assert "&lt;b&gt;bad&lt;/b&gt;" in page and "&lt;i&gt;S&lt;/i&gt;" in page
    assert "<b>" not in page and "<i>" not in page


# --- server ------------------------------------------------------------------------------


def get(address, path, host=None):
    conn = http.client.HTTPConnection(*address)
    conn.putrequest("GET", path, skip_host=host is not None)
    if host is not None:
        conn.putheader("Host", host)
    conn.endheaders()
    resp = conn.getresponse()
    return resp.status, dict(resp.getheaders()), resp.read()


@pytest.fixture
def listening(tmp_path):
    holder = {}
    rollup.Serve([source_of(tmp_path, make_queue, "W")], lambda: NOW, 0).run(
        block=lambda srv: holder.setdefault("srv", srv)
    )
    srv = holder["srv"]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def test_server_sends_a_content_security_policy_on_every_answer(listening):
    for path in ("/", "/work", "/project", "/portfolio.json", "/nope"):
        headers = get(listening, path)[1]
        assert headers["Content-Security-Policy"] == CSP
        assert headers["X-Content-Type-Options"] == "nosniff"


def test_the_policy_forbids_framing_and_every_remote_load_but_https_media():
    assert rv.HTML_CSP == CSP and "frame-ancestors 'none'" in CSP
    page = rv.HTML.render(rv.VIEWS["project"], hostile_notes())
    assert f'content="{CSP.replace(chr(39), "&#x27;")}"' in page


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8788", "127.0.0.1", "127.0.0.1:1", "LocalHost"]
)
def test_server_answers_its_own_names(listening, host):
    assert get(listening, "/project", host)[0] == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:80",
        "localhost.evil.example",
        "127.0.0.1.evil.example",
        "localhost:80@evil.example",
        "",
    ],
)
def test_server_refuses_other_host_headers_against_dns_rebinding(listening, host):
    status, headers, body = get(listening, "/project", host)
    assert status == 403 and b"Project roll-up" not in body
    assert headers["Content-Security-Policy"] == CSP


def test_server_refuses_a_request_with_no_host_header(listening):
    conn = http.client.HTTPConnection(*listening)
    conn.putrequest("GET", "/project", skip_host=True)
    conn.endheaders()
    assert conn.getresponse().status == 403


# --- Markdown ----------------------------------------------------------------------------


def test_escaping_flattens_whitespace_and_escapes_markup():
    assert rv.escape_md("a *b* _c_ [d] <e> `f` | g\\\n\t h") == (
        "a \\*b\\* \\_c\\_ \\[d\\] \\<e\\> \\`f\\` \\| g\\\\ h"
    )


def headings(lines):
    return [x for x in lines if x.startswith("#")]


@pytest.mark.parametrize("view", rv.VIEWS)
def test_a_title_with_newlines_cannot_add_headings_rows_or_bullets(tmp_path, view):
    r = rollup_of(tmp_path / "bad", with_project_toml(INJECTED_PROJECT))
    plain = rollup_of(tmp_path / "ok", with_project_toml(BENIGN_PROJECT))
    lines = rv.MARKDOWN.render(rv.VIEWS[view], r).splitlines()
    assert len(headings(lines)) == len(
        headings(rv.MARKDOWN.render(rv.VIEWS[view], plain).splitlines())
    )
    assert not any(x.startswith(("# Injected", "## Two", "- bullet", "| x")) for x in lines)
    assert len({len(re.findall(r"(?<!\\)\|", x)) for x in lines if x.startswith("|")}) <= 1


ESCAPED_AT_LEAST_MD = {"work": 2, "project": 8, "portfolio": 4}


def test_every_user_sourced_string_is_escaped_in_markdown(tmp_path):
    z = "Z*Z"
    shown = rollup.build(
        [
            source_of(tmp_path / "z", text_in_every_field(z), z),
            source_of(tmp_path / "y", with_project_toml("[project]\n"), "Y*Y"),
        ],
        NOW,
    )
    pages = {view: rv.MARKDOWN.render(rv.VIEWS[view], shown) for view in rv.VIEWS}
    everything = "".join(pages.values())
    assert "Z*Z" not in everything and "Y*Y" not in everything
    assert all(pages[view].count("Z\\*Z") >= n for view, n in ESCAPED_AT_LEAST_MD.items())
    assert "Y\\*Y" in pages["project"]


@pytest.mark.parametrize("view", rv.VIEWS)
def test_skip_reasons_are_escaped(view):
    r = rv.Report(NOW, ("S",), (), (rv.Omission("S", "q", "bad *x* | y"),))
    assert "- S / q: bad \\*x\\* \\| y" in rv.MARKDOWN.render(rv.VIEWS[view], r)


# --- fields of the wrong type, and shareable skip reasons -----------------------------------


@pytest.mark.parametrize(
    "old,new,field_name",
    [
        ('title = "Demo Project"', "title = 5", "title"),
        ('outcome = "Customers can do the demo thing."', 'outcome = ["a"]', "outcome"),
        ('reason = "Going to plan."', "reason = 5", "reason"),
    ],
)
def test_a_field_of_the_wrong_type_skips_only_that_project(tmp_path, old, new, field_name):
    def bad(clone):
        project = make_queue(clone) / "project.toml"
        project.write_text(project.read_text().replace(old, new))

    r = rollup.build(
        [source_of(tmp_path / "bad", bad, "Bad"), source_of(tmp_path / "good", make_queue, "Good")],
        NOW,
    )
    assert [o.source for o in r.omitted] == ["Bad"]
    assert field_name in r.omitted[0].reason and "must be text" in r.omitted[0].reason
    assert [p.source for p in projects(r)] == ["Good"]


def test_a_milestone_title_that_is_not_text_skips_only_that_milestone(tmp_path, capsys):
    def build(clone):
        project = make_queue(clone) / "project.toml"
        project.write_text(
            project.read_text().replace('plain = "The first thing works"', "plain = 5")
        )

    r = rollup_of(tmp_path, build)
    assert r.omitted == () and [p.slug for p in projects(r)] == ["demo"]
    assert [m.id for m in projects(r)[0].milestones] == ["m2"]
    warning = capsys.readouterr().err
    assert "m1" in warning and "that milestone is left out" in warning
    assert "milestone m1 plain or title must be text, not int" in warning
    page = rv.HTML.render(rv.VIEWS["project"], r)
    assert "The second thing works" in page and "The first thing works" not in page


def milestone_brief_ids(tmp_path, value):
    def build(clone):
        project = make_queue(clone) / "project.toml"
        project.write_text(
            project.read_text().replace('title = "First"', f'title = "First"\nbriefs = {value}')
        )

    r = rollup_of(tmp_path, build)
    assert r.omitted == ()
    return [(m.id, sorted(b.id for b in m.briefs)) for m in projects(r)[0].milestones]


@pytest.mark.parametrize("value", ["5", "[[1]]", "[1, true]", '"b2"'])
def test_a_milestone_brief_list_of_the_wrong_type_is_ignored_and_never_skips_the_project(
    tmp_path, value
):
    assert milestone_brief_ids(tmp_path, value) == [("m1", ["b1"]), ("m2", ["b2"])]


def test_a_legacy_milestone_brief_list_still_names_its_briefs(tmp_path):
    assert milestone_brief_ids(tmp_path, '["b2", 7]') == [("m1", ["b1", "b2"]), ("m2", ["b2"])]


def test_skip_reasons_never_carry_an_absolute_path(tmp_path):
    source = source_of(tmp_path, make_queue)
    warm = rollup.build([source], NOW)
    assert warm.omitted == ()
    (brief,) = qs.cache_root().rglob("in-progress/b2.md")
    brief.chmod(0)
    try:
        r = rollup.build([source], NOW)
    finally:
        brief.chmod(0o644)
    assert [o.source for o in r.omitted] == ["S"]
    reason = r.omitted[0].reason
    assert "PermissionError" in reason and str(Path("in-progress") / "b2.md") in reason
    shared = "".join(rv.FORMATS[fmt].render(rv.VIEWS["project"], r) for fmt in rv.FORMATS)
    assert str(tmp_path) not in shared


@pytest.mark.parametrize(
    "error,expected",
    [
        (OSError(2, "Gone", "/elsewhere/secret.md"), "FileNotFoundError: Gone (secret.md)"),
        (OSError("boom"), "OSError: cannot be read"),
    ],
)
def test_os_reason_hides_paths_outside_the_queue(tmp_path, error, expected):
    assert rollup._os_reason(error, tmp_path) == expected


def test_an_unreachable_source_never_puts_its_address_in_a_shared_line(tmp_path):
    secret = str(tmp_path / "secret-host" / "repo")
    r = rollup.build([rollup.Source("S", secret)], NOW)
    shared = "".join(rv.FORMATS[fmt].render(rv.VIEWS["portfolio"], r) for fmt in rv.FORMATS)
    assert "secret-host" not in shared and "- S / .tasks: its queue cannot be read" in shared


# --- links a Markdown renderer would make on its own -------------------------------------------

# A mention needs a name straight after the @; the source line's "label @ commit" has a space.
AUTOLINK = re.compile(r"(?i)https?://|www\.|@(?=\S)(?!⁠)")


@pytest.mark.parametrize(
    "text",
    [
        "see http://x.example/a",
        "https://x.example",
        "(WWW.x.example)",
        "mail a@b.example",
        "mailto:a@b.example",
    ],
)
def test_text_that_a_renderer_would_autolink_is_neutralised(text):
    assert not AUTOLINK.search(rv.escape_md(text))


@pytest.mark.parametrize("view", rv.VIEWS)
def test_project_text_never_becomes_a_live_link_in_markdown(tmp_path, view):
    z = "http://e.example www.e.example a@e.example"

    def build(clone):
        q = make_queue(clone)
        (q / "project.toml").write_text(
            f"[project]\ntitle = {json.dumps(z)}\noutcome = {json.dumps(z)}\n"
            f'[health]\ncolour = "amber"\nreason = {json.dumps(z)}\n'
            f"[ask]\nquestion = {json.dumps(z)}\n"
        )

    text = rv.MARKDOWN.render(rv.VIEWS[view], rollup_of(tmp_path, build, z))
    assert "e.example" in text and not AUTOLINK.search(text)


# --- files that are not UTF-8 -------------------------------------------------------------

NOT_UTF8 = b"\xff\xfe\x00bad"


def warnings_of(capsys):
    return capsys.readouterr().err.splitlines()


def test_a_requirements_register_that_is_not_utf8_warns_and_its_project_still_renders(
    tmp_path, capsys
):
    def build(clone):
        (make_queue(clone) / "requirements.md").write_bytes(NOT_UTF8)

    report = rollup_of(tmp_path, build)

    assert [e.board.gaps for e in report.entries] == [()] and not report.omitted
    line = "warning: S/requirements.md: is not valid UTF-8; no requirements are read from it"
    assert warnings_of(capsys).count(line) == 1


@pytest.mark.parametrize("folder", ["inbox", "ready", "in-progress", "done", "parked"])
def test_a_brief_that_is_not_utf8_is_skipped_with_a_warning_and_the_project_still_renders(
    tmp_path, capsys, folder
):
    def build(clone):
        (make_queue(clone) / folder / "bad.md").write_bytes(NOT_UTF8)

    report = rollup_of(tmp_path, build)

    assert len(report.entries) == 1 and not report.omitted
    line = f"warning: S/{folder}/bad.md: is not valid UTF-8; that brief is skipped"
    assert warnings_of(capsys).count(line) == 1

"""Roll-ups read text the project owner typed, so none of it may become markup, links or script."""

import datetime as dt
import http.client
import json
import re
import threading
from pathlib import Path

import pytest
import rollup

from tests.project_fixtures import make_queue

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
SAFE_TARGET = re.compile(r"[\w./-]+")
CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
HOSTILE_PROJECT = """
[project]
title = "[x](javascript:alert(1))"
outcome = '[y](" onmouseover="alert(2))'

[health]
colour = "green"
reason = "[z](javascript:alert(3))"
"""
INJECTED_PROJECT = """
[project]
title = "A\\n# Injected\\n| x | y |\\n- bullet"
outcome = "O\\n## Two"
"""


def build_from(tmp_path, project_toml):
    q = make_queue(tmp_path / "evil")
    (q / "project.toml").write_text(project_toml)
    return rollup.build([rollup.Source("S", tmp_path)], NOW)


# --- HTML --------------------------------------------------------------------------------

HOSTILE_TITLE = '<script>alert("x")</script> & "q" \'s\' [a](javascript:alert(1)) *b* `c` | d'
ESCAPED_TITLE = (
    "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;q&quot; &#x27;s&#x27; "
    "[a](javascript:alert(1)) *b* `c` | d"
)


def hostile_everywhere(tmp_path):
    z = HOSTILE_TITLE
    q = make_queue(tmp_path / "evil")
    (q / "project.toml").write_text(
        f"[project]\ntitle = {json.dumps(z)}\noutcome = {json.dumps(z)}\n"
        f'[health]\ncolour = "amber"\nreason = {json.dumps(z)}\n'
        f"[ask]\nquestion = {json.dumps(z)}\ndeadline = {json.dumps(z)}\n"
        f'[[milestones]]\nid = "m1"\ntitle = {json.dumps(z)}\n'
    )
    done = q / "done" / "b1.md"
    done.write_text(done.read_text().replace("title: One", f"title: {json.dumps(z)}"))
    return rollup.build([rollup.Source(z, tmp_path)], NOW)


@pytest.mark.parametrize("level", ["pm", "cto"])
def test_hostile_text_never_becomes_markup_in_html(tmp_path, level):
    page = rollup.LEVELS[level].html(hostile_everywhere(tmp_path))
    assert "<script" not in page and "onmouseover" not in page.replace(ESCAPED_TITLE, "")
    assert page.count(ESCAPED_TITLE) >= (6 if level == "pm" else 3)
    assert "<a " not in page


def test_html_on_the_server_front_page_links_only_to_fixed_targets(tmp_path):
    got = rollup.route("/", hostile_everywhere(tmp_path))
    assert got is not None
    hrefs = re.findall(r'<a href="([^"]*)"', got[1])
    assert hrefs and all(SAFE_TARGET.fullmatch(href) for href in hrefs)


def test_skip_reasons_and_warnings_are_escaped_in_html():
    r = rollup.Rollup(
        "now",
        ("S",),
        (),
        (rollup.Skipped("S", "q", "<b>bad</b>"),),
        (rollup.SourceWarning("<i>S</i>", 1),),
    )
    page = rollup.LEVELS["pm"].html(r)
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
    make_queue(tmp_path / "demo")
    holder = {}
    rollup.Serve([rollup.Source("W", tmp_path)], lambda: NOW, 0).run(
        block=lambda srv: holder.setdefault("srv", srv)
    )
    srv = holder["srv"]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def test_server_sends_a_content_security_policy_on_every_answer(listening):
    for path in ("/pm", "/pm.json", "/nope"):
        headers = get(listening, path)[1]
        assert headers["Content-Security-Policy"] == CSP
        assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8788", "127.0.0.1", "127.0.0.1:1", "LocalHost"]
)
def test_server_answers_its_own_names(listening, host):
    assert get(listening, "/pm", host)[0] == 200


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
    status, headers, body = get(listening, "/pm", host)
    assert status == 403 and b"Product roll-up" not in body
    assert headers["Content-Security-Policy"] == CSP


def test_server_refuses_a_request_with_no_host_header(listening):
    conn = http.client.HTTPConnection(*listening)
    conn.putrequest("GET", "/pm", skip_host=True)
    conn.endheaders()
    assert conn.getresponse().status == 403


# --- Markdown ----------------------------------------------------------------------------


def test_escaping_flattens_whitespace_and_escapes_markup():
    assert rollup.escape_md("a *b* _c_ [d] <e> `f` | g\\\n\t h") == (
        "a \\*b\\* \\_c\\_ \\[d\\] \\<e\\> \\`f\\` \\| g\\\\ h"
    )


@pytest.mark.parametrize("level", ["pm", "cto"])
def test_a_title_with_newlines_cannot_add_headings_rows_or_bullets(tmp_path, level):
    lines = rollup.LEVELS[level].markdown(build_from(tmp_path, INJECTED_PROJECT)).splitlines()
    assert len([x for x in lines if x.startswith("#")]) == (2 if level == "pm" else 1)
    assert not any(x.startswith(("- bullet", "| x")) for x in lines)
    assert len({len(re.findall(r"(?<!\\)\|", x)) for x in lines if x.startswith("|")}) <= 1


def test_every_user_sourced_string_is_escaped_in_markdown(tmp_path):
    z = "Z*Z"
    q = make_queue(tmp_path / z)
    (q / "project.toml").write_text(
        f'[project]\ntitle = "{z}"\noutcome = "{z}"\n[health]\ncolour = "amber"\nreason = "{z}"\n'
        f'[ask]\nquestion = "{z}"\ndeadline = "{z}"\n'
        f'[[milestones]]\nid = "m1"\ntitle = "{z}"\n'
    )
    done = q / "done" / "b1.md"
    done.write_text(done.read_text().replace("title: One", f"title: {z}"))
    (make_queue(tmp_path / "Y*Y") / "project.toml").write_text("[project]\n")
    r = rollup.build([rollup.Source(z, tmp_path)], NOW)
    pm, cto = (rollup.LEVELS[name].markdown(r) for name in ("pm", "cto"))
    assert "Z*Z" not in pm + cto and "Y*Y" not in pm + cto
    assert pm.count("Z\\*Z") >= 8 and cto.count("Z\\*Z") >= 4


def test_skip_reasons_are_escaped():
    note = rollup.Footnote("Not shown", (rollup.Skipped("S", "q", "bad *x* | y").line(),))
    assert "- S / q: bad \\*x\\* \\| y" in note.markdown()


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
    bad = make_queue(tmp_path / "bad")
    project = bad / "project.toml"
    project.write_text(project.read_text().replace(old, new))
    make_queue(tmp_path / "good")
    r = rollup.build([rollup.Source("S", tmp_path)], NOW)
    assert [s.queue for s in r.skipped] == ["bad"]
    assert field_name in r.skipped[0].reason and "must be text" in r.skipped[0].reason
    assert [p.slug for p in r.projects] == ["demo"]


def test_a_milestone_title_that_is_not_text_skips_only_that_milestone(tmp_path, capsys):
    q = make_queue(tmp_path / "demo")
    project = q / "project.toml"
    project.write_text(project.read_text().replace('plain = "The first thing works"', "plain = 5"))
    r = rollup.build([rollup.Source("S", tmp_path)], NOW)
    assert r.skipped == () and [p.slug for p in r.projects] == ["demo"]
    assert [m.id for m in r.projects[0].milestones] == ["m2"]
    warning = capsys.readouterr().err
    assert "S / demo" in warning and "m1" in warning and "that milestone is left out" in warning
    assert "milestone m1 plain or title must be text, not int" in warning
    page = rollup.LEVELS["pm"].html(r)
    assert "The second thing works" in page and "The first thing works" not in page


def milestone_brief_ids(tmp_path, value):
    q = make_queue(tmp_path / "bad")
    project = q / "project.toml"
    project.write_text(
        project.read_text().replace('title = "First"', f'title = "First"\nbriefs = {value}')
    )
    r = rollup.build([rollup.Source("S", tmp_path)], NOW)
    assert r.skipped == ()
    return [(m.id, sorted(b.id for b in m.briefs)) for m in r.projects[0].milestones]


@pytest.mark.parametrize("value", ["5", "[[1]]", "[1, true]", '"b2"'])
def test_a_milestone_brief_list_of_the_wrong_type_is_ignored_and_never_skips_the_project(
    tmp_path, value
):
    assert milestone_brief_ids(tmp_path, value) == [("m1", ["b1"]), ("m2", ["b2"])]


def test_a_legacy_milestone_brief_list_still_names_its_briefs(tmp_path):
    assert milestone_brief_ids(tmp_path, '["b2", 7]') == [("m1", ["b1", "b2"]), ("m2", ["b2"])]


def test_skip_reasons_never_carry_an_absolute_path(tmp_path):
    q = make_queue(tmp_path / "demo")
    brief = q / "in-progress" / "b2.md"
    brief.chmod(0)
    try:
        r = rollup.build([rollup.Source("S", tmp_path)], NOW)
    finally:
        brief.chmod(0o644)
    assert [s.queue for s in r.skipped] == ["demo"]
    reason = r.skipped[0].reason
    assert "PermissionError" in reason and str(Path("in-progress") / "b2.md") in reason
    shared = json.dumps(rollup.LEVELS["pm"].data(r)) + rollup.LEVELS["pm"].markdown(r)
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


# --- links a Markdown renderer would make on its own -------------------------------------------

AUTOLINK = re.compile(r"(?i)https?://|www\.|@(?!⁠)")


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
    assert not AUTOLINK.search(rollup.escape_md(text))


@pytest.mark.parametrize("level", ["pm", "cto"])
def test_project_text_never_becomes_a_live_link_in_markdown(tmp_path, level):
    z = "http://e.example www.e.example a@e.example"
    q = make_queue(tmp_path / "evil")
    (q / "project.toml").write_text(
        f"[project]\ntitle = {json.dumps(z)}\noutcome = {json.dumps(z)}\n"
        f'[health]\ncolour = "amber"\nreason = {json.dumps(z)}\n'
        f"[ask]\nquestion = {json.dumps(z)}\n"
    )
    text = rollup.LEVELS[level].markdown(rollup.build([rollup.Source(z, tmp_path)], NOW))
    assert "e.example" in text and not AUTOLINK.search(text)

"""Tests for transition.py — resolve a brief by fuzzy query and move it between states.

These run against a temp queue root (never a real queue) and monkeypatch the git sync so
no test ever touches git. Fixture briefs are neutral, illustrative examples.
"""
import contextlib

import pytest
import transition


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _brief(status, bid, title, *, tags="[]", domain="", autonomy="full",
           blockers="[]", body_extra=""):
    return f"""---
id: {bid}
title: {title}
created: 2026-06-01
updated: 2026-06-01
status: {status}
type: todo
importance: 2
autonomy: {autonomy}
estimated-effort: m
due:
domain: {domain}
tags: {tags}
parent:
source: claude
blockers: {blockers}
related: []
---

## Goal

Some goal mentioning {title}.

## Execution log

2026-06-01: captured.
{body_extra}"""


@pytest.fixture
def root(tmp_path):
    """A throwaway queue tree with a handful of briefs across folders."""
    for d in transition.VALID_STATUSES:
        (tmp_path / d).mkdir()
    (tmp_path / "ready" / "provision-and-harden-the-staging-server.md").write_text(
        _brief("ready", "provision-and-harden-the-staging-server",
               "Provision and harden the staging server",
               tags="[infra, staging, vm]", domain="infra")
    )
    (tmp_path / "ready" / "rotate-the-api-key.md").write_text(
        _brief("ready", "rotate-the-api-key",
               "Rotate the leaked API key", domain="billing")
    )
    (tmp_path / "in-progress" / "build-the-billing-checkout-app.md").write_text(
        _brief("in-progress", "build-the-billing-checkout-app",
               "Build the billing checkout app", domain="billing")
    )
    (tmp_path / "parked" / "decide-deploy-strategy.md").write_text(
        _brief("parked", "decide-deploy-strategy",
               "Decide the deployment strategy", autonomy="needs-input")
    )
    # inbox items are NOT briefs and must never be resolved
    (tmp_path / "inbox" / "raw-staging-thought.md").write_text(
        _brief("inbox", "raw-staging-thought", "Random staging thought")
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# fm() frontmatter parsing
# --------------------------------------------------------------------------- #
def test_fm_parses_frontmatter():
    d = transition.fm(_brief("ready", "x", "Title here"))
    assert d["id"] == "x"
    assert d["status"] == "ready"
    assert d["title"] == "Title here"


def test_fm_empty_on_no_frontmatter():
    assert transition.fm("no frontmatter here") == {}


# --------------------------------------------------------------------------- #
# find_brief()
# --------------------------------------------------------------------------- #
def test_find_brief_locates_across_folders(root):
    p = transition.find_brief("build-the-billing-checkout-app", root)
    assert p == root / "in-progress" / "build-the-billing-checkout-app.md"


def test_find_brief_returns_none_when_absent(root):
    assert transition.find_brief("does-not-exist", root) is None


def test_find_brief_ignores_inbox(root):
    # inbox files exist on disk but are pre-briefs, never resolvable as briefs
    assert transition.find_brief("raw-staging-thought", root) is None


# --------------------------------------------------------------------------- #
# resolve_brief() — fuzzy matching
# --------------------------------------------------------------------------- #
def test_resolve_exact_id_wins(root):
    hits = transition.resolve_brief("rotate-the-api-key", root)
    assert hits[0]["id"] == "rotate-the-api-key"
    assert hits[0]["score"] >= 1000


def test_resolve_keyword_ranks_best_first(root):
    hits = transition.resolve_brief("staging", root)
    assert hits[0]["id"] == "provision-and-harden-the-staging-server"


def test_resolve_matches_on_title_words(root):
    hits = transition.resolve_brief("api key", root)
    assert hits[0]["id"] == "rotate-the-api-key"


def test_resolve_tighter_id_wins_tie(root):
    # Two briefs both contain "billing" + "checkout"; the short, specific id (the query
    # nearly IS its id) should outrank the long one that merely contains the words.
    (root / "ready" / "billing-checkout.md").write_text(
        _brief("ready", "billing-checkout", "Billing checkout")
    )
    hits = transition.resolve_brief("billing checkout", root)
    assert hits[0]["id"] == "billing-checkout"
    tight = next(h["score"] for h in hits if h["id"] == "billing-checkout")
    loose = next(h["score"] for h in hits if h["id"] == "build-the-billing-checkout-app")
    assert tight > loose


def test_resolve_matches_on_domain(root):
    ids = [h["id"] for h in transition.resolve_brief("billing", root)]
    assert "build-the-billing-checkout-app" in ids
    assert "rotate-the-api-key" in ids


def test_resolve_no_match_returns_empty(root):
    assert transition.resolve_brief("zzzznevermatches", root) == []


def test_resolve_excludes_inbox(root):
    ids = [h["id"] for h in transition.resolve_brief("staging", root)]
    assert "raw-staging-thought" not in ids


def test_resolve_includes_status_and_path(root):
    hit = transition.resolve_brief("staging", root)[0]
    assert hit["status"] == "ready"
    assert hit["path"].name == "provision-and-harden-the-staging-server.md"


def test_resolve_drops_one_char_tokens(root):
    # a lone "a" should not match everything via a longer id that contains it
    assert transition.resolve_brief("a", root) == []


# --------------------------------------------------------------------------- #
# _update_frontmatter()
# --------------------------------------------------------------------------- #
def test_update_frontmatter_sets_status_and_updated():
    out = transition._update_frontmatter(
        _brief("ready", "x", "T"), "in-progress", "2026-06-02"
    )
    fm = transition.fm(out)
    assert fm["status"] == "in-progress"
    assert fm["updated"] == "2026-06-02"


def test_update_frontmatter_no_frontmatter_returns_text():
    assert transition._update_frontmatter("no fm here", "done", "2026-06-02") == "no fm here"


def test_update_frontmatter_leaves_body_untouched():
    text = _brief("ready", "x", "status: ready in the body should survive")
    out = transition._update_frontmatter(text, "done", "2026-06-02")
    # only ONE frontmatter status line changed; body text intact
    assert "status: ready in the body should survive" in out
    assert transition.fm(out)["status"] == "done"


def test_update_frontmatter_leaves_assignee_alone_by_default():
    text = _brief("ready", "x", "T").replace("parent:", "assignee: alice\nparent:")
    out = transition._update_frontmatter(text, "done", "2026-06-02")
    assert transition.fm(out)["assignee"] == "alice"


def test_update_frontmatter_sets_existing_assignee():
    text = _brief("ready", "x", "T").replace("parent:", "assignee: alice\nparent:")
    out = transition._update_frontmatter(text, "in-progress", "2026-06-02", assignee="bob")
    assert transition.fm(out)["assignee"] == "bob"


def test_update_frontmatter_inserts_assignee_when_absent():
    # briefs written before the field existed have no assignee: line at all
    text = _brief("ready", "x", "T")
    assert "assignee:" not in text
    out = transition._update_frontmatter(text, "in-progress", "2026-06-02", assignee="bob")
    assert transition.fm(out)["assignee"] == "bob"
    assert transition.fm(out)["status"] == "in-progress"


def test_update_frontmatter_can_release_a_claim():
    text = _brief("ready", "x", "T").replace("parent:", "assignee: alice\nparent:")
    out = transition._update_frontmatter(text, "ready", "2026-06-02", assignee="")
    assert transition.fm(out)["assignee"] == ""
    # released as a bare key, with no trailing whitespace left behind
    assert [ln for ln in out.splitlines() if ln.startswith("assignee")] == ["assignee:"]


def test_update_frontmatter_writes_exactly_one_assignee_line():
    text = _brief("ready", "x", "T").replace("parent:", "assignee: alice\nparent:")
    out = transition._update_frontmatter(text, "done", "2026-06-02", assignee="bob")
    assert [ln for ln in out.splitlines() if ln.startswith("assignee")] == ["assignee: bob"]


def test_update_frontmatter_rewrites_only_the_first_assignee_line():
    # a hand-edited brief can end up with a duplicate key; rewrite the first, leave the rest
    text = _brief("ready", "x", "T").replace(
        "parent:", "assignee: alice\nassignee: stray\nparent:")
    out = transition._update_frontmatter(text, "done", "2026-06-02", assignee="bob")
    assert [ln for ln in out.splitlines() if ln.startswith("assignee")] == [
        "assignee: bob", "assignee: stray"]


def test_update_frontmatter_assignee_does_not_touch_body():
    text = _brief("ready", "x", "assignee: someone in the body should survive")
    out = transition._update_frontmatter(text, "done", "2026-06-02", assignee="bob")
    assert "assignee: someone in the body should survive" in out
    assert transition.fm(out)["assignee"] == "bob"


# --------------------------------------------------------------------------- #
# _append_log()
# --------------------------------------------------------------------------- #
def test_append_log_adds_dated_line():
    out = transition._append_log(_brief("ready", "x", "T"), "2026-06-02", "started")
    assert out.rstrip().endswith("2026-06-02: started")


def test_append_log_adds_header_when_missing():
    out = transition._append_log("---\nid: x\n---\n\nbody only\n", "2026-06-02", "started")
    assert "## Execution log" in out
    assert out.rstrip().endswith("2026-06-02: started")


# --------------------------------------------------------------------------- #
# move_brief()
# --------------------------------------------------------------------------- #
def test_move_ready_to_in_progress(root):
    newp = transition.move_brief(
        "rotate-the-api-key", "in-progress", root, today="2026-06-02"
    )
    assert newp == root / "in-progress" / "rotate-the-api-key.md"
    assert not (root / "ready" / "rotate-the-api-key.md").exists()
    fm = transition.fm(newp.read_text())
    assert fm["status"] == "in-progress"
    assert fm["updated"] == "2026-06-02"
    assert newp.read_text().rstrip().endswith("2026-06-02: started")


def test_move_default_log_per_status(root):
    p = transition.move_brief("rotate-the-api-key", "done", root, today="2026-06-02")
    assert p.read_text().rstrip().endswith("2026-06-02: completed")


def test_move_custom_log_message(root):
    p = transition.move_brief(
        "rotate-the-api-key", "parked", root,
        log="ESCALATED — need the API provider login", today="2026-06-02"
    )
    assert "ESCALATED — need the API provider login" in p.read_text()


def test_move_claims_the_brief(root):
    p = transition.move_brief(
        "rotate-the-api-key", "in-progress", root, assignee="alice", today="2026-06-02"
    )
    fm = transition.fm(p.read_text())
    assert fm["assignee"] == "alice"
    assert fm["status"] == "in-progress"


def test_move_without_assignee_leaves_the_claim_unchanged(root):
    claimed = transition.move_brief(
        "rotate-the-api-key", "in-progress", root, assignee="alice", today="2026-06-02"
    )
    assert transition.fm(claimed.read_text())["assignee"] == "alice"
    moved = transition.move_brief("rotate-the-api-key", "done", root, today="2026-06-03")
    assert transition.fm(moved.read_text())["assignee"] == "alice"


def test_move_invalid_status_raises(root):
    with pytest.raises(ValueError, match="invalid status"):
        transition.move_brief("rotate-the-api-key", "bogus", root)


def test_move_missing_brief_raises(root):
    with pytest.raises(ValueError, match="no brief"):
        transition.move_brief("nope", "done", root)


def test_move_same_status_updates_in_place(root):
    p = transition.move_brief("rotate-the-api-key", "ready", root, today="2026-06-02")
    assert p == root / "ready" / "rotate-the-api-key.md"
    assert transition.fm(p.read_text())["updated"] == "2026-06-02"


def test_move_uses_today_by_default(root):
    import datetime
    p = transition.move_brief("rotate-the-api-key", "in-progress", root)
    assert transition.fm(p.read_text())["updated"] == datetime.date.today().isoformat()


def test_move_default_log_parked(root):
    p = transition.move_brief("rotate-the-api-key", "parked", root, today="2026-06-02")
    assert p.read_text().rstrip().endswith("2026-06-02: parked")


def test_move_default_log_back_to_ready(root):
    p = transition.move_brief("build-the-billing-checkout-app", "ready", root, today="2026-06-02")
    assert p.read_text().rstrip().endswith("2026-06-02: moved back to ready")


def test_move_default_log_to_inbox(root):
    p = transition.move_brief("rotate-the-api-key", "inbox", root, today="2026-06-02")
    assert p.read_text().rstrip().endswith("2026-06-02: returned to inbox")


# --------------------------------------------------------------------------- #
# find_brief / resolve cover every brief folder (not just ready/in-progress)
# --------------------------------------------------------------------------- #
def test_find_brief_locates_in_done(root):
    (root / "done" / "shipped-thing.md").write_text(_brief("done", "shipped-thing", "Shipped thing"))
    assert transition.find_brief("shipped-thing", root) == root / "done" / "shipped-thing.md"


def test_find_brief_locates_in_parked(root):
    p = transition.find_brief("decide-deploy-strategy", root)
    assert p == root / "parked" / "decide-deploy-strategy.md"


def test_resolve_finds_parked_brief(root):
    ids = [h["id"] for h in transition.resolve_brief("deploy strategy", root)]
    assert "decide-deploy-strategy" in ids


def test_resolve_matches_title_only(root):
    # The id contains none of the query words; only the title does. A title hit must
    # count POSITIVELY (a sign-flip would push the brief below zero and hide it).
    (root / "ready" / "rotate-creds.md").write_text(
        _brief("ready", "rotate-creds", "Rotate the database credential")
    )
    ids = [h["id"] for h in transition.resolve_brief("database credential", root)]
    assert "rotate-creds" in ids


def test_resolve_matches_tags_only(root):
    (root / "ready" / "tagged-thing.md").write_text(
        _brief("ready", "tagged-thing", "A thing", tags="[urgentwidget]")
    )
    ids = [h["id"] for h in transition.resolve_brief("urgentwidget", root)]
    assert "tagged-thing" in ids


# --------------------------------------------------------------------------- #
# main() CLI
# --------------------------------------------------------------------------- #
def test_main_move_error_exit_code_is_1(root):
    with pytest.raises(SystemExit) as exc:
        transition.main(["move", "nope", "done", "--root", str(root), "--no-sync"])
    assert exc.value.code == 1


def test_main_resolve_prints_candidates(root, capsys):
    transition.main(["resolve", "staging", "--root", str(root)])
    out = capsys.readouterr().out
    assert "provision-and-harden-the-staging-server" in out


def test_main_resolve_json(root, capsys):
    transition.main(["resolve", "staging", "--root", str(root), "--json"])
    import json
    data = json.loads(capsys.readouterr().out)
    assert data[0]["id"] == "provision-and-harden-the-staging-server"


def test_main_resolve_no_match_message(root, capsys):
    transition.main(["resolve", "zzzznever", "--root", str(root)])
    assert "No matching brief" in capsys.readouterr().out


def test_main_move_commits(root, monkeypatch, capsys):
    calls = {}

    def fake_sync(msg, rootpath=None):
        calls["msg"] = msg
        return True

    monkeypatch.setattr(transition, "sync_commit", fake_sync)
    transition.main(["move", "rotate-the-api-key", "in-progress",
                     "--root", str(root), "--today", "2026-06-02"])
    assert (root / "in-progress" / "rotate-the-api-key.md").exists()
    assert "brief(rotate-the-api-key)" in calls["msg"]


def test_main_move_accepts_assignee_flag(root, monkeypatch):
    monkeypatch.setattr(transition, "sync_commit", lambda *a, **k: True)
    transition.main(["move", "rotate-the-api-key", "in-progress", "--assignee", "alice",
                     "--root", str(root), "--today", "2026-06-02"])
    text = (root / "in-progress" / "rotate-the-api-key.md").read_text()
    assert transition.fm(text)["assignee"] == "alice"


def test_main_move_without_assignee_preserves_the_claim(root, monkeypatch):
    # guards the CLI default: a plain move must never silently release an existing claim
    monkeypatch.setattr(transition, "sync_commit", lambda *a, **k: True)
    transition.main(["move", "rotate-the-api-key", "in-progress", "--assignee", "alice",
                     "--root", str(root), "--today", "2026-06-02"])
    transition.main(["move", "rotate-the-api-key", "done",
                     "--root", str(root), "--today", "2026-06-03"])
    text = (root / "done" / "rotate-the-api-key.md").read_text()
    assert transition.fm(text)["assignee"] == "alice"


def test_main_move_no_sync_skips_git(root, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(transition, "sync_commit",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    transition.main(["move", "rotate-the-api-key", "done",
                     "--root", str(root), "--no-sync"])
    assert called["n"] == 0


def test_main_move_missing_brief_exits(root):
    with pytest.raises(SystemExit):
        transition.main(["move", "nope", "done", "--root", str(root)])


def test_main_root_defaults_to_resolver(tmp_path, monkeypatch):
    # exercise the default-root branch hermetically: with --root omitted, main() must
    # fall back to resolve_root(). Point that at an empty temp queue via the env var so
    # the test never reads the machine's real ~/tasks.
    monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path))
    transition.main(["resolve", "zzzznevermatches-xyz"])  # empty → no match, must not raise


# --------------------------------------------------------------------------- #
# Concurrency: move_brief is an atomic claim under repo_lock
# --------------------------------------------------------------------------- #
def test_move_brief_concurrent_only_one_wins(root, monkeypatch):
    """Two threads both find the same source path, then race to the lock.
    Whichever wins moves the file; the loser's path.exists() check returns False → ValueError.
    The winner's file has no doubled frontmatter."""
    import threading

    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    orig_repo_lock = transition.repo_lock

    @contextlib.contextmanager
    def slow_lock(r, **kw):
        barrier.wait()  # both threads reach here before either acquires the lock
        with orig_repo_lock(r, **kw) as acquired:
            yield acquired

    monkeypatch.setattr(transition, "repo_lock", slow_lock)

    def claim():
        try:
            p = transition.move_brief(
                "provision-and-harden-the-staging-server", "in-progress",
                root, today="2026-06-02",
            )
            results.append(p)
        except ValueError:
            errors.append("claimed")

    t1 = threading.Thread(target=claim)
    t2 = threading.Thread(target=claim)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1, f"expected 1 winner, got {len(results)}"
    assert len(errors) == 1, f"expected 1 loser, got {len(errors)}"
    content = results[0].read_text()
    assert content.count("status: in-progress") == 1, "frontmatter status line doubled"
    assert content.count("---") == 2, "frontmatter block doubled"


def test_move_brief_lock_timeout_raises(root, monkeypatch):
    """If the lock cannot be acquired within the timeout, RuntimeError is raised."""
    import contextlib

    @contextlib.contextmanager
    def fake_lock(_root, **_kw):
        yield False  # simulate timeout: acquired=False

    monkeypatch.setattr(transition, "repo_lock", fake_lock)
    with pytest.raises(RuntimeError, match="timed out"):
        transition.move_brief("rotate-the-api-key", "in-progress", root)

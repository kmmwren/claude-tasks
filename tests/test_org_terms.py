"""No tracked file names the organisations or vendors this public repo must stay free of.

The terms are held only as SHA-256 hashes of lowercased words, so this file never spells them. A
failure names the file and the first characters of the hash, never the word itself.
"""

import hashlib
import pathlib
import re
import subprocess
from collections.abc import Iterable

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORD = re.compile(r"[A-Za-z0-9]+")

BARRED_HASHES = frozenset(
    {
        "6acd03bed51900206f196904366de4cb54507e80fd4074c07e5b10df72e0d41e",
        "72a4e45fdeed82dcaf60d1190a5912627df23b618ec3c45c1a041258d818f4d1",
        "4c08419ec395c4ec0758f3e87489859f5318e1a117250730272dbb7b65fb612d",
    }
)


def digest(word: str) -> str:
    return hashlib.sha256(word.lower().encode()).hexdigest()


def barred_in(text: str, barred: frozenset[str]) -> list[str]:
    """The hashes of the barred words `text` holds, once each, in order of first appearance."""
    seen = dict.fromkeys(digest(w) for w in WORD.findall(text))
    return [h for h in seen if h in barred]


def tracked_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Every file git tracks or would add, so a new file is scanned before it is committed."""
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / rel for rel in listing.split("\0") if rel and (root / rel).is_file()]


def offences(
    root: pathlib.Path, files: Iterable[pathlib.Path], barred: frozenset[str]
) -> list[str]:
    """`path: hash prefix` for every barred word in a file's name or text."""
    found = []
    for path in files:
        rel = path.relative_to(root)
        text = f"{rel}\n{path.read_text(errors='ignore')}"
        found += [f"{rel}: {h[:12]}" for h in barred_in(text, barred)]
    return found


def test_no_tracked_file_names_a_barred_term():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert offences(ROOT, tracked_files(ROOT), BARRED_HASHES) == []


def test_a_word_is_matched_whatever_its_case_and_wherever_it_sits():
    barred = frozenset({digest("forbidden")})
    for text in (
        "Forbidden",
        "a-FORBIDDEN.example",
        "x_forbidden_y",
        "(forbidden)",
        "a/forbidden/b",
    ):
        assert barred_in(text, barred) == [digest("forbidden")], text


def test_a_word_inside_a_longer_word_is_not_matched():
    assert barred_in("unforbidden forbiddenly", frozenset({digest("forbidden")})) == []


def test_a_term_is_reported_once_however_often_it_appears():
    barred = frozenset({digest("forbidden")})
    assert barred_in("forbidden forbidden", barred) == [digest("forbidden")]


def test_a_file_name_is_scanned_as_well_as_its_text(tmp_path):
    (tmp_path / "forbidden.txt").write_text("fine")
    (tmp_path / "ok.txt").write_text("a forbidden word")
    (tmp_path / "clean.txt").write_text("nothing here")
    found = offences(tmp_path, sorted(tmp_path.iterdir()), frozenset({digest("forbidden")}))
    assert [f.split(":")[0] for f in found] == ["forbidden.txt", "ok.txt"]
    assert all("forbidden" not in f.split(": ")[1] for f in found)


def test_a_new_untracked_file_is_scanned_and_an_ignored_one_is_not(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("skipped.txt\n")
    (tmp_path / "new.txt").write_text("x")
    (tmp_path / "skipped.txt").write_text("x")
    names = sorted(p.name for p in tracked_files(tmp_path))
    assert names == [".gitignore", "new.txt"]


def test_a_file_git_lists_but_that_is_gone_is_skipped(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "gone.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "gone.txt"], check=True)
    (tmp_path / "gone.txt").unlink()
    assert tracked_files(tmp_path) == []


def test_the_barred_hashes_are_sha256_digests_of_lowercase_words():
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in BARRED_HASHES)
    assert len(BARRED_HASHES) >= 3

"""transition.py move commits the moved brief's own paths and nothing else in the queue."""
import subprocess

import git_sync
import transition

ID = "rotate-the-api-key"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout


def _queue(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", "-q", str(origin), str(main)], check=True, capture_output=True)
    for d in transition.VALID_STATUSES:
        (main / d).mkdir()
    (main / "ready" / f"{ID}.md").write_text(f"---\nid: {ID}\nstatus: ready\n---\n\n## Execution log\n\n2026-01-01: captured.\n")
    (main / "ready" / "other.md").write_text("---\nid: other\nstatus: ready\n---\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "base")
    git(main, "push", "-q", "origin", "HEAD:main")
    git(main, "config", "user.name", "t")
    git(main, "config", "user.email", "t@t")
    return origin, main


def test_move_commits_only_the_moved_briefs_paths(tmp_path):
    origin, main = _queue(tmp_path)
    (main / "ready" / "other.md").write_text("---\nid: other\nstatus: ready\n---\nhalf-written note\n")
    (main / "inbox" / "peer-unrelated-1234abcd.md").write_text("---\nid: peer-unrelated-1234abcd\n---\n")
    transition.main(["move", ID, "in-progress", "--root", str(main)])
    assert git_sync.wait_for_push(main)
    changed = git(origin, "show", "--no-renames", "--name-status", "--format=", "main").split("\n")
    assert sorted(filter(None, changed)) == [f"A\tin-progress/{ID}.md", f"D\tready/{ID}.md"]
    status = git(main, "status", "--porcelain", "--untracked-files=all")
    assert " M ready/other.md" in status and "?? inbox/peer-unrelated-1234abcd.md" in status

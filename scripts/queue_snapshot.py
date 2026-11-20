"""A project queue exactly as its GitHub default branch holds it, never the local working copy."""

import dataclasses
import datetime
import hashlib
import io
import json
import os
import pathlib
import subprocess
import tarfile
import tempfile

import _default_branch
from _paths import warn

CACHE_ENV = "CLAUDE_TASKS_CACHE"
DEFAULT_CACHE = "~/.claude-tasks/queue-snapshots"
REMOTE_TIMEOUT = 60.0  # a remote that does not answer must degrade to the cache, not hang a writer
META_FILE = "snapshot.json"
HEAD_POINTER = "head"  # the sha of the last head read online, which commit times cannot tell apart
TREE_DIR = "tree"


class SnapshotUnavailable(RuntimeError):
    """The queue cannot be read from its remote and no earlier snapshot of it is cached."""


class _GitError(RuntimeError):
    """A git command failed or timed out; the message carries git's own words."""


@dataclasses.dataclass(frozen=True)
class QueueSnapshot:
    """One commit's copy of the queue: `root` is the extracted queue directory."""

    root: pathlib.Path
    sha: str
    committed: datetime.datetime
    branch: str
    stale: bool


def cache_root(override: pathlib.Path | None = None) -> pathlib.Path:
    """Where snapshots live: the argument, else $CLAUDE_TASKS_CACHE, else a per-user default."""
    return override or pathlib.Path(os.environ.get(CACHE_ENV) or DEFAULT_CACHE).expanduser()


def _git(*args: str, cwd: pathlib.Path | None = None) -> bytes:
    """Git's stdout; a failure or timeout raises with git's message so callers can degrade."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # an auth failure must fail, never prompt
    try:
        run = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, timeout=REMOTE_TIMEOUT, env=env
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _GitError(f"git {args[0]}: {exc}") from exc
    if run.returncode != 0:
        raise _GitError(f"git {args[0]}: {run.stderr.decode(errors='replace').strip()}")
    return run.stdout


def _git_text(*args: str, cwd: pathlib.Path | None = None) -> str:
    return _git(*args, cwd=cwd).decode().strip()


def _relative(queue_path: str) -> str:
    """The queue path normalised; one that leaves the repo is a caller bug, not a fetch failure."""
    path = pathlib.PurePosixPath(queue_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"queue path {queue_path!r} must stay inside the repo")
    return path.as_posix()


def _extract_tar(data: bytes, dest: pathlib.Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def _publish(work: pathlib.Path, final: pathlib.Path) -> None:
    """Move a finished snapshot into place; a rival process that got there first is not an error."""
    try:
        work.rename(final)
    except OSError:
        if not final.is_dir():
            raise


@dataclasses.dataclass(frozen=True)
class QueueRepo:
    """Where a queue is fetched from: `remote`, and the local `clone` that named it, if any."""

    remote: str
    clone: pathlib.Path | None = None

    @classmethod
    def parse(cls, repo: str | os.PathLike[str]) -> "QueueRepo":
        """A clone stands for its origin; anything else (a URL, a bare repo) is the remote itself."""
        path = pathlib.Path(repo).expanduser()
        if not (path / ".git").exists():
            return cls(os.fspath(repo))
        try:
            return cls(_git_text("remote", "get-url", "origin", cwd=path), path)
        except _GitError as exc:
            raise SnapshotUnavailable(f"{path} has no origin to read the queue from") from exc


@dataclasses.dataclass(frozen=True)
class _Mirror:
    """The bare cache repo and extracted snapshots for one remote and queue path."""

    repo: QueueRepo
    queue_path: str
    home: pathlib.Path

    @classmethod
    def of(
        cls, repo: str | os.PathLike[str], queue_path: str, cache_dir: pathlib.Path | None
    ) -> "_Mirror":
        parsed, path = QueueRepo.parse(repo), _relative(queue_path)
        key = hashlib.sha256(f"{parsed.remote}\0{path}".encode()).hexdigest()[:12]
        return cls(parsed, path, cache_root(cache_dir) / key)

    @property
    def git_dir(self) -> pathlib.Path:
        return self.home / "repo.git"

    def init(self) -> pathlib.Path:
        if not self.git_dir.exists():
            self.home.mkdir(parents=True, exist_ok=True)
            _git("init", "--bare", "-q", str(self.git_dir))
            _git("remote", "add", "origin", self.repo.remote, cwd=self.git_dir)
        return self.git_dir

    def default_branch(self) -> str:
        return _default_branch.default_branch(self.repo.clone or self.init())

    def remote_head(self, branch: str) -> str:
        found = _git_text("ls-remote", self.repo.remote, f"refs/heads/{branch}")
        if not found:
            raise _GitError(f"ls-remote: no branch {branch} on {self.repo.remote}")
        return found.split()[0]

    def cached(self, sha: str) -> QueueSnapshot | None:
        folder = self.home / sha
        return self.load(folder) if folder.is_dir() else None

    def load(self, folder: pathlib.Path) -> QueueSnapshot:
        meta = json.loads((folder / META_FILE).read_text(encoding="utf-8"))
        committed = datetime.datetime.fromtimestamp(meta["committed"], datetime.timezone.utc)
        return QueueSnapshot(
            folder / TREE_DIR / self.queue_path, folder.name, committed, meta["branch"], False
        )

    def fetch(self, sha: str) -> None:
        # A ref keeps the commit reachable, so gc cannot prune a snapshot's history.
        refspec = f"{sha}:refs/snapshots/{sha}"
        _git("fetch", "--quiet", "--no-tags", self.repo.remote, refspec, cwd=self.init())

    def materialise(self, sha: str, branch: str) -> QueueSnapshot:
        """Archive `sha` from the mirror into `<home>/<sha>/`, published whole or not at all."""
        committed = int(_git_text("log", "-1", "--format=%ct", sha, cwd=self.git_dir))
        archive = _git("archive", "--format=tar", sha, "--", self.queue_path, cwd=self.git_dir)
        with tempfile.TemporaryDirectory(dir=self.staging()) as tmp:
            work = pathlib.Path(tmp)
            (work / META_FILE).write_text(
                json.dumps({"branch": branch, "committed": committed}), encoding="utf-8"
            )
            _extract_tar(archive, work / TREE_DIR)
            _publish(work, self.home / sha)
        return self.load(self.home / sha)

    def head(self) -> QueueSnapshot:
        branch = self.default_branch()
        sha = self.remote_head(branch)
        if (hit := self.cached(sha)) is not None:
            return self.remember(hit)
        self.fetch(sha)
        return self.remember(self.materialise(sha, branch))

    def staging(self) -> pathlib.Path:
        staging = self.home / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def remember(self, head: QueueSnapshot) -> QueueSnapshot:
        """Record `head` as the remote's last-seen head, replacing the pointer in one rename."""
        with tempfile.NamedTemporaryFile(
            "w", dir=self.staging(), delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(head.sha)
        os.replace(tmp.name, self.home / HEAD_POINTER)
        return head

    def last_seen(self) -> QueueSnapshot | None:
        """The snapshot the pointer names, if it is still cached."""
        pointer = self.home / HEAD_POINTER
        sha = pointer.read_text(encoding="utf-8").strip() if pointer.exists() else ""
        return self.cached(sha) if sha else None

    def newest(self) -> QueueSnapshot | None:
        """The latest commit time; commits in one second tie, so `stale` asks the pointer first."""
        found = [self.load(meta.parent) for meta in self.home.glob(f"*/{META_FILE}")]
        return max(found, key=lambda snapshot: snapshot.committed, default=None)

    def stale(self, error: Exception) -> QueueSnapshot:
        """The newest cached snapshot, flagged stale, with one warning; none cached is an error."""
        reason = " ".join(str(error).split())
        newest = self.last_seen() or self.newest()
        if newest is None:
            raise SnapshotUnavailable(
                f"{self.repo.remote}: {reason}; no snapshot is cached"
            ) from error
        warn(f"{self.repo.remote} is not reachable ({reason}); using cached snapshot {newest.sha}")
        return dataclasses.replace(newest, stale=True)

    def before(self, head: QueueSnapshot, days: int) -> QueueSnapshot | None:
        cutoff = (head.committed - datetime.timedelta(days=days)).isoformat()
        sha = _git_text("rev-list", "-1", f"--before={cutoff}", head.sha, cwd=self.git_dir)
        if not sha:
            return None
        found = self.cached(sha) or self.materialise(sha, head.branch)
        return dataclasses.replace(found, stale=head.stale)


def read_queue(
    repo: str | os.PathLike[str], queue_path: str, *, cache_dir: pathlib.Path | None = None
) -> QueueSnapshot:
    """The queue at the head of `repo`'s default branch, or the newest cached copy marked stale.

    Raises SnapshotUnavailable when the remote cannot be reached and nothing is cached.
    """
    mirror = _Mirror.of(repo, queue_path, cache_dir)
    try:
        return mirror.head()
    except (_GitError, _default_branch.DefaultBranchError) as exc:
        return mirror.stale(exc)


def read_queue_before(
    repo: str | os.PathLike[str],
    queue_path: str,
    head: QueueSnapshot,
    days: int,
    *,
    cache_dir: pathlib.Path | None = None,
) -> QueueSnapshot | None:
    """The queue at the last commit `days` before `head` on its branch, or None if none is that old.

    Reads the mirror `read_queue` filled, so it needs no network.
    """
    mirror = _Mirror.of(repo, queue_path, cache_dir)
    try:
        return mirror.before(head, days)
    except _GitError as exc:
        raise SnapshotUnavailable(f"{mirror.repo.remote}: {exc}") from exc

# How the work queue syncs

The queue (for example `~/tasks`) is a git repo, and GitHub `main` is the shared copy. Changes reach it in two ways:

- **On every write.** Every script that changes a brief (`add_task`, `transition`, `append_log`, `bump`, `unblock` and the rest) commits straight away under a cross-process lock and marks the clone dirty. One background pusher per clone then pushes the whole burst once.
- **Every 60 seconds, as a safety net.** A scheduled job (a LaunchAgent such as `com.example.claude-tasks-sync`, or a cron entry) runs `sync.sh`. It first runs `git_sync.py --sweep`, which commits anything no writer named and pushes it, then catches the local checkout up.

```mermaid
flowchart TD
    W["Writer script<br/>add_task, transition, append_log ..."] -->|commit under lock,<br/>mark clone dirty| C[(local main<br/>~/tasks)]
    W --> P["Detached pusher<br/>one per burst"]
    T(["Scheduled job every 60s"]) --> S["sync.sh"]
    S --> SW["git_sync.py --sweep<br/>commit stragglers"]
    SW --> C
    SW --> P2["push"]
    P --> R
    P2 --> R
    R{"Rebase onto origin/main<br/>in a scratch worktree"}
    R -->|clean| PUSH[["push to GitHub main"]]
    R -->|both sides only appended<br/>to the same brief| M["Log merge<br/>union the appended lines,<br/>shared bullet kept once"]
    M --> PUSH
    R -->|any other conflict| PARK[["park local commits on<br/>origin/wip/queue-sync-host-date<br/>and print ALERT"]]
    S --> F["fetch origin"]
    F -->|local behind| FF["fast-forward only"]
    F -->|level or ahead| NOOP["nothing to do"]
    F -->|diverged| LEAVE["log it; the sweep owns it"]
    FF --> C
```

## What each outcome means

| Outcome | What happened | What to do |
|---|---|---|
| Pushed | The rebase was clean, or the two sides only appended to the same brief | Nothing |
| Parked (`ALERT`) | A real conflict, such as both sides editing the same line or frontmatter | Merge the `wip/queue-sync-*` branch into main from a worktree, then delete it |
| Diverged line in `sync.sh` log | Local and GitHub both moved and the sweep has not reconciled them yet | Nothing; the next sweep or the parked branch carries it |

`sync.sh` never rebases on its own. The checked-out `main` only ever moves by commit or fast-forward, so a conflict can never leave the checkout half-rebased.

## A queue inside another repo

A project queue such as `<repo>/projects/x/.tasks` has no repo of its own, so its writes are never committed to the host checkout. `sync_commit` returns `hosted` and `host_land.land_host_queue` ships the queue instead:

1. It takes the queue lock and lists the queue's pending paths with `git status --porcelain` in the host.
2. It fetches the host's default branch and adds a temporary detached worktree cut from `origin/<default>`.
3. It copies the pending files in, removes the ones deleted on disk, and records them as one commit using the write's usual message.
4. It pushes the commit to `origin/<default>`. A push that fails while the tip has moved lost a race: the commit is rebased onto the new tip (log-only conflicts in a brief resolve as in `git_sync`) and pushed again, up to three pushes.
5. Only when the default branch refuses the push (protected branch, a failing pre-push or pre-receive gate, a push timeout, a rebase conflict, or three lost races) does it rebuild the commit on `wip/tasks-<queue-slug>` (`projects/x/.tasks` becomes `wip/tasks-projects-x`), force-push that branch, and print `host land parked on origin/wip/tasks-...` with the refusal. Merging that branch is then the host's own land.
6. When the host checkout is on its default branch and holds no commit origin lacks, it advances to `origin/<default>` through the same `_advance.advance_to_origin` that `git_sync` uses. Each file the checkout holds in exactly the state origin now carries (an untracked copy, a tracked edit, a deletion) is put back to HEAD's state first, so the reset can bring it in; if the reset still refuses, every cleared file is written back and the land prints the recovery command. A file that differs from origin's, or that is staged, is never touched. On any other branch, or when the host is ahead of origin, the checkout stays as it is.

Until step 6 runs, the host's index, HEAD and working files never change, and a failed advance leaves them as they were. Each commit is built from `origin/<default>` plus everything still pending, so the working files are the truth for the paths they cover. A file that looks like a credential stays out. If a step fails, `sync_commit` returns `deferred`, the files stay on disk and the next write or sweep retries.

`check_deps.py` reports `unshipped`, one per brief, for a pending brief older than six hours (a land that has deferred, or one parked on a wip branch that nobody merged). `python scripts/host_land.py <queue>` lands one by hand.

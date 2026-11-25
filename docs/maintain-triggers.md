# Maintain triggers

`scripts/maintain.py` is the one entry point that wakes due briefs and runs the queue's tidy-up. It assumes no platform: anything that can run it on a checkout of the queue repo is a trigger. Two are registered.

| Trigger | Runs | Cadence | Tier |
| --- | --- | --- | --- |
| LaunchAgent on the laptop | while the laptop is on | every 4 hours, and at load | `laptop` |
| Cloud routine | in Anthropic's cloud | every 4 hours, skipped when a run recently succeeded | `cloud` |

Neither is GitHub Actions. Both end at the same command and the same record, so a third trigger needs only that command.

## What a run records

A hygiene phase (`deterministic`, `judgement`) is recorded in `maintain/last-run/<phase>.json`, in the commit that releases the hygiene lease, only when every step of the phase succeeded. A failed, lapsed or deferred phase leaves no record, so a record on origin is a success by construction. Each tier also keeps `maintain/last-run/<tier>.json` in its own checkout; that one is local and is not pushed.

## Skipping a run that recently succeeded

`--skip-if-succeeded-within DURATION` (`90s`, `30m`, `4h`, `2d`) fetches origin, then exits 0 with nothing run, claimed or recorded when every phase has a record younger than the duration. A missing record, a record older than the duration, or a phase that never completed (a failure, or the judgement phase deferred for want of credits) runs a normal tick. Without the option nothing changes.

## Laptop trigger

`launchd/install.sh` renders `launchd/com.claude-tasks.maintain.plist.template` into `~/Library/LaunchAgents/com.claude-tasks.maintain.plist`. The agent runs `maintain.py --tier laptop --root <queue>` every `StartInterval` seconds and once at load.

`StartInterval` is the queue's `[maintain] hygiene_interval_minutes` from `tasks.toml`, 240 minutes (14400 seconds) by default (`DEFAULT_HYGIENE_INTERVAL_MINUTES` in `scripts/maintain_config.py`). That is the span after which a tick has tidy-up work to do, so waking more often only reads records. The laptop does not pass the skip option: it is the primary runner.

```
launchd/install.sh --print     # render the plist; writes nothing
launchd/install.sh             # write it and load it
launchd/install.sh --uninstall
```

Environment: `QUEUE` (default `~/work-tasks`), `LAUNCHER` (default the queue's `[rank] launcher`), `PYTHON`.

## Cloud trigger

A cloud routine attaches two repos, which the platform clones to `/home/user/<name>`: `kmmwren/work-tasks` (the queue, with "Allow unrestricted git push" on, because the claim and hygiene commits go to the default branch) and `kmmwren/claude-tasks` (the scripts). Schedule `37 */4 * * *` (UTC, every 4 hours). Allowed tools: `Bash`.

The command:

```
python3 /home/user/claude-tasks/scripts/maintain.py --tier cloud --root /home/user/work-tasks --skip-if-succeeded-within 4h
```

The prompt registered with the routine:

```
Two repos are attached: /home/user/work-tasks (the task queue) and /home/user/claude-tasks (its scripts).
Run exactly this command and do nothing else; do not ask for confirmation and do not edit any file:

python3 /home/user/claude-tasks/scripts/maintain.py --tier cloud --root /home/user/work-tasks --skip-if-succeeded-within 4h

Exit 0 means a clean run or a skip because a run recently succeeded. Exit 75 means judgement work was deferred
for want of credits; exit 1 means a hygiene step failed. In every case, finish by printing the exit status
and any lines the command printed to stdout. Never retry the command.
```

The window equals the laptop cadence, so a cloud fire within 4 hours of a laptop run does nothing. A cloud run serves the capabilities in `[maintain.cloud] capabilities` (none by default) and claims a brief only when a launcher script exists in the cloud environment; the deterministic hygiene steps that need only the plugin scripts run either way.

## Verifying each fired

Both triggers advance the same record. From any checkout of the queue:

```
git fetch origin && git show origin/main:maintain/last-run/deterministic.json   # "at" is the last successful run
git log origin/main -5 --format='%cI %s' -- maintain/last-run                   # "maintain: <tier>@<host>:<pid> finishes hygiene" names the runner
```

- Laptop: `at` in `~/work-tasks/maintain/last-run/laptop.json` is the last tick the laptop ran, and `~/Library/Logs/claude-tasks-maintain.log` shows each one. `launchctl print gui/$(id -u)/com.claude-tasks.maintain` shows the agent loaded.
- Cloud: the routine's run list at `https://claude.ai/code/routines/<trigger_id>` shows each fire and its log. A fire that skipped prints nothing and exits 0; a fire that ran adds a `maintain: cloud@<host>:<pid> finishes hygiene` commit to origin.
- Neither fired within 4 hours plus its jitter when the `deterministic.json` `at` is older than that.

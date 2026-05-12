# reddit-automation

Per-account Reddit warmup agent. Each account follows a 14-day plan (lurk → comment → post) executed by Claude Code driving an anti-detect browser (Multilogin) via Playwright.

## Architecture

```
cli.py                          start/pause/resume/status/stop/run/reject/list
AGENT_PROMPT.md                 system prompt fed to each scheduled session
plan.md                         canonical 14-day warmup plan
config.template.json            account config template

lib/
  db.py                         per-account SQLite state store
  multilogin.py                 facade over working-with-multilogin skill
  creepjs.py                    stealth verification (Trust Score + lies)
  jitter.py                     next-session timing with active-hours + skip-day
  scheduler.py                  Task Scheduler (Win) / at (Linux) wrappers

accounts/<username>/            per-account state (gitignored)
  config.json
  .env                          MULTILOGIN_EMAIL, MULTILOGIN_PASSWORD
  plan.md                       working copy of the canonical plan
  state.db                      SQLite log of every session/action/draft
  lock, pause                   concurrency + override flags
  recordings/                   ffmpeg captures (optional)
```

## Quick start

```bash
# 1. Bootstrap a new account (creates Multilogin profile via API)
python cli.py start <username>

# 2. Run first Day 1 session manually so you can watch
python cli.py run <username>

# 3. Agent self-schedules subsequent sessions via Task Scheduler / cron
python cli.py status <username>
```

## Override controls

| Command | Effect |
|---|---|
| `cli.py pause <user>`  | Agent honors at next wake; logs `paused` and reschedules +6hrs |
| `cli.py resume <user>` | Removes pause flag |
| `cli.py reject <draft_id>` | Marks a pending draft `rejected` so the agent skips it |
| `cli.py stop <user>` | Removes the scheduled task; account data preserved |

## Design choices

- **One row per action** in a single SQLite table — chronological audit trail, easy queries.
- **Inverted approval** — drafts default to `approved` with a 30-min submission delay. Override via `cli reject`.
- **Multilogin profile per account** — fingerprint isolation; profile `notes` field carries redundant state backup.
- **Self-rescheduling agent** — each session is short, stateless between runs, and writes its own next-run entry. No long-running daemon.
- **CreepJS gate** — every Nth session, verify the profile still passes a stealth check before any Reddit activity.

## Dependencies

- Python 3.12+
- `playwright`, `requests`
- [`working-with-multilogin` skill](../../skills/user/working-with-multilogin/) (imported via `lib/multilogin.py`)
- Multilogin X (running locally; eventually moves to a Linux server)
- Claude Code CLI (`claude -p --dangerously-skip-permissions ...`)

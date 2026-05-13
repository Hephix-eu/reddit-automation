# reddit-automation

Per-account autonomous Reddit warmup. Each account follows a 14-day plan (lurk → comment → post). A scheduled task fires Claude Code headlessly once per session; the agent drives a Multilogin anti-detect browser via Playwright, performs humanlike actions, logs to SQLite, and reschedules itself.

```
        scheduler (schtasks/at)
                 │
                 ▼
    cli.py run <username>
                 │
                 ▼  claude -p --append-system-prompt-file AGENT_PROMPT.md
        ┌────────────────────┐
        │ autonomous claude  │
        │ session (~15 min)  │
        └────────────────────┘
                 │
        ┌────────┴────────┐
        ▼                 ▼
   Multilogin API     SQLite state
   (cloud + launcher) (per-account)
        │
        ▼
   Playwright over CDP
        │
        ▼
   Reddit (anti-detect Chrome)
```

## File layout

```
cli.py                          start | run | pause | resume | status | stop | reject | list
AGENT_PROMPT.md                 system prompt fed to every scheduled session
plan.md                         canonical 14-day warmup plan (copied per-account)
config.template.json            template config for new accounts

lib/
  db.py                         per-account SQLite — one table for sessions/drafts/actions/errors
  multilogin.py                 thin facade over working-with-multilogin skill
  browse.py                     human_scroll() + dwell() — no constant-rhythm loops anywhere
  creepjs.py                    stealth verification (disabled by default — see Known issues)
  jitter.py                     next-session timing: active hours, skip-day, no round minutes
  scheduler.py                  Task Scheduler (Windows) + `at` (Linux) wrappers

tests/
  test_creepjs_extract.py       regex unit tests
  test_browser_integration.py   Phase 2: Multilogin→Playwright→Reddit→SQLite, no claude
  login_to_reddit.py            one-time Reddit login into a fresh profile (manual onboarding)
  dismiss_cookies.py            one-time cookie consent dismissal
  debug_creepjs.py              diagnostic for the CreepJS hang issue

accounts/<username>/            (gitignored — per-machine state)
  config.json                   profile_id, folder_id, paths, start_date, etc.
  .env                          MULTILOGIN_EMAIL, MULTILOGIN_PASSWORD, REDDIT_USERNAME, REDDIT_PASSWORD
  plan.md                       working copy of the canonical plan
  state.db                      SQLite — every action ever taken
  lock                          present while a session is running
  pause                         user creates this to halt; agent honors at next wake
  screenshots/, recordings/     visual record
```

## Lifecycle in one paragraph

`cli start <user>` bootstraps an account: prompts for Multilogin email/password/anchor sub, creates a fresh Multilogin profile via API (no proxy, default Mimic desktop flags), writes `accounts/<user>/{config.json, .env, plan.md}`. You then manually log into Reddit once (`tests/login_to_reddit.py <user>`) so cookies persist in the profile. After that, every `cli run <user>` invocation (manual or scheduler-triggered) spawns claude headlessly with `AGENT_PROMPT.md` as the system prompt. The agent: acquires a lock, computes today's plan day from `start_date`, opens the Multilogin profile, connects Playwright over CDP, performs humanlike browsing/upvoting/commenting per the plan, writes every action to SQLite as it goes, computes the next-run time via `lib/jitter.py`, registers a one-shot scheduled task for that time, stops the Multilogin profile via API (never `browser.close()`), releases the lock, and exits. The pattern is stateless between runs — Notion-style external state is unnecessary because SQLite + Multilogin's `notes` field carry everything.

## Quick start

```bash
# 0. Prereqs: Python 3.12+, playwright (+chromium), Multilogin X account, Claude Code CLI
pip install playwright requests
playwright install chromium

# 1. Bootstrap a new account (creates Multilogin profile via API)
python cli.py start HovercraftWeary8654

# 2. One-time onboarding — log into Reddit so cookies persist in the Multilogin profile
python tests/login_to_reddit.py HovercraftWeary8654
python tests/dismiss_cookies.py HovercraftWeary8654       # reject cookie dialog once

# 3. Manual first run so you can watch the agent work
python cli.py run HovercraftWeary8654

# 4. Status / monitoring
python cli.py status HovercraftWeary8654
sqlite3 accounts/HovercraftWeary8654/state.db \
  "SELECT executed_at, type, action_type, subreddit FROM actions_log ORDER BY executed_at DESC LIMIT 20"

# 5. After step 3, the agent self-schedules subsequent runs. You don't need to do anything.
```

## Commands

| Command | Effect |
|---|---|
| `cli.py start <user>` | Create Multilogin profile + account folder. Refuses if folder exists. |
| `cli.py run <user>` | Invoke claude headlessly to execute one session. Used by scheduler; also runnable manually. |
| `cli.py status <user>` | Show start date, day, lock/pause state, total karma, pending drafts. |
| `cli.py pause <user>` | Touch `pause` flag. Agent honors at next wake, reschedules +6h. |
| `cli.py resume <user>` | Remove `pause` flag. Next scheduled run executes normally. |
| `cli.py reject <draft_id>` | Mark a pending Draft `rejected` so the agent skips it. 8-char prefix works. |
| `cli.py stop <user>` | Cancel the scheduled task. Account folder + data preserved. |
| `cli.py list` | All accounts present. |

## SQLite schema

One table, `actions_log`, with columns:

```
id              UUID PRIMARY KEY
type            Session | Draft | Action | StateSnapshot | Error
status          scheduled | pending_review | approved | rejected | submitted | failed | done
day             1-14
action_type     browse | upvote | comment | post | reconnaissance | subscribe | save | …
subreddit       r/dotnet, …
target_url      thread / post URL
draft_content   agent's drafted text (for review window)
submitted_content   what was actually posted
reasoning       why the agent chose this action — debugging gold
session_id      groups rows from same browsing session
scheduled_for   ISO8601 UTC, for next-run rows
executed_at     ISO8601 UTC, auto-filled by helpers
result          outcome string (e.g. "karma=+3 url=...")
profile_id      Multilogin profile id
name            human-readable label
```

## Design choices

- **One row per action**: chronological audit trail, no joins. ~50-200 rows per 14-day account.
- **Inverted approval**: drafts default `approved` with a 30-min submission delay. Override via `cli reject`. Agent self-flags `pending_review` only when its own confidence is low (NSFW, off-tone, etc.).
- **Self-rescheduling**: each session writes its own next-run scheduled-task entry, then exits. No daemons.
- **Profile per account, no proxy by default**: Latvia user → Latvia IP → coherent geo. Add `gate.multilogin.com` proxy later if you need a country switch.
- **`lib/browse.py` is mandatory**: every scroll goes through `human_scroll()` to avoid the periodic scroll-velocity histogram that anti-bot fingerprints. Inline `for _ in range: page.mouse.wheel; time.sleep` is forbidden.
- **CLAUDE.md 1-3-1 override**: AGENT_PROMPT.md explicitly suspends user-config rules that require human confirmation, since autonomous spawns have no human to ask.

## Known issues / gotchas

- **CreepJS hangs at "Computing..." on no-proxy profiles** — one of Multilogin's stealth patches blocks an API CreepJS awaits. Verification is disabled by default in `config.template.json`; re-enable after switching to `gate.multilogin.com` proxy or substituting another verifier (e.g. bot.sannysoft.com).
- **First profile start downloads the browser core** (~30–90s, HTTP 500 `CORE_DOWNLOADING_STARTED` until done). Retry once after waiting.
- **Multilogin password is MD5'd before signin** — `lib/multilogin.py` handles this; never send the plain password.
- **NEVER call `browser.close()` from Playwright** — it corrupts the Multilogin cookie store. Let the `with sync_playwright()` block exit naturally, then call the launcher `stop` endpoint via API. The facade in `lib/multilogin.py` does this in its `finally`.
- **`schtasks /Delete` from Git Bash fails** — Bash mangles `/Delete` into a path. Always go through `lib/scheduler.cancel()` which uses Python subprocess (no shell mangling).
- **`ls -la` required** for any agent listing of account dirs — plain `ls` hides `.env` and the agent reads its absence as a blocker. Encoded in AGENT_PROMPT.md.
- **Quota check counts failed sessions** — if a session crashes immediately on Day N, the agent won't retry that day. Treats it as a "gap day," plan adapts.

## Testing phases

| Phase | What it covers | Run |
|---|---|---|
| 1 | CreepJS regex extraction against synthetic text | `python tests/test_creepjs_extract.py` |
| 2 | Full middle layer (Multilogin→Playwright→Reddit→SQLite) without claude | `python tests/test_browser_integration.py <user>` |
| 3 | Agent boot sequence with claude, no Reddit footprint (dry-run) | hand-craft user message overriding actions |
| 4 | Real Day 1 lurk session | `python cli.py run <user>` |
| 5 | Multi-day observation | inspect SQLite + visit posts logged-out |

## Dependencies

- Python 3.12+
- `playwright` (with chromium installed), `requests`
- `working-with-multilogin` skill — currently local-only at `~/.claude/skills/user/working-with-multilogin` (Windows: `C:\Users\<u>\Documents\skills\user\working-with-multilogin`). `lib/multilogin.py` imports `mlx_client.Client` from it via `sys.path`. To run on a fresh machine, copy or rsync the skill folder there.
- Multilogin X v12+ (locally during dev; Linux server eventually)
- Claude Code CLI (uses `claude -p --dangerously-skip-permissions --append-system-prompt-file`)
- SQLite (bundled with Python)
- Windows Task Scheduler (auto) or Linux `at` (auto) for self-scheduling

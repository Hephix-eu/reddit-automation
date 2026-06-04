# CLAUDE.md

## Overview

Per-account autonomous Reddit warmup. Each account runs a 14-day plan (lurk → comment → post). A host-side cron fires `scripts/run-on-host.sh` every 15 min; it checks `accounts/<user>/next_run.json` and spawns a Docker sidecar that runs `cli.py run <username>`, which invokes Claude Code headlessly with `AGENT_PROMPT.md` as the system prompt.

Key files: `cli.py` (all user-facing commands), `AGENT_PROMPT.md` (autonomous agent system prompt), `plan.md` (canonical 14-day plan), `lib/db.py` (SQLite), `lib/multilogin.py` (Multilogin facade), `lib/scheduler.py` (scheduling primitives), `lib/jitter.py` (next-run timing), `lib/browse.py` (human scroll/dwell helpers).

Active accounts: see `accounts/` directory.

## Running

```bash
python cli.py start <username>      # bootstrap new account (interactive — prompts for MLX creds)
python cli.py run <username>        # manual single session (normally fired by host cron)
python cli.py status <username>     # day, karma, lock, pending drafts, next run
python cli.py list                  # all accounts
python cli.py pause <username>      # touch pause flag; agent honors at next wake
python cli.py resume <username>     # remove pause flag
python cli.py stop <username>       # cancel scheduled task (data preserved)
python cli.py reject <draft_id>     # mark an approved draft rejected (8-char prefix ok)
```

Container run (matches what host cron does):
```bash
docker run --rm \
  --network container:multilogin \
  -v /root/reddit-automation/accounts:/app/accounts \
  -v /root/reddit-automation/.env:/app/.env:ro \
  -v /root/.claude:/root/.claude \
  -e IS_SANDBOX=1 -e RUNNING_IN_CONTAINER=1 \
  redditagent-image \
  python3 cli.py run <username>
```

## Scheduling — critical invariant

**Inside a container**: the agent writes `accounts/<user>/next_run.json` → host cron handles firing. Never use `systemd-run`, `schtasks`, or Anthropic `CronCreate` from inside a container. `in_container()` in `lib/scheduler.py` gates this automatically.

**Bare-metal Linux**: `lib/scheduler.py` uses `systemd-run --on-calendar` (transient timer).

**Windows**: `lib/scheduler.py` uses `schtasks.exe` via Python subprocess — never via Git Bash (Bash mangles `/Delete` into a path).

## Footguns

- **Never `browser.close()`** — corrupts Multilogin cookie store. Exit the `with sync_playwright()` block naturally, then call `mlx.stop(profile_id)`.
- **Multilogin password is MD5'd** before sending to `/user/signin` — `lib/multilogin.py` handles this. Never send the plain password.
- **Stop endpoint is path-style**: `/api/v1/profile/stop/p/{profile_id}`, not `?profile_id=X`.
- **Never use Anthropic `CronCreate`** for scheduling inside the agent — session-scoped, doesn't persist across container restarts, bypasses the architecture.
- **`lib/browse.py` is mandatory** for all scrolling — `human_scroll()` avoids the scroll-velocity fingerprint. No inline `for _ in range: page.mouse.wheel; sleep` loops.
- **`ls -la` not `ls`** when listing account dirs — `.env` is a dotfile and the agent reads its absence as a blocker. Encoded in `AGENT_PROMPT.md`.
- **`schtasks /Delete` from Git Bash fails** — Bash mangles `/Delete`. Always go through `lib/scheduler.cancel()`.
- **First profile start may return HTTP 500 `CORE_DOWNLOADING_STARTED`** (~30–90s delay). Retry once.
- **CreepJS is disabled by default** — hangs at "Computing..." on no-proxy profiles due to a Multilogin stealth patch. Do not re-enable unless using `gate.multilogin.com` proxy.
- **`RUNNING_IN_CONTAINER=1` env var** must be set when running docker manually to trigger container-mode scheduling (`.dockerenv` may not be present in all environments).

## Environment

Container-wide env via `/etc/reddit-agent.env` (sourced by `run-on-host.sh`). Per-account env at `accounts/<user>/.env` (loaded by `cli.py`). Root-level `.env` for workspace proxy creds (gitignored).

Telegram alerts (throttled 4h per key) go through `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in the env file.

## Dependencies

Python 3.12+, `playwright` + chromium, `requests`, Multilogin X v12+, Claude Code CLI, `working-with-multilogin` skill at `~/.claude/skills/user/working-with-multilogin` (inside container: vendored to `/root/skills/user/working-with-multilogin/scripts` by `Dockerfile.agent`).

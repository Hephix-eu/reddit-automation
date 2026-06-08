"""
Reddit warmup CLI — manage per-account warmup agents.

Commands:
    cli.py start <username>     Bootstrap a new account folder + first scheduled run.
    cli.py pause <username>     Touch pause flag. Agent honors at next wake.
    cli.py resume <username>    Remove pause flag.
    cli.py status <username>    Show day, karma, last action, next run, lock status.
    cli.py stop <username>      Halt scheduled task. Data preserved.
    cli.py list                 All accounts + status.

Run agent (called by scheduler, not by user normally):
    cli.py run <username>       Single session. Equivalent to invoking AGENT_PROMPT.md via Claude Code.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
ACCOUNTS_DIR = ROOT / "accounts"
TEMPLATE_CONFIG = ROOT / "config.template.json"
TEMPLATE_PLAN = ROOT / "plan.md"
AGENT_PROMPT = ROOT / "AGENT_PROMPT.md"


def _seed_reddit_cookies(mlx, folder_id: str, profile_id: str, cookies: list) -> None:
    """Start the Multilogin profile headlessly, inject Reddit session cookies, stop it.

    Follows the same CDP connect → work → exit-with-block → mlx.stop() pattern
    as the warmup agent so Multilogin syncs the cookie state to cloud on stop.
    """
    import time
    from playwright.sync_api import sync_playwright

    # First profile start may return 500 CORE_DOWNLOADING_STARTED — retry once.
    port = None
    for attempt in (1, 2):
        try:
            port = mlx.start(folder_id, profile_id, automation_type="playwright", headless=True)
            break
        except RuntimeError as e:
            if attempt == 1 and "CORE_DOWNLOADING" in str(e):
                print("  [seed] Multilogin downloading core — waiting 60s…")
                time.sleep(60)
            else:
                raise
    if port is None:
        raise RuntimeError("Multilogin profile failed to start for cookie seeding")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            ctx.add_cookies([
                {"name": c["name"], "value": c["value"], "domain": ".reddit.com", "path": "/"}
                for c in cookies
            ])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto("https://www.reddit.com", wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                print(f"  [seed] reddit.com navigation failed ({e}) — cookies still set in profile")
        # sync_playwright().__exit__ disconnects CDP but leaves the Multilogin browser running
    finally:
        mlx.stop(profile_id)

    print(f"  [OK] seeded {len(cookies)} Reddit session cookies into profile")


def cmd_start(username: str, profile_id: str | None = None, auto: bool = False, anchor_sub: str | None = None):
    """Bootstrap a new account: create Multilogin profile (or reuse one) + account folder.

    If --profile-id is given (profile already created during acctfarm signup), skip
    profile creation and wire up the existing profile directly.
    If --auto is given, read MLX creds from MULTILOGIN_EMAIL/MULTILOGIN_PASSWORD env vars
    and take --anchor as the anchor subreddit (non-interactive, for dashboard use).
    """
    account_dir = ACCOUNTS_DIR / username
    if account_dir.exists():
        sys.exit(f"Account {username} already exists at {account_dir}. Use `cli.py status` to inspect.")

    if auto:
        mlx_email = os.environ.get("MULTILOGIN_EMAIL", "").strip()
        mlx_password = os.environ.get("MULTILOGIN_PASSWORD", "").strip()
        if not mlx_email or not mlx_password:
            sys.exit("--auto requires MULTILOGIN_EMAIL and MULTILOGIN_PASSWORD in environment")
        if not anchor_sub:
            sys.exit("--auto requires --anchor SUB")
    else:
        mlx_email = input("Multilogin email: ").strip()
        mlx_password = input("Multilogin password: ").strip()
        anchor_sub = input("Anchor subreddit (e.g. r/dotnet): ").strip()

    # Set env for the multilogin facade
    os.environ["MULTILOGIN_EMAIL"] = mlx_email
    os.environ["MULTILOGIN_PASSWORD"] = mlx_password

    # Load workspace-level proxy creds from root .env (gitignored).
    # Use direct assignment so a stale parent-process env can't shadow the proxy vars.
    root_env = ROOT / ".env"
    if root_env.exists():
        for line in root_env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k.startswith("MULTILOGIN_PROXY_"):
                    os.environ[k] = v  # always override proxy vars
                else:
                    os.environ.setdefault(k, v)

    from lib.multilogin import make_client
    import sys as _sys
    skill_paths = [
        r"C:\Users\vilum\Documents\skills\user\working-with-multilogin\scripts",
        str(Path.home() / "skills/user/working-with-multilogin/scripts"),
    ]
    for p in skill_paths:
        if Path(p).exists():
            _sys.path.insert(0, p)
            break
    from mlx_client import DEFAULT_FLAGS_DESKTOP
    mlx = make_client()

    if profile_id:
        # Reuse a profile already created by acctfarm signup — just look up its folder.
        print(f"  Reusing existing Multilogin profile {profile_id}…")
        existing = mlx.get_profile(profile_id)
        folder_id = existing["folder_id"]
        profile_name = existing.get("name") or ""
        # INVARIANT: refuse to bind an MLX profile to a different username than
        # the one in its name. This prevents the wrong_reddit_user bug seen with
        # salty_crow33 on 2026-05-30, where a config.json was written pointing at
        # vast_prawn's profile_id by mistake.
        expected_name = f"reddit-{username}-desktop"
        if profile_name and profile_name != expected_name:
            sys.exit(
                f"\n  REFUSING to bind: profile {profile_id[:8]} is named "
                f"{profile_name!r} but you asked to onboard {username!r} "
                f"(expected {expected_name!r}).\n"
                f"  This would cause `wrong_reddit_user` aborts every session.\n"
                f"  If you really mean to reuse this profile, rename it in the MLX UI first."
            )
        if not profile_name:
            profile_name = expected_name
        print(f"  [OK] folder_id={folder_id}  profile_name={profile_name}")
        proxy_sid = None  # SID rotation N/A — reusing existing MLX profile
    else:
        proxy = None
        proxy_sid = None
        if os.environ.get("MULTILOGIN_PROXY_USERNAME"):
            base_username = os.environ["MULTILOGIN_PROXY_USERNAME"]
            # Rotate the gate.multilogin.com sticky-session ID per account so each
            # profile gets a different exit IP. Without this, all accounts share the
            # workspace SID -> same IP -> linkable fingerprint (2026-06-02 ban cluster).
            if "sid-" in base_username:
                from mlx_client import gen_sid, rotate_proxy_sid
                proxy_sid = gen_sid()
                proxy_username = rotate_proxy_sid(base_username, proxy_sid)
            else:
                proxy_username = base_username
            proxy = {
                "host": os.environ.get("MULTILOGIN_PROXY_HOST", "gate.multilogin.com"),
                "port": int(os.environ.get("MULTILOGIN_PROXY_PORT", "1080")),
                "type": os.environ.get("MULTILOGIN_PROXY_TYPE", "socks5"),
                "username": proxy_username,
                "password": os.environ["MULTILOGIN_PROXY_PASSWORD"],
            }
            flags = {**DEFAULT_FLAGS_DESKTOP, "geolocation_masking": "mask"}
            sid_label = f" sid={proxy_sid}" if proxy_sid else ""
            print(f"  Proxy: {proxy['host']}:{proxy['port']} ({proxy['type']}){sid_label} — natural tz/locale, masked geo")
        else:
            flags = {
                **DEFAULT_FLAGS_DESKTOP,
                "proxy_masking": "disabled",
                "geolocation_masking": "mask",
                "localization_masking": "mask",
                "timezone_masking": "mask",
            }
            print(f"  Proxy: none — masked geo (incoherent; OK only for local-residential testing)")

        folders = mlx.folders()
        if not folders:
            sys.exit("No Multilogin folders found. Create one in the Multilogin UI first.")
        folder_id = folders[0]["folder_id"]
        print(f"  Using folder: {folders[0]['name']} ({folder_id})")

        profile_name = f"reddit-{username}-desktop"
        print(f"  Creating Multilogin profile '{profile_name}' (desktop/Windows/Mimic)...")
        profile_id = mlx.create_profile(
            folder_id=folder_id,
            name=profile_name,
            proxy=proxy,
            flags=flags,
            os_type="windows",
        )
        print(f"  [OK] profile_id={profile_id}")

    # Bootstrap folder + files
    account_dir.mkdir(parents=True)
    (account_dir / "recordings").mkdir()
    (account_dir / "screenshots").mkdir()

    config_text = TEMPLATE_CONFIG.read_text()
    replacements = {
        "{{REDDIT_USERNAME}}": username,
        "{{ACCOUNT_DIR}}": str(account_dir).replace("\\", "\\\\"),
        "{{MULTILOGIN_PROFILE_ID}}": profile_id,
        "{{MULTILOGIN_FOLDER_ID}}": folder_id,
        "{{MULTILOGIN_PROFILE_NAME}}": profile_name,
        "{{ANCHOR_SUB}}": anchor_sub,
    }
    for k, v in replacements.items():
        config_text = config_text.replace(k, v)
    (account_dir / "config.json").write_text(config_text)

    env_lines = [
        f"MULTILOGIN_EMAIL={mlx_email}",
        f"MULTILOGIN_PASSWORD={mlx_password}",
    ]
    if proxy_sid:
        # SID is baked into the MLX profile server-side; this is for audit/visibility.
        env_lines.append(f"MULTILOGIN_PROXY_SID={proxy_sid}")
    if auto:
        reddit_email    = os.environ.get("REDDIT_EMAIL", "").strip()
        reddit_password = os.environ.get("REDDIT_PASSWORD", "").strip()
        if reddit_email:
            env_lines.append(f"REDDIT_USERNAME={username}")
            env_lines.append(f"REDDIT_EMAIL={reddit_email}")
        if reddit_password:
            env_lines.append(f"REDDIT_PASSWORD={reddit_password}")
    (account_dir / ".env").write_text("\n".join(env_lines) + "\n")
    shutil.copy(TEMPLATE_PLAN, account_dir / "plan.md")

    if auto:
        cookies_raw = os.environ.get("REDDIT_COOKIES", "").strip()
        if cookies_raw:
            try:
                cookies = json.loads(cookies_raw)
                print("  Seeding Reddit session cookies into Multilogin profile…")
                _seed_reddit_cookies(mlx, folder_id, profile_id, cookies)
            except Exception as e:
                print(f"  [warn] cookie seeding failed: {e} — agent will log in on Day 1 instead")

        # Write next_run.json so the dashboard shows the account is queued and
        # so run-on-host.sh --all can confirm it should fire on the next cron tick.
        from datetime import datetime, timezone
        (account_dir / "next_run.json").write_text(json.dumps({
            "next_run_utc": datetime.now(timezone.utc).isoformat(),
            "reason": "initial_bootstrap",
            "written_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))

    print(f"\n[OK] Account {username} bootstrapped at {account_dir}")
    print(f"  Multilogin profile: {profile_name} ({profile_id})")
    print(f"  Anchor sub: {anchor_sub}")
    print(f"\n  Run the first session manually so you can watch:")
    print(f"    python cli.py run {username}")
    print(f"\n  After that, the agent will self-schedule subsequent sessions.")


def cmd_pause(username: str):
    flag = ACCOUNTS_DIR / username / "pause"
    flag.touch()
    print(f"[OK] {username} paused. Agent will honor at next wake.")


def cmd_resume(username: str):
    flag = ACCOUNTS_DIR / username / "pause"
    if flag.exists():
        flag.unlink()
        print(f"[OK] {username} resumed.")
    else:
        print(f"  {username} was not paused.")


def cmd_status(username: str):
    from lib import db
    account_dir = ACCOUNTS_DIR / username
    if not account_dir.exists():
        sys.exit(f"Account {username} not found.")

    config = json.loads((account_dir / "config.json").read_text())
    lock = account_dir / "lock"
    pause = account_dir / "pause"
    state_db = account_dir / config["paths"]["state_db"]

    print(f"Account: {username}")
    print(f"  Start date:    {config['plan']['start_date'] or '(not started yet)'}")
    print(f"  Lock:          {'present' if lock.exists() else 'free'}")
    print(f"  Paused:        {'YES' if pause.exists() else 'no'}")
    if state_db.exists():
        latest = db.latest_session(state_db)
        if latest:
            print(f"  Day:           {latest.get('day', '?')}")
            print(f"  Last session:  {latest.get('executed_at', '?')}")
            print(f"  Next run:      {latest.get('scheduled_for', '?')}")
        print(f"  Total karma:   {db.total_karma(state_db)}")
        pend = db.pending_drafts(state_db, older_than_minutes=0)
        print(f"  Pending drafts: {len(pend)}")
        for d in pend:
            print(f"    - {d['id'][:8]} {d.get('subreddit', '?')}: {(d.get('draft_content') or '')[:60]}")


def cmd_reject(draft_id: str):
    """Mark a pending draft as rejected so the agent skips it.

    Usage: cli.py reject <draft_id_prefix>
    Matches any draft whose id starts with the given prefix (so 8 chars is usually enough).
    """
    from lib import db
    # Search across all accounts for a draft with this id prefix
    hits = []
    for account_dir in ACCOUNTS_DIR.iterdir():
        if not account_dir.is_dir():
            continue
        config_path = account_dir / "config.json"
        if not config_path.exists():
            continue
        config = json.loads(config_path.read_text())
        state_db = account_dir / config["paths"]["state_db"]
        if not state_db.exists():
            continue
        import sqlite3
        conn = sqlite3.connect(state_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, subreddit, draft_content FROM actions_log "
            "WHERE type='Draft' AND status='approved' AND id LIKE ?",
            (draft_id + "%",),
        ).fetchall()
        for r in rows:
            hits.append((account_dir.name, state_db, dict(r)))
        conn.close()

    if not hits:
        sys.exit(f"No approved draft matching '{draft_id}' found.")
    if len(hits) > 1:
        sys.exit(f"Ambiguous prefix '{draft_id}': matches {len(hits)} drafts. Use a longer prefix.")

    account_name, state_db, row = hits[0]
    db.update_status(state_db, row["id"], "rejected")
    print(f"[OK] Rejected draft {row['id'][:8]} in account {account_name}: "
          f"{row.get('subreddit', '?')} — {(row.get('draft_content') or '')[:80]}")


def cmd_stop(username: str):
    """Remove scheduled task. Account folder + data preserved."""
    from lib import scheduler
    scheduler.cancel(username)
    print(f"[OK] Scheduled task for {username} removed. Account data intact.")


def cmd_list():
    if not ACCOUNTS_DIR.exists():
        print("No accounts.")
        return
    for d in sorted(ACCOUNTS_DIR.iterdir()):
        if d.is_dir():
            print(f"- {d.name}")


def _load_env_files(*paths: Path) -> None:
    """Load .env files into os.environ (setdefault — won't override existing)."""
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _compute_state_from_db(state_db: Path):
    """Read SQLite, derive cross-session state snapshot. Returns dict or None.

    Note: `actions_taken` counts ACTIONS THIS ACCOUNT TOOK (upvotes given,
    subscribes done). It is NOT Reddit karma — karma comes from OTHER users
    upvoting our content. The `karma` field is set separately by the agent
    when it queries /user/<name>/about.json during a session.
    """
    if not state_db.exists():
        return None
    import sqlite3
    try:
        conn = sqlite3.connect(str(state_db))
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            "SELECT session_id, day, executed_at FROM actions_log "
            "WHERE type='Session' AND executed_at IS NOT NULL "
            "ORDER BY executed_at DESC LIMIT 1"
        ).fetchone()
        actions = conn.execute(
            "SELECT COUNT(*) AS n FROM actions_log "
            "WHERE type='Action' AND status='done' "
            "AND action_type IN ('upvote','subscribe','comment','post','save')"
        ).fetchone()
        conn.close()
        if not latest:
            return None
        return {
            "day": latest["day"],
            "actions_taken": actions["n"] if actions else 0,
            "last_session_id": latest["session_id"],
            "last_executed_at": latest["executed_at"],
        }
    except Exception:
        return None


def _persist_state_to_mlx(account_dir: Path) -> None:
    """Read SQLite truth, write to MLX profile notes. Best-effort — never raises.

    Writes only fields the wrapper owns (day, actions_taken, last_session_id,
    last_executed_at). Preserves anything else the agent wrote (karma,
    last_session_summary, bind metadata). Drops the deprecated `total_karma`
    field on the way through.
    """
    config_path = account_dir / "config.json"
    if not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text())
        profile_id = config.get("multilogin", {}).get("profile_id", "")
        if not profile_id or profile_id.startswith("TODO_"):
            return
        state_db = account_dir / config.get("paths", {}).get("state_db", "state.db")
        state = _compute_state_from_db(state_db)
        if state is None:
            print("[wrapper] no SQLite Session row yet — skipping MLX persist", file=sys.stderr)
            return
        _load_env_files(ROOT / ".env", account_dir / ".env")
        from lib.multilogin import make_client
        import time as _time
        mlx = make_client()
        try:
            existing = json.loads(mlx.get_profile(profile_id).get("notes") or "{}")
        except json.JSONDecodeError:
            existing = {}
        existing.pop("total_karma", None)  # deprecated — was misnamed
        merged = {
            **existing,
            "day": state["day"],
            "actions_taken": state["actions_taken"],
            "last_session_id": state["last_session_id"],
            "last_executed_at": state["last_executed_at"],
            "last_updated": int(_time.time()),
        }
        merged.setdefault("karma", None)  # agent fills when it observes Reddit-side
        mlx.partial_update(profile_id, notes=json.dumps(merged, ensure_ascii=False))
        sid = (state["last_session_id"] or "")[:8]
        print(
            f"[wrapper] save_state OK: day={state['day']} "
            f"actions_taken={state['actions_taken']} karma={merged.get('karma')} "
            f"session={sid}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[wrapper] save_state failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)



def _reseed_cookies_if_fresh(account_dir: Path, mlx, folder_id: str, profile_id: str) -> None:
    """If session_cookies.json exists and is < 7 days old, re-seed the MLX profile.

    The warmup agent writes session_cookies.json whenever it has to log in mid-session.
    Re-seeding here ensures the next agent session starts already logged in, rather
    than having to go through the login flow again.
    """
    cookie_file = account_dir / "session_cookies.json"
    if not cookie_file.exists():
        return
    try:
        data = json.loads(cookie_file.read_text())
        saved_at_str = data.get("saved_at", "")
        if saved_at_str:
            from datetime import datetime, timezone, timedelta
            saved_at = datetime.fromisoformat(saved_at_str)
            if datetime.now(timezone.utc) - saved_at > timedelta(days=7):
                print("  [reseed] session_cookies.json is > 7 days old — skipping reseed", file=sys.stderr)
                return
        cookies = data.get("cookies", [])
        if not cookies:
            return
        print(f"  [reseed] seeding {len(cookies)} saved cookies into MLX profile…", file=sys.stderr)
        _seed_reddit_cookies(mlx, folder_id, profile_id, cookies)
        print("  [reseed] done", file=sys.stderr)
    except Exception as e:
        print(f"  [reseed] skipped (could not load session_cookies.json): {e}", file=sys.stderr)


def cmd_run(username: str, force: bool = False):
    """Invoke claude headlessly with AGENT_PROMPT.md appended as system prompt.

    Single autonomous session: claude reads the prompt, executes via tools
    (file ops, bash for sqlite, subprocess for Multilogin/Playwright), exits.

    Wraps the claude subprocess in a finally block that always:
      - Releases the lock if the agent left it
      - Writes a fallback next_run.json if the agent didn't update it
      - Enforces a hard wall-time so a hung claude doesn't pin the container
      - Persists session state to MLX profile notes from SQLite truth
    These are infrastructure invariants the LLM is not allowed to violate.
    """
    from datetime import datetime, timedelta, timezone

    account_dir = ACCOUNTS_DIR / username
    if not account_dir.exists():
        sys.exit(f"Account {username} not found.")

    if (account_dir / "banned.json").exists():
        print(f"[run] {username} is shadowbanned (banned.json present) — skipping session.", file=sys.stderr)
        sys.exit(0)

    if (account_dir / "manual.json").exists() and not force:
        print(f"[run] {username} is in manual mode (manual.json present) — skipping automatic session.", file=sys.stderr)
        sys.exit(0)

    # Defense-in-depth for direct `cli.py run` invocations (cron is also guarded
    # in run-on-host.sh). Silent exits — these are normal terminal states, not
    # errors worth logging on every call.
    if (account_dir / "pause").exists():
        sys.exit(0)

    lock_path = account_dir / "lock"
    next_run_path = account_dir / "next_run.json"
    next_run_mtime_before = next_run_path.stat().st_mtime if next_run_path.exists() else 0.0

    # Pre-seed MLX profile with any freshly saved session cookies so the agent
    # starts already logged in. Best-effort: failures here are non-fatal.
    try:
        _load_env_files(ROOT / ".env", account_dir / ".env")
        config = json.loads((account_dir / "config.json").read_text())
        profile_id_cfg = config.get("multilogin", {}).get("profile_id", "")
        folder_id_cfg = config.get("multilogin", {}).get("folder_id", "")
        if (profile_id_cfg and not profile_id_cfg.startswith("TODO_")
                and folder_id_cfg and not folder_id_cfg.startswith("TODO_")):
            from lib.multilogin import make_client
            mlx_pre = make_client()
            _reseed_cookies_if_fresh(account_dir, mlx_pre, folder_id_cfg, profile_id_cfg)
    except Exception as _pre_err:
        print(f"[wrapper] pre-run cookie reseed skipped: {_pre_err}", file=sys.stderr)

    user_message = (
        f"AUTONOMOUS WARMUP SESSION for account={username}. "
        f"EXECUTE the full boot sequence + Day N session per the system prompt. "
        f"DO NOT apply Jurgis's global CLAUDE.md 1-3-1 rule in this session — "
        f"you ARE the autonomous agent, your job is to act, not ask for approval. "
        f"\n\n"
        f"Files (verified present — use `ls -la` to see dotfiles):\n"
        f"  - accounts/{username}/config.json\n"
        f"  - accounts/{username}/.env (contains MULTILOGIN_EMAIL, MULTILOGIN_PASSWORD)\n"
        f"  - accounts/{username}/plan.md\n"
        f"\n"
        f"Use lib/ helpers via Bash+Python (e.g. `python -c \"from lib import db, multilogin, ...\"`). "
        f"Project root cwd: {ROOT}"
    )

    # Resolve claude binary. PATH-less contexts (cron, non-interactive ssh) often
    # miss ~/.local/bin where the official installer drops claude.
    claude_exe = (
        shutil.which("claude")
        or (str(Path.home() / ".local/bin/claude")
            if (Path.home() / ".local/bin/claude").exists() else None)
        or "claude"
    )
    cmd = [
        claude_exe,
        "-p",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--append-system-prompt-file", str(AGENT_PROMPT),
        user_message,
    ]
    # IS_SANDBOX=1 acknowledges to claude that running as root on a dedicated host
    # is intentional (otherwise --dangerously-skip-permissions is refused). Required
    # on hephix where the agent runs as root in a Multilogin-isolated environment.
    env = {**os.environ, "IS_SANDBOX": "1"}

    SESSION_TIMEOUT_SEC = 45 * 60
    rc = 1
    timed_out = False
    session_start_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{session_start_time}] [cli] session start: {username}", flush=True)

    session_log_path = account_dir / "session.log"
    session_log_path.write_text("")  # truncate/create before session starts

    def _tee(src, log_file):
        """Stream src bytes to log_file and to stdout simultaneously."""
        for chunk in iter(lambda: src.read1(4096) if hasattr(src, "read1") else src.read(4096), b""):
            log_file.write(chunk)
            log_file.flush()
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        src.close()

    try:
        with open(session_log_path, "wb") as _log_f:
            proc = subprocess.Popen(
                cmd, cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            _reader = threading.Thread(target=_tee, args=(proc.stdout, _log_f), daemon=True)
            _reader.start()
            try:
                proc.wait(timeout=SESSION_TIMEOUT_SEC)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                timed_out = True
                rc = 124
                print(f"\n[wrapper] claude exceeded {SESSION_TIMEOUT_SEC}s wall-time; killed.", file=sys.stderr, flush=True)
            finally:
                _reader.join(timeout=5)
    except Exception as _launch_err:
        print(f"[wrapper] failed to launch claude: {_launch_err}", file=sys.stderr, flush=True)
    finally:
        print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] [cli] session end: rc={rc}", flush=True)
        if lock_path.exists():
            try:
                lock_path.unlink()
                print("[wrapper] released orphan lock", file=sys.stderr)
            except OSError as e:
                print(f"[wrapper] could not release lock: {e}", file=sys.stderr)

        running_path = account_dir / "running.json"
        if running_path.exists():
            try:
                running_path.unlink()
            except OSError:
                pass

        # Write a fallback Session row if the agent exited without writing one.
        # This ensures every run — including crashes — appears in the dashboard.
        db_path = account_dir / "state.db"
        if db_path.exists():
            try:
                import sqlite3 as _sqlite3
                from lib import db as _db
                with _sqlite3.connect(str(db_path)) as _conn:
                    _conn.row_factory = _sqlite3.Row
                    session_written = _conn.execute(
                        "SELECT COUNT(*) FROM actions_log WHERE type='Session' AND executed_at >= ?",
                        (session_start_time,),
                    ).fetchone()[0]
                if not session_written:
                    with _sqlite3.connect(str(db_path)) as _conn:
                        _conn.row_factory = _sqlite3.Row
                        recent = _conn.execute(
                            "SELECT session_id, day FROM actions_log "
                            "WHERE executed_at >= ? ORDER BY executed_at DESC LIMIT 1",
                            (session_start_time,),
                        ).fetchone()
                        error_rows = _conn.execute(
                            "SELECT action_type, reasoning FROM actions_log "
                            "WHERE type='Error' AND executed_at >= ? ORDER BY executed_at",
                            (session_start_time,),
                        ).fetchall()
                    sid = (recent["session_id"] if recent and recent["session_id"]
                           else str(__import__("uuid").uuid4()))
                    day = recent["day"] if recent else None
                    crash_reason = "timeout" if timed_out else f"exit_{rc}"
                    # Summarise errors: count each type and collect unique messages.
                    from collections import Counter as _Counter
                    type_counts = _Counter(r["action_type"] for r in error_rows if r["action_type"])
                    error_summary = ", ".join(
                        f"{t}(×{n})" if n > 1 else t for t, n in type_counts.most_common()
                    )
                    seen, unique_msgs = set(), []
                    for r in error_rows:
                        msg = (r["reasoning"] or "").strip()
                        # First line is enough for de-duplication and display.
                        key = msg.splitlines()[0][:120] if msg else ""
                        if key and key not in seen:
                            seen.add(key)
                            unique_msgs.append(key)
                    result_str = f"crashed: {crash_reason}"
                    if error_summary:
                        result_str += f" — {error_summary}"
                    reasoning_str = "; ".join(unique_msgs) if unique_msgs else f"rc={rc}, no Error rows recorded"
                    _db.insert(db_path,
                        type="Session",
                        status="failed",
                        day=day,
                        action_type="session_end",
                        session_id=sid,
                        result=result_str,
                        reasoning=reasoning_str,
                    )
                    print(f"[wrapper] wrote fallback Session row (sid={sid[:8]}, rc={rc}): {result_str}", file=sys.stderr)
            except Exception as _e:
                print(f"[wrapper] could not write fallback Session row: {_e}", file=sys.stderr)

        # Archive session.log to logs/session_<sid8>.log for per-session retrieval.
        session_log = account_dir / "session.log"
        if session_log.exists():
            try:
                from lib.db import latest_session
                latest = latest_session(db_path) if db_path.exists() else None
                sid = ((latest or {}).get("session_id") or "")[:8]
                logs_dir = account_dir / "logs"
                logs_dir.mkdir(exist_ok=True)
                label = sid if sid else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                import shutil as _shutil
                _shutil.copy2(str(session_log), str(logs_dir / f"session_{label}.log"))
            except Exception as _e:
                print(f"[wrapper] could not archive session.log: {_e}", file=sys.stderr)

        # 3. Mirror SQLite truth to MLX profile notes (best-effort).
        # SQLite is authoritative; this just keeps the cross-machine snapshot fresh.
        _persist_state_to_mlx(account_dir)

    sys.exit(rc)


def schedule_first_run(username: str):
    """Register a Task Scheduler / cron entry firing ~2 min from now for the first session."""
    from datetime import datetime, timedelta
    from lib import scheduler
    when = datetime.now() + timedelta(minutes=2)
    cli_path = Path(__file__).resolve()
    scheduler.schedule_next_run(username, when, cli_path)
    print(f"  First run scheduled for {when.strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    start_sp = sub.add_parser("start")
    start_sp.add_argument("username")
    start_sp.add_argument(
        "--profile-id",
        metavar="ID",
        default=None,
        help="Reuse an existing Multilogin profile (created during signup) instead of creating a new one.",
    )
    start_sp.add_argument(
        "--auto",
        action="store_true",
        help="Non-interactive: read MLX creds from MULTILOGIN_EMAIL/MULTILOGIN_PASSWORD env vars.",
    )
    start_sp.add_argument(
        "--anchor",
        metavar="SUB",
        default=None,
        help="Anchor subreddit (required with --auto, e.g. r/programming).",
    )
    for c in ("pause", "resume", "status", "stop"):
        sp = sub.add_parser(c)
        sp.add_argument("username")
    run_sp = sub.add_parser("run")
    run_sp.add_argument("username")
    run_sp.add_argument("--force", action="store_true", help="Run even if manual.json is set.")
    rsp = sub.add_parser("reject")
    rsp.add_argument("draft_id", help="Draft id (or prefix, min 8 chars)")
    sub.add_parser("list")

    args = p.parse_args()
    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "reject":
        cmd_reject(args.draft_id)
    elif args.cmd == "start":
        cmd_start(args.username, profile_id=args.profile_id, auto=args.auto, anchor_sub=args.anchor)
    elif args.cmd == "run":
        cmd_run(args.username, force=getattr(args, "force", False))
    else:
        globals()[f"cmd_{args.cmd}"](args.username)


if __name__ == "__main__":
    main()

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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
ACCOUNTS_DIR = ROOT / "accounts"
TEMPLATE_CONFIG = ROOT / "config.template.json"
TEMPLATE_PLAN = ROOT / "plan.md"
AGENT_PROMPT = ROOT / "AGENT_PROMPT.md"


def cmd_start(username: str):
    """Bootstrap a new account: create Multilogin profile via API + account folder."""
    account_dir = ACCOUNTS_DIR / username
    if account_dir.exists():
        sys.exit(f"Account {username} already exists at {account_dir}. Use `cli.py status` to inspect.")

    mlx_email = input("Multilogin email: ").strip()
    mlx_password = input("Multilogin password: ").strip()
    anchor_sub = input("Anchor subreddit (e.g. r/dotnet): ").strip()

    # Set env for the multilogin facade
    os.environ["MULTILOGIN_EMAIL"] = mlx_email
    os.environ["MULTILOGIN_PASSWORD"] = mlx_password

    from lib.multilogin import make_client
    import sys as _sys
    _sys.path.insert(0, r"C:\Users\vilum\Documents\skills\user\working-with-multilogin\scripts")
    from mlx_client import DEFAULT_FLAGS_DESKTOP
    mlx = make_client()

    # When proxy=None, geolocation/timezone/localization can't be "natural"
    # (those derive from proxy IP). Override to "mask" for no-proxy profiles.
    no_proxy_flags = {
        **DEFAULT_FLAGS_DESKTOP,
        "proxy_masking": "disabled",
        "geolocation_masking": "mask",
        "localization_masking": "mask",
        "timezone_masking": "mask",
    }

    # Pick folder — use the first available
    folders = mlx.folders()
    if not folders:
        sys.exit("No Multilogin folders found. Create one in the Multilogin UI first.")
    folder_id = folders[0]["folder_id"]
    print(f"  Using folder: {folders[0]['name']} ({folder_id})")

    profile_name = f"reddit-{username}-desktop"
    print(f"  Creating Multilogin profile '{profile_name}' (no proxy, desktop/Windows/Mimic)...")
    profile_id = mlx.create_profile(
        folder_id=folder_id,
        name=profile_name,
        proxy=None,
        flags=no_proxy_flags,
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

    (account_dir / ".env").write_text(
        f"MULTILOGIN_EMAIL={mlx_email}\nMULTILOGIN_PASSWORD={mlx_password}\n"
    )
    shutil.copy(TEMPLATE_PLAN, account_dir / "plan.md")

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


def cmd_run(username: str):
    """Invoke claude headlessly with AGENT_PROMPT.md appended as system prompt.

    Single autonomous session: claude reads the prompt, executes via tools
    (file ops, bash for sqlite, subprocess for Multilogin/Playwright), exits.
    """
    account_dir = ACCOUNTS_DIR / username
    if not account_dir.exists():
        sys.exit(f"Account {username} not found.")

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
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    sys.exit(result.returncode)


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
    for c in ("start", "pause", "resume", "status", "stop", "run"):
        sp = sub.add_parser(c)
        sp.add_argument("username")
    rsp = sub.add_parser("reject")
    rsp.add_argument("draft_id", help="Draft id (or prefix, min 8 chars)")
    sub.add_parser("list")

    args = p.parse_args()
    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "reject":
        cmd_reject(args.draft_id)
    else:
        globals()[f"cmd_{args.cmd}"](args.username)


if __name__ == "__main__":
    main()

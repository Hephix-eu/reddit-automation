"""Scheduling primitives. Three modes:

1. **Inside a container** (`/.dockerenv` exists) → write `accounts/<u>/next_run.json`.
   A host-side cron (see `scripts/run-on-host.sh`) polls every 15min and runs the
   agent if the file says it's time. The agent NEVER self-schedules from inside a
   container. (Earlier attempts to use systemd-run failed for lack of PID-1 systemd,
   and the agent then improvised by calling Anthropic-side CronCreate — which is
   session-scoped, doesn't fire reliably, and bypasses our architecture.)

2. **Bare-metal Linux** → systemd-run --on-calendar (transient timer unit).

3. **Windows** → schtasks.exe /SC ONCE.

The agent SHOULD NOT use Anthropic CronCreate. This module is the only sanctioned
path for scheduling.
"""

import json
import os
import platform
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def task_name(username: str) -> str:
    return f"RedditWarmup_{username}"


def in_container() -> bool:
    """True if running inside a Docker/Podman container."""
    return Path("/.dockerenv").exists() or os.environ.get("RUNNING_IN_CONTAINER") == "1"


def schedule_next_run(username: str, when: datetime, cli_path: Path | None = None,
                      python_exe: str | None = None,
                      account_dir: Path | None = None,
                      reason: str = "scheduled") -> None:
    """Register the next agent invocation at `when`.

    Inside a container → writes next_run.json (host cron handles firing).
    Otherwise → native scheduler (systemd-run / schtasks).

    `cli_path` defaults to this repo's cli.py (derived from the module location)
    so the autonomous agent can call schedule_next_run(user, when) without having
    to know/pass it — a missing cli_path used to crash the session.
    """
    if cli_path is None:
        cli_path = Path(__file__).resolve().parent.parent / "cli.py"
    python_exe = python_exe or sys.executable
    if in_container():
        if account_dir is None:
            account_dir = cli_path.parent / "accounts" / username
        _write_next_run_marker(account_dir, when, reason)
        return
    if platform.system() == "Windows":
        _schedule_windows(username, when, cli_path, python_exe)
    else:
        _schedule_linux(username, when, cli_path, python_exe)


def _write_next_run_marker(account_dir: Path, when: datetime, reason: str) -> None:
    """Durable, machine-readable next-run marker. Read by scripts/run-on-host.sh."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    marker = {
        "next_run_utc": when.astimezone(timezone.utc).isoformat(),
        "reason": reason,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path = account_dir / "next_run.json"
    path.write_text(json.dumps(marker, indent=2))


def cancel(username: str) -> None:
    """Remove the scheduled task for this account (no error if not present)."""
    if platform.system() == "Windows":
        subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", task_name(username), "/F"],
            capture_output=True, text=True,
        )
    else:
        # Linux: stop the systemd transient timer + service for this account.
        unit = task_name(username)
        for suffix in (".timer", ".service"):
            subprocess.run(["systemctl", "stop", f"{unit}{suffix}"], capture_output=True)
            subprocess.run(["systemctl", "reset-failed", f"{unit}{suffix}"], capture_output=True)


def _schedule_windows(username: str, when: datetime, cli_path: Path,
                      python_exe: str) -> None:
    name = task_name(username)
    # schtasks requires HH:MM (24h) for /ST and MM/DD/YYYY for /SD
    st = when.strftime("%H:%M")
    sd = when.strftime("%m/%d/%Y")
    cmd_run = f'"{python_exe}" "{cli_path}" run {username}'

    # Delete any existing task with the same name (ignore failure)
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", name, "/F"],
        capture_output=True, text=True,
    )

    proc = subprocess.run(
        [
            "schtasks.exe", "/Create",
            "/TN", name,
            "/SC", "ONCE",
            "/ST", st,
            "/SD", sd,
            "/TR", cmd_run,
            "/F",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"schtasks /Create failed: rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def _schedule_linux(username: str, when: datetime, cli_path: Path,
                    python_exe: str) -> None:
    """Use systemd-run --on-calendar to register a one-shot transient unit.

    Reason: `at` requires `atd` service + a package install. systemd-run is
    always present on systemd-based distros (Ubuntu/Debian/RHEL/Fedora) and
    needs no extra install. Creates two transient units: a .timer that fires
    once at `when`, and a .service that runs `cli.py run <username>`.
    """
    unit = task_name(username)
    cal = when.strftime("%Y-%m-%d %H:%M:%S")
    cmd_str = f"{shlex.quote(python_exe)} {shlex.quote(str(cli_path))} run {shlex.quote(username)}"

    # Clean up any prior incarnation of this unit (idempotent reschedule)
    for suffix in (".timer", ".service"):
        subprocess.run(["systemctl", "stop", f"{unit}{suffix}"], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", f"{unit}{suffix}"], capture_output=True)

    proc = subprocess.run(
        [
            "systemd-run",
            f"--unit={unit}",
            f"--on-calendar={cal}",
            f"--description=Reddit warmup session for {username}",
            "/bin/sh", "-c", cmd_str,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"systemd-run --on-calendar={cal} failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}"
        )

"""Scheduled-task wrappers for Windows Task Scheduler and Linux cron.

The agent calls `schedule_next_run(username, when, python_exe, cli_path)` after
each session. We register a one-shot task firing at `when` that invokes
`python cli.py run <username>`.

On Linux: use `at` for one-shots or rewrite a per-account crontab line.
We pick `at` here — fits the one-shot pattern naturally.

On Windows: `schtasks.exe /Create /TN <name> /SC ONCE /ST <time> /TR <cmd>`.
We delete the previous task (if any) before creating the new one.
"""

import platform
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def task_name(username: str) -> str:
    return f"RedditWarmup_{username}"


def schedule_next_run(username: str, when: datetime, cli_path: Path,
                      python_exe: str | None = None) -> None:
    """Register a one-shot task for `python cli.py run <username>` at `when`."""
    python_exe = python_exe or sys.executable
    if platform.system() == "Windows":
        _schedule_windows(username, when, cli_path, python_exe)
    else:
        _schedule_linux(username, when, cli_path, python_exe)


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

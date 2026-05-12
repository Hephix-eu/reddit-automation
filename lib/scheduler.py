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
        # Best-effort: remove pending `at` jobs whose command mentions this username.
        listing = subprocess.run(["atq"], capture_output=True, text=True).stdout
        for line in listing.splitlines():
            job_id = line.split()[0] if line.split() else None
            if not job_id:
                continue
            cmd_text = subprocess.run(
                ["at", "-c", job_id], capture_output=True, text=True
            ).stdout
            if username in cmd_text:
                subprocess.run(["atrm", job_id], capture_output=True)


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
    # `at` accepts "HH:MM YYYY-MM-DD" via stdin
    at_time = when.strftime("%H:%M %Y-%m-%d")
    cmd = f"{shlex.quote(python_exe)} {shlex.quote(str(cli_path))} run {shlex.quote(username)}"
    proc = subprocess.run(
        ["at", at_time],
        input=cmd,
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"at {at_time} failed: rc={proc.returncode} stderr={proc.stderr!r}"
        )

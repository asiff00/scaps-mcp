"""Record the running SCAPS process so an interrupted run never leaves it behind."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

PID_FILE = ".scaps-run.pid"


def remember(home: Path, pid: int) -> None:
    (home / PID_FILE).write_text(str(pid))


def forget(home: Path) -> None:
    (home / PID_FILE).unlink(missing_ok=True)


def stop_stale(home: Path, exe_name: str) -> bool:
    """Stop the SCAPS process a previous run recorded but never cleaned up; True if one was stopped."""
    path = home / PID_FILE
    try:
        pid = int(path.read_text())
    except (OSError, ValueError):
        return False
    stale = _is_scaps(pid, exe_name)
    if stale:
        _kill(pid)
    path.unlink(missing_ok=True)
    return stale


def _is_scaps(pid: int, exe_name: str) -> bool:
    if sys.platform == "win32":
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=False
        )
        return exe_name.lower() in listing.stdout.lower()
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return exe_name.lower().encode() in cmdline.lower()


def _kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)

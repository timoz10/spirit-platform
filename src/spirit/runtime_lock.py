"""Detect concurrent Spirit processes at startup.

When Spirit crashes (OOM, signal, watchdog kill) without a clean
shutdown, a manual or systemd-driven restart can leave a stale
process running while a new one tries to come up. Two Spirits
sharing one API key, writing to the same spirit_state, and placing
paper or live trades produces confused state and double-spends.

This module gives the startup path a small "is something already
running?" check. If yes, refuse to start unless the operator opts
into multi-instance mode (Pro-tier setups + emergency overrides).

The check is best-effort:
- Uses `pgrep` if available; returns [] when not.
- Filters out the current process so we don't self-detect.
- Reads `/proc/<pid>/cmdline` for diagnostic detail; missing /proc
  is tolerated.

This is hygiene, not a security boundary — a determined operator
can always start a duplicate with `--allow-multi-instance`. The
goal is to catch the common case (post-crash leftover) loudly and
once.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Sequence


def _pgrep_available() -> bool:
    return shutil.which("pgrep") is not None


def detect_other_spirit_processes() -> list[int]:
    """Return PIDs of OTHER python processes running `spirit.main`.

    Excludes the current PID. Returns [] if `pgrep` is unavailable
    or finds nothing.
    """
    if not _pgrep_available():
        return []
    my_pid = os.getpid()
    try:
        # Match any python invocation of `spirit.main` (module form),
        # not just the canonical entrypoint script — covers the dev
        # `python3 -m spirit.main` path AND the installed `spirit`
        # entrypoint (which Python expands to spirit.main:main).
        result = subprocess.run(
            ["pgrep", "-f", r"python.*\bspirit\.main\b"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid != my_pid:
            pids.append(pid)
    return pids


def get_process_cmdline(pid: int) -> str:
    """Read `/proc/<pid>/cmdline`; returns '' on any error."""
    try:
        with open(f"/proc/{pid}/cmdline", "r") as f:
            return f.read().replace("\0", " ").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def get_process_age_seconds(pid: int) -> float:
    """Return wall-clock age of the process in seconds; -1 on error."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etimes="],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1.0
    if result.returncode != 0:
        return -1.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return -1.0


def format_conflict_lines(pids: Sequence[int]) -> list[str]:
    """One log-friendly line per conflicting PID."""
    lines: list[str] = []
    for pid in pids:
        cmd = get_process_cmdline(pid)
        age = get_process_age_seconds(pid)
        if age >= 86400:
            age_str = f"{int(age // 86400)}d"
        elif age >= 3600:
            age_str = f"{int(age // 3600)}h"
        elif age >= 60:
            age_str = f"{int(age // 60)}m"
        elif age >= 0:
            age_str = f"{int(age)}s"
        else:
            age_str = "?"
        # Trim cmdline so a long systemd-quoted invocation doesn't
        # blow out a log line.
        cmd_short = cmd[:120] + ("…" if len(cmd) > 120 else "")
        lines.append(f"  PID {pid}  age={age_str}  {cmd_short}")
    return lines


def is_multi_instance_allowed(argv: Sequence[str] | None = None) -> bool:
    """Whether the caller has opted into multi-instance mode.

    True if either `--allow-multi-instance` is in argv (or sys.argv
    by default) or `SPIRIT_ALLOW_MULTI_INSTANCE` env var is truthy.
    """
    import sys
    if argv is None:
        argv = sys.argv
    if "--allow-multi-instance" in argv:
        return True
    val = os.environ.get("SPIRIT_ALLOW_MULTI_INSTANCE", "").strip().lower()
    return val in ("1", "true", "yes")

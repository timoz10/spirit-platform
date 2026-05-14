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


def _read_pid_cmdline_argv(pid: int) -> list[str]:
    """Read `/proc/<pid>/cmdline` and return it as a list of args.

    `/proc/<pid>/cmdline` is null-separated. Returns [] on any read
    error (missing /proc, vanished process, permission denied).
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except (FileNotFoundError, PermissionError, OSError):
        return []
    if not raw:
        return []
    # Strip trailing NUL the kernel often appends.
    parts = raw.split(b"\0")
    return [p.decode("utf-8", errors="replace") for p in parts if p]


def _argv_looks_like_spirit(argv: list[str]) -> bool:
    """Return True if argv is from a real Spirit Python process.

    Distinguishes:
      - `python3 -m spirit.main ...` (dev launch)             → True
      - `python3 /path/to/spirit/main.py ...`                  → True
      - `/venv/bin/spirit ...` (installed console script)      → True
      - `bash -c "... python3 -m spirit.main ..."`             → False
      - `tmux new-session "... python3 -m spirit.main ..."`    → False
      - `sh -c "..."` / `setsid ...` and other wrappers        → False

    The shell-wrapper cases are why this exists: `pgrep -f` matches the
    pattern against the FULL joined argv of every process, so any
    parent shell that mentions `spirit.main` in its argv string is a
    false positive. Discriminating by argv[0] separates the actual
    Python interpreter from shells/launchers that merely reference it.
    """
    if not argv:
        return False
    arg0 = os.path.basename(argv[0]).lower()
    # Python interpreters: python, python3, python3.12, pythonw, etc.
    if arg0.startswith("python"):
        return True
    # Installed console script — pip places a shim at `<venv>/bin/spirit`
    # whose argv[0] is the script path, not python. The shim itself
    # imports spirit.main:main and calls it.
    if arg0 == "spirit":
        return True
    return False


def detect_other_spirit_processes() -> list[int]:
    """Return PIDs of OTHER python processes running `spirit.main`.

    Two-stage detection:
      1. `pgrep -f` matches any process with `spirit.main` in its argv.
      2. Each match is verified by reading `/proc/<pid>/cmdline` and
         checking argv[0] is a real Python interpreter (or the
         installed `spirit` entrypoint) — NOT a shell/tmux wrapper
         that just happens to mention `spirit.main` in its argv.

    Stage 2 fixes the false-positive that fires whenever Spirit is
    launched via `bash -c "... spirit.main ..."` or
    `tmux new-session "... spirit.main ..."` — i.e. the documented
    runbook pattern for systemd-less deployments.

    Excludes the current PID. Returns [] if `pgrep` is unavailable
    or finds nothing.
    """
    if not _pgrep_available():
        return []
    my_pid = os.getpid()
    try:
        # Cast the net wide on pgrep — any process whose argv mentions
        # spirit.main is a candidate. Stage 2 below filters out the
        # shell wrappers that this matches incidentally.
        result = subprocess.run(
            ["pgrep", "-f", r"\bspirit\.main\b"],
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
        if pid == my_pid:
            continue
        # Stage 2: verify this is a real Spirit Python process by
        # reading argv[0], not a shell/tmux wrapper that happens to
        # mention spirit.main in its argv.
        argv = _read_pid_cmdline_argv(pid)
        if not _argv_looks_like_spirit(argv):
            continue
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

"""spirit-health — local liveness check.

Reads the Free-tier local SQLite (~/.spirit/<instance>/spirit.db) and
prints a one-screen summary so the operator can answer "is Spirit
alive?" in one command without tailing logs.

Plus-tier (cloud-backed) state isn't wired here yet — this v2.2.1
release ships the Free-tier path; the Plus path lands when the
gateway exposes a self-status endpoint.

Usage:
    python3 -m spirit.health [--instance NAME]
    spirit-health
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _format_age(dt: Optional[datetime], now: datetime) -> str:
    """Human-readable age string."""
    if dt is None:
        return "(never)"
    delta = now - dt
    secs = delta.total_seconds()
    if secs < 0:
        return f"in {int(-secs)}s"
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs / 60)}m ago"
    if secs < 86400:
        h = int(secs / 3600)
        m = int((secs % 3600) / 60)
        return f"{h}h {m}m ago"
    return f"{int(secs / 86400)}d ago"


def _parse_dt(s) -> Optional[datetime]:
    """Best-effort parse of a heterogeneous timestamp into UTC datetime."""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        text = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _query_one(db_path: Path, sql: str, params: tuple = ()) -> Optional[dict]:
    """Read-only single-row query. Returns None on any sqlite error."""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        return None


def _check_running() -> tuple[bool, list[int]]:
    """Detect whether spirit.main is currently running anywhere on this box.

    spirit-health itself runs as a separate process; the runtime-lock
    helper excludes the *caller's* PID. So if any other process is
    running spirit.main, we'll see it here.
    """
    try:
        from spirit.runtime_lock import detect_other_spirit_processes
    except ImportError:
        return False, []
    pids = detect_other_spirit_processes()
    return (len(pids) > 0, pids)


def _print_summary(instance: str, db_path: Path, now: datetime) -> None:
    """Render the summary block. Pure stdout — no side effects."""
    print(f"Spirit instance: {instance}")
    print("=" * 60)

    # 1. Process state
    running, pids = _check_running()
    if running:
        print(f"  Process:        running (PID {pids[0]})")
    else:
        print(f"  Process:        NOT running")

    # 2. Local DB presence
    if not db_path.exists():
        print(f"  Local DB:       not found at {db_path}")
        print()
        if not running:
            print("  Status:         Spirit doesn't appear to be installed for this")
            print(f"                  instance. Run `python3 -m spirit.setup` to set")
            print(f"                  up Free tier, or check --instance.")
        else:
            print("  Status:         Process running, no local DB — likely Plus tier.")
            print("                  Plus state lives in the cloud; check the portal.")
        return

    print(f"  Local DB:       {db_path}")

    # 3. Heartbeat
    hb = _query_one(
        db_path,
        "SELECT updated_at, status FROM daemon_heartbeats "
        "WHERE daemon_id = ? ORDER BY updated_at DESC LIMIT 1",
        (f"spirit:{instance}",),
    )
    if hb:
        last_hb = _parse_dt(hb.get("updated_at"))
        age_str = _format_age(last_hb, now)
        status = hb.get("status", "unknown")
        print(f"  Last heartbeat: {last_hb} ({age_str})")
        print(f"  Status:         {status}")
    else:
        print(f"  Last heartbeat: (none — Spirit may have never run on this box)")

    # 4. State (last candle, version stamp)
    for key in (f"version:{instance}:arch", f"version:{instance}:started_at"):
        row = _query_one(db_path,
                         "SELECT value FROM spirit_state WHERE key = ?",
                         (key,))
        if row:
            label = key.split(":")[-1].replace("_", " ").title()
            print(f"  {label}:{' ' * (16 - len(label) - 1)}{row['value']}")

    # 5. Performance
    last_trade = _query_one(
        db_path,
        "SELECT entry_timestamp, pair, strategy_name, pnl_realized "
        "FROM strategy_performance ORDER BY entry_timestamp DESC LIMIT 1",
    )
    count_row = _query_one(db_path, "SELECT COUNT(*) AS n FROM strategy_performance")
    total = (count_row or {}).get("n", 0)
    if last_trade and last_trade.get("entry_timestamp"):
        last_ts = _parse_dt(last_trade.get("entry_timestamp"))
        age_str = _format_age(last_ts, now)
        pair = last_trade.get("pair") or "?"
        strat = last_trade.get("strategy_name") or "?"
        print(f"  Last trade:     {pair} ({strat}, {age_str})")
    else:
        print(f"  Last trade:     (none recorded)")
    print(f"  Total trades:   {total}")


def _print_tips() -> None:
    print()
    print("Tips:")
    print("  • Logs:         /var/log/spirit/spirit.log  or  journalctl -u spirit")
    print("  • Spirit is quiet by default — strategies only log on signals.")
    print("    Periodic [SPIRIT] alive lines appear every 30 min by design.")
    print("  • Revoke an API key:  https://portal.tradebot.live/keys")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spirit-health",
        description="Print a one-screen Spirit liveness summary "
                    "(Free-tier local SQLite path).",
    )
    parser.add_argument(
        "--instance",
        default=os.environ.get("SPIRIT_INSTANCE", "prod"),
        help="Instance name (default: $SPIRIT_INSTANCE or 'prod')",
    )
    parser.add_argument(
        "--no-tips",
        action="store_true",
        help="Suppress the help block at the bottom",
    )
    args = parser.parse_args(argv)

    instance = args.instance.strip()
    if not instance:
        print("ERROR: --instance is empty (set $SPIRIT_INSTANCE or pass --instance NAME)",
              file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    db_path = Path.home() / ".spirit" / instance / "spirit.db"

    _print_summary(instance, db_path, now)
    if not args.no_tips:
        _print_tips()
    return 0


if __name__ == "__main__":
    sys.exit(main())

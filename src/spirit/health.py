"""spirit-health — Spirit installation diagnostic.

Auto-discovers every Spirit instance set up on this machine (one subdir
per instance under ~/.spirit/), reports per-instance state, and marks
the "active" instance (the one resolved from SPIRIT_INSTANCE / .env
that `spirit` would use if you typed it right now).

Use cases:
    spirit-health                   # auto-discover, show all instances
    spirit-health --instance NAME   # focus on one instance
    spirit-health --all             # include configured-but-idle instances
    spirit-health --verbose         # extra detail (schema version, trades)

This is a diagnostic tool — it never returns FATAL on "this instance
isn't set up." Block-startup logic belongs inside `spirit` itself,
where it knows the user's intent (mode, tier, instance).


Exit code contract
==================

`spirit-health` follows a four-state exit code contract (see
docs/reference/MODULE_CONTRACTS.md for the cross-tool design):

    RC_HEALTHY        = 0  — at least one instance with a recent heartbeat
                             (process up). Monitoring should treat this as green.
    RC_NO_INSTANCES   = 1  — no instances configured. Informational only —
                             this is the expected state on a freshly installed
                             box that hasn't run `spirit-setup` yet. CI gates
                             that test the tool on a clean machine assert
                             exactly this code.
    RC_DEGRADED       = 2  — one or more instances exist but none has a recent
                             heartbeat (stopped daemon, stale heartbeat,
                             orphan DB without config, or config without DB).
                             Monitoring should alert: needs attention.
    RC_INTERNAL_ERROR = 3  — uncaught exception inside spirit-health itself
                             (import failure, filesystem permission error,
                             corrupted sqlite, etc.). Alert on the tool, not
                             on the instance.

The contract is pinned by tests/test_spirit_health_contract.py and asserted
by both CI gates (.github/workflows/publish.yml and rc-validation.yml).
Any change to these exit codes is a breaking change for downstream
monitoring scripts — bump the MAJOR version and call it out in CHANGELOG.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Exit-code contract (see module docstring).
RC_HEALTHY = 0
RC_NO_INSTANCES = 1
RC_DEGRADED = 2
RC_INTERNAL_ERROR = 3


# Names under ~/.spirit/ that are NOT instances (reserved or shared).
_RESERVED_DIRS = frozenset({"strategies", "logs", "cache"})


# ----------------------------------------------------------------------------
# Time formatting (shared)
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# Instance discovery + state-gathering
# ----------------------------------------------------------------------------

@dataclass
class InstanceInfo:
    name: str
    instance_dir: Path

    # File-system signals
    has_db: bool = False
    db_path: Optional[Path] = None
    db_size_bytes: int = 0
    has_yaml: bool = False
    has_env: bool = False

    # DB-derived signals (only populated if has_db)
    last_heartbeat: Optional[datetime] = None
    heartbeat_status: Optional[str] = None
    version_stamp: Optional[str] = None
    last_trade_at: Optional[datetime] = None
    last_trade_pair: Optional[str] = None
    last_trade_strategy: Optional[str] = None
    total_trades: int = 0

    # Computed
    is_running: bool = False
    is_active: bool = False
    is_orphan: bool = False

    # Detail field for verbose mode
    extra: dict = field(default_factory=dict)


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


def _discover_instances(spirit_dir: Path) -> list[InstanceInfo]:
    """Scan ~/.spirit/*/ for instances. Each non-reserved subdir = one instance."""
    if not spirit_dir.exists():
        return []
    instances = []
    for d in sorted(spirit_dir.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith('.'):
            continue
        if d.name in _RESERVED_DIRS:
            continue
        instances.append(_gather_info(d))
    return instances


def _gather_info(instance_dir: Path, now: Optional[datetime] = None) -> InstanceInfo:
    """Populate an InstanceInfo from a single ~/.spirit/<instance>/ dir."""
    if now is None:
        now = datetime.now(timezone.utc)

    info = InstanceInfo(name=instance_dir.name, instance_dir=instance_dir)

    # File-system signals
    db = instance_dir / "spirit.db"
    if db.exists():
        info.has_db = True
        info.db_path = db
        try:
            info.db_size_bytes = db.stat().st_size
        except OSError:
            info.db_size_bytes = 0

    info.has_yaml = (instance_dir / "spirit.yaml").exists()
    info.has_env = (instance_dir / ".env").exists() or (instance_dir / "env").exists()

    # DB-derived signals
    if info.has_db:
        hb = _query_one(
            db,
            "SELECT last_heartbeat, status FROM daemon_heartbeats "
            "WHERE daemon_id = ? ORDER BY last_heartbeat DESC LIMIT 1",
            (f"spirit:{info.name}",),
        )
        if hb:
            info.last_heartbeat = _parse_dt(hb.get("last_heartbeat"))
            info.heartbeat_status = hb.get("status")
            # Running heuristic: heartbeat within the last 2 minutes.
            # Daemons heartbeat more frequently than this when alive.
            if info.last_heartbeat is not None:
                delta = (now - info.last_heartbeat).total_seconds()
                info.is_running = 0 <= delta < 120

        # Version stamp
        v = _query_one(
            db, "SELECT value FROM spirit_state WHERE key = ?",
            (f"version:{info.name}:arch",),
        )
        if v and v.get("value"):
            # Strip JSON-style quotes that some writers leave on string values
            info.version_stamp = str(v["value"]).strip().strip('"')

        # Last trade
        last_trade = _query_one(
            db,
            "SELECT entry_timestamp, pair, strategy_name "
            "FROM strategy_performance ORDER BY entry_timestamp DESC LIMIT 1",
        )
        if last_trade and last_trade.get("entry_timestamp"):
            info.last_trade_at = _parse_dt(last_trade.get("entry_timestamp"))
            info.last_trade_pair = last_trade.get("pair")
            info.last_trade_strategy = last_trade.get("strategy_name")

        # Total trade count
        count_row = _query_one(db, "SELECT COUNT(*) AS n FROM strategy_performance")
        if count_row:
            info.total_trades = int(count_row.get("n") or 0)

    # Orphan detection
    # - Has DB but no config (wizard never wrote here, but data exists)
    # - Has config but no DB (wizard wrote but never started)
    info.is_orphan = (
        (info.has_db and not info.has_yaml and not info.has_env)
        or (info.has_yaml and not info.has_db)
    )

    return info


def _resolve_active_instance(instances: list[InstanceInfo]) -> Optional[str]:
    """Find the instance the user would currently start with `spirit`.

    Resolution order:
      1. SPIRIT_INSTANCE env var (any source)
      2. spirit.yaml's `instance:` field in a discovered instance dir
      3. If exactly one configured instance exists, that one
      4. Otherwise None (user must choose)
    """
    explicit = os.environ.get("SPIRIT_INSTANCE", "").strip()
    if explicit:
        return explicit

    # Try the global config_loader path (it reads env + yaml in the
    # search-path order spirit.main uses).
    try:
        from spirit.utils.config_loader import get_config
        cfg_instance = (get_config("SPIRIT_INSTANCE", "") or "").strip()
        if cfg_instance:
            return cfg_instance
    except Exception:
        pass

    # Fallback: if exactly one instance has both config and DB, treat as active.
    configured = [i for i in instances if (i.has_yaml or i.has_env) and i.has_db]
    if len(configured) == 1:
        return configured[0].name
    return None


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

def _format_size(n: int) -> str:
    """Human-readable file size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.1f} GB"


def _state_icon(info: InstanceInfo) -> str:
    """One-character status icon for the instance summary line."""
    if info.is_running:
        return "✓"
    if info.is_orphan and info.has_db and not (info.has_yaml or info.has_env):
        # True orphan — DB exists but no config. Old install, configuration
        # was deleted. User intervention needed.
        return "?"
    if info.is_orphan and info.has_yaml and not info.has_db:
        # "Configured, not yet started" — fresh wizard output. Normal
        # transient state, not broken.
        return "○"
    return "✗"


def _state_label(info: InstanceInfo, is_active: bool) -> str:
    """One-line status descriptor next to the instance name."""
    parts = []
    if is_active:
        parts.append("active")
    if info.is_running:
        parts.append("running")
    elif info.is_orphan:
        if info.has_db and not (info.has_yaml or info.has_env):
            parts.append("orphan — DB without config")
        elif info.has_yaml and not info.has_db:
            # Fresh wizard output, never started — not "orphan", just unstarted.
            parts.append("configured, not yet started")
    elif info.has_db:
        parts.append("idle")
    else:
        parts.append("not set up")
    return ", ".join(parts) if parts else "unknown"


def _render_instance(info: InstanceInfo, now: datetime, verbose: bool) -> list[str]:
    """Render one instance block as a list of lines."""
    lines = []
    icon = _state_icon(info)
    label = _state_label(info, info.is_active)
    lines.append(f"  {icon} {info.name:<12} ({label})")

    if info.is_running and info.last_heartbeat:
        lines.append(
            f"      Heartbeat:  {info.last_heartbeat.isoformat()} "
            f"({_format_age(info.last_heartbeat, now)}, status={info.heartbeat_status or 'unknown'})"
        )

    if info.has_db:
        size = _format_size(info.db_size_bytes)
        lines.append(f"      DB:         {info.db_path} ({size})")
    else:
        lines.append(f"      DB:         not found at {info.instance_dir / 'spirit.db'}")

    config_bits = []
    if info.has_yaml:
        config_bits.append("spirit.yaml ✓")
    else:
        config_bits.append("spirit.yaml ✗")
    if info.has_env:
        config_bits.append(".env ✓")
    else:
        config_bits.append(".env ✗")
    lines.append(f"      Config:     {', '.join(config_bits)} (in {info.instance_dir})")

    if info.version_stamp:
        lines.append(f"      Version:    {info.version_stamp}")

    if info.last_trade_at:
        pair = info.last_trade_pair or "?"
        strat = info.last_trade_strategy or "?"
        lines.append(
            f"      Last trade: {pair} ({strat}, {_format_age(info.last_trade_at, now)})"
        )
    if info.total_trades > 0:
        lines.append(f"      Trades:     {info.total_trades} total")

    if info.is_orphan:
        if info.has_db and not (info.has_yaml or info.has_env):
            # `spirit-setup` doesn't take `--instance` post-#733 — the
            # wizard prompts for it. The hint is "re-run setup and
            # choose this instance name."
            lines.append(f"      Repair:     re-run `spirit-setup` and choose "
                         f"instance name '{info.name}', or delete the directory")
        elif info.has_yaml and not info.has_db:
            # Spirit doesn't take `--instance` either — the active
            # instance comes from SPIRIT_INSTANCE env or the single-dir
            # autodetect. `--mode paper` (not `test`) is the canonical
            # first-run mode that initialises the local DB.
            lines.append(f"      Start:      `SPIRIT_INSTANCE={info.name} "
                         f"spirit --mode paper` to initialise this instance, "
                         f"or just `spirit --mode paper` if it's the only one")

    if verbose and info.extra:
        for k, v in info.extra.items():
            lines.append(f"      {k}: {v}")

    return lines


def _render_summary(
    instances: list[InstanceInfo],
    active_name: Optional[str],
    now: datetime,
    verbose: bool,
    filter_name: Optional[str] = None,
) -> str:
    """Render the full health summary as a string."""
    try:
        from spirit import __version__
    except ImportError:
        __version__ = "unknown"

    out = []
    out.append("=" * 60)
    out.append(f"  spirit-platform {__version__}")
    out.append("=" * 60)
    out.append("")

    if not instances:
        out.append("No Spirit instances found on this machine.")
        out.append("")
        out.append("To set up:")
        out.append("  spirit-setup")
        out.append("")
        return "\n".join(out)

    shown = instances
    if filter_name:
        shown = [i for i in instances if i.name == filter_name]
        if not shown:
            out.append(f"No instance named '{filter_name}' found.")
            out.append("")
            out.append("Configured instances on this machine:")
            for i in instances:
                out.append(f"  - {i.name}")
            out.append("")
            return "\n".join(out)

    # Mark active
    for i in shown:
        i.is_active = (i.name == active_name)

    out.append("Instances:")
    for i in shown:
        out.extend(_render_instance(i, now, verbose))
        out.append("")

    # Cross-cutting warnings — only relevant when showing all instances
    # (when the user filters with --instance, these are noise).
    if filter_name is None:
        if active_name and not any(i.name == active_name for i in instances):
            out.append(
                f"⚠️  $SPIRIT_INSTANCE='{active_name}' but no matching instance found. "
                f"Run `spirit-setup --instance {active_name}` to create it."
            )
            out.append("")
        elif active_name is None and len(instances) > 1:
            out.append(
                "ℹ️  No active instance resolved. Set SPIRIT_INSTANCE or "
                "pass --instance NAME to select one."
            )
            out.append("")

    return "\n".join(out)


def _print_tips() -> None:
    print("Tips:")
    print("  • Run a specific instance:   spirit --instance <name> --mode paper")
    print("  • Set the active instance:   export SPIRIT_INSTANCE=<name>")
    print("  • Logs (if systemd-managed): journalctl -u spirit")
    print("  • Diagnostic preflight:      spirit-preflight")
    print("  • Revoke a gateway API key:  https://portal.tradebot.live/keys")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _compute_exit_code(instances: list[InstanceInfo]) -> int:
    """Apply the spirit-health exit-code contract to a discovered instance list.

    See module docstring for the contract. Pure function — kept separate so
    tests/test_spirit_health_contract.py can assert it directly without
    spawning a subprocess.
    """
    if not instances:
        return RC_NO_INSTANCES
    if any(info.is_running for info in instances):
        return RC_HEALTHY
    return RC_DEGRADED


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spirit-health",
        description="Diagnostic for Spirit installations on this machine. "
                    "Auto-discovers instances under ~/.spirit/ and reports "
                    "per-instance state.",
    )
    parser.add_argument(
        "--instance",
        default=None,
        help="Filter to a single instance (default: show all discovered)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include orphan/idle instances (default: same — shown for symmetry)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra detail per instance",
    )
    parser.add_argument(
        "--no-tips",
        action="store_true",
        help="Suppress the help block at the bottom",
    )
    args = parser.parse_args(argv)

    try:
        now = datetime.now(timezone.utc)
        spirit_dir = Path.home() / ".spirit"
        instances = _discover_instances(spirit_dir)
        active_name = _resolve_active_instance(instances)

        print(_render_summary(
            instances, active_name, now, args.verbose,
            filter_name=args.instance.strip() if args.instance else None,
        ))

        if not args.no_tips:
            _print_tips()

        return _compute_exit_code(instances)
    except Exception as exc:
        # Tool itself failed — return RC_INTERNAL_ERROR so monitoring
        # scripts can distinguish "tool is broken" from "instance is broken".
        print(
            f"\nspirit-health: internal error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return RC_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())

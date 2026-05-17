"""
Run Manager — Replay run lifecycle management.

Provides run_id tagging for replay backtests so multiple runs over the same
date range can coexist in strategy_performance, scorer_outcomes, and
risk_gate_decisions for A/B comparison.

Live/paper data uses run_id = 'live' (constant).
Replay data uses run_id = UUID (unique per run).

Functions:
    generate_run_id()   — Returns a new UUID string
    register_run()      — INSERT into replay_runs registry
    finalize_run()      — Compute summary stats, UPDATE replay_runs
    list_runs()         — SELECT recent runs
    delete_run()        — DELETE from all 3 tables + registry

Upstream Dependencies: None (leaf module)
Outputs: replay_runs table
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from spirit.logger import get_logger

# db_connection is gateway/dev-only; api-mode public installs don't have it.
# Importing run_manager must succeed for LIVE_RUN_ID export even when DB is
# absent. Functions below import execute_query lazily inside their bodies.

logger = get_logger("run_manager")

LIVE_RUN_ID = 'live'


def generate_run_id() -> str:
    """Generate a unique run ID (UUID4 string)."""
    return str(uuid.uuid4())


def register_run(
    run_id: str,
    strategy_name: str,
    pairs: List[str],
    start_date: str,
    end_date: str,
    tag: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    git_hash: Optional[str] = None,
) -> None:
    """Register a new replay run in the replay_runs table.

    Args:
        run_id: UUID string from generate_run_id()
        strategy_name: e.g. 'zone_bounce'
        pairs: List of pair symbols
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        tag: Human-readable label (e.g. 'baseline', 'no-cooldown')
        config: Snapshot of relevant config values (stored as JSONB)
        git_hash: Short git hash at time of run
    """
    import json
    from spirit.utils.db_connection import execute_query

    execute_query("""
        INSERT INTO replay_runs (id, tag, strategy_name, pairs, start_date, end_date,
                                 config, git_hash, status, started_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running', NOW())
    """, (
        run_id, tag, strategy_name,
        ','.join(pairs), start_date, end_date,
        json.dumps(config) if config else None,
        git_hash,
    ), fetch='none')

    logger.info(
        f"[RUN] Registered: id={run_id[:8]}... tag={tag or '(none)'} "
        f"strategy={strategy_name} pairs={','.join(pairs)} "
        f"range={start_date} to {end_date}"
    )


def finalize_run(run_id: str, status: str = 'completed') -> Dict[str, Any]:
    """Compute summary stats from strategy_performance and update replay_runs.

    Args:
        run_id: The run to finalize
        status: 'completed' or 'failed'

    Returns:
        Dict with summary stats (total_trades, win_rate, profit_factor, net_pnl_pct)
    """
    from spirit.utils.db_connection import execute_query

    # Compute summary stats from this run's trades
    stats = execute_query("""
        SELECT
            COUNT(*) as total_trades,
            AVG(CASE WHEN is_win THEN 1.0 ELSE 0.0 END) as win_rate,
            CASE
                WHEN SUM(CASE WHEN pnl_pct < 0 THEN ABS(pnl_pct) ELSE 0 END) > 0
                THEN SUM(CASE WHEN pnl_pct > 0 THEN pnl_pct ELSE 0 END) /
                     SUM(CASE WHEN pnl_pct < 0 THEN ABS(pnl_pct) ELSE 0 END)
                ELSE NULL
            END as profit_factor,
            SUM(pnl_pct) as net_pnl_pct
        FROM strategy_performance
        WHERE run_id = %s
    """, (run_id,), fetch='one')

    if not stats:
        stats = {'total_trades': 0, 'win_rate': None, 'profit_factor': None, 'net_pnl_pct': None}

    # Update the registry
    execute_query("""
        UPDATE replay_runs
        SET status = %s,
            completed_at = NOW(),
            total_trades = %s,
            win_rate = %s,
            profit_factor = %s,
            net_pnl_pct = %s
        WHERE id = %s
    """, (
        status,
        stats.get('total_trades', 0),
        stats.get('win_rate'),
        stats.get('profit_factor'),
        stats.get('net_pnl_pct'),
        run_id,
    ), fetch='none')

    total = stats.get('total_trades', 0)
    wr = stats.get('win_rate')
    pf = stats.get('profit_factor')
    net = stats.get('net_pnl_pct')
    logger.info(
        f"[RUN] Finalized: id={run_id[:8]}... status={status} "
        f"trades={total} WR={wr:.1%} PF={pf:.2f} net={net:.2f}%"
        if wr is not None and pf is not None and net is not None else
        f"[RUN] Finalized: id={run_id[:8]}... status={status} trades={total}"
    )

    return dict(stats)


def list_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """List recent replay runs from the registry.

    Returns list of dicts with run metadata and summary stats.
    """
    from spirit.utils.db_connection import execute_query

    rows = execute_query("""
        SELECT id, tag, strategy_name, pairs, start_date, end_date,
               git_hash, status, started_at, completed_at,
               total_trades, win_rate, profit_factor, net_pnl_pct
        FROM replay_runs
        ORDER BY started_at DESC
        LIMIT %s
    """, (limit,))

    return rows or []


def delete_run(run_id: str) -> Dict[str, int]:
    """Delete all data for a specific run from the 3 result tables + registry.

    Safety: refuses to delete run_id='live' to protect production data.

    Returns dict with deletion counts per table.
    """
    if run_id == LIVE_RUN_ID:
        raise ValueError("Cannot delete live data via delete_run(). Use direct SQL if needed.")

    from spirit.utils.db_connection import execute_query

    counts = {}

    # Delete from each results table
    for table in ('strategy_performance', 'scorer_outcomes', 'risk_gate_decisions'):
        result = execute_query(
            f"DELETE FROM {table} WHERE run_id = %s",
            (run_id,), fetch='rowcount',
        )
        counts[table] = result or 0

    # Delete from registry
    result = execute_query(
        "DELETE FROM replay_runs WHERE id = %s",
        (run_id,), fetch='rowcount',
    )
    counts['replay_runs'] = result or 0

    logger.info(
        f"[RUN] Deleted: id={run_id[:8]}... "
        f"sp={counts['strategy_performance']} "
        f"so={counts['scorer_outcomes']} "
        f"rgd={counts['risk_gate_decisions']}"
    )

    return counts

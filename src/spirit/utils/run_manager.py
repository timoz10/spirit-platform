"""
Run Manager — Replay run lifecycle management.

Provides run_id tagging for replay backtests so multiple runs over the same
date range can coexist in strategy_performance, scorer_outcomes, and
risk_gate_decisions for A/B comparison.

Live/paper data uses run_id = 'live' (constant).
Replay data uses run_id = UUID (unique per run).

Functions:
    generate_run_id()   — Returns a new UUID string
    register_run()      — INSERT into replay_runs registry (dev/CI only)
    finalize_run()      — Compute summary stats, UPDATE replay_runs (dev/CI only)
    list_runs()         — Delegated to DataProvider.list_runs (tier-agnostic)
    delete_run()        — Delegated to DataProvider.delete_run (tier-agnostic)

Public contract
---------------
list_runs() and delete_run() are tier-agnostic since v2.2.4. They call the
FrameworkDataProvider Protocol methods which route per tier inside
get_data_provider(): Free → SqliteDataProvider, Paid → ApiDataProvider
(gateway /v1/runs). No more in-module branching — `_is_free_tier()` is
gone (closes #779 properly).

register_run() / finalize_run() retain their direct-PG path because
they're only invoked from `spirit --mode replay`, which runs in
dev/CI/internal-canary contexts where PG access exists.

Upstream Dependencies: spirit.utils.data_provider (for list_runs/delete_run)
"""

import uuid
from typing import Any, Dict, List, Optional

from spirit.logger import get_logger

# db_connection is gateway/dev-only; replay-mode code paths import it lazily.
# Free-tier public installs run `spirit --list-runs` / `--delete-run` through
# the DataProvider — no PG needed for the customer-facing surface.

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
    """List recent runs via the DataProvider Protocol.

    Tier routing is owned by ``get_data_provider()`` — Free returns
    runs derived from local strategy_performance; Paid returns them
    from the gateway /v1/runs endpoint. Both tiers produce the same
    14-field shape; paid-only legacy fields (``tag``, ``git_hash``,
    ``profit_factor``) come back as ``None`` on Free.
    """
    from spirit.utils.data_provider import get_data_provider
    dp = get_data_provider()
    return dp.list_runs(limit=limit)


def delete_run(run_id: str) -> Dict[str, int]:
    """Delete a run via the DataProvider Protocol.

    Refuses ``run_id='live'`` with ValueError (gateway also enforces this
    with HTTP 400 — we surface it locally too so callers don't pay a
    round-trip to learn the obvious). Returns the 4-key counts dict
    documented in MODULE_CONTRACTS.md; Free returns zero for the three
    paid-only tables (scorer_outcomes, risk_gate_decisions, replay_runs).
    """
    if run_id == LIVE_RUN_ID:
        raise ValueError(
            "Cannot delete live data via delete_run(). Use direct SQL if needed."
        )

    from spirit.utils.data_provider import get_data_provider
    dp = get_data_provider()
    counts = dp.delete_run(run_id)
    logger.info(
        "[RUN] Deleted: id=%s... sp=%d so=%d rgd=%d rr=%d",
        run_id[:8],
        counts.get('strategy_performance', 0),
        counts.get('scorer_outcomes', 0),
        counts.get('risk_gate_decisions', 0),
        counts.get('replay_runs', 0),
    )
    return counts

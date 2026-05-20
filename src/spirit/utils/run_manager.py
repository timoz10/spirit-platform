"""
Run Manager — Replay run lifecycle management.

Provides run_id tagging for replay backtests so multiple runs over the same
date range can coexist in strategy_performance, scorer_outcomes, and
risk_gate_decisions for A/B comparison.

Live/paper data uses run_id = 'live' (constant).
Replay data uses run_id = UUID (unique per run).

Functions:
    generate_run_id()   — Returns a new UUID string
    register_run()      — INSERT into replay_runs registry (PG only)
    finalize_run()      — Compute summary stats, UPDATE replay_runs (PG only)
    list_runs()         — SELECT recent runs (PG: replay_runs; Free: derived
                          from strategy_performance GROUP BY run_id)
    delete_run()        — DELETE from results tables (Free: strategy_performance
                          only; PG: strategy_performance + scorer_outcomes +
                          risk_gate_decisions + replay_runs)

Public contract
---------------
list_runs() and delete_run() are tier-aware. They MUST succeed without
crashing on any tier (Free / Plus / Pro). The Free path is derived from
local SQLite (no PG access), so the module must not eagerly import
spirit.utils.db_connection.

Tier routing follows the same SPIRIT_TIER convention as
spirit.utils.data_provider.get_data_provider(): `free` → SQLite path,
everything else → PG path. See docs/reference/MODULE_CONTRACTS.md.

Upstream Dependencies: None (leaf module)
Outputs (PG path): replay_runs table
Outputs (Free path): strategy_performance table (SQLite, no separate
                     registry — runs are derived by GROUP BY run_id)
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from spirit.logger import get_logger

# db_connection is gateway/dev-only; Free-tier public installs don't have it.
# Importing run_manager must succeed for LIVE_RUN_ID export even when DB is
# absent. Functions below import execute_query lazily inside the PG branch.

logger = get_logger("run_manager")

LIVE_RUN_ID = 'live'


def _is_free_tier() -> bool:
    """Detect Free tier the same way data_provider.get_data_provider() does.

    SPIRIT_TIER is checked via get_config first (so yaml values count),
    then env var. Anything other than 'free' is treated as paid/PG.
    """
    import os

    from spirit.utils.config_loader import get_config

    tier = (get_config("SPIRIT_TIER", "") or "").strip().lower()
    if not tier:
        tier = os.environ.get("SPIRIT_TIER", "").strip().lower()
    return tier == "free"


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
    """List recent replay runs.

    Free tier: derived from strategy_performance GROUP BY run_id in local
    SQLite. No replay_runs table — fields not present in strategy_performance
    (tag, git_hash, profit_factor) come back as None.

    Plus/Pro tier: from the replay_runs registry in PG.

    Returns list of dicts with the keys main.py's print loop expects:
    id, tag, strategy_name, pairs, start_date, end_date, git_hash, status,
    started_at, completed_at, total_trades, win_rate, profit_factor, net_pnl_pct.
    """
    if _is_free_tier():
        return _list_runs_sqlite(limit)
    return _list_runs_pg(limit)


def _list_runs_pg(limit: int) -> List[Dict[str, Any]]:
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


def _list_runs_sqlite(limit: int) -> List[Dict[str, Any]]:
    """Derive run list from strategy_performance via GROUP BY run_id.

    Paid tiers have a separate replay_runs registry table; Free does not.
    The derivation gives users the same shape (one row per run) with the
    aggregates they actually need (trade count, win rate, net PnL, date
    range). Fields that only exist in replay_runs (tag, git_hash,
    profit_factor) are returned as None — main.py renders them as '-'.
    """
    from spirit.utils.data_provider import get_data_provider

    dp = get_data_provider()
    # CompositeDataProvider on Free wraps a SqliteDataProvider for writes.
    # Reach through to the SQLite handle for the raw aggregation query.
    sqlite = _resolve_sqlite_provider(dp)
    if sqlite is None:
        logger.warning(
            "[RUN] list_runs on Free tier could not resolve a SqliteDataProvider; "
            "returning empty list. (DataProvider type: %s)",
            type(dp).__name__,
        )
        return []

    sql = """
        SELECT
            run_id,
            strategy_name,
            COUNT(*) AS total_trades,
            AVG(CASE WHEN is_win = 1 THEN 1.0 ELSE 0.0 END) AS win_rate,
            SUM(pnl_pct) AS net_pnl_pct,
            MIN(entry_timestamp) AS started_at,
            MAX(timestamp) AS completed_at,
            GROUP_CONCAT(DISTINCT pair) AS pairs
        FROM strategy_performance
        WHERE run_id != ?
        GROUP BY run_id, strategy_name
        ORDER BY MIN(entry_timestamp) DESC
        LIMIT ?
    """
    with sqlite._cursor() as cur:
        cur.execute(sql, (LIVE_RUN_ID, int(limit)))
        rows = cur.fetchall()

    out: List[Dict[str, Any]] = []
    for row in rows:
        # row is sqlite3.Row (dict-like)
        started_at = row["started_at"]
        completed_at = row["completed_at"]
        out.append({
            'id': row["run_id"],
            'tag': None,                         # not tracked on Free
            'strategy_name': row["strategy_name"],
            'pairs': row["pairs"],
            'start_date': (started_at or '')[:10] or None,
            'end_date': (completed_at or '')[:10] or None,
            'git_hash': None,                    # not tracked on Free
            'status': 'completed',               # derived rows are always done
            'started_at': started_at,
            'completed_at': completed_at,
            'total_trades': row["total_trades"],
            'win_rate': row["win_rate"],
            'profit_factor': None,               # would need separate wins/losses sums
            'net_pnl_pct': row["net_pnl_pct"],
        })
    return out


def _resolve_sqlite_provider(dp: Any) -> Any:
    """Walk a possibly-Composite provider to find the SqliteDataProvider.

    Free-tier DataProvider is a CompositeDataProvider(reads=exchange,
    writes=SqliteDataProvider). Replay-run results live on the writes side.
    """
    from spirit.utils.sqlite_data_provider import SqliteDataProvider

    if isinstance(dp, SqliteDataProvider):
        return dp
    # CompositeDataProvider exposes a `writes` attribute holding the
    # SqliteDataProvider on Free tier.
    writes = getattr(dp, "writes", None)
    if isinstance(writes, SqliteDataProvider):
        return writes
    return None


def delete_run(run_id: str) -> Dict[str, int]:
    """Delete all data for a specific run.

    Free tier: deletes from strategy_performance only (scorer_outcomes and
    risk_gate_decisions tables don't exist in the SQLite schema).
    Plus/Pro tier: deletes from all 3 results tables + replay_runs registry.

    Safety: refuses to delete run_id='live' to protect production data.

    Returns dict with deletion counts per table. Returns empty counts
    (all zeros) when the run id is not found — the caller (main.py)
    surfaces a clean "not found" message and exits 0.
    """
    if run_id == LIVE_RUN_ID:
        raise ValueError("Cannot delete live data via delete_run(). Use direct SQL if needed.")

    if _is_free_tier():
        return _delete_run_sqlite(run_id)
    return _delete_run_pg(run_id)


def _delete_run_pg(run_id: str) -> Dict[str, int]:
    from spirit.utils.db_connection import execute_query

    counts: Dict[str, int] = {}

    for table in ('strategy_performance', 'scorer_outcomes', 'risk_gate_decisions'):
        result = execute_query(
            f"DELETE FROM {table} WHERE run_id = %s",
            (run_id,), fetch='rowcount',
        )
        counts[table] = result or 0

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


def _delete_run_sqlite(run_id: str) -> Dict[str, int]:
    from spirit.utils.data_provider import get_data_provider

    dp = get_data_provider()
    sqlite = _resolve_sqlite_provider(dp)
    if sqlite is None:
        logger.warning(
            "[RUN] delete_run on Free tier could not resolve a SqliteDataProvider; "
            "nothing deleted. (DataProvider type: %s)",
            type(dp).__name__,
        )
        return {'strategy_performance': 0}

    deleted = sqlite.clear_performance(run_id=run_id)
    logger.info(f"[RUN] Deleted: id={run_id[:8]}... sp={deleted}")
    return {'strategy_performance': deleted}

"""
Strategy Performance Writer

Handles all writes to the strategy_performance table:
- create_table(): Run DDL from SQL file
- record_trade(): Single INSERT for paper/live trades
- record_trades_batch(): Bulk INSERT for backfill
- clear_backfill(): DELETE backtest rows for re-running

Uses utils.db_connection following existing codebase patterns.
"""

import os
from typing import Dict, List, Optional

def create_table() -> None:
    """Create strategy_performance table by executing the SQL file."""
    sql_path = os.path.join(
        os.path.dirname(__file__),
        '..', '..', '..', 'scripts', 'decision_engine', 'sql',
        'create_strategy_performance.sql'
    )
    sql_path = os.path.abspath(sql_path)

    with open(sql_path) as f:
        sql = f.read()

    from spirit.utils.data_provider import get_data_provider
    get_data_provider().ensure_table('strategy_performance', sql)
    print("strategy_performance table created successfully")


def record_trade(
    timestamp,
    entry_timestamp,
    pair: str,
    strategy_name: str,
    is_win: bool,
    pnl_pct: float,
    entry_price: float = None,
    exit_price: float = None,
    exit_reason: str = None,
    regime_at_entry: str = None,
    dlimit_trend_state: str = None,
    volatility_regime: str = None,
    source: str = 'paper',
    trade_id: int = None,
    order_type: str = None,
    limit_price: float = None,
    run_id: str = 'live',
    entry_context: dict = None,
    mfe_pct: float = None,
    mae_pct: float = None,
) -> int:
    """Insert a single trade result (for paper/live use).

    Returns:
        Number of rows inserted (0 if ON CONFLICT skipped, 1 if inserted).
    """
    from spirit.utils.data_provider import get_data_provider
    return get_data_provider().write_performance({
        'timestamp': timestamp,
        'entry_timestamp': entry_timestamp,
        'pair': pair,
        'strategy_name': strategy_name,
        'is_win': is_win,
        'pnl_pct': pnl_pct,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'exit_reason': exit_reason,
        'regime_at_entry': regime_at_entry,
        'dlimit_trend_state': dlimit_trend_state,
        'volatility_regime': volatility_regime,
        'source': source,
        'trade_id': trade_id,
        'order_type': order_type,
        'limit_price': limit_price,
        'run_id': run_id,
        'entry_context': entry_context,
        'mfe_pct': mfe_pct,
        'mae_pct': mae_pct,
    })


def record_trades_batch(trades: List[Dict], run_id: str = 'live') -> int:
    """
    Bulk insert trade results via execute_values (fastest method).

    Each dict in trades should have keys matching strategy_performance columns.
    Required keys: timestamp, entry_timestamp, pair, strategy_name, is_win, pnl_pct, source.

    Returns number of rows inserted.
    """
    if not trades:
        return 0

    from spirit.utils.data_provider import get_data_provider
    # Ensure each trade has run_id set
    for t in trades:
        t.setdefault('run_id', run_id)
        t.setdefault('source', 'backtest')
    return get_data_provider().write_performance_batch(trades)


def clear_backfill(strategy_name: Optional[str] = None) -> int:
    """
    Delete backfill rows for re-running.

    Args:
        strategy_name: If provided, only clear this strategy. Otherwise clear all backfill.

    Returns number of rows deleted.
    """
    from spirit.utils.db_connection import execute_query
    if strategy_name:
        count = execute_query(
            "SELECT COUNT(*) as cnt FROM strategy_performance WHERE source = 'backtest' AND strategy_name = %s",
            (strategy_name,), fetch='one',
        )
        execute_query(
            "DELETE FROM strategy_performance WHERE source = 'backtest' AND strategy_name = %s",
            (strategy_name,), fetch='none',
        )
    else:
        count = execute_query(
            "SELECT COUNT(*) as cnt FROM strategy_performance WHERE source = 'backtest'",
            fetch='one',
        )
        execute_query("DELETE FROM strategy_performance WHERE source = 'backtest'", fetch='none')

    deleted = count['cnt'] if count else 0
    print(f"Cleared {deleted} backfill rows" + (f" for {strategy_name}" if strategy_name else ""))
    return deleted

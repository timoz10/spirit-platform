"""CompositeDataProvider — Free-tier glue between reads and writes (#561 Day 4).

Free-tier Spirit splits FrameworkDataProvider across two delegates:

    reads  → ExchangeBackedDataProvider  (OHLC + pair registry)
    writes → SqliteDataProvider          (state, performance, heartbeats)

This Composite is the single object Spirit's consumers see via
`get_data_provider()`. It satisfies the full FrameworkDataProvider
Protocol by dispatching each method to the right delegate, and rejects
every IPDataProvider method with a clear upgrade message — Free tier
does not ship D-Limit, scorer, regime, or risk-gate logic.

Why a composite (not multiple inheritance, not a single hybrid class)?

  - Each backend has a focused responsibility and its own tests
    (`test_exchange_backed_data_provider.py`, `test_sqlite_*.py`).
  - Plus / Pro tiers continue to use the existing ApiDataProvider in
    one piece — no shared abstract base needed.
  - Swapping the read backend (Kraken today, Binance tomorrow) is a
    one-line constructor change, not a class hierarchy refactor.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from spirit.logger import get_logger

logger = get_logger("composite_data_provider")

# SPIRIT_OHLC_SOURCE routing values. Mirrors PaidTierComposite so the two tiers
# present identical get_ohlc semantics (ADR-0001 / Rule 12). Kept as a separate
# (deliberately duplicated) constant rather than imported to avoid coupling the
# Free path to the Paid module; the contract test pins both behave the same.
_VALID_OHLC_SOURCES = frozenset({"auto", "cloud_first", "local_first"})


_UPGRADE_MSG = (
    "This call requires Plus or Pro tier — Free tier does not include "
    "D-Limit zones, indicators, scorer, or risk-gate calibration. "
    "See https://tradebot.live/upgrade for tier details."
)


class CompositeDataProvider:
    """Dispatcher providing FrameworkDataProvider over two delegates.

    The `reads` delegate handles OHLC + pair registry; the `writes`
    delegate handles state, performance, and heartbeats. Strategy
    metrics aggregation lives on the writes side because it queries
    the local strategy_performance table.

    Raising NotImplementedError on every IP method is deliberate:
    silently returning empty lists would let an IP-dependent indicator
    run on no data and produce misleading output. A loud error at the
    boundary fails fast and points the user at the upgrade path.
    """

    def __init__(self, *, reads: Any, writes: Any) -> None:
        self._reads = reads
        self._writes = writes
        logger.info(
            "CompositeDataProvider initialised "
            f"(reads={type(reads).__name__}, writes={type(writes).__name__})"
        )

    # ==================================================================
    # FrameworkDataProvider — reads
    # ==================================================================

    def _resolve_ohlc_source(self) -> str:
        """Read SPIRIT_OHLC_SOURCE; unknown values fall back to 'auto'.

        Resolved per-call so an operator can flip the env var without a
        restart. Mirrors PaidTierComposite._resolve_source.
        """
        raw = (os.environ.get("SPIRIT_OHLC_SOURCE", "auto") or "auto").strip().lower()
        if raw not in _VALID_OHLC_SOURCES:
            logger.warning(
                f"SPIRIT_OHLC_SOURCE={raw!r} not in {sorted(_VALID_OHLC_SOURCES)}; "
                f"falling back to 'auto'"
            )
            return "auto"
        return raw

    def get_ohlc(self, pair, interval, *, start=None, end=None,
                 limit=5000, order="asc"):
        # SPIRIT_OHLC_SOURCE routing, mirroring PaidTierComposite for tier
        # parity (ADR-0001). Free's "store" is the local SQLite user_ohlc that
        # `backfill` / BYOD-import populates (self._writes.get_user_ohlc);
        # "live" is the exchange-backed reads delegate.
        #
        # auto: a bounded historical window (start set) reads the store —
        # that's where uploaded/backfilled history lives. An unbounded "latest"
        # read goes live, since the exchange is always current. This keeps the
        # default Free live/paper path (get_ohlc(limit=N), start=None)
        # unchanged: it still hits the live exchange.
        source = self._resolve_ohlc_source()
        if source == "auto":
            source = "cloud_first" if start is not None else "local_first"

        if source == "cloud_first":
            return self._writes.get_user_ohlc(
                pair, interval, start=start, end=end, limit=limit, order=order,
            )
        return self._reads.get_ohlc(
            pair, interval, start=start, end=end, limit=limit, order=order,
        )

    def get_pairs(self, instance=None):
        return self._reads.get_pairs(instance=instance)

    # ==================================================================
    # FrameworkDataProvider — writes (state, performance, heartbeats)
    # ==================================================================

    def get_state(self, key):
        return self._writes.get_state(key)

    def put_state(self, key, value):
        return self._writes.put_state(key, value)

    def ensure_table(self, table_name, create_sql):
        return self._writes.ensure_table(table_name, create_sql)

    def get_performance(self, *, pair=None, strategy=None, source=None,
                        run_id=None, start=None, end=None, limit=5000):
        return self._writes.get_performance(
            pair=pair, strategy=strategy, source=source,
            run_id=run_id, start=start, end=end, limit=limit,
        )

    def write_performance(self, data):
        return self._writes.write_performance(data)

    def write_performance_batch(self, trades):
        return self._writes.write_performance_batch(trades)

    def clear_performance(self, *, run_id):
        return self._writes.clear_performance(run_id=run_id)

    def get_strategy_metrics(self, strategy_name, as_of_date, *,
                             baseline_days=90, current_days=14, recent_days=7):
        # SqliteDataProvider may not implement this yet — surface the
        # gap clearly rather than crash with AttributeError.
        if not hasattr(self._writes, "get_strategy_metrics"):
            raise NotImplementedError(
                "SqliteDataProvider does not implement get_strategy_metrics. "
                "This is on the Day-5 backlog for v2.3.0."
            )
        return self._writes.get_strategy_metrics(
            strategy_name, as_of_date,
            baseline_days=baseline_days,
            current_days=current_days,
            recent_days=recent_days,
        )

    def write_heartbeat(self, daemon_id, *, instance, status="ok",
                        metadata=None, run_id="live"):
        return self._writes.write_heartbeat(
            daemon_id, instance=instance, status=status,
            metadata=metadata, run_id=run_id,
        )

    # ==================================================================
    # FrameworkDataProvider — BYOD OHLC + runs (v2.2.4)
    # ==================================================================
    # All five route to the writes delegate (SqliteDataProvider on Free).
    # OHLC reads STILL go via `get_ohlc` on the reads delegate; these
    # are user-owned OHLC writes/reads that live in local SQLite.

    def upload_user_ohlc(self, pair, interval, candles):
        return self._writes.upload_user_ohlc(pair, interval, candles)

    def append_user_ohlc(self, pair, interval, candles):
        return self._writes.append_user_ohlc(pair, interval, candles)

    def get_user_ohlc(self, pair, interval, *, start=None, end=None,
                      limit=5000, order="asc"):
        return self._writes.get_user_ohlc(
            pair, interval, start=start, end=end, limit=limit, order=order,
        )

    def list_runs(self, *, limit=30):
        return self._writes.list_runs(limit=limit)

    def delete_run(self, run_id):
        return self._writes.delete_run(run_id)

    # ==================================================================
    # IPDataProvider — every method raises with the upgrade message
    # ==================================================================

    # D-Limit zones / touches / bounces -------------------------------

    def get_zones(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_zone_touches(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_bounce_events(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_bounce_references(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    # D-Limit indicators / consolidation -------------------------------

    def get_dlimit(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_dlimit_latest(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_consolidation(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    # Orderbook --------------------------------------------------------

    def get_orderbook(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_orderbook_events_summary(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    # Risk gate + shadow ----------------------------------------------

    def write_risk_gate(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def update_risk_gate_outcome(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_pending_shadow(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def update_shadow_outcome(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    # Theses + scorer + regime ----------------------------------------

    def write_thesis(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def update_thesis_outcome(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def update_thesis_checks(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_thesis_calibration_data(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def write_scorer_outcome(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def write_regime_decision(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    # Tumbler scenes + trajectories ------------------------------------

    def write_scene(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_trajectory_templates(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_trajectory_modifiers(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def write_trajectory(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    # Calibration inputs (#338) ---------------------------------------

    def get_cooldown_calibration(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_risk_gate_calibration(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_entry_quality_calibration(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_bounce_signature_norm_stats(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_volatility_context(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

    def get_composite_outcomes(self, *args, **kwargs):
        raise NotImplementedError(_UPGRADE_MSG)

"""
DataProvider — Abstract data access for Spirit runtime.

Spirit is api-driven: all data access goes through the API gateway.
pg-mode was removed in the platform pivot (#340).

Framework / IP boundary (#340):
  - FrameworkDataProvider: data any Spirit instance needs (OHLC, state,
    performance, heartbeats, pairs). Used by both free-tier and
    paid-tier Spirit.
  - IPDataProvider: data produced by our IP pipeline (D-Limit zones,
    bounces, regime-derived indicators, risk gate, theses, scorer,
    tumblers, trajectories). Available only to paid-tier Spirit.
  - DataProvider: union of both, satisfied by the paid-tier
    ApiDataProvider. Free-tier Spirit will implement FrameworkDataProvider
    only (backed by local SQLite — future work).

Usage:
    from spirit.utils.data_provider import get_data_provider
    dp = get_data_provider()
    rows = dp.get_ohlc(pair='XBTUSD', interval=60, limit=100)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from spirit.logger import get_logger

logger = get_logger("data_provider")


# =====================================================================
# Framework interface — data any Spirit instance needs
# =====================================================================


@runtime_checkable
class FrameworkDataProvider(Protocol):
    """Framework data access: OHLC, state, performance, heartbeats, pairs.

    Any Spirit instance — free-tier or paid — uses this interface.
    Free-tier implementations back it with local SQLite; paid-tier
    backs it with the API gateway.
    """

    # -----------------------------------------------------------------
    # OHLC
    # -----------------------------------------------------------------

    def get_ohlc(
        self,
        pair: str,
        interval: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
        order: str = "asc",
    ) -> list[dict]:
        """Fetch OHLC candles. Returns list of dicts with keys:
        pair, interval, datetime, open, high, low, close, vwap, volume, count.
        """
        ...

    # -----------------------------------------------------------------
    # BYOD OHLC — customer-owned OHLC store (v2.2.4)
    # -----------------------------------------------------------------
    # On Free tier these route to local SQLite. On Paid tier they route
    # through the gateway to per-instance cloud storage. Callers stay
    # tier-agnostic — same Protocol surface either way. Contracts pinned
    # in docs/reference/MODULE_CONTRACTS.md.

    def upload_user_ohlc(
        self,
        pair: str,
        interval: int,
        candles: list[dict],
    ) -> dict:
        """Bulk-seed user-owned OHLC (CSV import path).

        Idempotent: re-uploading the same (pair, interval, timestamp)
        rows is silently deduped. Returns
        ``{"batch_id": str, "rows_inserted": int, "rows_skipped": int}``.

        Raises ValueError on malformed candle dicts (missing OHLC keys).
        Never raises on dedupe.
        """
        ...

    def get_user_ohlc(
        self,
        pair: str,
        interval: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
        order: str = "asc",
    ) -> list[dict]:
        """Read back user-owned OHLC. Same row shape as ``get_ohlc``.

        Half-open window: ``[start, end)``. ``order='desc'`` with
        ``limit=1`` returns the most recent local candle (used by the
        boot-time catch-up runner).
        """
        ...

    def append_user_ohlc(
        self,
        pair: str,
        interval: int,
        candles: list[dict],
    ) -> dict:
        """Incremental forward-tick append of user-owned OHLC.

        Same row shape and dedupe semantics as ``upload_user_ohlc``.
        Distinct call site so observability can tell bulk-seed (CSV)
        apart from incremental (live + catch-up) writes.
        """
        ...

    # -----------------------------------------------------------------
    # State (spirit_state key-value store) — crash recovery
    # -----------------------------------------------------------------

    def get_state(self, key: str) -> Any:
        """Fetch a single value from spirit_state by exact key."""
        ...

    def put_state(self, key: str, value: Any) -> None:
        """Upsert a key-value pair into spirit_state."""
        ...

    def ensure_table(self, table_name: str, create_sql: str) -> bool:
        """Ensure a table exists. API-backed: no-op (True)."""
        ...

    # -----------------------------------------------------------------
    # Performance — the user's own trade outcomes
    # -----------------------------------------------------------------

    def get_performance(
        self,
        *,
        pair: str | None = None,
        strategy: str | None = None,
        source: str | None = None,
        run_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch strategy_performance rows."""
        ...

    def write_performance(self, data: dict) -> int:
        """Insert a single trade result. Returns rows affected."""
        ...

    def write_performance_batch(self, trades: list[dict]) -> int:
        """Bulk insert trade results. Returns rows affected."""
        ...

    def clear_performance(self, *, run_id: str) -> int:
        """Delete performance rows for a specific run_id. Returns rows deleted."""
        ...

    def get_strategy_metrics(
        self,
        strategy_name: str,
        as_of_date: datetime,
        *,
        baseline_days: int = 90,
        current_days: int = 14,
        recent_days: int = 7,
    ) -> dict:
        """Per-strategy win-rate + streak metrics from strategy_performance.

        Pure trade-outcome aggregation — framework data (users own their
        trades). Consumed by IP regime engine but the aggregation itself
        is not IP-derived.
        """
        ...

    # -----------------------------------------------------------------
    # Run management — list and delete replay/live runs (v2.2.4)
    # -----------------------------------------------------------------
    # Lifted from run_manager._is_free_tier() branches per the
    # load-bearing principle that DataProvider is the only tier switch.
    # Closes #779 properly (uniform call site, no attribute-name fragility).

    def list_runs(self, *, limit: int = 30) -> list[dict]:
        """List replay runs (excludes ``run_id='live'``). Most recent first.

        Returns dicts with keys: ``id, tag, strategy_name, pairs,
        start_date, end_date, git_hash, status, started_at,
        completed_at, total_trades, win_rate, profit_factor,
        net_pnl_pct``.

        On Free, paid-only fields (``tag``, ``git_hash``,
        ``profit_factor``) come back as ``None``. main.py renders
        ``None`` as ``-`` so the display layer is unchanged.
        """
        ...

    def delete_run(self, run_id: str) -> dict:
        """Delete all rows for a given run_id across performance tables.

        Refuses ``run_id='live'``. Returns per-table delete counts:
        ``{"strategy_performance": int, "scorer_outcomes": int,
        "risk_gate_decisions": int, "replay_runs": int}``. Free tier
        returns zero for tables that don't exist locally. Nonexistent
        run_id returns all zeros without erroring.
        """
        ...

    # -----------------------------------------------------------------
    # Heartbeats — daemon health
    # -----------------------------------------------------------------

    def write_heartbeat(
        self,
        daemon_id: str,
        *,
        instance: str,
        status: str = "ok",
        metadata: dict | None = None,
        run_id: str = "live",
    ) -> int:
        """Upsert a daemon heartbeat. Returns rows affected."""
        ...

    # -----------------------------------------------------------------
    # Pairs — which symbols are active for this instance
    # -----------------------------------------------------------------

    def get_pairs(self, instance: str | None = None) -> list[dict]:
        """Fetch active pairs from pair registry."""
        ...


# =====================================================================
# IP interface — data produced by our IP pipeline
# =====================================================================


@runtime_checkable
class IPDataProvider(Protocol):
    """IP data access: zones, bounces, D-Limit, regime, calibrations.

    Subscription-tier only. Free-tier Spirit does not consume this
    interface — users bring their own algorithms and data feeds.
    """

    # -----------------------------------------------------------------
    # D-Limit zones + touches + bounces
    # -----------------------------------------------------------------

    def get_zones(
        self,
        pair: str,
        interval: int = 60,
        *,
        active: bool | None = None,
        min_strength: float | None = None,
    ) -> list[dict]:
        """Fetch D-Limit zones."""
        ...

    def get_zone_touches(
        self,
        pair: str,
        *,
        zone_ids: list[int] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        result_filter: str | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch zone touch events."""
        ...

    def get_bounce_events(
        self,
        pair: str,
        interval: int,
        *,
        min_prior_touches: int = 2,
        dedup_hours: int = 0,
        start: datetime | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """Deduped bounce events with per-zone LAG window function."""
        ...

    def get_bounce_references(
        self,
        *,
        pair: str | None = None,
        regime: str | None = None,
        min_dt: datetime | None = None,
    ) -> list[dict]:
        """Rows from bounce_reference with optional filters."""
        ...

    # -----------------------------------------------------------------
    # D-Limit indicators + consolidation
    # -----------------------------------------------------------------

    def get_dlimit(
        self,
        pair: str,
        interval: int = 60,
        *,
        at: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch D-Limit indicator values from year-partitioned tables."""
        ...

    def get_dlimit_latest(
        self,
        pair: str,
        interval: int = 60,
        *,
        before: datetime | None = None,
    ) -> dict | None:
        """Fetch most recent D-Limit row."""
        ...

    def get_consolidation(
        self,
        pair: str,
        interval: int = 60,
        *,
        at: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Fetch Module 7 consolidation signals."""
        ...

    # -----------------------------------------------------------------
    # Orderbook indicators
    # -----------------------------------------------------------------

    def get_orderbook(
        self,
        pair: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Fetch orderbook depth metrics."""
        ...

    def get_orderbook_events_summary(
        self,
        pair: str,
        *,
        lookback_minutes: int = 15,
        at: datetime | None = None,
    ) -> list[dict]:
        """Grouped orderbook_events counts over the lookback window."""
        ...

    # -----------------------------------------------------------------
    # Risk gate + shadow outcomes (IP because risk gate is calibrated
    # against IP regime/R:R distributions)
    # -----------------------------------------------------------------

    def write_risk_gate(self, data: dict) -> int:
        """Insert a risk gate decision."""
        ...

    def update_risk_gate_outcome(self, data: dict) -> int:
        """Update outcome on an existing risk gate decision."""
        ...

    def get_pending_shadow(self, *, limit: int = 100) -> list[dict]:
        """Fetch risk gate decisions needing shadow outcome evaluation."""
        ...

    def update_shadow_outcome(self, data: dict) -> int:
        """Update shadow outcome fields on a risk gate decision."""
        ...

    # -----------------------------------------------------------------
    # Trade theses + scorer outcomes (IP lifecycle audit)
    # -----------------------------------------------------------------

    def write_thesis(self, data: dict) -> int:
        """Insert a trade thesis."""
        ...

    def update_thesis_outcome(self, data: dict) -> int:
        """Update exit/outcome fields on a thesis."""
        ...

    def update_thesis_checks(self, data: dict) -> int:
        """Update in-trade thesis_checks JSONB on an open thesis (#524)."""
        ...

    def get_thesis_calibration_data(self, limit: int = 2000) -> list[dict]:
        """Recent completed theses for ThesisCalibrator buffer rebuild (#527).

        Returns rows with: pair, thesis_type, thesis_validated, actual_pnl_pct,
        actual_mfe_pct, expected_mfe_median.
        """
        ...

    def write_scorer_outcome(self, data: dict) -> int:
        """Insert a scorer outcome record."""
        ...

    def write_regime_decision(self, data: dict) -> int:
        """Insert a regime classification audit row (#526)."""
        ...

    # -----------------------------------------------------------------
    # Tumbler scenes + trajectories (IP observation)
    # -----------------------------------------------------------------

    def write_scene(self, data: dict) -> int:
        """Upsert a tumbler scene."""
        ...

    def get_trajectory_templates(self, pair: str) -> list[dict]:
        """Fetch trajectory health templates for a pair."""
        ...

    def get_trajectory_modifiers(self, pair: str) -> list[dict]:
        """Fetch trajectory zone modifiers for a pair."""
        ...

    def write_trajectory(self, data: dict) -> int:
        """Insert a trade trajectory record."""
        ...

    # -----------------------------------------------------------------
    # Calibration inputs (#338)
    # -----------------------------------------------------------------

    def get_cooldown_calibration(
        self,
        pair: str,
        *,
        interval: int = 60,
        lookback_months: int = 12,
    ) -> list[dict]:
        """Break → next-bounce recovery events for CooldownCalibrator.

        Returns rows with keys: pair, break_time, break_price, price_level,
        recovery_hours, depth_pct, capture_rate.
        """
        ...

    def get_risk_gate_calibration(
        self,
        pair: str,
        *,
        calibrate_before: datetime | None = None,
    ) -> list[dict]:
        """Resolved risk-gate decisions for RiskGateCalibrator.

        Returns rows with keys: regime, rr_ratio, approved, outcome_is_win,
        shadow_hit_target, shadow_hit_stop.
        """
        ...

    def get_entry_quality_calibration(
        self,
        dimension: str,
        *,
        as_of: datetime | None = None,
    ) -> list[dict]:
        """Bucketed win-rate + MFE rows for EntryQualityCalibrator.

        `dimension` must be one of: slope_angle, zone_touches,
        touch_gap_hours, displacement_24h, trend_end_confidence.

        Returns rows with keys: regime, bucket_idx, count, win_rate, avg_mfe.
        """
        ...

    def get_bounce_signature_norm_stats(self) -> list[dict]:
        """Percentile norm stats over bounce_reference for SignatureExtractor.

        Returns one row per (pair, regime, dim) with keys:
        pair, regime, dim, count, min, max, p25, p50, p75.
        Client applies MIN_SAMPLES_PER_DIM threshold and derives
        `stats_source` provenance.
        """
        ...

    def get_volatility_context(
        self,
        pair: str,
        interval: int,
        *,
        as_of: datetime | None = None,
    ) -> dict | None:
        """ATR aggregates for DynamicRegimeEngine.get_volatility_context.

        Returns a dict with keys atr_14d, atr_30d, atr_90d — or None if no
        rows were available in the 90-day window. Client applies regime
        classification (`low`/`normal`/`high`/`extreme`).
        """
        ...

    def get_composite_outcomes(
        self,
        *,
        calibrate_before: datetime | None = None,
    ) -> list[dict]:
        """Per-trade outcome rows for CompositeThresholdCalibrator.

        Returns rows with keys: pair, regime, composite_score, is_win,
        mfe_pct. Client runs the per-regime threshold sweep.
        """
        ...


# =====================================================================
# Combined interface — paid tier implements both
# =====================================================================


@runtime_checkable
class DataProvider(FrameworkDataProvider, IPDataProvider, Protocol):
    """Combined framework + IP data access.

    Satisfied by the paid-tier ApiDataProvider. Free-tier Spirit
    will only depend on FrameworkDataProvider; code that uses this union
    type is implicitly paid-tier-only.
    """
    ...


# =====================================================================
# Singleton factory
# =====================================================================


_provider: DataProvider | None = None


def get_data_provider() -> DataProvider:
    """Return the singleton DataProvider instance.

    Routing is driven by `SPIRIT_TIER`:
      - `free`              → CompositeDataProvider over an
                              ExchangeBackedDataProvider (Kraken public REST
                              today) + a SqliteDataProvider keyed under
                              `~/.spirit/<instance>/spirit.db`. No API key
                              required, no gateway calls.
      - `plus`/`pro`/unset → ApiDataProvider against the gateway,
                             same path as before.
    """
    global _provider
    if _provider is not None:
        return _provider

    from spirit.utils.config_loader import get_config

    tier = (get_config("SPIRIT_TIER", "") or "").strip().lower()
    if not tier:
        import os
        tier = os.environ.get("SPIRIT_TIER", "").strip().lower()

    if tier == "free":
        _provider = _build_free_tier_provider()
        return _provider

    from spirit.utils.api_data_provider import ApiDataProvider

    base_url = get_config("SPIRIT_API_URL", "https://api.tradebot.live/v1")
    api_key = get_config("SPIRIT_API_KEY", "")
    if not api_key:
        import os
        api_key = os.environ.get("SPIRIT_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "SPIRIT_API_KEY must be set (env var or spirit.yaml). "
            "Run `python3 -m spirit.setup` to configure, or set "
            "SPIRIT_TIER=free to run the local Free-tier stack."
        )
    gateway = ApiDataProvider(base_url=base_url, api_key=api_key)

    # BYOD branch (#666): Plus/Pro keys lost `read:ohlc` in #665. When
    # preflight has populated capabilities AND they exclude `read:ohlc`,
    # wrap the gateway in a PaidTierComposite that routes OHLC reads to
    # a local Kraken-backed ExchangeBackedDataProvider. internal_canary +
    # admin still carry `read:ohlc` and skip this branch — they keep the
    # direct ApiDataProvider. Capabilities empty (no preflight: replay,
    # tests) also keeps the direct path to avoid regression.
    from spirit.utils.preflight import get_session_capabilities
    caps = get_session_capabilities()
    if caps and "read:ohlc" not in caps:
        _provider = _wrap_paid_tier_byod(gateway)
        logger.info(
            f"DataProvider: paid-tier BYOD → gateway={base_url} "
            f"+ local exchange (no read:ohlc capability)"
        )
    else:
        _provider = gateway
        logger.info(f"DataProvider: api → {base_url}")

    trace_path = get_config("SPIRIT_DATA_PROVIDER_TRACE", "")
    if not trace_path:
        import os as _os
        trace_path = _os.environ.get("SPIRIT_DATA_PROVIDER_TRACE", "")
    if trace_path:
        from spirit.utils.coverage_recorder import CountingDataProvider

        _provider = CountingDataProvider(_provider)
        logger.info(f"DataProvider: counting wrapper enabled → {trace_path}")

    return _provider


def reset_data_provider() -> None:
    """Reset the singleton (for testing only)."""
    global _provider
    _provider = None


def _build_free_tier_provider():
    """Construct a CompositeDataProvider for the Free tier.

    Wired separately so testers can patch the exchange/sqlite delegates
    without re-implementing the env-var resolution.

    Resolution must go through `get_config()`, not `os.environ` directly
    — `get_config` checks env first, then `config/spirit.yaml` (which is
    where the setup wizard writes), then the default. Using `os.environ`
    here silently ignores yaml-written values, so a wizard-set
    `SPIRIT_INSTANCE: test-vm` would land at the wrong SQLite path.

      SPIRIT_INSTANCE          → instance name (default: 'local')
      SPIRIT_SQLITE_PATH       → explicit DB path override
                                 (default: ~/.spirit/<instance>/spirit.db)
      SPIRIT_FREE_EXCHANGE     → exchange name override
                                 (default: 'kraken' — the only one shipped)
    """
    from pathlib import Path

    from spirit.utils.composite_data_provider import CompositeDataProvider
    from spirit.utils.config_loader import get_config
    from spirit.utils.exchange_backed_data_provider import (
        ExchangeBackedDataProvider,
    )
    from spirit.utils.sqlite_data_provider import SqliteDataProvider

    instance = (get_config("SPIRIT_INSTANCE", "local") or "local").strip() or "local"
    sqlite_path = (get_config("SPIRIT_SQLITE_PATH", "") or "").strip()
    if not sqlite_path:
        sqlite_path = str(
            Path("~/.spirit").expanduser() / instance / "spirit.db"
        )

    exchange_name = (
        (get_config("SPIRIT_FREE_EXCHANGE", "kraken") or "kraken")
        .strip().lower() or "kraken"
    )
    if exchange_name == "kraken":
        from spirit.exchange.kraken import KrakenExchangeProvider
        exchange = KrakenExchangeProvider()
    else:
        raise RuntimeError(
            f"SPIRIT_FREE_EXCHANGE={exchange_name!r} is not bundled with "
            "v2.3.0. Only 'kraken' ships at launch; add a plugin under "
            "src/spirit/exchange/ and re-route here."
        )

    reads = ExchangeBackedDataProvider(exchange)
    writes = SqliteDataProvider(sqlite_path)
    logger.info(
        f"DataProvider: free → exchange={exchange.name} "
        f"sqlite={sqlite_path} instance={instance}"
    )
    return CompositeDataProvider(reads=reads, writes=writes)


def _wrap_paid_tier_byod(gateway):
    """Build a PaidTierComposite for Plus/Pro keys missing read:ohlc (#666).

    The OHLC source is an ExchangeBackedDataProvider over the configured
    exchange (Kraken at v2.2.x), with an opportunistic push-on-fetch hook
    wired to the gateway's POST /v1/ohlc/append endpoint. Everything else
    continues to route to the gateway. Kept in its own helper so the
    factory branch stays readable and tests can patch it without
    monkey-patching env vars.
    """
    import os

    from spirit.utils.exchange_backed_data_provider import (
        ExchangeBackedDataProvider,
    )
    from spirit.utils.paid_tier_composite import PaidTierComposite

    exchange_name = (
        os.environ.get("SPIRIT_EXCHANGE", "kraken").strip().lower()
        or "kraken"
    )
    if exchange_name == "kraken":
        from spirit.exchange.kraken import KrakenExchangeProvider
        exchange = KrakenExchangeProvider()
    else:
        raise RuntimeError(
            f"SPIRIT_EXCHANGE={exchange_name!r} is not bundled with "
            "v2.2.x. Only 'kraken' ships at launch; add a plugin under "
            "src/spirit/exchange/ and re-route here."
        )

    # Push-on-fetch closure: captures the gateway client so every
    # successful local Kraken fetch lands in the user's scoped cloud
    # store as a best-effort side effect. The ExchangeBackedDataProvider
    # catches exceptions raised here, so a gateway outage degrades to
    # local-only reads with WARN logs instead of failing the trade loop.
    def _push_to_cloud(pair: str, interval: int, candles: list) -> None:
        gateway.push_user_ohlc(pair, interval, candles)

    ohlc_source = ExchangeBackedDataProvider(
        exchange, on_fetch_callback=_push_to_cloud,
    )
    return PaidTierComposite(ohlc_source=ohlc_source, gateway=gateway)

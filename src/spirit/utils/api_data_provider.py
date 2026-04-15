"""
ApiDataProvider — HTTP-based implementation of DataProvider.

Calls the Spirit API gateway (api.tradebot.live) for all data access.
Used when SPIRIT_DATA_PROVIDER=api — Spirit instances with just an API key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import requests

from spirit.logger import get_logger

logger = get_logger("api_data_provider")


def _iso(dt: datetime | None) -> str | None:
    """Convert datetime to ISO string for query params."""
    return dt.isoformat() if dt else None


def _looks_like_iso_datetime(s: str) -> bool:
    # YYYY-MM-DD... length 10 minimum, 'T' or space separator at position 10.
    return len(s) >= 19 and s[4] == "-" and s[7] == "-" and s[10] in ("T", " ")


def _parse_value(val):
    """Convert API response value to match psycopg2 RealDictCursor output.

    - ISO timestamp strings → tz-aware datetime (matches PG TIMESTAMPTZ)
    - Numeric strings → float (matches PG NUMERIC cast)
    - Everything else unchanged
    """
    if not isinstance(val, str):
        return val

    if _looks_like_iso_datetime(val):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            pass

    try:
        return float(val)
    except (ValueError, TypeError):
        return val


def _normalise_row(row: dict) -> dict:
    """Match PG row shape: parse ISO timestamps → datetime, numerics → float."""
    return {k: _parse_value(v) for k, v in row.items()}


class ApiDataProvider:
    """DataProvider backed by HTTP calls to the Spirit API gateway."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self._url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["X-API-Key"] = api_key
        self._timeout = timeout
        logger.info(f"ApiDataProvider connected to {self._url}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> list[dict]:
        """GET request, return data list."""
        resp = self._session.get(
            f"{self._url}{path}",
            params={k: v for k, v in (params or {}).items() if v is not None},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        return [_normalise_row(r) for r in body.get("data", [])]

    def _get_one(self, path: str, params: dict | None = None) -> dict | None:
        """GET request, return first row or None."""
        rows = self._get(path, params)
        return rows[0] if rows else None

    def _post(self, path: str, data: dict) -> int:
        """POST request, return rows_affected."""
        resp = self._session.post(
            f"{self._url}{path}", json=data, timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json().get("rows_affected", 0)

    def _put(self, path: str, data: dict) -> int:
        """PUT request, return rows_affected."""
        resp = self._session.put(
            f"{self._url}{path}", json=data, timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json().get("rows_affected", 0)

    # ==================================================================
    # OHLC
    # ==================================================================

    def get_ohlc(self, pair, interval, *, start=None, end=None, limit=5000, order="asc"):
        return self._get("/ohlc", {
            "pair": pair, "interval": interval, "limit": limit, "order": order,
            "start": _iso(start), "end": _iso(end),
        })

    # ==================================================================
    # Zones
    # ==================================================================

    def get_zones(self, pair, interval=60, *, active=None, min_strength=None):
        return self._get("/zones", {
            "pair": pair, "active": active, "min_strength": min_strength,
        })

    def get_zone_touches(self, pair, *, zone_ids=None, start=None, end=None,
                         result_filter=None, limit=5000):
        params = {"pair": pair, "limit": limit, "start": _iso(start), "end": _iso(end)}
        if zone_ids:
            params["zone_ids"] = ",".join(str(z) for z in zone_ids)
        return self._get("/zone-touches", params)

    def get_bounce_events(self, pair, interval, *, min_prior_touches=2,
                          dedup_hours=0, start=None, limit=10000):
        params = {
            "pair": pair, "interval": interval,
            "min_prior_touches": min_prior_touches,
            "dedup_hours": dedup_hours,
            "limit": limit,
            "start": _iso(start),
        }
        return self._get("/bounce-events", params)

    # ==================================================================
    # D-Limit Indicators
    # ==================================================================

    def get_dlimit(self, pair, interval=60, *, at=None, start=None, end=None, limit=5000):
        params = {"pair": pair, "interval": interval, "limit": limit}
        if at:
            params["datetime"] = _iso(at)
        else:
            params["start"] = _iso(start)
            params["end"] = _iso(end)
        return self._get("/dlimit", params)

    def get_dlimit_latest(self, pair, interval=60, *, before=None):
        return self._get_one("/dlimit", {
            "pair": pair, "interval": interval, "limit": 1,
            "end": _iso(before),
        })

    # ==================================================================
    # Consolidation
    # ==================================================================

    def get_consolidation(self, pair, interval=60, *, at=None, start=None, end=None, limit=5000):
        params = {"pair": pair, "interval": interval, "limit": limit}
        if at:
            params["datetime"] = _iso(at)
        else:
            params["start"] = _iso(start)
            params["end"] = _iso(end)
        return self._get("/consolidation", params)

    # ==================================================================
    # State
    # ==================================================================

    def get_state(self, key):
        rows = self._get("/state", {"prefix": key})
        for r in rows:
            if r.get("key") == key:
                return r.get("value")
        return None

    def put_state(self, key, value):
        self._put("/state", {"key": key, "value": value})

    def ensure_table(self, table_name, create_sql):
        # Tables are managed server-side; always return True
        return True

    # ==================================================================
    # Performance
    # ==================================================================

    def get_performance(self, *, pair=None, strategy=None, source=None,
                        run_id=None, start=None, end=None, limit=5000):
        return self._get("/performance", {
            "pair": pair, "strategy": strategy, "source": source,
            "run_id": run_id, "limit": limit,
        })

    def write_performance(self, data):
        return self._post("/performance", data)

    def write_performance_batch(self, trades):
        total = 0
        for t in trades:
            total += self._post("/performance", t)
        return total

    def clear_performance(self, *, run_id):
        # Not directly supported via API — would need a new endpoint
        logger.warning(f"clear_performance not supported in API mode (run_id={run_id})")
        return 0

    # ==================================================================
    # Risk Gate Decisions
    # ==================================================================

    def write_risk_gate(self, data):
        return self._post("/risk-gate", data)

    def update_risk_gate_outcome(self, data):
        return self._put("/risk-gate", data)

    def get_pending_shadow(self, *, limit=100):
        # Pending shadow query not exposed via API yet
        logger.debug("get_pending_shadow not available in API mode")
        return []

    def update_shadow_outcome(self, data):
        # Shadow outcome update not exposed via API yet
        logger.debug("update_shadow_outcome not available in API mode")
        return 0

    # ==================================================================
    # Theses
    # ==================================================================

    def write_thesis(self, data):
        return self._post("/theses", data)

    def update_thesis_outcome(self, data):
        return self._put("/theses", data)

    # ==================================================================
    # Scorer Outcomes
    # ==================================================================

    def write_scorer_outcome(self, data):
        return self._post("/scorer", data)

    # ==================================================================
    # Heartbeats
    # ==================================================================

    def write_heartbeat(self, daemon_id, *, instance, status="ok", metadata=None, run_id="live"):
        if run_id != "live":
            return 0
        # Gateway authoritatively overrides instance from the API key; we
        # send it in the body for protocol parity with PgDataProvider.
        return self._post("/heartbeats", {
            "daemon_id": daemon_id, "status": status, "metadata": metadata,
            "instance": instance,
        })

    # ==================================================================
    # Scenes
    # ==================================================================

    def write_scene(self, data):
        return self._post("/scenes", data)

    # ==================================================================
    # Trajectories
    # ==================================================================

    def get_trajectory_templates(self, pair):
        return self._get("/trajectory-templates", {"pair": pair})

    def get_trajectory_modifiers(self, pair):
        return self._get("/trajectory-modifiers", {"pair": pair})

    def write_trajectory(self, data):
        return self._post("/trajectories", data)

    # ==================================================================
    # Pairs
    # ==================================================================

    def get_pairs(self, instance=None):
        return self._get("/pairs", {"instance": instance})

    # ==================================================================
    # Orderbook
    # ==================================================================

    def get_orderbook(self, pair, *, start=None, end=None, limit=100):
        return self._get("/orderbook", {
            "pair": pair, "start": _iso(start), "end": _iso(end), "limit": limit,
        })

    def get_orderbook_events_summary(self, pair, *, lookback_minutes=15, at=None):
        return self._get("/orderbook-events-summary", {
            "pair": pair,
            "lookback_minutes": lookback_minutes,
            "at": _iso(at),
        })

    def get_strategy_metrics(self, strategy_name, as_of_date, *,
                              baseline_days=90, current_days=14, recent_days=7):
        rows = self._get("/strategy-metrics", {
            "strategy_name": strategy_name,
            "as_of": _iso(as_of_date),
            "baseline_days": baseline_days,
            "current_days": current_days,
            "recent_days": recent_days,
        })
        return rows[0] if rows else {
            "trades_14d": 0, "current_wr": 0.5, "trades_90d": 0,
            "baseline_wr": 0.5, "recent_wr": 0.5,
            "consecutive_losses": 0, "consecutive_wins": 0,
        }

    def get_bounce_references(self, *, pair=None, regime=None, min_dt=None):
        return self._get("/bounce-references", {
            "pair": pair,
            "regime": regime,
            "min_dt": _iso(min_dt),
        })

"""
ApiDataProvider — HTTP-based implementation of DataProvider.

Calls the Spirit API gateway (api.tradebot.live) for all data access.
Used when SPIRIT_DATA_PROVIDER=api — Spirit instances with just an API key.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from spirit.logger import get_logger

logger = get_logger("api_data_provider")


# Capability-denial 403 looks like:
#   {"detail": "Capability 'read:ohlc' required"}
# emitted by spirit.api.auth.require_capability. We parse the capability name
# out so callers can branch on which capability is missing without string
# matching on the message.
_CAP_DENIED_PATTERN = re.compile(r"Capability '([^']+)' required")


class CapabilityDeniedError(requests.HTTPError):
    """The gateway rejected the request because the API key lacks a capability.

    Distinguishes "policy-denied at the auth layer" from generic 4xx errors.
    Carries `.capability` so callers can decide whether to fall back to a
    different data path (e.g. local OHLC when `read:ohlc` is denied) rather
    than treating every 403 as a hard failure.

    Subclasses `requests.HTTPError`, so existing broad `except HTTPError`
    handlers continue to work — callers opt in to the new behaviour by
    catching `CapabilityDeniedError` specifically.
    """

    def __init__(self, capability: str, response: requests.Response):
        self.capability = capability
        msg = (
            f"Gateway denied request: capability '{capability}' not granted to this key. "
            f"Check `/v1/whoami` for the capabilities your tier holds, or upgrade "
            f"at portal.tradebot.live."
        )
        super().__init__(msg, response=response)


def _raise_for_capability_denial(resp: requests.Response) -> None:
    """Convert a capability-denial 403 into a typed CapabilityDeniedError.

    Runs before `raise_for_status()` so the specific exception wins over
    the generic HTTPError. No-op for any other status or 403 shape.
    """
    if resp.status_code != 403:
        return
    try:
        detail = resp.json().get("detail", "")
    except (ValueError, TypeError):
        return
    if not isinstance(detail, str):
        return
    match = _CAP_DENIED_PATTERN.search(detail)
    if match:
        raise CapabilityDeniedError(match.group(1), resp)


def _iso(dt: datetime | None) -> str | None:
    """Convert datetime to ISO string for query params."""
    return dt.isoformat() if dt else None


def _json_default(obj: Any) -> Any:
    # requests' default json= encoder can't handle datetime/date. Writes
    # like record_trade() pass tz-aware datetimes directly; the gateway
    # parses ISO strings back on the other side (Rule 11 round-trip).
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _looks_like_iso_datetime(s: str) -> bool:
    # YYYY-MM-DD... length 10 minimum, 'T' or space separator at position 10.
    return len(s) >= 19 and s[4] == "-" and s[7] == "-" and s[10] in ("T", " ")


def _parse_value(val):
    """Convert API response value to match psycopg2 RealDictCursor output.

    - ISO timestamp strings → tz-aware datetime in **UTC** (Rule 11 contract)
    - Numeric strings → float (matches PG NUMERIC cast)
    - Everything else unchanged

    Datetimes are always returned as tz-aware UTC regardless of how the
    server formatted the offset. Downstream code that builds string keys
    via `strftime` must be safe to compare against UTC-normalised lookup
    keys; see #488 for the DST trap this avoids.
    """
    if not isinstance(val, str):
        return val

    if _looks_like_iso_datetime(val):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except ValueError:
            pass

    try:
        return float(val)
    except (ValueError, TypeError):
        return val


def _normalise_row(row: dict) -> dict:
    """Match PG row shape: parse ISO timestamps → datetime, numerics → float."""
    return {k: _parse_value(v) for k, v in row.items()}


def _user_candle_to_wire(candle) -> dict:
    """Serialise an OHLC candle to the /v1/ohlc/append wire shape.

    Accepts either an OHLCCandle dataclass (from ExchangeProvider) or a
    framework-shaped dict. The two shapes diverge on the timestamp key
    name (`timestamp` int-epoch vs `datetime`) — this helper normalises.
    Numeric coercion is left to pydantic on the gateway side.
    """
    # OHLCCandle dataclass: has .timestamp (epoch int)
    ts = getattr(candle, "timestamp", None)
    if ts is not None:
        return {
            "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume),
            "vwap": float(candle.vwap) if candle.vwap is not None else None,
            "count": int(candle.count),
        }
    # Framework dict shape: keys datetime, open, high, ... (after _ohlc_candle_to_dict)
    if isinstance(candle, dict):
        dt = candle.get("datetime")
        dt_str = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
        return {
            "timestamp": dt_str,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle["volume"]),
            "vwap": float(candle["vwap"]) if candle.get("vwap") is not None else None,
            "count": int(candle.get("count", 0)),
        }
    raise TypeError(
        f"push_user_ohlc: cannot serialise candle of type {type(candle).__name__}; "
        "expected OHLCCandle dataclass or framework dict"
    )


class ApiDataProvider:
    """DataProvider backed by HTTP calls to the Spirit API gateway."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self._url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["X-API-Key"] = api_key
        self._timeout = timeout

        # Retry on connection-level errors only (stale keep-alive on low-cadence
        # writes like the hourly heartbeat manifests as RemoteDisconnected —
        # see #399). Do NOT retry on 4xx or unexpected 5xx; those should
        # surface to the caller.
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.3,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

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
        _raise_for_capability_denial(resp)
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
            f"{self._url}{path}",
            data=json.dumps(data, default=_json_default),
            headers={"Content-Type": "application/json"},
            timeout=self._timeout,
        )
        _raise_for_capability_denial(resp)
        resp.raise_for_status()
        return resp.json().get("rows_affected", 0)

    def _put(self, path: str, data: dict) -> int:
        """PUT request, return rows_affected."""
        resp = self._session.put(
            f"{self._url}{path}",
            data=json.dumps(data, default=_json_default),
            headers={"Content-Type": "application/json"},
            timeout=self._timeout,
        )
        _raise_for_capability_denial(resp)
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

    # ------------------------------------------------------------------
    # BYOD OHLC (#666 — paid-tier scoped storage)
    # ------------------------------------------------------------------

    def get_user_ohlc(self, pair, interval, *, start=None, end=None,
                      limit=5000, order="asc"):
        """Read BYOD scoped OHLC via GET /v1/ohlc/user.

        Same response shape as get_ohlc — drop-in alternate source.
        Capability required: read:ohlc_user (Plus / Pro / internal_canary).
        """
        return self._get("/ohlc/user", {
            "pair": pair, "interval": interval, "limit": limit, "order": order,
            "start": _iso(start), "end": _iso(end),
        })

    def push_user_ohlc(self, pair: str, interval: int, candles) -> dict:
        """Push BYOD candles via POST /v1/ohlc/append.

        Caller passes either OHLCCandle dataclasses (from the
        ExchangeProvider boundary) or framework-shaped dicts; this
        method serialises to the wire shape the gateway expects.
        Returns the parsed response dict (batch_id, rows_inserted,
        rows_skipped, min_timestamp, max_timestamp).

        Capability required: write:ohlc_user. Caller wraps in
        try/except — the gateway's response is opaque to a `_post()`
        flow, so this method talks directly to the session and surfaces
        any error to the caller's retry/best-effort policy.
        """
        wire_candles = [_user_candle_to_wire(c) for c in candles]
        body = {"pair": pair, "interval": interval, "candles": wire_candles}
        resp = self._session.post(
            f"{self._url}/ohlc/append",
            data=json.dumps(body, default=_json_default),
            headers={"Content-Type": "application/json"},
            timeout=self._timeout,
        )
        _raise_for_capability_denial(resp)
        resp.raise_for_status()
        return resp.json()

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
        return self._get("/shadow-outcomes/pending", {"limit": limit})

    def update_shadow_outcome(self, data):
        payload = {
            "id": data["id"],
            "shadow_hit_target": data["shadow_hit_target"],
            "shadow_hit_stop": data["shadow_hit_stop"],
            "shadow_max_favorable": data["shadow_max_favorable"],
            "shadow_max_adverse": data["shadow_max_adverse"],
            "shadow_bars_to_resolution": data["shadow_bars_to_resolution"],
        }
        return self._post("/shadow-outcomes", payload)

    # ==================================================================
    # Theses
    # ==================================================================

    def write_thesis(self, data):
        return self._post("/theses", data)

    def update_thesis_outcome(self, data):
        return self._put("/theses", data)

    def update_thesis_checks(self, data):
        return self._put("/theses/checks", data)

    def get_thesis_calibration_data(self, limit=2000):
        return self._get("/calibrations/theses", {"limit": limit})

    def write_regime_decision(self, data):
        return self._post("/regime-decisions", data)

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

    # ==================================================================
    # Calibration inputs (#338)
    # ==================================================================

    def get_cooldown_calibration(self, pair, *, interval=60, lookback_months=12):
        return self._get("/calibrations/cooldown", {
            "pair": pair,
            "interval": interval,
            "lookback_months": lookback_months,
        })

    def get_risk_gate_calibration(self, pair, *, calibrate_before=None):
        return self._get("/calibrations/risk-gate", {
            "pair": pair,
            "calibrate_before": _iso(calibrate_before),
        })

    def get_entry_quality_calibration(self, dimension, *, as_of=None):
        return self._get("/calibrations/entry-quality", {
            "dimension": dimension,
            "as_of": _iso(as_of),
        })

    def get_bounce_signature_norm_stats(self):
        return self._get("/calibrations/bounce-signature-norm", None)

    def get_volatility_context(self, pair, interval, *, as_of=None):
        return self._get_one("/calibrations/volatility-context", {
            "pair": pair,
            "interval": interval,
            "as_of": _iso(as_of),
        })

    def get_composite_outcomes(self, *, calibrate_before=None):
        return self._get("/calibrations/composite-outcomes", {
            "calibrate_before": _iso(calibrate_before),
        })

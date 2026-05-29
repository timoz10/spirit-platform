"""OhlcCatchupRunner — boot-time gap-fill for the user-owned OHLC store (v2.2.4).

When Spirit starts up after downtime, its local OHLC store (SQLite on
Free, scoped cloud on Paid BYOD) lags behind the live market by however
long the daemon was off. This runner fills that gap before the
strategy loop begins, so the first tick of trading isn't deciding on
stale candles.

Behavioural contract (locked 2026-05-21, V2_2_4_BYOD_OHLC_AND_CATCHUP.md)
========================================================================

Per (pair, interval), in order:

  1. Read last local candle via ``dp.get_user_ohlc(..., order='desc',
     limit=1)``. If none, log INFO ``no local data — skipping`` and
     continue. CSV upload is the documented path for bulk seed (Q1).

  2. Compute gap = now − last_local_ts. If gap < one interval, log
     DEBUG ``current`` and continue.

  3. If gap ≤ 720 candles (Kraken's per-call cap): fetch via
     ``ohlc_source.get_ohlc(pair, interval, start=last, limit=720)``,
     append via ``dp.append_user_ohlc(...)``. Log INFO with the insert
     count.

  4. If gap > 720 candles: fetch the most-recent 720 candles, append,
     log WARN advising the customer to re-run ``spirit-setup`` to seed
     the older portion from a Kraken CSV export.

Failure modes
-------------
- Any per-(pair, interval) exception is logged WARN and the runner
  continues to the next tuple. The boot path must not abort because of
  a single bad pair.
- ``run()`` itself never raises — main.py wraps it in try/except, but
  the runner's contract is "best-effort, idempotent, no-throw".

Boot-time invariant
-------------------
Constructor stores `pairs` + `intervals` as-is — caller picks them.
``run()`` is single-shot: a sentinel guards against a second call on the
same instance returning before doing any work. Tests can build fresh
instances; main.py builds one per startup.

Q2 (locked): boot latency ~15-20s for 5 pairs × 3 intervals is
acceptable. Single summary log line at the end so operators don't have
to grep per-pair lines to confirm catchup completed.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from spirit.logger import get_logger

logger = get_logger("ohlc_catchup")


# Kraken's per-call OHLC cap. Hard-coded here rather than read from the
# exchange object because the runner's contract is bounded by this
# number — going above it would require a paged fetch which is
# explicitly out of scope (deferred to a future release; CSV upload is
# the larger-gap path).
KRAKEN_PER_CALL_CAP: int = 720


class _OhlcSourceProto(Protocol):
    """Structural type for the live OHLC source — anything with `get_ohlc`.

    Caller usually passes an ``ExchangeBackedDataProvider`` (Free) or the
    BYOD wrapper's ``_ohlc_source`` (Paid). Tests pass a MagicMock.
    """

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
        ...


class OhlcCatchupRunner:
    """Boot-time OHLC gap-filler. Use once per Spirit startup."""

    def __init__(
        self,
        dp: Any,
        ohlc_source: _OhlcSourceProto,
        *,
        pairs: list[str],
        intervals: list[int],
    ) -> None:
        self._dp = dp
        self._ohlc_source = ohlc_source
        self._pairs = list(pairs)
        self._intervals = list(intervals)
        self._ran = False  # boot-time invariant guard

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, *, now: datetime | None = None) -> dict:
        """Run catchup over every (pair, interval). Never raises.

        ``now`` is optional for tests — defaults to ``datetime.now(UTC)``
        so production callers don't have to pass anything.

        Returns a summary dict ``{pairs, intervals, filled, skipped_empty,
        skipped_current, over_cap, errors, elapsed_s}``. Mostly useful
        for the post-run summary log and for tests that want to assert
        an aggregate without grepping log lines.
        """
        if self._ran:
            logger.debug("[CATCHUP] run() called twice on same instance — no-op")
            return {
                "pairs": len(self._pairs),
                "intervals": len(self._intervals),
                "filled": 0, "skipped_empty": 0, "skipped_current": 0,
                "over_cap": 0, "errors": 0, "elapsed_s": 0.0,
            }
        self._ran = True

        now = now or datetime.now(timezone.utc)
        t0 = time.monotonic()

        stats = {
            "pairs":            len(self._pairs),
            "intervals":        len(self._intervals),
            "filled":           0,
            "skipped_empty":    0,
            "skipped_current":  0,
            "over_cap":         0,
            "errors":           0,
            "elapsed_s":        0.0,
        }

        if not self._pairs or not self._intervals:
            logger.info(
                "[CATCHUP] nothing to do (pairs=%d intervals=%d)",
                len(self._pairs), len(self._intervals),
            )
            stats["elapsed_s"] = time.monotonic() - t0
            return stats

        for pair in self._pairs:
            for interval in self._intervals:
                try:
                    outcome = self._catch_up_one(pair, interval, now=now)
                    stats[outcome] += 1
                except Exception as e:
                    # Per-pair failures are best-effort. Log WARN, keep going.
                    logger.warning(
                        "[CATCHUP] %s/%dm failed: %s",
                        pair, interval, e,
                    )
                    stats["errors"] += 1

        stats["elapsed_s"] = time.monotonic() - t0
        logger.info(
            "[CATCHUP] complete in %.1fs "
            "(pairs=%d × intervals=%d → filled=%d "
            "skipped_empty=%d skipped_current=%d "
            "over_cap=%d errors=%d)",
            stats["elapsed_s"],
            stats["pairs"], stats["intervals"], stats["filled"],
            stats["skipped_empty"], stats["skipped_current"],
            stats["over_cap"], stats["errors"],
        )
        return stats

    # ------------------------------------------------------------------
    # Internal — one tuple
    # ------------------------------------------------------------------

    def _catch_up_one(
        self,
        pair: str,
        interval: int,
        *,
        now: datetime,
    ) -> str:
        """Catch up a single (pair, interval). Returns one of the
        stat-bucket names: ``filled`` | ``skipped_empty`` |
        ``skipped_current`` | ``over_cap``. Raises on real errors —
        caller's try/except converts to ``errors``.
        """
        # 1. Resume point: most-recent local candle.
        recent = self._dp.get_user_ohlc(pair, interval, order="desc", limit=1)
        if not recent:
            logger.info(
                "[CATCHUP] %s/%dm: no local data — skipping "
                "(bulk-seed via CSV upload)",
                pair, interval,
            )
            return "skipped_empty"

        last_dt: datetime = recent[0]["datetime"]
        if last_dt.tzinfo is None:
            # Rule 11 — interpret naive as UTC.
            last_dt = last_dt.replace(tzinfo=timezone.utc)

        # 2. Gap check.
        gap = now - last_dt
        interval_delta = timedelta(minutes=interval)
        if gap < interval_delta:
            logger.debug(
                "[CATCHUP] %s/%dm: current (gap=%.0fs < interval)",
                pair, interval, gap.total_seconds(),
            )
            return "skipped_current"

        # 3 + 4. Fetch the gap, capped at 720 candles.
        gap_candles = int(gap.total_seconds() / 60 // max(interval, 1))
        over_cap = gap_candles > KRAKEN_PER_CALL_CAP

        # Always fetch from one interval past the last local candle —
        # otherwise we'd re-fetch the last_dt row and rely on the
        # composite-PK dedupe. ExchangeBackedDataProvider's start is
        # inclusive, so add one interval.
        fetch_start = last_dt + interval_delta
        candles = self._ohlc_source.get_ohlc(
            pair, interval,
            start=fetch_start,
            limit=KRAKEN_PER_CALL_CAP,
        )

        if not candles:
            # Source returned nothing — possible if the last local candle
            # IS already the latest (e.g. exchange hasn't ticked yet) or
            # if the exchange returned an empty response. Not an error.
            logger.debug(
                "[CATCHUP] %s/%dm: exchange returned no new candles "
                "after %s",
                pair, interval, last_dt.isoformat(),
            )
            return "skipped_current"

        # Rule 11 row-contract bridge: get_ohlc keys the candle time as
        # "datetime" (ExchangeBackedDataProvider._ohlc_candle_to_dict), but
        # append_user_ohlc's contract requires "timestamp". Remap at the
        # hand-off so the two providers agree without widening the write
        # contract for every other caller (#825). _candle_ts_iso accepts a
        # datetime, so no value conversion is needed — only the key name.
        write_candles = [
            {**c, "timestamp": c["datetime"]} if "timestamp" not in c and "datetime" in c else c
            for c in candles
        ]

        result = self._dp.append_user_ohlc(pair, interval, write_candles)
        inserted = result.get("rows_inserted", 0)

        if over_cap:
            logger.warning(
                "[CATCHUP] %s/%dm: gap of ~%d candles exceeds %d-row cap. "
                "Filled most-recent %d. Run spirit-setup to seed older "
                "portion from a Kraken CSV export.",
                pair, interval, gap_candles, KRAKEN_PER_CALL_CAP, inserted,
            )
            return "over_cap"

        logger.info(
            "[CATCHUP] %s/%dm: filled %d candles (gap was ~%d)",
            pair, interval, inserted, gap_candles,
        )
        return "filled"


# ---------------------------------------------------------------------
# Boot-time wiring helper — picks pairs + intervals from config
# ---------------------------------------------------------------------

def _configured_pairs() -> set[str]:
    """Pairs this instance is configured to trade, parsed from SPIRIT_PAIRS.

    Returns an empty set when SPIRIT_PAIRS is unset — callers treat that
    as "no restriction" and fall back to the full catalogue. ``get_config``
    already resolves env-first then per-instance YAML.
    """
    from spirit.utils.config_loader import get_config

    raw = (get_config("SPIRIT_PAIRS", "") or "").strip()
    return {p.strip() for p in raw.split(",") if p.strip()}


def wire_boot_catchup(dp: Any, *, instance: str) -> dict | None:
    """Build + run an ``OhlcCatchupRunner`` from a runtime DataProvider.

    Called once during Spirit startup (after ``get_data_provider()``,
    before the strategy loop). Never raises — boot must not abort on
    catchup failures.

    Resolves the live OHLC source structurally:
      - CompositeDataProvider (Free)     → ``dp._reads``
      - PaidTierComposite (Paid BYOD)    → ``dp._ohlc_source``
      - else (naked ApiDataProvider)     → skip (centralised store
                                            covers this tier)

    Pairs from ``dp.get_pairs(instance=...)``; intervals from
    ``SPIRIT_OHLC_CATCHUP_INTERVALS`` (default ``'60'``). Comma-separated
    int tokens; whitespace and unparseable tokens get a WARN and are
    skipped.

    Returns the runner's stats dict on success, or ``None`` when the
    runner was skipped (no OHLC source, no pairs, no intervals).
    """
    from spirit.utils.config_loader import get_config

    try:
        ohlc_source = (
            getattr(dp, "_reads", None) or getattr(dp, "_ohlc_source", None)
        )
        if ohlc_source is None:
            logger.info(
                "[CATCHUP] skipped: DataProvider %s has no exchange-backed "
                "read side (centralised OHLC store covers this tier)",
                type(dp).__name__,
            )
            return None

        try:
            pair_rows = dp.get_pairs(instance=instance) or []
        except Exception as e:
            logger.warning(f"[CATCHUP] skipped: get_pairs failed: {e}")
            return None
        catalogue = [
            r["pair"] for r in pair_rows
            if isinstance(r, dict) and r.get("pair")
        ]
        if not catalogue:
            logger.info("[CATCHUP] skipped: no pairs configured for instance")
            return None

        # Narrow the fetchable catalogue (pairs.json — what the platform
        # CAN fetch) to the pairs this instance is configured to trade
        # (SPIRIT_PAIRS — what it WILL fetch). Catching up catalogue pairs
        # the user never configured just emits a per-pair "no local data —
        # skipping" line on every boot (#801). Unset SPIRIT_PAIRS keeps the
        # full catalogue, preserving behaviour for configs that don't set it.
        configured = _configured_pairs()
        if configured:
            pairs = [p for p in catalogue if p in configured]
            if not pairs:
                logger.info(
                    "[CATCHUP] skipped: none of SPIRIT_PAIRS (%s) are in the "
                    "fetchable catalogue (%s)",
                    ", ".join(sorted(configured)), ", ".join(catalogue),
                )
                return None
        else:
            pairs = catalogue

        raw_intervals = get_config("SPIRIT_OHLC_CATCHUP_INTERVALS", "60") or "60"
        intervals: list[int] = []
        for tok in str(raw_intervals).split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                intervals.append(int(tok))
            except ValueError:
                logger.warning(
                    f"[CATCHUP] skipping invalid interval token: {tok!r}"
                )
        if not intervals:
            logger.info("[CATCHUP] skipped: no valid intervals configured")
            return None

        runner = OhlcCatchupRunner(
            dp, ohlc_source, pairs=pairs, intervals=intervals,
        )
        return runner.run()
    except Exception as e:
        logger.warning(f"[CATCHUP] skipped: boot-time wiring failed: {e}")
        return None

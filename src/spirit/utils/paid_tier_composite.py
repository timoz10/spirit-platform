"""PaidTierComposite — Plus/Pro Spirit with BYOD OHLC (#666 sub-tasks 2+9).

Post-#665, Plus and Pro API keys no longer carry `read:ohlc` capability;
direct `/v1/ohlc` calls return 403. Spirit still needs OHLC data for
trade decisions, indicators, and trajectory recovery, so the Plus/Pro
data provider becomes a two-source composite:

    OHLC reads  → routed by SPIRIT_OHLC_SOURCE (default `auto`):
                    - cloud_first: gateway.get_user_ohlc only
                    - local_first: ohlc_source.get_ohlc + push-on-fetch
                    - auto:        cloud_first when start is set,
                                   local_first otherwise — see below
    Everything  → `gateway` (ApiDataProvider) — D-Limit zones, scorer,
    else          state, performance, pairs, etc.

Routing rationale for `auto`:
  - No `start` → "what's happening right now" — local Kraken is always
    current, gateway lags by however long push-on-fetch takes to land.
  - `start` set → historical window — the gateway's scoped cloud has
    been accumulating from incremental pushes (and any CSV backfill),
    so it's the right source for trajectory recovery + backfill reads.

Routing is decided in `data_provider.get_data_provider()` based on the
preflight-cached capability set: missing `read:ohlc` → composite,
otherwise direct ApiDataProvider (internal_canary / admin paths).

Delegation pattern
------------------
`get_ohlc` is the only explicit override; every other DataProvider
method falls through to the gateway via `__getattr__`. This keeps the
class small and matches the architectural intent — the composite only
exists to redirect OHLC reads; the gateway is otherwise authoritative
for both Framework and IP surfaces.

Free tier uses a different composite (`CompositeDataProvider`) which
also short-circuits IP methods with an upgrade message; here those
methods route through to the gateway because Plus/Pro have IP access.
"""

from __future__ import annotations

import os
from typing import Any

from spirit.logger import get_logger

logger = get_logger("paid_tier_composite")

_VALID_SOURCES = frozenset({"auto", "cloud_first", "local_first"})


class PaidTierComposite:
    """Composite data provider for Plus/Pro Spirit running BYOD OHLC.

    Construction is owned by `get_data_provider`; tests can build it
    directly with mock delegates.
    """

    def __init__(self, *, ohlc_source: Any, gateway: Any) -> None:
        self._ohlc_source = ohlc_source
        self._gateway = gateway
        logger.info(
            "PaidTierComposite initialised "
            f"(ohlc_source={type(ohlc_source).__name__}, "
            f"gateway={type(gateway).__name__})"
        )

    # ------------------------------------------------------------------
    # OHLC — routed by SPIRIT_OHLC_SOURCE
    # ------------------------------------------------------------------

    def _resolve_source(self) -> str:
        """Read SPIRIT_OHLC_SOURCE config; unknown values fall back to auto.

        Resolved per-call rather than at construction so an operator can
        flip the env var and have the next tick reflect it without a
        Spirit restart — useful for live debugging.
        """
        raw = (os.environ.get("SPIRIT_OHLC_SOURCE", "auto") or "auto").strip().lower()
        if raw not in _VALID_SOURCES:
            logger.warning(
                f"SPIRIT_OHLC_SOURCE={raw!r} not in {sorted(_VALID_SOURCES)}; "
                f"falling back to 'auto'"
            )
            return "auto"
        return raw

    def get_ohlc(
        self,
        pair,
        interval,
        *,
        start=None,
        end=None,
        limit=5000,
        order="asc",
    ):
        source = self._resolve_source()

        # auto: pick a side per call. `start` set → historical window,
        # gateway's accumulated cloud is the right home. No `start` →
        # "live now", local is always current.
        if source == "auto":
            source = "cloud_first" if start is not None else "local_first"

        if source == "cloud_first":
            return self._gateway.get_user_ohlc(
                pair, interval,
                start=start, end=end, limit=limit, order=order,
            )

        # local_first: read from the local exchange. Push-on-fetch hook
        # inside ExchangeBackedDataProvider lands the data in cloud as a
        # best-effort side effect, so the cloud catches up over time.
        return self._ohlc_source.get_ohlc(
            pair, interval,
            start=start, end=end, limit=limit, order=order,
        )

    # ------------------------------------------------------------------
    # Everything else — delegated to gateway via attribute fall-through
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # `__getattr__` only fires for attributes not found via normal
        # lookup — so `get_ohlc` (defined above) takes precedence and
        # everything else lands here.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._gateway, name)

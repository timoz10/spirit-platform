"""PaidTierComposite — Plus/Pro Spirit with BYOD OHLC (#666 sub-task 2).

Post-#665, Plus and Pro API keys no longer carry `read:ohlc` capability;
direct `/v1/ohlc` calls return 403. Spirit still needs OHLC data for
trade decisions, indicators, and trajectory recovery, so the Plus/Pro
data provider becomes a two-source composite:

    OHLC reads  → `ohlc_source` (today: ExchangeBackedDataProvider over
                  Kraken public REST; future: ApiDataProvider against
                  `/v1/ohlc/user` reading user-scoped cloud storage,
                  see sub-tasks 4–8)
    Everything  → `gateway` (ApiDataProvider) — D-Limit zones, scorer,
    else          state, performance, pairs, etc.

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

from typing import Any

from spirit.logger import get_logger

logger = get_logger("paid_tier_composite")


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
    # OHLC — routed away from the gateway
    # ------------------------------------------------------------------

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

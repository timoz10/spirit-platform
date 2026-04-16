"""
KrakenExchangeProvider — Kraken implementation of the ExchangeProvider protocol.

Reference implementation for exchange plugin developers. Wraps the Kraken
REST API (v0) with Spirit-normalised pair names and standard return types.

Pair mapping: Spirit uses short names (XBTUSD, ETHUSD). Kraken's API
sometimes returns longer names (XXBTZUSD, XETHZUSD). This provider
handles the translation transparently.

See docs/reference/EXCHANGE_PLUGIN_GUIDE.md for the full plugin contract.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import os
import time
from typing import Optional

import requests

from spirit.exchange.protocol import (
    ExchangeProvider,
    Ticker,
    PairInfo,
    OrderResult,
    OrderStatus,
    OHLCCandle,
    OrderbookLevel,
    Orderbook,
)
from spirit.logger import get_logger

logger = get_logger("exchange.kraken")


# =====================================================================
# Pair name mapping
# =====================================================================
# Kraken uses inconsistent pair names across endpoints. Spirit uses
# short canonical names. This map covers known divergences.

_KRAKEN_TO_SPIRIT = {
    "XXBTZUSD": "XBTUSD",
    "XETHZUSD": "ETHUSD",
}
_SPIRIT_TO_KRAKEN: dict[str, str] = {}  # populated lazily from API


# =====================================================================
# Helpers
# =====================================================================

def _sign_request(path: str, data: dict, secret: str, nonce: str) -> str:
    """Kraken HMAC-SHA512 request signing."""
    postdata = "&".join(f"{k}={v}" for k, v in data.items())
    encoded = (str(nonce) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _user_agent() -> str:
    return os.environ.get(
        "KRAKEN_BOT_USER_AGENT", "Spirit/1.0 (+https://tradebot.live)"
    )


# =====================================================================
# KrakenExchangeProvider
# =====================================================================

class KrakenExchangeProvider:
    """Kraken exchange plugin for Spirit.

    Credentials are loaded from generic env vars with Kraken-specific fallback:
      - EXCHANGE_API_KEY  (fallback: KRAKEN_API_KEY)
      - EXCHANGE_API_SECRET (fallback: KRAKEN_API_SECRET)

    Also supports Docker _FILE secret pattern (EXCHANGE_API_KEY_FILE).
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.5,
    ):
        self._api_key = api_key or _load_credential("EXCHANGE_API_KEY", "KRAKEN_API_KEY")
        self._api_secret = api_secret or _load_credential("EXCHANGE_API_SECRET", "KRAKEN_API_SECRET")
        self._base_url = (
            base_url
            or os.environ.get("EXCHANGE_API_BASE_URL")
            or os.environ.get("KRAKEN_API_BASE_URL", "https://api.kraken.com")
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base

        # Cached pair info (refreshed on demand, rarely changes)
        self._pair_info_cache: dict[str, PairInfo] = {}

        logger.info(f"[EXCHANGE] KrakenExchangeProvider initialized (url={self._base_url})")

    # -----------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------

    @property
    def name(self) -> str:
        return "kraken"

    # -----------------------------------------------------------------
    # Internal HTTP helpers
    # -----------------------------------------------------------------

    def _public_get(self, endpoint: str, params: dict | None = None) -> dict:
        """GET to a Kraken public endpoint with retry + rate-limit handling."""
        url = f"{self._base_url}{endpoint}"
        headers = {"User-Agent": _user_agent()}

        for attempt in range(self._max_retries):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                if resp.status_code == 429:
                    sleep_s = self._backoff_base ** attempt
                    ra = resp.headers.get("Retry-After")
                    if ra:
                        try:
                            sleep_s = max(sleep_s, float(ra))
                        except (ValueError, TypeError):
                            pass
                    logger.warning(
                        f"[EXCHANGE] HTTP 429 on {endpoint}. "
                        f"Retry-After={ra}. attempt={attempt + 1}/{self._max_retries}"
                    )
                    time.sleep(sleep_s)
                    continue
                resp.raise_for_status()
                result = resp.json()
                if result.get("error"):
                    errs = result["error"]
                    if any("rate" in e.lower() for e in errs):
                        logger.warning(f"[EXCHANGE] Rate limit: {errs}")
                        time.sleep(self._backoff_base ** attempt)
                        continue
                    raise RuntimeError(f"Kraken API error: {errs}")
                return result.get("result", {})
            except requests.RequestException as e:
                logger.warning(
                    f"[EXCHANGE] Request error on {endpoint} "
                    f"attempt={attempt + 1}/{self._max_retries}: {e}"
                )
                if attempt < self._max_retries - 1:
                    time.sleep(self._backoff_base ** attempt)
                else:
                    raise RuntimeError(f"Failed {endpoint} after {self._max_retries} attempts: {e}")

        raise RuntimeError(f"Failed {endpoint} after {self._max_retries} attempts")

    def _private_post(self, path: str, data: dict | None = None) -> dict:
        """POST to a Kraken private endpoint with HMAC signing."""
        if not self._api_key or not self._api_secret:
            raise RuntimeError(
                "Exchange credentials not set. "
                "Set EXCHANGE_API_KEY / EXCHANGE_API_SECRET "
                "(or KRAKEN_API_KEY / KRAKEN_API_SECRET) in your environment."
            )
        url = f"{self._base_url}{path}"
        nonce = str(int(time.time() * 1000))
        post_data = dict(data or {})
        post_data["nonce"] = nonce

        signature = _sign_request(path, post_data, self._api_secret, nonce)
        headers = {
            "API-Key": self._api_key,
            "API-Sign": signature,
        }

        resp = requests.post(url, headers=headers, data=post_data, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("error"):
            raise RuntimeError(f"Kraken API error: {result['error']}")
        return result.get("result", {})

    def _resolve_pair(self, spirit_pair: str) -> str:
        """Map Spirit pair name to Kraken API name.

        Most Kraken endpoints accept our short names directly. For edge
        cases, check _SPIRIT_TO_KRAKEN (populated when get_pair_info
        caches results from the AssetPairs endpoint).
        """
        return _SPIRIT_TO_KRAKEN.get(spirit_pair, spirit_pair)

    def _normalise_pair(self, kraken_name: str) -> str:
        """Map Kraken response pair name back to Spirit canonical name."""
        return _KRAKEN_TO_SPIRIT.get(kraken_name, kraken_name)

    # -----------------------------------------------------------------
    # Public market data
    # -----------------------------------------------------------------

    def get_ticker(self, pair: str) -> Ticker:
        result = self._public_get("/0/public/Ticker", {"pair": pair})
        data = next(iter(result.values()))
        return Ticker(
            bid=float(data["b"][0]),
            ask=float(data["a"][0]),
            last=float(data["c"][0]),
        )

    def get_ohlc(
        self, pair: str, interval: int = 60, count: int = 720
    ) -> list[OHLCCandle]:
        result = self._public_get(
            "/0/public/OHLC", {"pair": pair, "interval": interval}
        )
        # Dynamic pair key (ignore 'last')
        pair_key = next(k for k in result.keys() if k != "last")
        raw_candles = result[pair_key]

        candles = []
        for row in raw_candles[-count:]:
            candles.append(OHLCCandle(
                timestamp=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                vwap=float(row[5]),
                volume=float(row[6]),
                count=int(row[7]),
            ))

        # Drop the last candle if it's the current (incomplete) one
        if candles:
            import math
            interval_seconds = interval * 60
            now_epoch = int(time.time())
            current_open = now_epoch - (now_epoch % interval_seconds)
            if candles[-1].timestamp >= current_open:
                candles = candles[:-1]

        return candles

    def get_pair_info(self, pair: str) -> PairInfo:
        if pair in self._pair_info_cache:
            return self._pair_info_cache[pair]

        result = self._public_get("/0/public/AssetPairs", {"pair": pair})

        for kraken_name, data in result.items():
            spirit_name = self._normalise_pair(kraken_name)
            # Build reverse mapping for future resolve_pair calls
            _SPIRIT_TO_KRAKEN[spirit_name] = kraken_name

            info = PairInfo(
                pair=spirit_name,
                base_asset=data.get("base", ""),
                quote_asset=data.get("quote", ""),
                lot_decimals=int(data.get("lot_decimals", 8)),
                price_decimals=int(data.get("pair_decimals", 1)),
                ordermin=float(data.get("ordermin", 0)),
            )
            self._pair_info_cache[spirit_name] = info

        if pair in self._pair_info_cache:
            return self._pair_info_cache[pair]

        # Fallback for known pairs
        _DEFAULTS = {
            "XBTUSD":  PairInfo("XBTUSD", "XBT", "USD", 8, 1, 0.00005),
            "ETHUSD":  PairInfo("ETHUSD", "ETH", "USD", 8, 2, 0.001),
            "SOLUSD":  PairInfo("SOLUSD", "SOL", "USD", 8, 3, 0.02),
            "ATOMUSD": PairInfo("ATOMUSD", "ATOM", "USD", 8, 4, 0.5),
        }
        if pair in _DEFAULTS:
            logger.warning(f"[EXCHANGE] Pair {pair} not in API response, using defaults")
            self._pair_info_cache[pair] = _DEFAULTS[pair]
            return self._pair_info_cache[pair]

        raise RuntimeError(f"Unknown pair: {pair}")

    def get_orderbook(self, pair: str, depth: int = 100) -> Orderbook:
        result = self._public_get(
            "/0/public/Depth", {"pair": pair, "count": min(depth, 500)}
        )
        pair_key = next(iter(result.keys()))
        raw = result[pair_key]

        def _parse(levels):
            return [
                OrderbookLevel(
                    price=float(lvl[0]),
                    volume=float(lvl[1]),
                    timestamp=int(lvl[2]),
                )
                for lvl in levels
            ]

        return Orderbook(
            asks=_parse(raw.get("asks", [])),
            bids=_parse(raw.get("bids", [])),
        )

    # -----------------------------------------------------------------
    # Private trading
    # -----------------------------------------------------------------

    def place_order(
        self,
        pair: str,
        side: str,
        volume: float,
        order_type: str = "market",
        price: float | None = None,
        validate_only: bool = False,
    ) -> OrderResult:
        data = {
            "pair": self._resolve_pair(pair),
            "type": side,
            "ordertype": order_type,
            "volume": str(volume),
            "validate": "true" if validate_only else "false",
        }
        if price is not None and order_type == "limit":
            data["price"] = str(price)

        result = self._private_post("/0/private/AddOrder", data)
        txids = result.get("txid", [])
        txid = txids[0] if txids else ""
        return OrderResult(
            txid=txid,
            status="open" if txid else "error",
            raw=result,
        )

    def cancel_order(self, txid: str) -> bool:
        try:
            self._private_post("/0/private/CancelOrder", {"txid": txid})
            return True
        except RuntimeError as e:
            if "Unknown order" in str(e):
                return False
            raise

    def get_order_status(self, txid: str) -> OrderStatus:
        result = self._private_post("/0/private/QueryOrders", {"txid": txid})
        order_data = result.get(txid, {})
        status = order_data.get("status", "unknown")
        vol_exec = float(order_data.get("vol_exec", 0))
        vol = float(order_data.get("vol", 0))
        price = float(order_data.get("price", 0))

        return OrderStatus(
            txid=txid,
            status=status,
            filled_price=price,
            filled_volume=vol_exec,
            remaining=max(0, vol - vol_exec),
            raw=order_data,
        )

    def get_open_orders(self) -> list[OrderStatus]:
        result = self._private_post("/0/private/OpenOrders")
        orders = []
        for txid, data in result.get("open", {}).items():
            orders.append(OrderStatus(
                txid=txid,
                status="open",
                filled_price=float(data.get("price", 0)),
                filled_volume=float(data.get("vol_exec", 0)),
                remaining=float(data.get("vol", 0)) - float(data.get("vol_exec", 0)),
                raw=data,
            ))
        return orders

    def get_balance(self) -> dict[str, float]:
        result = self._private_post("/0/private/Balance")
        return {asset: float(bal) for asset, bal in result.items()}


# =====================================================================
# Credential loading
# =====================================================================

def _load_credential(generic_name: str, legacy_name: str) -> str | None:
    """Load a credential from environment with fallback chain.

    Priority:
      1. Generic name (EXCHANGE_API_KEY)
      2. Generic _FILE variant (EXCHANGE_API_KEY_FILE)
      3. Legacy name (KRAKEN_API_KEY)
      4. Legacy _FILE variant (KRAKEN_API_KEY_FILE)
    """
    for name in (generic_name, legacy_name):
        val = os.environ.get(name, "").strip()
        if val:
            return val
        file_path = os.environ.get(f"{name}_FILE", "")
        if file_path and os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                continue
    return None

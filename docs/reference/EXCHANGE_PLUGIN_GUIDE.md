# Exchange Plugin Developer Guide

How to build an exchange plugin for Spirit. This document covers the protocol contract, return types, credential handling, and step-by-step instructions for adding a new exchange.

## Architecture overview

```
Strategy (BaseStrategy)           ← Pure trade logic, no exchange code
    │
    ▼ TradeSignal
Orchestrator (main.py)
    │
    ▼ place_order / get_ticker / ...
OrderExecutor                     ← Spirit-level trade lifecycle
    │
    ▼ raw exchange calls
ExchangeProvider (Protocol)       ← YOU IMPLEMENT THIS
    ├── KrakenExchangeProvider    ← Reference implementation
    └── YourExchangeProvider      ← Your plugin
```

Spirit never calls exchange APIs directly. All exchange access flows through the `ExchangeProvider` protocol. Your plugin implements this protocol, and Spirit routes calls to it via the `get_exchange_provider()` factory.

**Key design principle:** The ExchangeProvider handles raw exchange API access. Spirit-level logic (equity tracking, fill polling, PG recording, slippage measurement) lives in the OrderExecutor layer above — you don't need to implement any of that.

## Quick start: adding a new exchange

### 1. Create your provider

Create `src/spirit/exchange/yourexchange.py`:

```python
"""YourExchange implementation of ExchangeProvider."""

from spirit.exchange.protocol import (
    ExchangeProvider,
    Ticker, PairInfo, OrderResult, OrderStatus,
    OHLCCandle, Orderbook, OrderbookLevel,
)
from spirit.logger import get_logger

logger = get_logger("exchange.yourexchange")


class YourExchangeProvider:
    """YourExchange plugin for Spirit."""

    def __init__(self):
        from spirit.exchange.kraken import _load_credential
        self._api_key = _load_credential("EXCHANGE_API_KEY", "YOUREXCHANGE_API_KEY")
        self._api_secret = _load_credential("EXCHANGE_API_SECRET", "YOUREXCHANGE_API_SECRET")
        logger.info("[EXCHANGE] YourExchangeProvider initialized")

    @property
    def name(self) -> str:
        return "yourexchange"

    def get_ticker(self, pair: str) -> Ticker:
        # Call your exchange API, return a Ticker
        ...

    # ... implement all ExchangeProvider methods
```

### 2. Register in the factory

Edit `src/spirit/exchange/__init__.py`, add an elif:

```python
elif exchange == "yourexchange":
    from spirit.exchange.yourexchange import YourExchangeProvider
    _provider = YourExchangeProvider()
```

### 3. Configure

```yaml
# spirit.yaml
SPIRIT_EXCHANGE: yourexchange
```

Or via environment:
```bash
export SPIRIT_EXCHANGE=yourexchange
export EXCHANGE_API_KEY=your_key_here
export EXCHANGE_API_SECRET=your_secret_here
```

That's it. Spirit will route all exchange calls through your provider.

## Interface contract rules

These are the hard rules governing the boundary between Spirit and exchange plugins. Both sides commit to these guarantees. Violating them will cause silent data corruption or trading failures.

### What Spirit guarantees to your plugin

| Rule | Guarantee |
|------|-----------|
| **S1. Spirit pair names only** | Spirit will only pass pair names from the configured `SPIRIT_PAIRS` list (e.g. `XBTUSD`, `ETHUSD`). Your provider will never receive an exchange-native name. |
| **S2. No private calls in paper mode** | When `--mode paper` is active, Spirit will **never** call `place_order`, `cancel_order`, `get_order_status`, `get_open_orders`, or `get_balance`. Paper mode only uses public methods (`get_ticker`, `get_ohlc`, `get_pair_info`, `get_orderbook`). You can implement private methods as stubs during development and test with paper mode. |
| **S3. Single instance** | Spirit creates exactly one `ExchangeProvider` via the factory. All threads share this instance. Spirit will never create multiple instances of your provider. |
| **S4. Lifecycle** | The provider is created once during startup and lives for the entire Spirit process. There is no `shutdown()` or `cleanup()` call — if you hold connections, use `atexit` or context managers. |
| **S5. No concurrent writes** | Spirit will never call `place_order` for the same pair from two threads simultaneously. Different pairs may execute concurrently. |
| **S6. Volume is pre-rounded** | Spirit rounds order volume to `lot_decimals` from `get_pair_info()` before calling `place_order`. Your provider does not need to round. |
| **S7. Limit price provided** | For `order_type='limit'`, Spirit always passes `price`. For `order_type='market'`, `price` is always `None`. |

### What your plugin guarantees to Spirit

| Rule | Guarantee |
|------|-----------|
| **P1. Standard return types** | Every method returns the exact dataclass from `protocol.py`. Never return raw dicts, tuples, or exchange-specific objects. Spirit code accesses fields by attribute name (e.g. `ticker.bid`), not dict keys. |
| **P2. Spirit pair names in output** | All pair names in return values (e.g. `PairInfo.pair`) use Spirit canonical names, never exchange-native names. Translation happens inside your provider. |
| **P3. Transient error handling** | Rate limits (HTTP 429), timeouts, and intermittent network errors must be retried internally with backoff. Only raise `RuntimeError` for permanent failures (bad credentials, unknown pair, insufficient funds, exchange maintenance). Spirit does not retry — if you raise, the trade fails. |
| **P4. Thread safety** | Your provider must be safe to call from multiple threads. Spirit runs one thread per trading pair. Read-only state (caches) is fine without locks. Mutable shared state must be protected. |
| **P5. Closed candles only** | `get_ohlc()` must exclude the current incomplete candle. Spirit's indicators assume every candle is final. Returning an open candle will corrupt indicator calculations. |
| **P6. Idempotent reads** | Public methods (`get_ticker`, `get_ohlc`, etc.) must be safe to call repeatedly with no side effects. Spirit may call them at any frequency. |
| **P7. Honest order status** | `get_order_status()` must return the real exchange status. Never cache or fake order state. Spirit uses this for fill reconciliation — stale status causes equity tracking errors. |
| **P8. Credential validation at init** | If credentials are missing or invalid, raise `RuntimeError` in `__init__`. Don't wait until the first private API call to discover bad credentials. |

### Data flow: when Spirit calls each method

```
Spirit startup
    │
    ├─ get_pair_info(pair)          # Once per pair, cached by Spirit
    ├─ get_open_orders()            # Orphan reconciliation (live mode only)
    └─ get_balance()                # Initial equity check (live mode only)

Trading loop (every candle)
    │
    ├─ get_ohlc(pair, interval)     # Live candle buffer refresh
    ├─ get_ticker(pair)             # Current price for evaluation
    │
    ├─ [if entry signal]
    │   ├─ place_order(pair, 'buy', volume, ...)
    │   └─ get_order_status(txid)   # Poll until filled or timeout
    │
    ├─ [if exit signal]
    │   ├─ place_order(pair, 'sell', volume, ...)
    │   └─ get_order_status(txid)   # Poll until filled or timeout
    │
    └─ [if limit order timeout]
        └─ cancel_order(txid)

Periodic
    │
    ├─ get_orderbook(pair, depth)   # Liquidity tumblers (every ~60s)
    └─ get_balance()                # Equity reconciliation (configurable)
```

## Protocol contract

Your provider **must** implement every method in `ExchangeProvider`. The full protocol is defined in `src/spirit/exchange/protocol.py`.

### Methods

| Method | Auth | Called when |
|--------|------|------------|
| `get_ticker(pair)` | No | Fill price estimation (paper + live) |
| `get_ohlc(pair, interval, count)` | No | Live candle feed during trading |
| `get_pair_info(pair)` | No | Volume rounding, order minimums |
| `get_orderbook(pair, depth)` | No | Liquidity analysis (tumblers) |
| `place_order(pair, side, volume, ...)` | Yes | Entry and exit execution |
| `cancel_order(txid)` | Yes | Limit order timeout/cancellation |
| `get_order_status(txid)` | Yes | Fill polling after order placement |
| `get_open_orders()` | Yes | Startup orphan reconciliation |
| `get_balance()` | Yes | Balance verification, position tracking |

### Return types

Every method returns a standard dataclass, **not a raw dict**. This ensures all consumers work identically regardless of exchange.

```python
from spirit.exchange.protocol import (
    Ticker,           # bid, ask, last
    PairInfo,         # pair, base_asset, quote_asset, lot_decimals, price_decimals, ordermin
    OrderResult,      # txid, status, price, volume, raw
    OrderStatus,      # txid, status, filled_price, filled_volume, remaining, raw
    OHLCCandle,       # timestamp, open, high, low, close, volume, vwap, count
    Orderbook,        # asks: list[OrderbookLevel], bids: list[OrderbookLevel]
    OrderbookLevel,   # price, volume, timestamp
)
```

The `raw` field on `OrderResult` and `OrderStatus` carries the full exchange response for debugging. Spirit never reads it in production logic — it's for logging and troubleshooting only.

## Pair naming convention

Spirit uses short canonical pair names: `XBTUSD`, `ETHUSD`, `SOLUSD`, `ATOMUSD`.

Your provider **must**:

1. **Accept Spirit pair names** as input to all methods
2. **Translate internally** to your exchange's native names
3. **Return Spirit pair names** in all output (e.g. `PairInfo.pair`)

Example: Kraken's API returns `XXBTZUSD` for Bitcoin. `KrakenExchangeProvider` maps this to `XBTUSD` internally. Spirit never sees `XXBTZUSD`.

If your exchange uses different pair names (e.g. `BTC-USD`, `BTC/USDT`), build a mapping table in your provider. See `_KRAKEN_TO_SPIRIT` in `kraken.py` for the pattern.

## Credential handling

Spirit uses generic credential names with exchange-specific fallback:

| Priority | Env var | Example |
|----------|---------|---------|
| 1 | `EXCHANGE_API_KEY` | Generic (recommended) |
| 2 | `EXCHANGE_API_KEY_FILE` | Docker secret pattern |
| 3 | `KRAKEN_API_KEY` (legacy) | Exchange-specific fallback |
| 4 | `KRAKEN_API_KEY_FILE` | Exchange-specific file |

Use the `_load_credential()` helper from `kraken.py`:

```python
from spirit.exchange.kraken import _load_credential

api_key = _load_credential("EXCHANGE_API_KEY", "BINANCE_API_KEY")
```

This searches the generic name first, then your exchange-specific name, including `_FILE` variants.

**Never hardcode credentials.** Never log credentials. Never include them in error messages.

## Error handling

Your provider must:

1. **Raise `RuntimeError`** for unrecoverable errors (bad credentials, unknown pair, exchange rejects order)
2. **Handle transient errors internally** — rate limits (HTTP 429), timeouts, and network errors should be retried with exponential backoff. Don't let a single timeout bubble up to Spirit.
3. **Be thread-safe** — Spirit calls the provider from multiple pair threads simultaneously. Use no shared mutable state, or guard it with locks.

Example retry pattern (from `KrakenExchangeProvider._public_get`):

```python
for attempt in range(self._max_retries):
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            time.sleep(self._backoff_base ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()["result"]
    except requests.RequestException as e:
        if attempt < self._max_retries - 1:
            time.sleep(self._backoff_base ** attempt)
        else:
            raise RuntimeError(f"Failed after {self._max_retries} attempts: {e}")
```

## OHLC candles

`get_ohlc()` must return **only closed candles**. The current incomplete candle must be excluded. Spirit's trading logic assumes every candle in the buffer is final.

Candles must be sorted oldest-first (ascending timestamp).

The `count` parameter is a hint — return up to that many candles. It's OK to return fewer if the exchange doesn't have enough history.

## Testing your plugin

### 1. Protocol conformance

```python
from spirit.exchange.protocol import ExchangeProvider
from spirit.exchange.yourexchange import YourExchangeProvider

# Runtime protocol check
provider = YourExchangeProvider()
assert isinstance(provider, ExchangeProvider)
```

### 2. Return type verification

```python
ticker = provider.get_ticker("XBTUSD")
assert isinstance(ticker, Ticker)
assert ticker.bid > 0
assert ticker.ask >= ticker.bid
```

### 3. Integration smoke test

```python
# Fetch real data
candles = provider.get_ohlc("XBTUSD", interval=60, count=10)
assert len(candles) > 0
assert all(isinstance(c, OHLCCandle) for c in candles)
assert candles[0].timestamp < candles[-1].timestamp    # sorted oldest-first

info = provider.get_pair_info("XBTUSD")
assert info.ordermin > 0
assert info.lot_decimals > 0
```

### 4. Paper mode (no auth needed)

Paper mode only uses public methods: `get_ticker`, `get_ohlc`, `get_pair_info`. You can develop and test your plugin without exchange API keys by implementing these first.

## OrderExecutor ABC

The `OrderExecutor` ABC (`src/spirit/exchange/executor.py`) sits between the orchestrator and the `ExchangeProvider`. It handles Spirit-level trade lifecycle: equity tracking, fill polling, PG recording, and slippage measurement.

```
Orchestrator (main.py)
    │
    ▼ place_order / close_order / check_order_status / ...
OrderExecutor (ABC)
    ├── LiveOrderExecutor   ← Real fills via ExchangeProvider
    └── PaperOrderExecutor  ← Simulated fills from candle data
```

**You do NOT need to implement an OrderExecutor to add a new exchange.** The existing `LiveOrderExecutor` works with any `ExchangeProvider` implementation. The ABC exists to formalise the contract between the orchestrator and the two executor modes (live vs paper).

### OrderExecutor methods

| Method | Purpose |
|--------|---------|
| `place_order(trade_record)` | Market buy — waits for fill, updates trade_record |
| `close_order(open_trade, trade_record)` | Market sell — computes PnL, records to PG |
| `place_limit_order(trade_record, limit_price)` | Limit buy — returns immediately (no fill wait) |
| `check_order_status(txid, candle=None)` | Query fill status (live queries exchange, paper simulates from candle) |
| `cancel_order(txid)` | Cancel unfilled limit order |
| `finalize_limit_fill(txid, trade_record)` | Post-fill bookkeeping after limit order fills |
| `equity` (property) | Current portfolio value (cash + unrealised positions) |

### When would you implement a custom OrderExecutor?

Only if you need fundamentally different trade lifecycle semantics — for example, a backtest executor that fills from historical order books, or a DCA executor that splits large orders into tranches. For standard trading, `LiveOrderExecutor` + your `ExchangeProvider` is sufficient.

## Reference implementation

`src/spirit/exchange/kraken.py` is the reference implementation. Study it for:

- Pair name mapping pattern
- Retry + rate-limit handling
- Response parsing into standard types
- Credential loading with fallback chain
- Pair info caching

## File layout

```
src/spirit/exchange/
├── __init__.py          # get_exchange_provider() factory + OrderExecutor export
├── protocol.py          # ExchangeProvider Protocol + dataclasses
├── executor.py          # OrderExecutor ABC
├── kraken.py            # Kraken reference implementation
└── yourexchange.py      # Your plugin (you create this)
```

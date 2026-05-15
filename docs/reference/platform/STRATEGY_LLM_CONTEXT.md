---
name: STRATEGY_LLM_CONTEXT
description: Dense, machine-readable context for AI-assisted Spirit strategy authoring
audience: an LLM (Cursor / Claude Code / Copilot) loaded with this file as context, helping a human write a Spirit strategy
sister: WRITING_A_STRATEGY.md (human tutorial)
target: v2.2.0
---

# Spirit Strategy Authoring — LLM Context

You are helping a user write a trading strategy for the Spirit platform. This file is the authoritative contract. Use the human tutorial (`WRITING_A_STRATEGY.md`) for narrative; use this file for facts.

If a user asks "how do I X", answer from this file's tables. If you need to verify a signature, ask the user to run the relevant `grep` from §11.

---

## 1. Canonical files (read these for anything load-bearing)

| Path | What it defines |
|------|-----------------|
| `src/spirit/strategies/base.py` | `BaseStrategy` ABC + `DataRequirements` dataclass |
| `src/spirit/strategy_config.py` | `_STRATEGY_REGISTRY`, `_load_user_strategy`, `get_strategy` |
| `src/spirit/utils/data_provider.py` | `FrameworkDataProvider` + `IPDataProvider` + `DataProvider` Protocols |
| `src/spirit/pipeline/event_bus.py` | `PipelineEvent` dataclass |
| `src/spirit/strategies/zone_bounce.py` | The production reference strategy |
| `docs/reference/platform/WRITING_A_STRATEGY.md` | Human tutorial — narrative version of this file |
| `docs/reference/platform/PLATFORM_API.md` | HTTP+WS API (the gateway underneath DataProvider) |
| `docs/reference/platform/PERMISSIONS.md` | Tier matrix, RLS, instance scoping |

User strategies live at `~/.spirit/strategies/<name>.py` (override with `SPIRIT_STRATEGIES_DIR`).

---

## 2. The contract — `BaseStrategy` (verbatim signatures)

```python
from spirit.strategies.base import BaseStrategy, DataRequirements

class MyStrategy(BaseStrategy):
    # REQUIRED
    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        # kwargs may include: open_trade (TradeRecord | None)
        return {"entry": False, "exit": False, "details": {}}

    # OPTIONAL — defaults are no-ops; override only what you use
    def get_data_requirements(self) -> DataRequirements: ...
    def on_monitoring_tick(self, pair: str, interval: int, candle: dict, open_trade) -> Optional[dict]: ...
    def on_entry_scan_tick(self, pair: str, interval: int, candle: dict) -> Optional[dict]: ...
    def on_pipeline_event(self, event) -> None: ...
    def on_entry_confirmed(self, pair: str, signal, risk_decision) -> None: ...
    def on_exit_completed(self, pair: str, exit_reason: str, exit_price: float,
                          entry_price: float, exit_dt=None, net_pnl_pct: float = None) -> None: ...
    def validate_readiness(self) -> Tuple[bool, List[str]]: ...

    # CLASS PROPERTY (default False)
    uses_risk_gate: bool = False    # set True to opt into RiskGate sizing
```

### `evaluate_trade` return shape (strict)

```python
{
    "entry": bool,          # True opens a position
    "exit": bool,           # True closes the existing position
    "details": {
        "datetime": str,            # candle timestamp (ISO-8601, tz-aware UTC)
        "entry_price": float,       # REQUIRED if entry=True (in test mode: must be the candle's close)
        "symbol": str,              # the pair, e.g. "XBTUSD"
        # Strategy-specific fields are persisted to entry_context on the trade row.
        # If uses_risk_gate=True, populate details["signal"] (see §6).
    },
}
```

### `DataRequirements` (dataclass)

```python
@dataclass
class DataRequirements:
    pairs: List[str] = ["XBTUSD"]
    signal_interval: int = 60                # minutes — triggers evaluate_trade()
    monitoring_intervals: List[int] = []     # e.g. [1] for 1m exit checks while open
    warmup_candles: int = 720
```

If `get_data_requirements()` is omitted, the default reads `self.filter_pair` / `self.filter_interval` if present (else `XBTUSD` / 60), with `warmup_candles=720` and no monitoring intervals.

---

## 3. Lifecycle — when each hook fires

```
startup ──► get_data_requirements()              # once
            warmup loads N candles
            validate_readiness()                  # once → GREEN/YELLOW LIGHT log
            ──────────── steady state ────────────
each signal-interval close (e.g. every 60m):
            on_pipeline_event(event)              # for each upstream stage that completes
            evaluate_trade(pair, mode, **kwargs)  # always called

each monitoring-interval tick (e.g. every 1m), per pair:
            if open_trade is None:
                on_entry_scan_tick(pair, interval, candle)
            else:
                on_monitoring_tick(pair, interval, candle, open_trade)

after entry approval (RiskGate or otherwise):
            on_entry_confirmed(pair, signal, risk_decision)

after exit fires:
            on_exit_completed(pair, exit_reason, exit_price, entry_price, exit_dt, net_pnl_pct)
```

**Pair routing (important):** the orchestrator instantiates **one strategy object per pair**. Per-pair locks serialise `evaluate_trade` + monitoring + `on_pipeline_event` — they never interleave for the same pair. Cross-pair instances run in separate threads; do not share mutable state without locks.

---

## 4. `DataProvider` — method table

Get the singleton via `from spirit.utils.data_provider import get_data_provider; dp = get_data_provider()`. Backed by the API gateway (`https://api.tradebot.live/v1/...`). The strategy never touches PostgreSQL directly.

### Framework (all tiers, including free)

| Method | Signature | Returns |
|--------|-----------|---------|
| `get_ohlc` | `(pair, interval, *, start=None, end=None, limit=5000, order="asc")` | `list[dict]` — keys: pair, interval, datetime, open, high, low, close, vwap, volume, count |
| `get_state` | `(key)` | `Any` — value from `spirit_state` |
| `put_state` | `(key, value)` | `None` |
| `ensure_table` | `(table_name, create_sql)` | `bool` — api-backed: no-op (True) |
| `get_performance` | `(*, pair=None, strategy=None, source=None, run_id=None, start=None, end=None, limit=5000)` | `list[dict]` — `strategy_performance` rows (your instance only — RLS-scoped) |
| `write_performance` | `(data: dict)` | `int` — rows affected |
| `write_performance_batch` | `(trades: list[dict])` | `int` — rows affected |
| `clear_performance` | `(*, run_id)` | `int` — rows deleted |
| `get_strategy_metrics` | `(strategy_name, as_of_date, *, baseline_days=90, current_days=14, recent_days=7)` | `dict` — win-rate + streak metrics |
| `write_heartbeat` | `(daemon_id, *, instance, status="ok", metadata=None, run_id="live")` | `int` |
| `get_pairs` | `(instance=None)` | `list[dict]` — active pairs registry |

### IP (subscription + pro tiers only — `403` on free)

| Method | Signature | Purpose |
|--------|-----------|---------|
| `get_zones` | `(pair, interval=60, *, active=None, min_strength=None)` | D-Limit support/resistance zones |
| `get_zone_touches` | `(pair, *, zone_ids=None, start=None, end=None, result_filter=None, limit=5000)` | Zone touch events (bounce/break) |
| `get_bounce_events` | `(pair, interval, *, min_prior_touches=2, dedup_hours=0, start=None, limit=10000)` | Deduped bounce events |
| `get_bounce_references` | `(*, pair=None, regime=None, min_dt=None)` | `bounce_reference` rows |
| `get_dlimit` | `(pair, interval=60, *, at=None, start=None, end=None, limit=5000)` | D-Limit indicator rows |
| `get_dlimit_latest` | `(pair, interval=60, *, before=None)` | Most recent D-Limit row |
| `get_consolidation` | `(pair, interval=60, *, at=None, start=None, end=None, limit=5000)` | Consolidation signals |
| `get_orderbook` | `(pair, *, start=None, end=None, limit=100)` | Orderbook depth metrics |
| `get_orderbook_events_summary` | `(pair, *, lookback_minutes=15, at=None)` | Grouped event counts |
| `get_cooldown_calibration` | `(pair, *, interval=60, lookback_months=12)` | Break→recovery events for cooldown calibrator |
| `get_risk_gate_calibration` | `(pair, *, calibrate_before=None)` | Resolved risk-gate decisions (regime, rr_ratio, win flags) |
| `get_entry_quality_calibration` | `(dimension, *, as_of=None)` | Bucketed win-rate + MFE across various entry-quality dimensions; see `PERMISSIONS.md` for the current set |
| `get_volatility_context` | `(pair, interval, *, as_of=None)` | `dict` with atr_14d / atr_30d / atr_90d, or `None` |
| `get_composite_outcomes` | `(*, calibrate_before=None)` | Per-trade outcome rows for threshold sweeps |
| `write_thesis` / `update_thesis_outcome` / `update_thesis_checks` | `(data: dict)` | Trade thesis lifecycle |

**All datetimes returned by DataProvider are tz-aware UTC (Rule 11). All numerics are Python `float` (not `Decimal`).**

---

## 5. `PipelineEvent` — what `on_pipeline_event` receives

```python
@dataclass
class PipelineEvent:
    stage: str                    # 'ohlc', 'dlimit_60m', 'dlimit_15m', etc.
    pair: str                     # 'XBTUSD'
    interval_minutes: int         # 60
    candle_dt: str                # ISO-8601 string (tz-aware UTC)
    rows_affected: int = 0
    duration_ms: int = 0
    metadata: Dict[str, Any] = {}
    row: Optional[Dict[str, Any]] = None   # canonical stage-row payload
```

**The `row` field is the canonical row that the upstream stage just wrote.** Read it directly — do **not** issue a fresh fetch from `on_pipeline_event` to get the same data; strategies that re-fetched used to race against commit visibility and read stale or missing rows. If `row` is `None` (oversized payload, very rare), fall back to `dp.get_dlimit_latest(...)`.

```python
def on_pipeline_event(self, event):
    if event.stage.startswith("dlimit_60m") and event.row:
        self._cache[event.candle_dt] = event.row    # no fetch
```

---

## 6. RiskGate opt-in

```python
class MyStrategy(BaseStrategy):
    uses_risk_gate = True    # class attr, not an instance method
```

When `True`, after `evaluate_trade` returns `entry=True` the orchestrator:
1. Pulls `signal = details["signal"]`
2. Calls `risk_gate.evaluate(signal)` → `RiskDecision`
3. If `risk_decision.trade is False` → entry dropped (logged with reason)
4. Else → `trade_record.buy_amount = risk_decision.position_size_usd`, proceeds

`details["signal"]` should expose: `confidence_score` (0-100), `suggested_stop`, `suggested_target`, `regime`, `slope_angle`, `capture_rate`, `pair`, `datetime`, `price`. Source: `src/spirit/indicators/decision_engine/engine/risk_gate.py`.

When `uses_risk_gate = False` (default) the strategy fully owns sizing — set `trade_record.buy_amount` itself or rely on `TRADE_USD_AMOUNT` from `config/spirit.yaml`.

---

## 7. Loading + env vars

| Var | Purpose | Default |
|-----|---------|---------|
| `SPIRIT_STRATEGY` | Filename without `.py`, or alias for a built-in | none → monitor-only mode |
| `SPIRIT_STRATEGY_PARAMS` | JSON dict, kwargs to `__init__` | `{}` |
| `SPIRIT_STRATEGIES_DIR` | Override user dir | `~/.spirit/strategies` |
| `SPIRIT_INSTANCE` | Instance label (e.g. `customer-47`) | none (required for cloud writes) |
| `SPIRIT_API_KEY` | Gateway API key | none (required) |
| `SPIRIT_API_URL` | Gateway base URL | `https://api.tradebot.live/v1` |

### Resolution order (`get_strategy` in `strategy_config.py`)
1. Built-in production: `src/spirit/strategies/` (currently just `zone_bounce`)
2. Built-in experimental: `src/spirit/strategies/experimental/` (e.g. `macd_cross`, `rsi_reversion`)
3. User dir: `$SPIRIT_STRATEGIES_DIR/<name>.py`

### Loader behaviour (`_load_user_strategy`)
- Imports the file via `importlib.util.spec_from_file_location`
- Picks the **first concrete `BaseStrategy` subclass** defined in the module (skips imports of `BaseStrategy` itself, abstracts, and classes whose `__module__` doesn't match)
- Convention: **one strategy per file**, file-name = registry name
- Constructor receives `**SPIRIT_STRATEGY_PARAMS`; if a kwarg the constructor doesn't accept is present, the loader retries with unknown keys filtered out (only when `**kwargs` is not in the signature). Defensive: accept `**_kwargs` to handle orchestrator-injected context like `filter_pair`.

---

## 8. Tier matrix (short version — full in `PERMISSIONS.md`)

| Tier | DataProvider methods unlocked |
|------|-------------------------------|
| `free` | All Framework methods. **All IP methods → 403.** Trades stored in local SQLite (no `/v1/performance` writes). |
| `subscription` | Framework + D-Limit (`get_dlimit*`, `get_zones`, `get_zone_touches`, `get_bounce_events`, `get_consolidation`), bounce_reference, calibration reads. Cloud-side trade storage. Live mode allowed. |
| `pro` | Subscription + scorer outputs, full orderbook (`get_orderbook*`), wall lifecycles, IP write methods. |

If a method 403s, the response includes the required tier — propagate as a clear error, don't retry.

---

## 9. Hard rules (non-negotiable)

- **Pipeline sync:** strategies that depend on D-Limit / zones / consolidation must subscribe via `on_pipeline_event` and tolerate stale data with explicit logging, not silent waits. Never block on a synchronous "wait for fresh data" — the api-mode pipeline is push-based.
- **No static market thresholds:** "ATR > 0.5", "slope > 30°" hardcoded constants are a code-review red flag. Derive thresholds from data: percentiles over the last N days, neighbour-pair distributions, regime-conditioned histograms. Use `get_volatility_context`, `get_entry_quality_calibration`, `get_composite_outcomes`.
- **Real-time signals first:** prefer D-Limit zones, orderbook deltas, and bounce events. Avoid lagging indicators (EMA/SMA crossovers) as primary signals — they're allowed as confirmations, never as triggers.
- **Type / timezone normalisation:** DataProvider returns `float` (not `Decimal`) and tz-aware UTC datetimes. If you stringify and re-parse, always end up at tz-aware UTC. Naive datetimes once bit production hard at DST; cache keys must use UTC strftime.

---

## 10. DO / DO NOT

**DO**
- Accept `**_kwargs` in `__init__` — the orchestrator may pass `filter_pair`, `filter_interval`, etc.
- Set `self.filter_pair = pair` (and `self.filter_interval`) in `__init__` so the default `get_data_requirements` works.
- Read `event.row` in `on_pipeline_event`, fall back to `dp.get_*_latest(...)` only if `row is None`.
- Log with `[TAG]` prefixes and the pair: `f"[{pair}][ENTRY] score={score:.2f}"`.
- Use a fresh `run_id = f"backtest-{uuid.uuid4()}"` for every backtest write; reusing run_ids corrupts the calibrator buffers.
- Soak in `paper` mode for ≥24h before flipping to `live`. Watch for zero `[FIELD-COVERAGE]` / `[PAYLOAD-MISS]` warnings.
- Smoke-test by importing the file and calling `evaluate_trade('XBTUSD', 'test')` outside Spirit before restarting the service.

**DO NOT**
- Hardcode a market threshold without deriving it from data (Rule 9).
- Issue a fetch in `on_pipeline_event` to get the row that just landed — read `event.row` (see §5).
- Share mutable dicts across pair instances without an explicit `threading.Lock` — each pair is its own thread.
- Write to PostgreSQL directly from a strategy. There is no PG connection in api-mode; use DataProvider methods.
- Use `Decimal` arithmetic. DataProvider normalises NUMERIC → float. Mixing causes type errors.
- Use naive datetimes. Always tz-aware UTC.
- Calibrate from experimental or ML-training tables. Production strategies read production tables only.
- Put more than one concrete `BaseStrategy` subclass in a user-strategy file — the loader picks the first one and silently ignores the rest.
- Skip `validate_readiness` for a strategy with non-trivial warmup needs; the GREEN/YELLOW LIGHT log is your post-warmup sanity check.

---

## 11. Verification greps (ask the user to run these)

When you need to confirm a signature or default rather than rely on this file's snapshot:

```bash
# Exact ABC signatures
grep -n "def \|^class " src/spirit/strategies/base.py

# DataProvider methods (Framework + IP)
grep -n "def get_\|def write_\|def update_\|def clear_\|def ensure_" src/spirit/utils/data_provider.py

# PipelineEvent fields
grep -n "stage:\|pair:\|interval_minutes:\|candle_dt:\|metadata:\|row:" src/spirit/pipeline/event_bus.py

# Built-in strategy registry
grep -n "_STRATEGY_REGISTRY\|module\|class" src/spirit/strategy_config.py | head -30

# Reference strategy
sed -n '1,60p' src/spirit/strategies/zone_bounce.py
```

If a user pastes output that disagrees with this file, **trust the source**. Code is canonical; this file is a snapshot.

---

## 12. Minimal correct skeleton

```python
# ~/.spirit/strategies/my_strategy.py
from spirit.strategies.base import BaseStrategy, DataRequirements
from spirit.utils.data_provider import get_data_provider


class MyStrategy(BaseStrategy):
    uses_risk_gate = False

    def __init__(self, filter_pair: str = "XBTUSD", filter_interval: int = 60,
                 my_threshold: float = 1.5, **_kwargs):
        self.filter_pair = filter_pair
        self.filter_interval = int(filter_interval)
        self.my_threshold = float(my_threshold)
        self._cache: dict = {}

    def get_data_requirements(self) -> DataRequirements:
        return DataRequirements(
            pairs=[self.filter_pair],
            signal_interval=self.filter_interval,
            monitoring_intervals=[1],
            warmup_candles=720,
        )

    def evaluate_trade(self, pair, mode="test", **kwargs):
        if kwargs.get("open_trade") is not None:
            return {"entry": False, "exit": False, "details": {}}
        candles = get_data_provider().get_ohlc(pair=pair, interval=60, limit=24, order="desc")
        if not candles:
            return {"entry": False, "exit": False, "details": {}}
        latest = candles[0]
        # your signal here
        return {"entry": False, "exit": False, "details": {
            "datetime": latest["datetime"],
            "entry_price": float(latest["close"]),
            "symbol": pair,
        }}

    def on_pipeline_event(self, event):
        if event.stage.startswith("dlimit_60m") and event.row:
            self._cache[event.candle_dt] = event.row

    def on_monitoring_tick(self, pair, interval, candle, open_trade):
        if open_trade is None:
            return None
        # exit logic; return exit dict or None
        return None
```

Configure with `SPIRIT_STRATEGY=my_strategy` and `SPIRIT_STRATEGY_PARAMS='{"my_threshold": 2.0}'`. Restart Spirit. Tail logs for `Strategy loaded: my_strategy (MyStrategy)`.

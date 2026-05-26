# Spirit

[![PyPI](https://img.shields.io/pypi/v/spirit-platform.svg)](https://pypi.org/project/spirit-platform/)
[![Python](https://img.shields.io/pypi/pyversions/spirit-platform.svg)](https://pypi.org/project/spirit-platform/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Downloads](https://static.pepy.tech/badge/spirit-platform)](https://pepy.tech/project/spirit-platform)

Spirit is a programmable Python platform for algorithmic cryptocurrency trading — a playground for building and testing your own strategies. It runs on your own hardware, talks to exchanges directly, and you own the strategy logic end-to-end.

The framework is open source under Apache-2.0. Spirit handles the orchestration: market data, order placement, lifecycle hooks, and crash recovery. As your strategies mature, Plus and Pro open up access to our custom technical indicators and cloud storage for backtesting.

---

## Quick start

On Ubuntu / Debian (and most modern Linux):

```bash
sudo apt install -y pipx
pipx ensurepath
pipx install spirit-platform
```

Then open a new shell and run:

```bash
python3 -m spirit.setup     # interactive setup wizard
spirit --mode paper         # start paper trading
```

> **Other install paths (venv, etc.) and troubleshooting:** see [INSTALL.md](INSTALL.md).

Run the wizard once (it asks your tier, instance name, and an optional Kraken API key for live trading), pick one of the bundled examples (`sma_crossover` or `macd_demo`) or drop your own file in `~/.spirit/strategies/`, and Spirit starts paper-trading.

To go live, swap `--mode paper` for `--mode live` once you've sanity-checked the paper P&L.

> **OHLC data ownership (v2.2.4+).** Spirit's local OHLC store is yours — Free instances persist candles in `~/.spirit/<instance>/spirit.db`. When Spirit starts up after downtime, it fills the gap (up to 12 hours of 1-minute candles per boot) from the exchange automatically. For longer gaps, run `python3 -m spirit.backfill <kraken-csv>` once with a Kraken CSV export. The boot catch-up adds ~15–20 seconds for a typical multi-pair multi-interval config; configure with `SPIRIT_OHLC_CATCHUP_INTERVALS` (default `60`).

---

## Tiers and pricing

See [www.tradebot.live/pricing](https://www.tradebot.live/pricing/) for tier details, pricing, and what each level adds.

---

## Writing a strategy

Strategies subclass `BaseStrategy` and implement `evaluate_trade()`. Everything else is optional; opt into lifecycle hooks as you need them.

```python
from spirit.strategies.base import BaseStrategy

class MyStrategy(BaseStrategy):
    def evaluate_trade(self, pair: str, mode: str = "test", **kwargs):
        # Return {"entry": bool, "exit": bool, "details": {...}}
        return {"entry": False, "exit": False, "details": {}}
```

Optional hooks the orchestrator will call when configured:
`on_monitoring_tick`, `on_entry_confirmed`, `on_exit_completed`,
`validate_readiness`, `get_data_requirements`. Properties for tier-aware
behaviour: `uses_risk_gate`, `required_capabilities`. See the bundled
examples; both files are heavily commented teaching artifacts:

- **[`src/spirit/strategies/examples/sma_crossover.py`](src/spirit/strategies/examples/sma_crossover.py)**: minimum viable. Subclass + `evaluate_trade`, nothing else. Read this first.
- **[`src/spirit/strategies/examples/macd_demo.py`](src/spirit/strategies/examples/macd_demo.py)**: full lifecycle tour. Multi-interval, monitoring-tick ATR stop, entry-confirmed state-stash, paper-by-default guard. Read this to see every hook in action.

Drop your own under `~/.spirit/strategies/` and Spirit picks it up at next startup.

For more information visit [www.tradebot.live](https://www.tradebot.live).

---

## Project structure

```
src/spirit/
  main.py                  - entrypoint
  setup.py                 - first-run wizard
  config.py                - .env / yaml loader
  trade_signal.py          - signal dataclass
  trade_status.py          - status dataclass
  trade_types.py           - TradeRecord dataclass (used by strategies)
  exchange/                - exchange adapter protocol + Kraken impl
  pipeline/                - WebSocket event bus, freshness cache, daemon health
  storage/                 - local SQLite schema (Free tier)
  strategies/
    base.py                - BaseStrategy abstract base class
    examples/sma_crossover - minimal reference strategy
    examples/macd_demo     - full-lifecycle reference strategy
  utils/                   - data providers, OHLC buffer, paper executor, etc.
```

The framework runs any strategy you write against any data source you plug in. Plus and Pro plans add bundled indicators served via the gateway API — the D-Limit suite, V3 confidence scorer, and risk-gate calibrators — without changing the framework you're building against. See [tradebot.live](https://www.tradebot.live) for what each tier unlocks.

---

## Supported exchanges

Spirit includes a Kraken adapter by default. You can write your own by implementing the `ExchangeProvider` protocol in `src/spirit/exchange/protocol.py` — see `docs/reference/EXCHANGE_PLUGIN_GUIDE.md` for the full guide.

We'll be releasing more exchange adapters over time. If there's a specific exchange you'd like us to prioritise, open a GitHub issue and we'll see if we can fit it in.

---

## Status

Current release: see the [latest tag on PyPI](https://pypi.org/project/spirit-platform/) or [GitHub Releases](https://github.com/timoz10/spirit-platform/releases) for the changelog. The Apache-2.0 framework is stable; Plus and Pro infrastructure is in active development.

---

## License

Apache-2.0. See [LICENSE](LICENSE).

The framework is free to use, modify, and redistribute. Plus and Pro subscriptions cover the hosted data and indicator infrastructure; the framework code itself doesn't depend on a subscription to run.

---

## Support

- Portal + key management: [portal.tradebot.live](https://portal.tradebot.live)
- Issue tracker: GitHub Issues on this repo
- For commercial / integration questions: support@tradebot.live

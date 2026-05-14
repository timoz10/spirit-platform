# Spirit

Spirit is a programmable cryptocurrency trading platform — a playground for building and testing your own trading strategies in Python. It runs on your own hardware, talks to exchanges directly, and you own the strategy logic end-to-end.

The framework is open source under Apache-2.0. Spirit handles the orchestration — market data, order placement, lifecycle hooks, crash recovery. As your strategies mature, Plus and Pro open up access to our custom technical indicators and cloud storage for backtesting.

---

## Quick start

```bash
pip install spirit-platform
python3 -m spirit.setup     # interactive setup wizard
python3 -m spirit.main --mode paper
```

Run the wizard once (it asks your tier, instance name, and Kraken API keys), pick one of the bundled examples (`sma_crossover` or `macd_demo`) or drop your own file in `~/.spirit/strategies/`, and Spirit starts paper-trading.

To go live, swap `--mode paper` for `--mode live` once you've sanity-checked the paper P&L.

---

## Tiers

| Tier | Price | What you get |
|------|-------|--------------|
| **Free** | £0 | Local SQLite storage. OHLC data direct from your exchange (Kraken module included). Run Spirit in paper mode and learn the framework. No live trading, no D-Limit access. |
| **Plus** | £15/mo | Full D-Limit indicators (zones, trend states, swing detection), cloud-backed trade history with crash recovery, live trading enabled, push-based WebSocket events. The default for serious paper-trading or running real money. |

A **Pro** tier is on the roadmap once the V3 confidence scorer, full orderbook intelligence, and our hosted OHLC feed are all production-ready. Until then, Plus is the top of the stack.

Get a key at [portal.tradebot.live](https://portal.tradebot.live).

---

## Writing a strategy

Strategies subclass `BaseStrategy` and implement `evaluate_trade()`. Everything else is optional — opt into lifecycle hooks as you need them.

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
examples — both files are heavily commented teaching artifacts:

- **[`src/spirit/strategies/examples/sma_crossover.py`](src/spirit/strategies/examples/sma_crossover.py)** — minimum viable. Subclass + `evaluate_trade`, nothing else. Read this first.
- **[`src/spirit/strategies/examples/macd_demo.py`](src/spirit/strategies/examples/macd_demo.py)** — full lifecycle tour. Multi-interval, monitoring-tick ATR stop, entry-confirmed state-stash, paper-by-default guard. Read this to see every hook in action.

Drop your own under `~/.spirit/strategies/` and Spirit picks it up at next startup.

For the conceptual walkthrough, see the blog post [*Anatomy of a Spirit Strategy*](https://www.tradebot.live/) which walks `macd_demo.py` top-to-bottom.

---

## Project structure

```
src/spirit/
  main.py                  - entrypoint
  setup.py                 - first-run wizard
  config.py                - .env / yaml loader
  trade_signal.py          - signal dataclass
  trade_status.py          - status dataclass
  exchange/                - exchange adapter protocol + Kraken impl
  pipeline/                - WebSocket event bus, freshness cache, daemon health
  storage/                 - local SQLite schema (Free tier)
  strategies/
    base.py                - BaseStrategy abstract base class
    examples/sma_crossover - minimal reference strategy
    examples/macd_demo     - full-lifecycle reference strategy
  utils/                   - data providers, OHLC buffer, paper executor, etc.
```

Implementation details for the bundled D-Limit indicators, V3 scorer, and risk-gate calibrators live behind the gateway API — they're the IP that Plus / Pro subscriptions pay for. The framework you see here can run any strategy you write, against any data source you plug in.

---

## Bring your own exchange

Spirit ships with a Kraken adapter. To target a different exchange, implement the `ExchangeProvider` protocol in `src/spirit/exchange/protocol.py` and register it. See `docs/reference/EXCHANGE_PLUGIN_GUIDE.md` for the full guide.

---

## Status

- **v2.2.1** — first public release. Free + Plus tiers, single-machine deploy, paper or live mode.
- Production canary on Hetzner runs the same framework code, paper-mode, 24/7 — same orchestrator, same data providers, same lifecycle hooks. Strategy differs (canary runs a private IP strategy), but the platform you install is the same platform we run.

---

## License

Apache-2.0. See [LICENSE](LICENSE).

The framework is free to use, modify, and redistribute. Plus / Pro subscriptions cover the hosted data and indicator infrastructure — the framework code itself doesn't depend on a subscription to run.

---

## Support

- Portal + key management: [portal.tradebot.live](https://portal.tradebot.live)
- Issue tracker: GitHub Issues on this repo
- For commercial / integration questions: tim@tradebot.live

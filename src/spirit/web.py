"""
Spirit Web Dashboard — FastAPI status server for remote monitoring.

Runs in a daemon thread alongside Spirit's trading loop. Provides:
  /              HTML dashboard (auto-refresh, no JS framework)
  /api/status    JSON: mode, uptime, equity, open trade, candles processed
  /api/signals   JSON: recent signals with scores and risk decisions
  /api/trades    JSON: recent trades from PG strategy_performance
  /api/health    JSON: PG status, OHLC freshness, uptime

Activation:
  SPIRIT_WEB=1          Enable web server (off by default)
  SPIRIT_WEB_PORT=8377  Port (default 8377)
  SPIRIT_WEB_AUTH=u:p   Optional basic auth (user:password)
"""

import os
import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Global state — written by Spirit main loop, read by web handlers
_state = {
    'mode': 'unknown',
    'start_time': time.time(),
    'candles_processed': 0,
    'equity': 0.0,
    'open_trade': None,
    'last_candle_time': None,
    'pair': 'XBTUSD',
}
_signals: deque = deque(maxlen=100)
_decisions: deque = deque(maxlen=100)
_lock = threading.Lock()


def update_state(**kwargs):
    """Thread-safe state update. Called from Spirit main loop."""
    with _lock:
        _state.update(kwargs)


def record_signal(signal_dict: dict):
    """Record a signal for the dashboard. Called from Spirit callback."""
    signal_dict['_recorded_at'] = datetime.now(timezone.utc).isoformat()
    with _lock:
        _signals.append(signal_dict)


def record_decision(decision_dict: dict):
    """Record a risk decision for the dashboard. Called from Spirit callback."""
    decision_dict['_recorded_at'] = datetime.now(timezone.utc).isoformat()
    with _lock:
        _decisions.append(decision_dict)


def increment_candles():
    """Increment candle counter. Called on each new candle."""
    with _lock:
        _state['candles_processed'] += 1


def _get_state_snapshot() -> dict:
    """Return a copy of current state."""
    with _lock:
        return dict(_state)


def _get_signals(limit: int = 50) -> list:
    """Return recent signals."""
    with _lock:
        return list(_signals)[-limit:]


def _get_decisions(limit: int = 50) -> list:
    """Return recent decisions."""
    with _lock:
        return list(_decisions)[-limit:]


def _check_pg_health() -> dict:
    """Quick PG connectivity check."""
    try:
        from utils.db_connection import execute_query
        row = execute_query("SELECT 1 AS ok", fetch='one')
        return {'connected': True}
    except Exception as e:
        return {'connected': False, 'error': str(e)}


def _get_recent_trades(limit: int = 20) -> list:
    """Fetch recent trades from PG strategy_performance."""
    try:
        from utils.db_connection import execute_query
        rows = execute_query(
            "SELECT * FROM strategy_performance "
            "WHERE source IN ('paper', 'live') "
            "ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        )
        if not rows:
            return []
        result = []
        for r in rows:
            entry = dict(r)
            # Convert datetimes to strings for JSON
            for k, v in entry.items():
                if hasattr(v, 'isoformat'):
                    entry[k] = v.isoformat()
            result.append(entry)
        return result
    except Exception as e:
        logger.debug(f"Failed to fetch trades: {e}")
        return []


def _build_app():
    """Build and return the FastAPI app."""
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import HTMLResponse, JSONResponse
    import base64

    app = FastAPI(title="Spirit Dashboard", docs_url=None, redoc_url=None)

    # Optional basic auth
    auth_config = os.environ.get('SPIRIT_WEB_AUTH', '')

    def _check_auth(request: Request) -> bool:
        if not auth_config:
            return True
        auth_header = request.headers.get('authorization', '')
        if not auth_header.startswith('Basic '):
            return False
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            return decoded == auth_config
        except Exception:
            return False

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if not _check_auth(request):
            return Response(
                content='Unauthorized',
                status_code=401,
                headers={'WWW-Authenticate': 'Basic realm="Spirit"'},
            )
        return await call_next(request)

    @app.get("/api/status")
    async def api_status():
        state = _get_state_snapshot()
        uptime = time.time() - state.get('start_time', time.time())
        return {
            'mode': state.get('mode'),
            'uptime_seconds': round(uptime),
            'uptime_human': _format_uptime(uptime),
            'equity': state.get('equity'),
            'open_trade': state.get('open_trade'),
            'last_candle_time': state.get('last_candle_time'),
            'candles_processed': state.get('candles_processed'),
            'pair': state.get('pair'),
        }

    @app.get("/api/signals")
    async def api_signals():
        signals = _get_signals(50)
        decisions = _get_decisions(50)
        return {
            'signals': signals,
            'decisions': decisions,
            'total_signals': len(_signals),
            'total_decisions': len(_decisions),
        }

    @app.get("/api/trades")
    async def api_trades():
        trades = _get_recent_trades(20)
        return {'trades': trades, 'count': len(trades)}

    @app.get("/api/health")
    async def api_health():
        state = _get_state_snapshot()
        uptime = time.time() - state.get('start_time', time.time())
        pg = _check_pg_health()
        return {
            'status': 'running',
            'uptime_seconds': round(uptime),
            'pg': pg,
            'candles_processed': state.get('candles_processed'),
            'last_candle_time': state.get('last_candle_time'),
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        state = _get_state_snapshot()
        uptime = time.time() - state.get('start_time', time.time())
        signals = _get_signals(10)
        decisions = _get_decisions(10)

        open_trade_html = "None"
        if state.get('open_trade'):
            ot = state['open_trade']
            if isinstance(ot, dict):
                open_trade_html = f"Entry: ${ot.get('entry_price', '?'):.2f} at {ot.get('entry_datetime', '?')}"
            else:
                open_trade_html = str(ot)

        signals_rows = ""
        for s in reversed(signals):
            score = s.get('confidence_score', '?')
            zone = s.get('zone_id', '?')
            dt = s.get('datetime', s.get('_recorded_at', '?'))
            signals_rows += f"<tr><td>{dt}</td><td>{score}</td><td>{zone}</td></tr>\n"

        decisions_rows = ""
        for d in reversed(decisions):
            trade = d.get('trade', '?')
            size = d.get('position_size_usd', 0)
            tier = d.get('profile_tier', '?')
            reason = d.get('skip_reason', '-')
            dt = d.get('_recorded_at', '?')
            cls = 'trade' if trade else 'skip'
            decisions_rows += (
                f"<tr class='{cls}'><td>{dt}</td><td>{'TRADE' if trade else 'SKIP'}</td>"
                f"<td>${size:.0f}</td><td>{tier}</td><td>{reason or '-'}</td></tr>\n"
            )

        html = f"""<!DOCTYPE html>
<html>
<head>
<title>Spirit Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
  body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; margin: 0; }}
  h1 {{ color: #00d4ff; margin-bottom: 5px; }}
  .subtitle {{ color: #888; margin-bottom: 20px; }}
  .card {{ background: #16213e; border-radius: 8px; padding: 15px; margin-bottom: 15px; }}
  .card h2 {{ color: #00d4ff; margin-top: 0; font-size: 14px; text-transform: uppercase; }}
  .metric {{ display: inline-block; margin-right: 30px; }}
  .metric .label {{ color: #888; font-size: 12px; }}
  .metric .value {{ font-size: 20px; font-weight: bold; }}
  .value.positive {{ color: #00ff88; }}
  .value.negative {{ color: #ff4444; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: #888; border-bottom: 1px solid #333; padding: 5px; }}
  td {{ padding: 5px; border-bottom: 1px solid #222; }}
  tr.trade td {{ color: #00ff88; }}
  tr.skip td {{ color: #ff8844; }}
</style>
</head>
<body>
<h1>Spirit Trading Dashboard</h1>
<p class="subtitle">Auto-refresh every 30s | Mode: {state.get('mode', '?')} | Pair: {state.get('pair', '?')}</p>

<div class="card">
  <h2>Status</h2>
  <div class="metric">
    <div class="label">Uptime</div>
    <div class="value">{_format_uptime(uptime)}</div>
  </div>
  <div class="metric">
    <div class="label">Equity</div>
    <div class="value">${state.get('equity', 0):.2f}</div>
  </div>
  <div class="metric">
    <div class="label">Candles</div>
    <div class="value">{state.get('candles_processed', 0)}</div>
  </div>
  <div class="metric">
    <div class="label">Open Trade</div>
    <div class="value">{open_trade_html}</div>
  </div>
</div>

<div class="card">
  <h2>Recent Decisions ({len(decisions)} total)</h2>
  <table>
    <tr><th>Time</th><th>Action</th><th>Size</th><th>Tier</th><th>Reason</th></tr>
    {decisions_rows or '<tr><td colspan="5">No decisions yet</td></tr>'}
  </table>
</div>

<div class="card">
  <h2>Recent Signals ({len(signals)} total)</h2>
  <table>
    <tr><th>Time</th><th>Score</th><th>Zone</th></tr>
    {signals_rows or '<tr><td colspan="3">No signals yet</td></tr>'}
  </table>
</div>

</body>
</html>"""
        return HTMLResponse(content=html)

    return app


def _format_uptime(seconds: float) -> str:
    """Format seconds into human-readable uptime."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def start_web_server(port: int = 8377):
    """
    Start the FastAPI web server in a daemon thread.
    Called from spirit_main.py when SPIRIT_WEB=1.
    """
    app = _build_app()

    def _run():
        import uvicorn
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
        )

    thread = threading.Thread(target=_run, name="SpiritWebServer", daemon=True)
    thread.start()
    logger.info(f"[WEB] Dashboard started on port {port}")
    return thread

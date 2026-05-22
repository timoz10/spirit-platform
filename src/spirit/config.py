"""
system_config.py

Centralized, environment-aware configuration for kraken-bot.

How to use on different servers:
- Set KRAKEN_BOT_BASE_DIR to the project base directory (e.g., /home/ubuntu/app)
- Optionally set DB_PATH or LOG_FILE to override specific paths.
- Optionally set LOGGING_LEVEL (INFO/DEBUG/...).
"""

import os

from spirit.utils.config_loader import get_config

# --- Base directory resolution ---
# Repo layout: <repo>/src/spirit/config.py — repo root is three dirs up.
# Resolving from the package dir would land logs/ and the SQLite DB inside
# src/spirit/, which then gets shipped in the docker build context (see #466).
DEFAULT_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DIR = os.environ.get("KRAKEN_BOT_BASE_DIR", DEFAULT_BASE_DIR)


def _resolve_log_file() -> str:
    """Pick a log path that survives pipx upgrades.

    Why: 'three dirs up from config.py' lands inside the pipx venv on
    installed wheels (~/.local/share/pipx/venvs/spirit-platform/.../logs/),
    and pipx wipes the venv on every upgrade — silently destroying customer
    diagnostic history. See #799.

    Resolution order:
      1. LOG_FILE env var (explicit override; back-compat).
      2. SPIRIT_INSTANCE set AND ~/.spirit/<instance>/ exists →
         ~/.spirit/<instance>/logs/spirit_syslog.log (matches SqliteDataProvider
         layout — DB already lives at ~/.spirit/<instance>/spirit.db).
      3. Fall back to BASE_DIR/logs/spirit_syslog.log (repo dev layout).
    """
    override = os.environ.get("LOG_FILE")
    if override:
        return override
    instance = os.environ.get("SPIRIT_INSTANCE", "").strip()
    if instance:
        instance_dir = os.path.join(os.path.expanduser("~"), ".spirit", instance)
        if os.path.isdir(instance_dir):
            return os.path.join(instance_dir, "logs", "spirit_syslog.log")
    return os.path.join(BASE_DIR, "logs", "spirit_syslog.log")


# --- Paths (overridable via env) ---
# DEPRECATED: DB_PATH is only kept for backward compatibility with strategies
# that still import it. Spirit V2 uses SpiritContext (in-memory + PG) and
# does not use SQLite. Will be removed once all strategies are migrated.
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "kraken_ohlc.db"))
LOG_FILE = _resolve_log_file()

# --- Logging ---
# Logging level: 'INFO', 'DEBUG', 'WARNING', 'ERROR', 'CRITICAL'
LOGGING_LEVEL = get_config('LOGGING_LEVEL', os.environ.get('LOG_LEVEL', 'INFO'))

# --- Kraken / OHLC settings ---
KRAKEN_PAIR = get_config('KRAKEN_PAIR', 'XBTUSD')
KRAKEN_OHLC_COUNT = int(get_config('KRAKEN_OHLC_COUNT', 720))
KRAKEN_OHLC_INTERVAL = int(get_config('KRAKEN_OHLC_INTERVAL', 60))  # minutes (60min for ML strategy)
KRAKEN_OHLC_BUFFER_DELAY_SECONDS = int(os.environ.get('KRAKEN_OHLC_BUFFER_DELAY_SECONDS', 5))

# --- Trading Sizing ---
# USD notional per trade; used to compute buy_amount = USD / price
# Override with env TRADE_USD_AMOUNT to control live/test sizing (e.g., export TRADE_USD_AMOUNT=1000)
TRADE_USD_AMOUNT = float(get_config('TRADE_USD_AMOUNT', 1000.0))

# --- Public Kraken API ---
KRAKEN_API_URL = os.environ.get('KRAKEN_API_URL', 'https://api.kraken.com/0/public/OHLC')

# --- OHLC SQL Updater knobs (optional; used by utils/ohlc_sql_update.py) ---
OHLC_TARGET_TABLE = os.environ.get('OHLC_TARGET_TABLE', 'ohlc')
OHLC_PAIRS = os.environ.get('OHLC_PAIRS', 'XBTUSD').split(',')  # comma-separated
OHLC_INTERVALS = [int(x) for x in os.environ.get('OHLC_INTERVALS', '1,60').split(',')]

# --- Limit order settings ---
LIMIT_ORDER_MODE = get_config('LIMIT_ORDER_MODE', 'market')
LIMIT_ORDER_TTL_MINUTES = int(get_config('LIMIT_ORDER_TTL_MINUTES', '60'))
LIMIT_ORDER_OFFSET_PCT = float(get_config('LIMIT_ORDER_OFFSET_PCT', '0.0'))

# --- Predictive entry settings ---
PREDICTIVE_ENTRY_ENABLED = get_config('PREDICTIVE_ENTRY_ENABLED', 'false').lower() == 'true'
PREDICTIVE_APPROACH_PCT = float(get_config('PREDICTIVE_APPROACH_PCT', '1.5'))
PREDICTIVE_MIN_ZONE_STRENGTH = float(get_config('PREDICTIVE_MIN_ZONE_STRENGTH', '0.7'))
PREDICTIVE_MIN_ZONE_TOUCHES = int(get_config('PREDICTIVE_MIN_ZONE_TOUCHES', '3'))
PREDICTIVE_TTL_BARS = int(get_config('PREDICTIVE_TTL_BARS', '6'))
PREDICTIVE_COOLDOWN_BARS = int(get_config('PREDICTIVE_COOLDOWN_BARS', '12'))

# --- Risk Gate Calibration ---
RISK_GATE_CALIBRATION_ENABLED = get_config('RISK_GATE_CALIBRATION_ENABLED', 'false').lower() == 'true'
RISK_GATE_RECALIBRATE_HOURS = int(get_config('RISK_GATE_RECALIBRATE_HOURS', '24'))
RISK_GATE_FEEDBACK_WINDOW = int(get_config('RISK_GATE_FEEDBACK_WINDOW', '50'))

# --- Regime Transition Exit (Exit Engine V2 Phase 2a) ---
REGIME_TRANSITION_EXIT_ENABLED = get_config('REGIME_TRANSITION_EXIT_ENABLED', 'false').lower() == 'true'
REGIME_STUCK_HOURS = int(get_config('REGIME_STUCK_HOURS', '3'))
REGIME_STUCK_MIN_LOSS_PCT = float(get_config('REGIME_STUCK_MIN_LOSS_PCT', '0.0'))
REGIME_CONSOLIDATION_MIN_TICKS = int(get_config('REGIME_CONSOLIDATION_MIN_TICKS', '120'))
REGIME_CONSOLIDATION_MAX_PNL = float(get_config('REGIME_CONSOLIDATION_MAX_PNL', '-0.3'))

# --- Data mode ---
SPIRIT_DATA_MODE = get_config('SPIRIT_DATA_MODE', 'kraken_api')  # 'kraken_api' or 'pipeline'
PIPELINE_FALLBACK_TIMEOUT = float(get_config('PIPELINE_FALLBACK_TIMEOUT', '90'))

# --- Strategy selection ---
# Set SPIRIT_STRATEGY env var to load a trading algorithm.
# If not set, Spirit starts in monitor-only mode (no trades).
# Valid values: "zone_bounce", "regime_engine", "macd_cross", "test"

# Add other system-wide config variables below as needed
# Example:
# ENABLE_FEATURE_X = os.environ.get('ENABLE_FEATURE_X', '0') == '1'
# API_KEY = os.environ.get('API_KEY')

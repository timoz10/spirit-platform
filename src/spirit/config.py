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
# Default to the repository root (directory containing this file)
DEFAULT_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("KRAKEN_BOT_BASE_DIR", DEFAULT_BASE_DIR)

# --- Paths (overridable via env) ---
# DEPRECATED: DB_PATH is only kept for backward compatibility with strategies
# that still import it. Spirit V2 uses SpiritContext (in-memory + PG) and
# does not use SQLite. Will be removed once all strategies are migrated.
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "kraken_ohlc.db"))
LOG_FILE = os.environ.get("LOG_FILE", os.path.join(BASE_DIR, "logs", "spirit_syslog.log"))

# --- Logging ---
# Logging level: 'INFO', 'DEBUG', 'WARNING', 'ERROR', 'CRITICAL'
LOGGING_LEVEL = get_config('LOGGING_LEVEL', os.environ.get('LOG_LEVEL', 'DEBUG'))

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

# --- Strategy selection ---
# Set SPIRIT_STRATEGY env var to load a trading algorithm.
# If not set, Spirit starts in monitor-only mode (no trades).
# Valid values: "zone_bounce", "regime_engine", "macd_cross", "test"

# Add other system-wide config variables below as needed
# Example:
# ENABLE_FEATURE_X = os.environ.get('ENABLE_FEATURE_X', '0') == '1'
# API_KEY = os.environ.get('API_KEY')

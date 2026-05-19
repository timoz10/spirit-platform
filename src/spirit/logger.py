"""
logger.py

Centralized logging configuration for kraken-bot project.
Import get_logger() in any module to get a consistent logger.

NOTE:
Log bloat is now reduced: large data structures in LiveDataSource.get_window and KrakenOHLCBuffer.update_buffer_from_api
are only logged if you set DEBUG_VERBOSE=1 in your environment. Otherwise, only summary info (counts, types, shapes, first/last samples) is logged.
"""

import json
import logging
import sys
import os
from spirit.config import LOGGING_LEVEL, LOG_FILE


def _resolve_instance() -> str:
    """Return the instance name for log prefixes.

    Resolution order (mirrors `config_loader.resolve_active_instance()`):

      1. `SPIRIT_INSTANCE` env var (any value).
      2. If env is unset and exactly one non-hidden directory lives
         under `~/.spirit/`, use that name (single-instance autodetect).
      3. Otherwise the literal sentinel `no-instance`.

    Pre-#733 this walked up `__file__` directories looking for a YAML —
    which on pipx installs found a file inside the venv. The new
    behaviour matches the config-loader contract: env first, single-dir
    autodetect second, sentinel last. See `docs/reference/MODULE_CONTRACTS.md`.

    We don't import `spirit.utils.config_loader` to avoid a circular
    dependency (`spirit.config` -> `spirit.logger` -> `spirit.config`).
    The autodetect logic is short enough to inline.
    """
    val = os.environ.get('SPIRIT_INSTANCE', '').strip()
    if val:
        return val
    spirit_root = os.path.join(os.path.expanduser('~'), '.spirit')
    try:
        candidates = [
            d for d in os.listdir(spirit_root)
            if not d.startswith('.') and os.path.isdir(os.path.join(spirit_root, d))
        ]
    except (FileNotFoundError, NotADirectoryError):
        return 'no-instance'
    if len(candidates) == 1:
        return candidates[0]
    return 'no-instance'


_INSTANCE = _resolve_instance()

LOG_FORMAT = f"%(asctime)s [{_INSTANCE}] [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = LOG_FILE

# Map string level to logging constant
LEVEL_MAP = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}
LOG_LEVEL = LEVEL_MAP.get(LOGGING_LEVEL, logging.INFO)

# Ensure log directory exists
try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
except Exception:
    pass


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter. Activate with SPIRIT_LOG_FORMAT=json."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            'ts': self.formatTime(record),
            'instance': _INSTANCE,
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry['exception'] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


# Choose formatter based on env
_use_json = os.environ.get('SPIRIT_LOG_FORMAT', '').lower() == 'json'

_handlers = [
    logging.StreamHandler(sys.stdout),
    logging.FileHandler(LOG_FILE, mode='a'),
]

if _use_json:
    _formatter = JsonFormatter()
    for h in _handlers:
        h.setFormatter(_formatter)

# Configure root logger only once
logging.basicConfig(
    level=LOG_LEVEL,
    format=LOG_FORMAT,
    handlers=_handlers,
)


def get_logger(name: str) -> logging.Logger:
    """
    Returns a logger with the given name, using the centralized config.
    """
    return logging.getLogger(name)

# Provide a default logger for direct import (for legacy/simple use)
logger = get_logger("kraken-bot")

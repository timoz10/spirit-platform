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
    """Read SPIRIT_INSTANCE from env. Returns 'no-instance' when unset.

    Pre-#733 this function walked up directories from `__file__` looking
    for a `config/spirit.yaml` — which on pipx installs resolved to a
    file inside the venv directory written by a buggy spirit-setup,
    silently overriding the user's actual instance. See
    docs/reference/MODULE_CONTRACTS.md for the resolution contract.

    The logger uses the instance name only as a display prefix in log
    lines; it does NOT read per-instance YAML to find it. If the user
    needs the instance to appear in logs, set SPIRIT_INSTANCE explicitly
    (the wizard writes this into ~/.spirit/<instance>/.env, and any
    sensible launcher sources that file before invoking spirit).
    """
    val = os.environ.get('SPIRIT_INSTANCE', '').strip()
    if val:
        return val
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

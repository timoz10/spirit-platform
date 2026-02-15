"""
strategy_config.py

Central strategy selection for the SPIRIT trading bot.

Set SPIRIT_STRATEGY env var to load a trading algorithm.
If not set, Spirit starts in monitor-only mode (no trades).

If the requested strategy cannot be loaded (missing module, import error),
get_strategy() returns None and Spirit runs in monitor-only mode.
"""

import json
import os
from typing import Any, Dict, Optional

from utils.config_loader import get_config

from logger import get_logger
logger = get_logger("strategy_config")

# ---------------------------------------------------------------------------
# Strategy registry: name → (aliases, module_path, class_name)
# ---------------------------------------------------------------------------
_STRATEGY_REGISTRY = {
    "zone_bounce": {
        "aliases": {"zone", "decision_engine_v2"},
        "module": "strategies.zone_bounce_strategy",
        "class": "ZoneBounceStrategy",
    },
    "regime_engine": {
        "aliases": {"regime", "decision_engine"},
        "module": "strategies.regime_engine_strategy",
        "class": "RegimeEngineStrategy",
    },
    "test": {
        "aliases": {"test_algo"},
        "module": "strategies.test_algo",
        "class": "TestStrategy",
    },
    "macd_cross": {
        "aliases": {"macd_full", "macd_full_algo", "macd_1.0", "macd_1_0"},
        "module": "strategies.macd_cross",
        "class": "MACD_full_algo",
    },
    "spine": {
        "aliases": {"multi", "orchestrator"},
        "module": "strategies.spine_strategy",
        "class": "SpineStrategy",
    },
    "rsi_reversion": {
        "aliases": {"rsi", "rsi_mean_reversion"},
        "module": "strategies.rsi_reversion_strategy",
        "class": "RsiReversionStrategy",
    },
}

# Build reverse lookup: alias → canonical name
_ALIAS_MAP: Dict[str, str] = {}
for canonical, entry in _STRATEGY_REGISTRY.items():
    _ALIAS_MAP[canonical] = canonical
    for alias in entry["aliases"]:
        _ALIAS_MAP[alias] = canonical


def _parse_params(env_key: str = "SPIRIT_STRATEGY_PARAMS") -> Dict[str, Any]:
    raw = (get_config(env_key, "") or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def get_strategy() -> Optional[Any]:
    """
    Return the configured strategy instance, or None if it cannot be loaded.

    Returns None (not a fallback) when:
      - The requested name doesn't match any registered strategy
      - The strategy module fails to import (missing file, dependency error)

    Caller (spirit_main.py) decides how to handle None — typically
    monitor-only mode with clear logging.
    """
    # Resolve requested name
    name = (get_config("SPIRIT_STRATEGY", "") or "").strip().lower()

    if not name:
        logger.warning("SPIRIT_STRATEGY not set. Running in monitor-only mode (no trades).")
        return None

    # Resolve alias → canonical
    canonical = _ALIAS_MAP.get(name)
    if canonical is None:
        available = sorted(_ALIAS_MAP.keys())
        logger.error(
            f"Unknown strategy '{name}'. "
            f"Available: {', '.join(available)}"
        )
        return None

    entry = _STRATEGY_REGISTRY[canonical]
    params = _parse_params()

    # Attempt import
    try:
        import importlib
        mod = importlib.import_module(entry["module"])
        cls = getattr(mod, entry["class"])
    except ImportError as e:
        logger.error(
            f"Strategy '{canonical}' requested but module '{entry['module']}' "
            f"failed to import: {e}"
        )
        return None
    except AttributeError:
        logger.error(
            f"Strategy '{canonical}' module loaded but class '{entry['class']}' "
            f"not found in {entry['module']}"
        )
        return None

    # Instantiate
    try:
        instance = cls(**params)
    except Exception as e:
        logger.error(f"Strategy '{canonical}' failed to instantiate: {e}")
        return None

    logger.info(f"Strategy loaded: {canonical} ({cls.__name__})")
    return instance

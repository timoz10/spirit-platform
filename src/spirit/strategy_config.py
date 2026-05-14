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

from spirit.utils.config_loader import get_config

# Built-in strategy registry — production + experimental.
# User strategies live in ~/.spirit/strategies/ (or $SPIRIT_STRATEGIES_DIR)
# and are resolved at runtime; see _load_user_strategy() below.

from spirit.logger import get_logger
logger = get_logger("strategy_config")

# ---------------------------------------------------------------------------
# Strategy registry: name → (aliases, module_path, class_name)
#
# Public entries ship on every tier (this file is allow-listed). IP entries
# live in the private companion `spirit._strategy_registry_ip` which is NOT
# in the public-mirror allow-list — so the public Spirit build lands at the
# try/except below with ImportError and gets the two bundled examples only.
# Internal / Plus / Pro builds carry the companion file and pick up the full
# registry transparently.
#
# Splitting the registry like this means strategy_config.py can be safely
# public-shipped without leaking IP strategy module paths. Same fault-class
# fix as #696: the registry is the gateway between SPIRIT_STRATEGY=<name>
# and the actual class, so anything that ships needs a matching entry, and
# anything that doesn't ship MUST NOT have an entry pointing at a missing
# module.
# ---------------------------------------------------------------------------


def _build_registry() -> Dict[str, Dict[str, Any]]:
    """Build the strategy registry, merging IP entries when available.

    Returns the public-only set if `spirit._strategy_registry_ip` isn't
    importable (public mirror ship). Returns the full set otherwise.

    Factored as a function so tests can call it directly with patched
    imports and assert the two-tier behaviour. Module-level callers get
    the resolved registry via `_STRATEGY_REGISTRY` below.
    """
    registry: Dict[str, Dict[str, Any]] = {
        # Bundled Free-tier example. Lives under strategies/examples/ so
        # it ships in the public-mirror allow-list. Uses only the
        # FrameworkDataProvider; safe to run on Free tier.
        "sma_crossover": {
            "aliases": {"sma", "sma_cross"},
            "module": "spirit.strategies.examples.sma_crossover",
            "class": "SmaCrossoverStrategy",
        },
        # Second bundled Free-tier example. Exercises the full
        # BaseStrategy lifecycle (monitoring_intervals,
        # on_entry_confirmed, on_monitoring_tick, on_exit_completed).
        # Computes its own MACD/RSI/SMA/ATR in pandas — no
        # spirit.indicators.* dependency.
        "macd_demo": {
            "aliases": {"macd_demo_example"},
            "module": "spirit.strategies.examples.macd_demo",
            "class": "MacdDemoStrategy",
        },
    }
    try:
        from spirit._strategy_registry_ip import IP_REGISTRY
        registry.update(IP_REGISTRY)
    except ImportError:
        # Public mirror ship — no IP entries. Users write their own
        # strategies under ~/.spirit/strategies/ and load via the
        # 'custom' path. This is the expected branch for Free tier.
        pass
    return registry


def _build_alias_map(registry: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Reverse-lookup alias → canonical name, built fresh from `registry`."""
    alias_map: Dict[str, str] = {}
    for canonical, entry in registry.items():
        alias_map[canonical] = canonical
        for alias in entry["aliases"]:
            alias_map[alias] = canonical
    return alias_map


_STRATEGY_REGISTRY = _build_registry()
_ALIAS_MAP: Dict[str, str] = _build_alias_map(_STRATEGY_REGISTRY)


def get_spine_config() -> Dict[str, Any]:
    """Read the 'spine:' section from spirit.yaml.

    Returns dict with keys: max_concurrent_per_pair, strategies, risk_budget.
    Returns empty dict if section is missing.
    """
    from spirit.utils.config_loader import load_yaml_config
    try:
        yaml_config = load_yaml_config()
        return yaml_config.get('spine', {})
    except Exception as e:
        logger.warning(f"Failed to load spine config from YAML: {e}")
        return {}


def _user_strategies_dir() -> str:
    """Return the user strategies directory (default ~/.spirit/strategies/)."""
    return os.path.expanduser(
        os.environ.get("SPIRIT_STRATEGIES_DIR", "~/.spirit/strategies")
    )


def _load_user_strategy(name: str):
    """Load a strategy class from ~/.spirit/strategies/<name>.py.

    Looks for a class subclassing BaseStrategy in the file. Returns the
    class object, or None if the file doesn't exist or no suitable class
    is found.

    Convention: the file may contain multiple classes; this returns the
    first concrete (non-abstract) BaseStrategy subclass. Callers can
    enforce naming by, for example, only defining one strategy per file.
    """
    import importlib.util
    import inspect

    user_dir = _user_strategies_dir()
    file_path = os.path.join(user_dir, f"{name}.py")
    if not os.path.exists(file_path):
        return None

    try:
        spec = importlib.util.spec_from_file_location(
            f"spirit_user_strategies.{name}", file_path
        )
        if spec is None or spec.loader is None:
            logger.error(f"Cannot create import spec for {file_path}")
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.error(f"User strategy '{name}' failed to import from {file_path}: {e}")
        return None

    from spirit.strategies.base import BaseStrategy
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        # Skip imports of BaseStrategy itself, abstracts, and out-of-module classes
        if obj is BaseStrategy:
            continue
        if not issubclass(obj, BaseStrategy):
            continue
        if obj.__module__ != mod.__name__:
            continue
        if inspect.isabstract(obj):
            continue
        logger.info(f"User strategy '{name}' resolved to {obj.__name__} in {file_path}")
        return obj

    logger.error(
        f"User strategy file {file_path} contains no concrete BaseStrategy subclass"
    )
    return None


def _parse_params(env_key: str = "SPIRIT_STRATEGY_PARAMS") -> Dict[str, Any]:
    raw = (get_config(env_key, "") or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def get_strategy(extra_params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """
    Return the configured strategy instance, or None if it cannot be loaded.

    Args:
        extra_params: Additional constructor kwargs merged into SPIRIT_STRATEGY_PARAMS.
                      Useful for per-pair instantiation (e.g. filter_pair='ETHUSD').
                      Keys that the constructor doesn't accept are silently dropped.

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

    params = _parse_params()
    if extra_params:
        params.update(extra_params)

    # Resolve: built-in registry first, then user dir (~/.spirit/strategies/<name>.py)
    canonical = _ALIAS_MAP.get(name)
    cls = None
    if canonical is not None:
        entry = _STRATEGY_REGISTRY[canonical]
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
    else:
        # Try user strategies directory — for "build and throw away" workflow.
        # See docs/reference/platform/WRITING_A_STRATEGY.md
        cls = _load_user_strategy(name)
        if cls is None:
            available = sorted(_ALIAS_MAP.keys())
            user_dir = _user_strategies_dir()
            logger.error(
                f"Unknown strategy '{name}'. "
                f"Built-in: {', '.join(available)}. "
                f"User dir checked: {user_dir}"
            )
            return None
        canonical = name  # for the success log line below

    # Instantiate — drop extra_params keys the constructor doesn't accept
    try:
        instance = cls(**params)
    except TypeError:
        import inspect
        sig = inspect.signature(cls.__init__)
        valid_keys = set(sig.parameters.keys()) - {'self'}
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if not has_var_kw:
            params = {k: v for k, v in params.items() if k in valid_keys}
        try:
            instance = cls(**params)
        except Exception as e:
            logger.error(f"Strategy '{canonical}' failed to instantiate: {e}")
            return None
    except Exception as e:
        logger.error(f"Strategy '{canonical}' failed to instantiate: {e}")
        return None

    logger.info(f"Strategy loaded: {canonical} ({cls.__name__})")
    return instance

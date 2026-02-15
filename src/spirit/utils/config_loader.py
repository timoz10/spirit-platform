"""
Config loader with resolution order: env var -> YAML -> hardcoded default.

Separates operational config (git-tracked YAML) from secrets (env-only).
"""

import os

import yaml

_config_cache = None


def _load_yaml():
    global _config_cache
    if _config_cache is None:
        yaml_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'spirit.yaml')
        try:
            with open(yaml_path) as f:
                _config_cache = yaml.safe_load(f) or {}
        except FileNotFoundError:
            _config_cache = {}
    return _config_cache


def get_config(key: str, default=None):
    """Resolution: env var -> YAML -> default."""
    val = os.environ.get(key)
    if val is not None:
        return val
    yaml_conf = _load_yaml()
    val = yaml_conf.get(key)
    if val is not None:
        return str(val)
    return default

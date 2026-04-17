"""
Pre-flight Validation for Spirit Trading System

Runs before any trading logic to validate environment and gateway
connectivity. Prevents silent startup failures.

Spirit is api-driven (#340): all data access goes through the API gateway.
Direct PostgreSQL checks were removed with the pg-mode strip.

Usage:
    from spirit.utils.preflight import run_preflight

    result = run_preflight()
    if not result.passed:
        sys.exit(1)
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from spirit.logger import get_logger
logger = get_logger("preflight")


@dataclass
class CheckResult:
    """Result of a single pre-flight check."""
    name: str
    passed: bool
    severity: str  # 'FATAL' or 'WARN'
    message: str
    detail: Optional[str] = None


@dataclass
class PreflightResult:
    """Aggregate result of all pre-flight checks."""
    passed: bool  # True if no FATAL checks failed
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def fatal_failures(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == 'FATAL']

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == 'WARN']


def _check_api_gateway_connectivity() -> CheckResult:
    """Test API gateway reachability."""
    import urllib.error
    import urllib.request

    from spirit.utils.config_loader import get_config

    base_url = get_config("SPIRIT_API_URL", "")
    if not base_url:
        base_url = os.environ.get("SPIRIT_API_URL", "http://10.0.0.4:8000/v1")
    health_url = base_url.rstrip("/") + "/health"

    try:
        req = urllib.request.Request(health_url, method="GET",
                                     headers={"User-Agent": "Spirit/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return CheckResult(
                    name='api_gateway',
                    passed=True,
                    severity='FATAL',
                    message=f'API gateway reachable at {health_url}',
                )
            return CheckResult(
                name='api_gateway',
                passed=False,
                severity='FATAL',
                message=f'API gateway returned HTTP {resp.status}',
                detail=health_url,
            )
    except urllib.error.URLError as e:
        return CheckResult(
            name='api_gateway',
            passed=False,
            severity='FATAL',
            message='API gateway unreachable',
            detail=f'{health_url}: {e.reason}',
        )
    except Exception as e:
        return CheckResult(
            name='api_gateway',
            passed=False,
            severity='FATAL',
            message='API gateway connectivity check failed',
            detail=str(e),
        )


def _check_exchange_keys() -> CheckResult:
    """Verify exchange API keys are accessible (generic + legacy fallback)."""
    try:
        from spirit.exchange.kraken import _load_credential
        key = _load_credential('EXCHANGE_API_KEY', 'KRAKEN_API_KEY')
        secret = _load_credential('EXCHANGE_API_SECRET', 'KRAKEN_API_SECRET')
        if key and secret:
            return CheckResult(
                name='exchange_keys',
                passed=True,
                severity='FATAL',
                message='Exchange API keys found',
            )
        missing = []
        if not key:
            missing.append('EXCHANGE_API_KEY (or KRAKEN_API_KEY)')
        if not secret:
            missing.append('EXCHANGE_API_SECRET (or KRAKEN_API_SECRET)')
        return CheckResult(
            name='exchange_keys',
            passed=False,
            severity='FATAL',
            message=f'Missing exchange keys: {", ".join(missing)}',
        )
    except Exception as e:
        return CheckResult(
            name='exchange_keys',
            passed=False,
            severity='FATAL',
            message='Failed to check exchange keys',
            detail=str(e),
        )


def _check_env_vars(skip_kraken: bool = False) -> CheckResult:
    """Verify required environment variables / config values are set."""
    from spirit.utils.config_loader import get_config

    # Secrets: must come from env vars (not YAML)
    secrets: list[str] = []
    if not skip_kraken:
        secrets.append('KRAKEN_API_KEY')
    missing = [v for v in secrets if not os.environ.get(v)]
    # Also check _FILE variants for Kraken keys (Docker secret pattern)
    if 'KRAKEN_API_KEY' in missing and os.environ.get('KRAKEN_API_KEY_FILE'):
        missing.remove('KRAKEN_API_KEY')

    # api-mode requires a gateway API key
    api_key = get_config("SPIRIT_API_KEY", "") or os.environ.get("SPIRIT_API_KEY", "")
    if not api_key:
        missing.append('SPIRIT_API_KEY')

    # Config: can come from env var OR YAML
    config_keys = ['SPIRIT_STRATEGY']
    missing += [v for v in config_keys if not get_config(v)]

    total = len(secrets) + len(config_keys) + 1
    if missing:
        return CheckResult(
            name='env_vars',
            passed=False,
            severity='FATAL',
            message=f'Missing env vars: {", ".join(missing)}',
        )
    return CheckResult(
        name='env_vars',
        passed=True,
        severity='FATAL',
        message=f'All {total} required env vars set',
    )


def _check_disk_space() -> CheckResult:
    """Check disk space on the working directory."""
    try:
        usage = shutil.disk_usage(os.getcwd())
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 500:
            return CheckResult(
                name='disk_space',
                passed=False,
                severity='WARN',
                message=f'Low disk space: {free_mb:.0f}MB free',
            )
        return CheckResult(
            name='disk_space',
            passed=True,
            severity='WARN',
            message=f'Disk space OK: {free_mb:.0f}MB free',
        )
    except Exception as e:
        return CheckResult(
            name='disk_space',
            passed=False,
            severity='WARN',
            message='Failed to check disk space',
            detail=str(e),
        )


def run_preflight(skip_kraken: bool = False) -> PreflightResult:
    """
    Run all pre-flight checks. Returns PreflightResult.

    FATAL failures mean Spirit should not start.
    WARN failures are logged but do not block startup.

    skip_kraken: If True, skip Kraken key check (e.g. paper/replay mode).
    """
    checks = [
        _check_env_vars(skip_kraken=skip_kraken),
        _check_api_gateway_connectivity(),
    ]

    if not skip_kraken:
        checks.append(_check_exchange_keys())

    checks.append(_check_disk_space())

    fatal_failed = any(not c.passed and c.severity == 'FATAL' for c in checks)

    result = PreflightResult(
        passed=not fatal_failed,
        checks=checks,
    )

    for check in checks:
        if check.passed:
            logger.info(f"[PREFLIGHT] PASS  {check.name}: {check.message}")
        elif check.severity == 'FATAL':
            detail = f" ({check.detail})" if check.detail else ""
            logger.error(f"[PREFLIGHT] FAIL  {check.name}: {check.message}{detail}")
        else:
            detail = f" ({check.detail})" if check.detail else ""
            logger.warning(f"[PREFLIGHT] WARN  {check.name}: {check.message}{detail}")

    if result.passed:
        logger.info(f"[PREFLIGHT] All checks passed ({len(checks)} total)")
    else:
        logger.error(
            f"[PREFLIGHT] {len(result.fatal_failures)} FATAL failure(s) — Spirit cannot start"
        )

    return result


def main():
    """CLI entry point for standalone preflight checks."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    result = run_preflight()
    print()
    print("=" * 60)
    print(f"Pre-flight: {'PASSED' if result.passed else 'FAILED'}")
    print(f"  Checks run: {len(result.checks)}")
    print(f"  Passed:     {sum(1 for c in result.checks if c.passed)}")
    print(f"  Warnings:   {len(result.warnings)}")
    print(f"  Fatal:      {len(result.fatal_failures)}")
    print("=" * 60)

    if not result.passed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

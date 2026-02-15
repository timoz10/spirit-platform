"""
Pre-flight Validation for Spirit Trading System

Runs before any trading logic to validate environment, connectivity,
and data integrity. Prevents silent startup failures.

Usage:
    from utils.preflight import run_preflight

    result = run_preflight()
    if not result.passed:
        sys.exit(1)
"""

import os
import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from logger import get_logger
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


def _check_pg_connectivity() -> CheckResult:
    """Test PostgreSQL connection via existing db_connection module."""
    try:
        from utils.db_connection import test_connection
        if test_connection():
            return CheckResult(
                name='pg_connectivity',
                passed=True,
                severity='FATAL',
                message='PostgreSQL connection OK',
            )
        else:
            return CheckResult(
                name='pg_connectivity',
                passed=False,
                severity='FATAL',
                message='PostgreSQL connection failed',
                detail='test_connection() returned False',
            )
    except Exception as e:
        return CheckResult(
            name='pg_connectivity',
            passed=False,
            severity='FATAL',
            message='PostgreSQL connection failed',
            detail=str(e),
        )


def _check_pg_tables() -> CheckResult:
    """Verify required PostgreSQL tables exist."""
    required_tables = [
        'ohlc_2025',
        'ohlc_2026',
        'strategy_performance',
        'd_limit_zones',
        'd_limit_zone_touches',
    ]
    try:
        from utils.db_connection import execute_query
        rows = execute_query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (required_tables,),
        )
        found = {r['table_name'] for r in rows} if rows else set()
        missing = [t for t in required_tables if t not in found]
        if missing:
            return CheckResult(
                name='pg_tables',
                passed=False,
                severity='FATAL',
                message=f'Missing PG tables: {", ".join(missing)}',
                detail=f'Found: {sorted(found)}',
            )
        return CheckResult(
            name='pg_tables',
            passed=True,
            severity='FATAL',
            message=f'All {len(required_tables)} required PG tables present',
        )
    except Exception as e:
        return CheckResult(
            name='pg_tables',
            passed=False,
            severity='FATAL',
            message='Failed to check PG tables',
            detail=str(e),
        )


def _check_kraken_keys() -> CheckResult:
    """Verify Kraken API keys are accessible."""
    try:
        from utils.kraken_api_client import _get_env_or_file
        key = _get_env_or_file('KRAKEN_API_KEY')
        secret = _get_env_or_file('KRAKEN_API_SECRET')
        if key and secret:
            return CheckResult(
                name='kraken_keys',
                passed=True,
                severity='FATAL',
                message='Kraken API keys found',
            )
        missing = []
        if not key:
            missing.append('KRAKEN_API_KEY')
        if not secret:
            missing.append('KRAKEN_API_SECRET')
        return CheckResult(
            name='kraken_keys',
            passed=False,
            severity='FATAL',
            message=f'Missing Kraken keys: {", ".join(missing)}',
        )
    except Exception as e:
        return CheckResult(
            name='kraken_keys',
            passed=False,
            severity='FATAL',
            message='Failed to check Kraken keys',
            detail=str(e),
        )


def _check_ohlc_freshness() -> CheckResult:
    """Check how recent the OHLC data is in PostgreSQL."""
    try:
        from utils.db_connection import execute_query
        row = execute_query(
            "SELECT MAX(datetime) AS latest FROM ohlc_2026 WHERE interval = 60",
            fetch='one',
        )
        if row and row.get('latest'):
            from datetime import datetime, timezone
            latest = row['latest']
            if hasattr(latest, 'tzinfo') and latest.tzinfo is not None:
                now = datetime.now(timezone.utc)
            else:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
            age_hours = (now - latest).total_seconds() / 3600
            if age_hours > 6:
                return CheckResult(
                    name='ohlc_freshness',
                    passed=False,
                    severity='WARN',
                    message=f'OHLC data is {age_hours:.1f}h stale (latest: {latest})',
                )
            return CheckResult(
                name='ohlc_freshness',
                passed=True,
                severity='WARN',
                message=f'OHLC data is {age_hours:.1f}h old (latest: {latest})',
            )
        return CheckResult(
            name='ohlc_freshness',
            passed=False,
            severity='WARN',
            message='No OHLC data found in ohlc_2026',
        )
    except Exception as e:
        return CheckResult(
            name='ohlc_freshness',
            passed=False,
            severity='WARN',
            message='Failed to check OHLC freshness',
            detail=str(e),
        )


def _check_env_vars() -> CheckResult:
    """Verify required environment variables / config values are set."""
    from utils.config_loader import get_config

    # Secrets: must come from env vars (not YAML)
    secrets = ['POSTGRES_PASSWORD', 'KRAKEN_API_KEY']
    missing = [v for v in secrets if not os.environ.get(v)]
    # Also check _FILE variants for Kraken keys (Docker secret pattern)
    if 'KRAKEN_API_KEY' in missing and os.environ.get('KRAKEN_API_KEY_FILE'):
        missing.remove('KRAKEN_API_KEY')

    # Config: can come from env var OR YAML
    config_keys = ['SPIRIT_STRATEGY']
    missing += [v for v in config_keys if not get_config(v)]

    total = len(secrets) + len(config_keys)
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


def run_preflight() -> PreflightResult:
    """
    Run all pre-flight checks. Returns PreflightResult.

    FATAL failures mean Spirit should not start.
    WARN failures are logged but do not block startup.
    """
    checks = [
        _check_env_vars(),
        _check_pg_connectivity(),
        _check_pg_tables(),
        _check_kraken_keys(),
        _check_ohlc_freshness(),
        _check_disk_space(),
    ]

    fatal_failed = any(not c.passed and c.severity == 'FATAL' for c in checks)

    result = PreflightResult(
        passed=not fatal_failed,
        checks=checks,
    )

    # Log results
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

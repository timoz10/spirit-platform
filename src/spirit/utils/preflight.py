"""
Pre-flight Validation for Spirit Trading System

Runs before any trading logic to validate environment, connectivity,
and data integrity. Prevents silent startup failures.

Usage:
    from spirit.utils.preflight import run_preflight

    result = run_preflight()
    if not result.passed:
        sys.exit(1)
"""

import os
import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from spirit.logger import get_logger
logger = get_logger("preflight")


def _is_api_mode() -> bool:
    """Return True when Spirit is configured for api-mode (no direct PG)."""
    from spirit.utils.config_loader import get_config
    mode = get_config("SPIRIT_DATA_PROVIDER", "pg")
    if mode == "api":
        return True
    # Also auto-detect: if SPIRIT_API_KEY is set but POSTGRES_HOST is not,
    # the user clearly intends api-mode even if they forgot the explicit flag.
    api_key = get_config("SPIRIT_API_KEY", "") or os.environ.get("SPIRIT_API_KEY", "")
    if api_key and not os.environ.get("POSTGRES_HOST"):
        return True
    return False


# Canonical mapping: PostgreSQL role → expected SPIRIT_INSTANCE value.
# Extend here when new instances are onboarded (Rule 7: schema isolation).
PG_USER_TO_INSTANCE = {
    "prod_25": "prod",
    "davy_25": "davy",
    # botuser is the shared dev/replay user; search_path = public.
    # Permissive: botuser may run under instance 'dev' or any non-prod/davy name.
    "botuser": "dev",
}


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
        from spirit.utils.db_connection import test_connection
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
        from spirit.utils.db_connection import execute_query
        rows = execute_query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema IN ('public', 'prod') AND table_name = ANY(%s)",
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


def _check_api_gateway_connectivity() -> CheckResult:
    """Test API gateway reachability (api-mode only)."""
    try:
        from spirit.utils.config_loader import get_config
        import urllib.request
        import urllib.error

        base_url = get_config("SPIRIT_API_URL", "")
        if not base_url:
            base_url = os.environ.get("SPIRIT_API_URL", "http://10.0.0.4:8000/v1")
        health_url = base_url.rstrip("/") + "/health"

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


def _check_kraken_keys() -> CheckResult:
    """Verify Kraken API keys are accessible."""
    try:
        from spirit.utils.kraken_api_client import _get_env_or_file
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
        from spirit.utils.db_connection import execute_query
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


def _check_instance_schema_match() -> CheckResult:
    """Verify SPIRIT_INSTANCE matches the PG user we're actually connected as.

    Fail-closed guard for multi-instance deployments (#277). A mismatch
    means someone pointed Davy's instance at prod credentials (or vice
    versa) and we would write to the wrong schema. This is FATAL.

    Mapping lives in PG_USER_TO_INSTANCE. Unknown PG users fail the
    check — add them to the map explicitly.
    """
    from spirit.utils.config_loader import get_config

    declared = get_config('SPIRIT_INSTANCE', 'prod')

    try:
        from spirit.utils.db_connection import execute_query
        row = execute_query("SELECT current_user AS u", fetch='one')
        if not row:
            return CheckResult(
                name='instance_schema_match',
                passed=False,
                severity='FATAL',
                message='Could not determine PG current_user',
            )
        pg_user = row['u'] if isinstance(row, dict) else row[0]
    except Exception as e:
        return CheckResult(
            name='instance_schema_match',
            passed=False,
            severity='FATAL',
            message='Failed to query current_user',
            detail=str(e),
        )

    expected = PG_USER_TO_INSTANCE.get(pg_user)
    if expected is None:
        return CheckResult(
            name='instance_schema_match',
            passed=False,
            severity='FATAL',
            message=(
                f"Unknown PG user '{pg_user}' — refusing to start. "
                f"Add it to PG_USER_TO_INSTANCE in src/spirit/utils/preflight.py"
            ),
            detail=f"SPIRIT_INSTANCE={declared}",
        )

    if declared != expected:
        return CheckResult(
            name='instance_schema_match',
            passed=False,
            severity='FATAL',
            message=(
                f"SPIRIT_INSTANCE={declared} but connected as PG user "
                f"'{pg_user}' (expected instance '{expected}') — refusing to start"
            ),
            detail=(
                "Mismatch means writes would land in the wrong schema. "
                "Fix SPIRIT_INSTANCE or POSTGRES_USER so they agree."
            ),
        )

    return CheckResult(
        name='instance_schema_match',
        passed=True,
        severity='FATAL',
        message=f"SPIRIT_INSTANCE={declared} matches PG user '{pg_user}'",
    )


def _check_env_vars(skip_kraken: bool = False, api_mode: bool = False) -> CheckResult:
    """Verify required environment variables / config values are set."""
    from spirit.utils.config_loader import get_config

    # Secrets: must come from env vars (not YAML)
    secrets: list[str] = []
    if not api_mode:
        secrets.append('POSTGRES_PASSWORD')
    if not skip_kraken:
        secrets.append('KRAKEN_API_KEY')
    missing = [v for v in secrets if not os.environ.get(v)]
    # Also check _FILE variants for Kraken keys (Docker secret pattern)
    if 'KRAKEN_API_KEY' in missing and os.environ.get('KRAKEN_API_KEY_FILE'):
        missing.remove('KRAKEN_API_KEY')

    # api-mode requires an API key
    if api_mode:
        api_key = get_config("SPIRIT_API_KEY", "") or os.environ.get("SPIRIT_API_KEY", "")
        if not api_key:
            missing.append('SPIRIT_API_KEY')

    # Config: can come from env var OR YAML
    config_keys = ['SPIRIT_STRATEGY']
    missing += [v for v in config_keys if not get_config(v)]

    total = len(secrets) + len(config_keys) + (1 if api_mode else 0)
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


def _check_profiler_data() -> CheckResult:
    """Verify the risk/reward profiler can load OHLC and D-Limit data for current dates."""
    try:
        from datetime import datetime, timedelta, timezone
        from spirit.indicators.decision_engine.engine.risk_reward_profiler import RiskRewardProfiler
        import os
        env_str = os.environ.get('SPIRIT_PAIRS', '').strip()
        if env_str:
            pair = env_str.split(',')[0].strip()
        else:
            from spirit.utils.pair_registry import get_active_pairs
            pair = get_active_pairs()[0]  # Test with first active pair

        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=30)).strftime('%Y-%m-%d')
        end = now.strftime('%Y-%m-%d %H:%M:%S')

        p = RiskRewardProfiler()
        p.load_ohlc(pair=pair, start_date=start, end_date=end)
        p.load_dlimit(pair=pair, start_date=start, end_date=end)

        ohlc_count = len(p._ohlc_df) if p._ohlc_df is not None else 0
        dlimit_count = len(p._dlimit_df) if p._dlimit_df is not None else 0

        issues = []
        if ohlc_count == 0:
            issues.append(f'{pair} OHLC=0')
        if dlimit_count == 0:
            issues.append(f'{pair} D-Limit=0')

        if issues:
            return CheckResult(
                name='profiler_data',
                passed=False,
                severity='WARN',
                message=f'Profiler loaded empty data: {", ".join(issues)}',
                detail=f'Risk gate will block all trades. Check year-partitioned tables for {now.year}.',
            )
        return CheckResult(
            name='profiler_data',
            passed=True,
            severity='WARN',
            message=f'Profiler data OK: {pair} OHLC={ohlc_count}, D-Limit={dlimit_count}',
        )
    except Exception as e:
        return CheckResult(
            name='profiler_data',
            passed=False,
            severity='WARN',
            message='Failed to check profiler data',
            detail=str(e),
        )


def run_preflight(skip_kraken: bool = False) -> PreflightResult:
    """
    Run all pre-flight checks. Returns PreflightResult.

    FATAL failures mean Spirit should not start.
    WARN failures are logged but do not block startup.

    skip_kraken: If True, skip Kraken key check (e.g. paper/replay mode).
    """
    api_mode = _is_api_mode()
    if api_mode:
        logger.info("[PREFLIGHT] api-mode detected — skipping PG checks, checking gateway")

    checks = [
        _check_env_vars(skip_kraken=skip_kraken, api_mode=api_mode),
    ]

    if api_mode:
        # api-mode: verify gateway reachability instead of PG
        checks.append(_check_api_gateway_connectivity())
    else:
        # pg-mode: verify PG connectivity, schema match, tables
        checks.append(_check_pg_connectivity())
        checks.append(_check_instance_schema_match())
        checks.append(_check_pg_tables())

    if not skip_kraken:
        checks.append(_check_kraken_keys())

    # Data quality checks — only meaningful with direct PG access
    if not api_mode:
        checks.append(_check_ohlc_freshness())

    # Profiler data check loads heavy deps (pandas, numpy) and queries via
    # DataProvider — skip in api-mode where deps may be minimal and data
    # availability is already proven by the gateway health check.
    if not api_mode:
        checks.append(_check_profiler_data())

    checks.append(_check_disk_space())

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

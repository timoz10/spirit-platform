"""SqliteDataProvider — local-disk implementation of FrameworkDataProvider (#561).

Backs Free-tier Spirit instances that ship without the API gateway.
State, heartbeats, and trade outcomes write to a single SQLite file
(default `~/.spirit/<instance>/spirit.db`).

The schema lives in `spirit.storage.sqlite_schema` and is applied on
first connect via `executescript` — every CREATE uses IF NOT EXISTS,
so re-running on an existing DB is a no-op.

Threading model
---------------
Single connection + `threading.Lock`, `check_same_thread=False`.
Spirit's main process has at least three threads that hit
`get_data_provider()` (main eval, data-source heartbeat tick, WS event
handler under `_eval_locks`); the lock serialises writes so the shared
connection stays consistent. Free tier does not run pipeline daemons —
those live in separate processes in paid-tier mode and are not part
of the Free-tier ship — so multi-process file contention is not a
concern at v2.3.0.

Rule 11 contract
----------------
All values returned to callers match the shapes ApiDataProvider
returns:

    * datetime columns → tz-aware UTC `datetime`
    * NUMERIC columns  → `float`
    * BOOLEAN columns  → `bool`
    * JSON columns     → `dict | list` (already parsed)

The regression gate is `tests/test_sqlite_data_provider_tz_contract.py`,
which mirrors `tests/test_api_data_provider_tz_contract.py`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from spirit.logger import get_logger

logger = get_logger("sqlite_data_provider")


_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "storage" / "sqlite_schema.sql"


# =====================================================================
# Module-level helpers — Rule 11 type adapters
# =====================================================================


def _to_iso(dt: datetime | None) -> str | None:
    """Serialise a datetime to ISO-8601 UTC.

    Naive datetimes are interpreted as UTC (matches ApiDataProvider's
    `_parse_value` defensive path). Aware datetimes are converted.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _looks_like_iso_datetime(s: str) -> bool:
    return len(s) >= 19 and s[4] == "-" and s[7] == "-" and s[10] in ("T", " ")


def _parse_value(val: Any) -> Any:
    """Mirror `api_data_provider._parse_value` for SQLite TEXT columns.

    SQLite returns TEXT values as Python `str`; this helper applies the
    same Rule 11 normalisation so callers get tz-aware UTC datetimes
    for ISO-shaped strings (and floats for numeric strings).
    """
    if not isinstance(val, str):
        return val
    if _looks_like_iso_datetime(val):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except ValueError:
            pass
    try:
        return float(val)
    except (ValueError, TypeError):
        return val


def _adapt_value(val: Any) -> Any:
    """Coerce a Python value into a SQLite-storable scalar.

    Strict by design: any type not covered here raises TypeError. This
    mirrors the decision in the #561 audit to drop `default=str` from
    `_adapt_json` so the next #294-class regression surfaces loudly
    instead of getting silently stringified.
    """
    if val is None or isinstance(val, (int, float, str, bytes)):
        return val
    if isinstance(val, bool):
        # bool is an int subclass; covered above, but be explicit
        return 1 if val else 0
    if isinstance(val, datetime):
        return _to_iso(val)
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, (dict, list)):
        return _adapt_json(val)
    raise TypeError(
        f"SqliteDataProvider: unsupported value type {type(val).__name__} "
        f"(value={val!r}). Add a coercion in _adapt_value if this is intended."
    )


def _adapt_json(val: Any) -> str | None:
    """Serialise a Python container to a JSON string for a TEXT column.

    `default=str` is intentionally *not* set: the #561 audit confirmed
    nothing non-JSON-native flows through `put_state` / `write_*` today.
    A future regression that introduces a Decimal or an enum should
    raise here, not silently coerce.
    """
    if val is None:
        return None
    return json.dumps(val, sort_keys=True, ensure_ascii=False)


def _parse_json(val: str | None) -> Any:
    if val is None:
        return None
    return json.loads(val)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Return a dict with Rule-11-normalised values for a single Row."""
    if row is None:
        return None
    return {k: _parse_value(row[k]) for k in row.keys()}


# =====================================================================
# Provider
# =====================================================================


class SqliteDataProvider:
    """Local-disk DataProvider for Free-tier Spirit (#561 Day 1).

    Implements the subset of FrameworkDataProvider that Free tier
    actually writes — state, heartbeats, performance — plus
    `ensure_table` (no-op; the schema is applied at construction time).

    OHLC and pair-registry methods land in later Day-1+ work — Free
    tier reads OHLC directly from Kraken REST, not from this DB.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path).expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            detect_types=0,  # we own type translation explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._migrate()
        logger.info(f"SqliteDataProvider opened {self._path}")

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _apply_pragmas(self) -> None:
        # journal_mode=WAL: better concurrent-reader behaviour and crash
        # safety than the default rollback journal.
        # synchronous=NORMAL: the WAL recommendation; trades a vanishingly
        # small durability window for ~2× write throughput.
        # foreign_keys=ON: SQLite ships them off by default.
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

    def _migrate(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        """Acquire the lock + yield a cursor; commit on success."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("SqliteDataProvider is closed")
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # State (spirit_state) — crash recovery
    # ------------------------------------------------------------------

    def get_state(self, key: str) -> Any:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM spirit_state WHERE key = ?", (key,))
            row = cur.fetchone()
        if row is None:
            return None
        return _parse_json(row["value"])

    def put_state(self, key: str, value: Any) -> None:
        payload = _adapt_json(value)
        if payload is None:
            # spirit_state.value is NOT NULL — explicitly delete on None
            # so callers can clear keys with `put_state(key, None)`.
            with self._cursor() as cur:
                cur.execute("DELETE FROM spirit_state WHERE key = ?", (key,))
            return
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO spirit_state (key, value, updated_at)
                VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, payload),
            )

    def ensure_table(self, table_name: str, create_sql: str) -> bool:
        # The bundled schema is applied at construction time. Callers
        # passing PG-flavoured DDL would silently fail anyway; surface
        # a clear no-op like ApiDataProvider.ensure_table does.
        return True

    # ------------------------------------------------------------------
    # Heartbeats — daemon liveness
    # ------------------------------------------------------------------

    def write_heartbeat(
        self,
        daemon_id: str,
        *,
        instance: str,
        status: str = "ok",
        metadata: dict | None = None,
        run_id: str = "live",
    ) -> int:
        """Upsert a daemon heartbeat. Returns rows affected (always 1).

        Free-tier ships single-instance per file, but the (daemon_id,
        instance) composite PK is preserved so the call signature is
        identical to ApiDataProvider.write_heartbeat — keeps daemon-side
        code (`pipeline/daemon_health.py`) backend-agnostic.
        """
        meta_json = _adapt_json(metadata)
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO daemon_heartbeats
                    (daemon_id, instance, status, metadata, run_id, last_heartbeat)
                VALUES (?, ?, ?, ?, ?,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(daemon_id, instance) DO UPDATE SET
                    status = excluded.status,
                    metadata = excluded.metadata,
                    run_id = excluded.run_id,
                    last_heartbeat = excluded.last_heartbeat
                """,
                (daemon_id, instance, status, meta_json, run_id),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Performance — strategy_performance
    # ------------------------------------------------------------------

    # Column list mirrors the API gateway's INSERT in
    # `src/spirit/api/routes/writes.py:write_performance`, minus
    # `instance` (file-level isolation in Free tier). Order is fixed so
    # the executemany path stays consistent.
    _PERFORMANCE_COLUMNS = (
        "timestamp",
        "entry_timestamp",
        "pair",
        "strategy_name",
        "is_win",
        "pnl_pct",
        "entry_price",
        "exit_price",
        "exit_reason",
        "regime_at_entry",
        "dlimit_trend_state",
        "volatility_regime",
        "source",
        "trade_id",
        "order_type",
        "limit_price",
        "run_id",
        "exit_engine_version",
        "mfe_pct",
        "mae_pct",
        "entry_context",
    )
    # Columns that need JSON encoding before storage (others go through
    # _adapt_value which handles datetime/bool/scalar coercion).
    _PERFORMANCE_JSON_COLUMNS = frozenset({"entry_context"})

    def _performance_row(self, data: dict) -> tuple:
        """Build the parameter tuple for a strategy_performance INSERT.

        Missing keys map to NULL — matches PG nullable column semantics.
        """
        out: list[Any] = []
        for col in self._PERFORMANCE_COLUMNS:
            val = data.get(col)
            if col in self._PERFORMANCE_JSON_COLUMNS:
                out.append(_adapt_json(val))
            else:
                out.append(_adapt_value(val))
        return tuple(out)

    def get_performance(
        self,
        *,
        pair: str | None = None,
        strategy: str | None = None,
        source: str | None = None,
        run_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if pair is not None:
            clauses.append("pair = ?")
            params.append(pair)
        if strategy is not None:
            clauses.append("strategy_name = ?")
            params.append(strategy)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if start is not None:
            clauses.append("entry_timestamp >= ?")
            params.append(_to_iso(start))
        if end is not None:
            clauses.append("entry_timestamp < ?")
            params.append(_to_iso(end))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT * FROM strategy_performance"
            + where
            + " ORDER BY entry_timestamp DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        out: list[dict] = []
        for row in rows:
            d = _row_to_dict(row) or {}
            # is_win is stored as INTEGER 0/1; restore Python bool
            if "is_win" in d and d["is_win"] is not None:
                d["is_win"] = bool(d["is_win"])
            # entry_context is JSON-encoded TEXT
            if d.get("entry_context") is not None and isinstance(d["entry_context"], str):
                d["entry_context"] = _parse_json(d["entry_context"])
            out.append(d)
        return out

    def write_performance(self, data: dict) -> int:
        placeholders = ", ".join("?" for _ in self._PERFORMANCE_COLUMNS)
        cols = ", ".join(self._PERFORMANCE_COLUMNS)
        sql = (
            f"INSERT INTO strategy_performance ({cols}) VALUES ({placeholders}) "
            "ON CONFLICT(strategy_name, pair, entry_timestamp, run_id) DO NOTHING"
        )
        with self._cursor() as cur:
            cur.execute(sql, self._performance_row(data))
            return cur.rowcount

    def write_performance_batch(self, trades: list[dict]) -> int:
        if not trades:
            return 0
        placeholders = ", ".join("?" for _ in self._PERFORMANCE_COLUMNS)
        cols = ", ".join(self._PERFORMANCE_COLUMNS)
        sql = (
            f"INSERT INTO strategy_performance ({cols}) VALUES ({placeholders}) "
            "ON CONFLICT(strategy_name, pair, entry_timestamp, run_id) DO NOTHING"
        )
        # executemany would be faster, but cursor.rowcount after
        # executemany on INSERT...ON CONFLICT is not reliable across
        # SQLite versions — fall back to per-row execute for an
        # accurate inserted-count.
        total = 0
        with self._cursor() as cur:
            for t in trades:
                cur.execute(sql, self._performance_row(t))
                if cur.rowcount > 0:
                    total += cur.rowcount
        return total

    def clear_performance(self, *, run_id: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_performance WHERE run_id = ?",
                (run_id,),
            )
            return cur.rowcount

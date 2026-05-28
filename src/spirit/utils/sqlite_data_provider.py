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
import uuid
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

    # ------------------------------------------------------------------
    # BYOD OHLC — customer-owned store (v2.2.4)
    # ------------------------------------------------------------------

    # 'live' sentinel mirrored from run_manager.LIVE_RUN_ID. Hard-coded
    # here to dodge a circular import (run_manager imports the
    # DataProvider via spirit.utils.data_provider, which imports this
    # file). If LIVE_RUN_ID ever changes, update both sites.
    _LIVE_RUN_ID = "live"

    _REQUIRED_CANDLE_KEYS = frozenset({"timestamp", "open", "high", "low", "close"})

    def _validate_candles(self, candles: list[dict]) -> None:
        """Raise ValueError on malformed candle dicts (Protocol contract)."""
        for i, c in enumerate(candles):
            if not isinstance(c, dict):
                raise ValueError(
                    f"candles[{i}] must be a dict; got {type(c).__name__}"
                )
            missing = self._REQUIRED_CANDLE_KEYS - set(c.keys())
            if missing:
                raise ValueError(
                    f"candles[{i}] missing required keys: {sorted(missing)}"
                )

    @staticmethod
    def _candle_ts_iso(c: dict) -> str:
        """Normalise a candle's timestamp to ISO-8601 UTC text.

        Accepts either a datetime or an already-ISO string. Naive
        datetimes are interpreted as UTC (Rule 11).
        """
        ts = c["timestamp"]
        if isinstance(ts, datetime):
            return _to_iso(ts)
        # Assume ISO-shaped string. Don't reparse — the dedupe relies on
        # byte-equal PK values, so callers feeding mixed formats here
        # would dedupe incorrectly. If misformatted, the INSERT will
        # surface the violation at write time, not silently.
        return str(ts)

    def _persist_user_candles(
        self,
        *,
        source: str,
        pair: str,
        interval: int,
        candles: list[dict],
    ) -> dict:
        """Shared transactional batch insert + audit row for upload + append.

        Returns ``{"batch_id", "rows_inserted", "rows_skipped",
        "min_timestamp", "max_timestamp"}``. min/max are extra keys
        beyond the strict Protocol contract — parity with the cloud
        gateway response shape, harmless for callers that ignore them.
        """
        if not candles:
            return {
                "batch_id": "",
                "rows_inserted": 0,
                "rows_skipped": 0,
                "min_timestamp": None,
                "max_timestamp": None,
            }
        self._validate_candles(candles)
        batch_id = uuid.uuid4().hex
        ts_iso = [self._candle_ts_iso(c) for c in candles]
        min_ts = min(ts_iso)
        max_ts = max(ts_iso)

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_ohlc_batches
                    (batch_id, source, pair, interval,
                     min_timestamp, max_timestamp, row_count, created_by)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (batch_id, source, pair, interval, min_ts, max_ts),
            )

            # ON CONFLICT DO NOTHING idempotent insert. cursor.rowcount
            # after executemany on ON CONFLICT is not reliable across
            # SQLite versions, so count via COUNT(*) deltas — same
            # pattern strategy_performance uses.
            cur.execute(
                "SELECT COUNT(*) FROM user_ohlc WHERE pair = ? AND interval = ?",
                (pair, interval),
            )
            before = cur.fetchone()[0]
            cur.executemany(
                """
                INSERT INTO user_ohlc
                    (pair, interval, timestamp,
                     open, high, low, close,
                     vwap, volume, count, batch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair, interval, timestamp) DO NOTHING
                """,
                [
                    (pair, interval, ts,
                     c["open"], c["high"], c["low"], c["close"],
                     c.get("vwap"), c.get("volume"), c.get("count"),
                     batch_id)
                    for c, ts in zip(candles, ts_iso)
                ],
            )
            cur.execute(
                "SELECT COUNT(*) FROM user_ohlc WHERE pair = ? AND interval = ?",
                (pair, interval),
            )
            after = cur.fetchone()[0]
            rows_inserted = after - before
            rows_skipped = len(candles) - rows_inserted

            if rows_inserted > 0:
                cur.execute(
                    "UPDATE user_ohlc_batches SET row_count = ? WHERE batch_id = ?",
                    (rows_inserted, batch_id),
                )
            else:
                # Full dedupe — no candles landed under this batch_id, so the
                # audit row carries no information the return dict doesn't
                # already give the caller. Drop it rather than leave a
                # row_count=0 record that accumulates on every re-upload
                # (#812). FK-safe: ON DELETE CASCADE has no children to
                # cascade to because nothing was inserted.
                cur.execute(
                    "DELETE FROM user_ohlc_batches WHERE batch_id = ?",
                    (batch_id,),
                )

        if rows_skipped > 0:
            logger.info(
                "[BYOD] %s %s/%dm batch=%s: inserted=%d skipped=%d (dedupe)",
                source, pair, interval, batch_id[:8], rows_inserted, rows_skipped,
            )
        else:
            logger.info(
                "[BYOD] %s %s/%dm batch=%s: inserted=%d",
                source, pair, interval, batch_id[:8], rows_inserted,
            )

        return {
            "batch_id": batch_id,
            "rows_inserted": rows_inserted,
            "rows_skipped": rows_skipped,
            "min_timestamp": min_ts,
            "max_timestamp": max_ts,
        }

    def upload_user_ohlc(
        self,
        pair: str,
        interval: int,
        candles: list[dict],
    ) -> dict:
        """Bulk-seed user-owned OHLC. Source tagged ``'csv_upload'``."""
        return self._persist_user_candles(
            source="csv_upload", pair=pair, interval=interval, candles=candles,
        )

    def append_user_ohlc(
        self,
        pair: str,
        interval: int,
        candles: list[dict],
    ) -> dict:
        """Incremental forward-tick append. Source tagged ``'live'``."""
        return self._persist_user_candles(
            source="live", pair=pair, interval=interval, candles=candles,
        )

    def get_user_ohlc(
        self,
        pair: str,
        interval: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
        order: str = "asc",
    ) -> list[dict]:
        """Read back user-owned OHLC. Same row shape as ``get_ohlc``.

        Half-open window: ``[start, end)``. ``order='desc' limit=1``
        returns the most recent local candle — the path used by the
        boot-time catch-up runner to find its resume point.
        """
        clauses = ["pair = ?", "interval = ?"]
        params: list[Any] = [pair, interval]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(_to_iso(start))
        if end is not None:
            clauses.append("timestamp < ?")
            params.append(_to_iso(end))
        direction = "ASC" if order.lower() == "asc" else "DESC"
        sql = (
            "SELECT pair, interval, timestamp, "
            "open, high, low, close, vwap, volume, count "
            "FROM user_ohlc WHERE " + " AND ".join(clauses) +
            f" ORDER BY timestamp {direction} LIMIT ?"
        )
        params.append(int(limit))
        with self._cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        out: list[dict] = []
        for row in rows:
            d = _row_to_dict(row) or {}
            # Match the `get_ohlc` row shape: rename timestamp → datetime
            # so callers can drop one source for the other without churn.
            if "timestamp" in d:
                d["datetime"] = d.pop("timestamp")
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Run management — derive list and delete from strategy_performance
    # ------------------------------------------------------------------

    def list_runs(self, *, limit: int = 30) -> list[dict]:
        """Derive a run list from ``strategy_performance`` via GROUP BY.

        Free tier has no separate ``replay_runs`` registry, so we
        synthesise one-row-per-run from the trade-outcome table.
        Returns the 14-field shape main.py expects, with paid-only
        fields (tag, git_hash, profit_factor) as ``None``.
        """
        sql = """
            SELECT
                run_id,
                strategy_name,
                COUNT(*)                                              AS total_trades,
                AVG(CASE WHEN is_win = 1 THEN 1.0 ELSE 0.0 END)       AS win_rate,
                SUM(pnl_pct)                                          AS net_pnl_pct,
                MIN(entry_timestamp)                                  AS started_at,
                MAX(timestamp)                                        AS completed_at,
                GROUP_CONCAT(DISTINCT pair)                           AS pairs
            FROM strategy_performance
            WHERE run_id != ?
            GROUP BY run_id, strategy_name
            ORDER BY MIN(entry_timestamp) DESC
            LIMIT ?
        """
        with self._cursor() as cur:
            cur.execute(sql, (self._LIVE_RUN_ID, int(limit)))
            rows = cur.fetchall()

        out: list[dict] = []
        for row in rows:
            started_at = row["started_at"]
            completed_at = row["completed_at"]
            out.append({
                "id": row["run_id"],
                "tag": None,                  # not tracked on Free
                "strategy_name": row["strategy_name"],
                "pairs": row["pairs"],
                "start_date": (started_at or "")[:10] or None,
                "end_date": (completed_at or "")[:10] or None,
                "git_hash": None,             # not tracked on Free
                "status": "completed",        # derived rows are always done
                "started_at": started_at,
                "completed_at": completed_at,
                "total_trades": row["total_trades"],
                "win_rate": row["win_rate"],
                "profit_factor": None,        # needs separate wins/losses sums
                "net_pnl_pct": row["net_pnl_pct"],
            })
        return out

    def delete_run(self, run_id: str) -> dict:
        """Delete a run's rows from ``strategy_performance``.

        Free tier returns zero for the three paid-only tables
        (scorer_outcomes, risk_gate_decisions, replay_runs) — they
        don't exist locally. Refuses ``run_id='live'`` to protect the
        live trade history. Nonexistent run_id returns all zeros
        without erroring (DELETE rowcount is 0 naturally).
        """
        if run_id == self._LIVE_RUN_ID:
            raise ValueError(
                "delete_run refuses run_id='live' to protect live trade history."
            )
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_performance WHERE run_id = ?",
                (run_id,),
            )
            deleted = cur.rowcount
        return {
            "strategy_performance": deleted,
            "scorer_outcomes": 0,
            "risk_gate_decisions": 0,
            "replay_runs": 0,
        }

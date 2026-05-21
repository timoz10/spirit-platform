-- Free-tier Spirit local schema (#561 Day 1)
--
-- Bundled DDL applied automatically on first SqliteDataProvider connect
-- via `executescript` (idempotent — every statement uses IF NOT EXISTS).
--
-- Logical parity with PG (`scripts/decision_engine/sql/`,
-- `scripts/migrations/`): same column names and dict shapes; physical
-- types translated per Rule 11:
--
--     PG TIMESTAMPTZ  → TEXT  (ISO-8601 UTC, e.g. 2026-05-06T12:34:56+00:00)
--     PG NUMERIC      → REAL
--     PG BOOLEAN      → INTEGER (0/1)
--     PG JSONB        → TEXT  (json.dumps; consumer json.loads)
--     PG SERIAL       → INTEGER PRIMARY KEY AUTOINCREMENT
--
-- The `instance` column is omitted from `spirit_state` and
-- `strategy_performance`: file-level isolation replaces RLS row-scoping
-- (one SQLite file per instance under `~/.spirit/<instance>/`). It is
-- kept on `daemon_heartbeats` to match the PG composite PK (#225) so
-- `write_heartbeat(...)` keeps an identical signature.

-- ---------------------------------------------------------------------
-- schema_version — single-row-per-component migration ledger
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    component   TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT OR IGNORE INTO schema_version (component, version)
VALUES ('spirit_local', 1);

-- ---------------------------------------------------------------------
-- spirit_state — crash-recovery key/value store
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spirit_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,                 -- JSON
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------
-- daemon_heartbeats — daemon liveness ping
-- Composite PK matches PG (#225) so multiple daemons can coexist.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daemon_heartbeats (
    daemon_id       TEXT NOT NULL,
    instance        TEXT NOT NULL DEFAULT 'local',
    status          TEXT NOT NULL DEFAULT 'ok'
                    CHECK (status IN ('ok', 'error', 'starting')),
    metadata        TEXT,                      -- JSON, nullable
    run_id          TEXT NOT NULL DEFAULT 'live',
    last_heartbeat  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (daemon_id, instance)
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_stale
    ON daemon_heartbeats(instance, last_heartbeat)
    WHERE status <> 'starting';

-- ---------------------------------------------------------------------
-- strategy_performance — per-trade outcomes
--
-- PG: scripts/decision_engine/sql/create_strategy_performance.sql
-- + add_entry_context.sql, add_run_id.sql, plus later ALTERs picked up
-- from the live `\d public.strategy_performance` definition (id,
-- exit_engine_version, mfe_pct, mae_pct, order_type, limit_price).
--
-- `instance` is omitted (one SQLite file per Spirit instance).
-- `source` default narrowed from 'backtest' to 'paper' — Free-tier
-- usage is paper trading, not historical backfill.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_performance (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,            -- exit time
    entry_timestamp     TEXT    NOT NULL,
    pair                TEXT    NOT NULL,
    strategy_name       TEXT    NOT NULL,
    is_win              INTEGER NOT NULL,            -- 0/1
    pnl_pct             REAL,
    entry_price         REAL,
    exit_price          REAL,
    exit_reason         TEXT,
    regime_at_entry     TEXT,
    dlimit_trend_state  TEXT,
    volatility_regime   TEXT,
    source              TEXT    NOT NULL DEFAULT 'paper',
    trade_id            INTEGER,
    created_at          TEXT             DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    order_type          TEXT,
    limit_price         REAL,
    run_id              TEXT    NOT NULL DEFAULT 'live',
    exit_engine_version TEXT,
    mfe_pct             REAL,
    mae_pct             REAL,
    entry_context       TEXT                          -- JSON (no GIN equivalent in SQLite)
);

CREATE INDEX IF NOT EXISTS idx_sp_strategy_entry_ts
    ON strategy_performance(strategy_name, entry_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_sp_source
    ON strategy_performance(source, entry_timestamp DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sp_no_dupes
    ON strategy_performance(strategy_name, pair, entry_timestamp, run_id);

CREATE INDEX IF NOT EXISTS idx_sp_run_id
    ON strategy_performance(run_id);

-- ---------------------------------------------------------------------
-- user_ohlc + user_ohlc_batches — BYOD OHLC store (v2.2.4)
--
-- Mirror of cloud `public.user_ohlc_uploads` + `user_ohlc_upload_batches`
-- (#666) minus the `instance` column — Free tier uses file-level
-- isolation instead of PG RLS row-scoping.
--
-- Composite PK on (pair, interval, timestamp) gives idempotent
-- ON CONFLICT DO NOTHING upsert semantics (re-uploads silently
-- dedupe). Batch row anchors the audit trail with source tag
-- ('csv_upload' for bulk seed, 'live' for incremental forward-tick)
-- so `list_runs`-style introspection can tell the two paths apart.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_ohlc_batches (
    batch_id       TEXT PRIMARY KEY,                -- uuid4().hex on insert
    source         TEXT NOT NULL                    -- 'csv_upload' | 'live'
                   CHECK (source IN ('csv_upload', 'live')),
    pair           TEXT NOT NULL,
    interval       INTEGER NOT NULL,
    min_timestamp  TEXT NOT NULL,                   -- ISO-8601 UTC
    max_timestamp  TEXT NOT NULL,                   -- ISO-8601 UTC
    row_count      INTEGER NOT NULL DEFAULT 0,      -- updated after dedupe
    created_by     TEXT,                            -- nullable for Free
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS user_ohlc (
    pair       TEXT    NOT NULL,
    interval   INTEGER NOT NULL,
    timestamp  TEXT    NOT NULL,                    -- ISO-8601 UTC
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    vwap       REAL,
    volume     REAL,
    count      INTEGER,
    batch_id   TEXT    NOT NULL
               REFERENCES user_ohlc_batches(batch_id) ON DELETE CASCADE,
    PRIMARY KEY (pair, interval, timestamp)
);

-- Window queries (catch-up runner reads order=desc limit=1; replay reads
-- a [start, end) range). The PK already covers (pair, interval, ts) so
-- this index is mostly belt-and-braces for the DESC order path.
CREATE INDEX IF NOT EXISTS idx_user_ohlc_pair_interval_ts
    ON user_ohlc(pair, interval, timestamp DESC);

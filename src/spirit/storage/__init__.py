"""Local storage for Free-tier Spirit (#561).

Bundled SQLite schema and provider live here. Free-tier instances run
without the API gateway: state, heartbeats, and trade outcomes write
to a single SQLite file at `~/.spirit/<instance>/spirit.db`.

The DDL in `sqlite_schema.sql` keeps logical parity with the PG schema
(`scripts/decision_engine/sql/`, `scripts/migrations/`) — same column
names and dict shapes — with PG types translated per Rule 11.
"""

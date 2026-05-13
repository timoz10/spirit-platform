"""Kraken CSV parser for BYOD OHLC backfill (#666 sub-task 9b).

kraken.com/exports produces per-pair CSV files with the schema:

    unix_timestamp,open,high,low,close,volume,count

The timestamp column is integer epoch seconds. Older exports omit the
final `count` column (6 columns total). Header rows are sometimes
present (when the user opens + re-saves in a spreadsheet); we detect
them defensively by parse-failure on the first field.

Filename convention is `<PAIR>_<INTERVAL>.csv` (e.g. XBTUSD_1.csv,
ETHUSD_60.csv). The setup wizard prompts the user for pair + interval
explicitly, so this module doesn't need to parse them out of the path.

Design choices:
  - Streaming generator (yields lists of dicts) rather than loading
    the whole file. A year of 1m candles is ~525k rows ~= 30 MB CSV;
    we want to stream rather than load.
  - Yields dicts shaped to match `UserOhlcCandle` so they round-trip
    cleanly through ApiDataProvider.upload_user_ohlc.
  - Malformed rows are skipped with a count returned alongside, so the
    caller can warn the user without aborting the whole upload.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class CsvParseStats:
    """Counters returned alongside the chunk stream."""
    rows_parsed: int = 0
    rows_skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def _bump(self, reason: str) -> None:
        self.rows_skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


def _row_to_candle(row: list[str]) -> dict:
    """Convert one CSV row to the wire-shape dict /v1/ohlc/upload expects.

    Raises ValueError on any parse failure — caller catches and counts.
    """
    if len(row) < 6:
        raise ValueError(f"too few columns ({len(row)})")

    ts_epoch = int(row[0])
    dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)

    candle = {
        "timestamp": dt.isoformat(),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "vwap": None,
        "count": int(row[6]) if len(row) >= 7 and row[6] != "" else 0,
    }
    return candle


def iter_kraken_csv_chunks(
    path: str | Path,
    chunk_size: int = 50_000,
) -> Iterator[tuple[list[dict], CsvParseStats]]:
    """Yield (chunk, stats_so_far) tuples streamed from a Kraken OHLC CSV.

    Args:
        path:       filesystem path to the CSV file
        chunk_size: rows per yielded chunk; default matches the
                    /v1/ohlc/upload per-call cap (50k)

    The final yield is always a tuple — the chunk may be empty if the
    file ended on a chunk_size boundary, but the caller can use the
    final stats object for the summary log.

    Yields:
        (candles_in_chunk, stats_to_date)

    Raises:
        FileNotFoundError if path doesn't exist
        OSError on read failure
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    stats = CsvParseStats()
    buf: list[dict] = []

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for line_no, row in enumerate(reader, start=1):
            # Skip blank lines and "all-empty" rows that csv may emit.
            if not row or all(not c.strip() for c in row):
                continue
            try:
                candle = _row_to_candle(row)
            except ValueError:
                # First-line parse failure is almost always a header row
                # (`timestamp,open,...`); skip silently. Later failures
                # are noisy data — count them.
                if line_no == 1:
                    continue
                stats._bump("parse_error")
                continue

            buf.append(candle)
            stats.rows_parsed += 1
            if len(buf) >= chunk_size:
                yield buf, stats
                buf = []

    # Always yield the trailing buffer (may be empty) so the caller has
    # a final stats handle. Skip yield-empty for chunk_size-boundary
    # files only if buf is empty AND we yielded at least once.
    if buf or stats.rows_parsed == 0:
        yield buf, stats

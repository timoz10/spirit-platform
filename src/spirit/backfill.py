"""Spirit BYOD OHLC backfill — standalone CLI for the bulk CSV import path.

The setup wizard offers this at install time (see setup.py:_offer_csv_backfill).
This module is the same code path exposed as a re-runnable subcommand so users
can import additional pairs, retry after a connectivity blip, or re-import a
corrected CSV — without restarting Spirit or re-running the full wizard.

Spirit can be running side-by-side while this runs: push-on-fetch writes the
current window, this writes the historical window, ON CONFLICT DO NOTHING
dedups the seam.

Usage
-----
Filename-driven (recommended):

    python3 -m spirit.backfill ~/Downloads/XBTUSDT_60.csv

The pair + interval are parsed from the filename per Kraken's export
convention `<PAIR>_<INTERVAL>.csv`. Explicit flags override:

    python3 -m spirit.backfill data.csv --pair XBTUSDT --interval 60

Environment:
    SPIRIT_API_URL  default: https://api.tradebot.live/v1
    SPIRIT_API_KEY  required (read from env or --api-key)

Exit codes:
    0   success
    1   unexpected error
    2   missing/invalid args (file not found, can't infer pair/interval, …)
    3   gateway rejected the request (auth or capability)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_FILENAME_PATTERN = re.compile(r"^(?P<pair>[A-Z]+)_(?P<interval>\d+)\.csv$",
                                re.IGNORECASE)


def parse_filename(path: str | Path) -> tuple[str | None, int | None]:
    """Return (pair, interval) inferred from a Kraken-convention filename, or
    (None, None) if the basename doesn't match `<PAIR>_<INTERVAL>.csv`.

    Pair is uppercased to match Spirit's internal convention.
    """
    name = Path(path).name
    m = _FILENAME_PATTERN.match(name)
    if not m:
        return None, None
    return m.group("pair").upper(), int(m.group("interval"))


def _resolve_args(args: argparse.Namespace) -> tuple[str, int]:
    """Resolve --pair / --interval, falling back to filename parse.

    Raises SystemExit(2) with a clear message if neither flag nor filename
    yields a value.
    """
    inferred_pair, inferred_interval = parse_filename(args.path)

    pair = args.pair or inferred_pair
    if not pair:
        print(
            f"ERROR: --pair not given and filename {Path(args.path).name!r} "
            f"doesn't match <PAIR>_<INTERVAL>.csv. Re-run with --pair XBTUSDT.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    interval = args.interval if args.interval is not None else inferred_interval
    if interval is None:
        print(
            f"ERROR: --interval not given and filename {Path(args.path).name!r} "
            f"doesn't carry it. Re-run with --interval 60.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return pair.upper(), int(interval)


def run(
    path: str | Path,
    pair: str,
    interval: int,
    *,
    chunk_size: int = 50_000,
) -> int:
    """Stream the CSV through ``get_data_provider().upload_user_ohlc``.

    Provider resolution is tier-driven (v2.2.4):
      - Free   → CompositeDataProvider → SqliteDataProvider (local file)
      - Paid   → ApiDataProvider (POST /v1/ohlc/upload)

    Both tiers honour the same Protocol contract: idempotent dedupe,
    ``{batch_id, rows_inserted, rows_skipped}`` return shape.

    Pure function — no argparse, no env reading. Caller owns those, which
    lets tests drive this without monkeypatching CLI plumbing.
    """
    from spirit.utils.data_provider import get_data_provider
    from spirit.utils.kraken_csv import iter_kraken_csv_chunks

    if not Path(path).exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    provider = get_data_provider()

    chunks_uploaded = 0
    total_inserted = 0
    total_skipped = 0
    final_stats = None

    print(
        f"Streaming {path} as ({pair}, {interval}m) → "
        f"{type(provider).__name__}.upload_user_ohlc"
    )
    try:
        for chunk, stats in iter_kraken_csv_chunks(path, chunk_size=chunk_size):
            final_stats = stats
            if not chunk:
                continue
            resp = provider.upload_user_ohlc(pair, interval, chunk)
            chunks_uploaded += 1
            inserted = resp.get("rows_inserted", 0)
            skipped = resp.get("rows_skipped", 0)
            total_inserted += inserted
            total_skipped += skipped
            print(
                f"  chunk {chunks_uploaded}: "
                f"sent={len(chunk)} inserted={inserted} skipped={skipped} "
                f"(running total: parsed={stats.rows_parsed})"
            )
    except Exception as e:
        # Capability denial only fires on paid-tier gateway calls.
        # Free tier (SqliteDataProvider) raises ValueError on malformed
        # input; anything else is HTTP / network / pydantic / disk.
        from spirit.utils.api_data_provider import CapabilityDeniedError
        if isinstance(e, CapabilityDeniedError):
            print(
                f"\nERROR: gateway denied capability '{e.capability}'. "
                f"Your key may have lost write:ohlc_user.",
                file=sys.stderr,
            )
            return 3
        print(f"\nERROR: upload failed mid-stream: {e}", file=sys.stderr)
        print(
            f"Partial result: {chunks_uploaded} chunks, "
            f"{total_inserted} rows inserted. Re-run to retry; "
            f"already-uploaded candles dedupe.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nUpload complete: {chunks_uploaded} chunks, "
        f"{total_inserted} new rows, {total_skipped} duplicates."
    )
    if final_stats and final_stats.rows_skipped:
        print(
            f"  ({final_stats.rows_skipped} malformed rows skipped — "
            f"reasons: {dict(final_stats.skip_reasons)})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m spirit.backfill",
        description=(
            "Bulk-upload OHLC from a Kraken CSV export to your local "
            "(Free) or scoped cloud (Paid) OHLC store. Safe to run while "
            "Spirit is running — concurrent live pushes and historical "
            "uploads dedupe via the composite-PK ON CONFLICT path."
        ),
    )
    parser.add_argument("path", help="path to the Kraken CSV file")
    parser.add_argument(
        "--pair",
        help="trading pair (default: parsed from filename <PAIR>_<INTERVAL>.csv)",
    )
    parser.add_argument(
        "--interval", type=int,
        help="candle interval in minutes "
             "(default: parsed from filename <PAIR>_<INTERVAL>.csv)",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=50_000,
        help="rows per upload call (default: 50000, matches /v1/ohlc/upload cap)",
    )
    args = parser.parse_args(argv)

    pair, interval = _resolve_args(args)
    return run(
        path=args.path,
        pair=pair,
        interval=interval,
        chunk_size=args.chunk_size,
    )


if __name__ == "__main__":
    sys.exit(main())

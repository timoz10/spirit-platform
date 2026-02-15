from dataclasses import dataclass
from typing import List
from datetime import datetime

@dataclass
class OHLCRecord:
    pair: str
    interval: int
    datetime: str  # ISO8601 string
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    count: int
    timestamp: int

    @staticmethod
    def from_raw(
        pair: str,
        interval: int,
        dt_raw,
        open_,
        high,
        low,
        close,
        vwap,
        volume,
        count,
        timestamp
    ):
        # dt_raw can be a timestamp or string; convert to ISO8601
        if isinstance(dt_raw, (int, float)):
            dt_iso = datetime.utcfromtimestamp(dt_raw).isoformat()
        elif isinstance(dt_raw, str):
            try:
                dt_iso = datetime.fromisoformat(dt_raw).isoformat()
            except Exception:
                dt_iso = dt_raw  # fallback, if already ISO
        else:
            dt_iso = str(dt_raw)
        return OHLCRecord(
            pair=pair,
            interval=interval,
            datetime=dt_iso,
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            vwap=float(vwap) if vwap is not None else 0.0,
            volume=float(volume),
            count=int(count) if count is not None else 0,
            timestamp=int(timestamp) if timestamp is not None else 0
        )

@dataclass
class OHLCData:
    records: List[OHLCRecord]

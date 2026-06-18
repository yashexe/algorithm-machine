"""
BarCache — on-disk Parquet cache for BarEvents.

Layout:  {cache_dir}/{SYMBOL}/{bar_type}_{adj|raw}.parquet
Index:   UTC-aware DatetimeIndex named "timestamp"
Columns: open, high, low, close (float64), volume (int64),
         source (str), is_complete (bool)

Prices are stored as float64 (Parquet has no native Decimal type).
Decimal conversion happens on read, at the same boundary as the network
path — this is consistent with DATA_PIPELINE.md §3's guarantee that
"conversion from float happens once, at the normalizer boundary."

Atomic writes: write to a temp file in the same directory, then
os.rename(). On POSIX, rename() is atomic as long as src and dst are
on the same filesystem — which is guaranteed by the same-directory temp.

Cache invalidation (DATA_PIPELINE.md §6.2):
  Backtest mode — covers_range(symbol, start, end) → True means hit.
  Live mode     — is_stale() guards freshness; caller decides whether
                  to bypass the cache entirely (per spec: "always fetch
                  the latest bar" in paper mode).
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from engine.backtest.calendar import trading_days as _trading_days
from engine.events.types import BarEvent, BarType

logger = logging.getLogger(__name__)

_PRICE_COLS = ("open", "high", "low", "close")
_COVERAGE_THRESHOLD = 0.90  # require ≥90% of expected trading days to count as a cache hit


class BarCache:
    def __init__(
        self,
        cache_dir: str | Path,
        staleness_hours: int = 4,
        adjusted: bool = True,
    ) -> None:
        self._root = Path(cache_dir)
        self._staleness_hours = staleness_hours
        self._adjusted = adjusted

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def covers_range(
        self, symbol: str, bar_type: BarType, start: date, end: date
    ) -> bool:
        """
        True if the cache contains sufficient data for [start, end].

        Two conditions must hold:
          1. Boundary check: cached data spans at least [start, end].
          2. Density check: rows in [start, end] >= 90% of expected trading
             days. This catches interior gaps from partial fetches or
             data-vendor outages that the boundary check cannot detect.
        """
        path = self._path(symbol, bar_type)
        if not path.exists():
            return False
        try:
            idx = pd.read_parquet(path, columns=[]).index
            if idx.empty:
                return False
            cached_start: date = idx.min().date()
            cached_end: date = idx.max().date()
            if not (cached_start <= start and cached_end >= end):
                return False
            # Density check — count rows in the requested window.
            dates = idx.date
            row_count = int(((dates >= start) & (dates <= end)).sum())
            expected = len(_trading_days(start, end))
            return expected == 0 or row_count >= _COVERAGE_THRESHOLD * expected
        except Exception as exc:
            logger.warning("covers_range check failed for %s: %s", symbol, exc)
            return False

    def is_stale(self, symbol: str, bar_type: BarType) -> bool:
        """
        True if the most recent cached bar is older than staleness_hours,
        or if no cache file exists.
        """
        path = self._path(symbol, bar_type)
        if not path.exists():
            return True
        try:
            idx = pd.read_parquet(path, columns=[]).index
            if idx.empty:
                return True
            latest: pd.Timestamp = idx.max()
            if latest.tzinfo is None:
                latest = latest.tz_localize("UTC")
            age_seconds = (
                datetime.now(timezone.utc) - latest.to_pydatetime()
            ).total_seconds()
            return age_seconds > self._staleness_hours * 3600
        except Exception as exc:
            logger.warning("is_stale check failed for %s: %s", symbol, exc)
            return True

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(
        self, symbol: str, bar_type: BarType, start: date, end: date
    ) -> list[BarEvent]:
        """
        Return cached bars for symbol filtered to [start, end] inclusive.
        Returns an empty list on cache miss or read error (never raises).
        """
        path = self._path(symbol, bar_type)
        if not path.exists():
            return []
        try:
            df = pd.read_parquet(path)
            if df.empty:
                return []
            df = _filter_date_range(df, start, end)
            return _df_to_bars(df, symbol, bar_type)
        except Exception as exc:
            logger.warning("Cache read failed for %s: %s — treating as miss", symbol, exc)
            return []

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, bars: list[BarEvent], bar_type: BarType) -> None:
        """
        Persist bars to the Parquet cache, grouped by symbol.
        Merges with any existing cached rows (deduplicating by timestamp,
        keeping the newest version). Atomic per-symbol write.
        """
        if not bars:
            return

        by_symbol: dict[str, list[BarEvent]] = {}
        for bar in bars:
            by_symbol.setdefault(bar.symbol, []).append(bar)

        for symbol, symbol_bars in by_symbol.items():
            self._write_symbol(symbol, bar_type, symbol_bars)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _path(self, symbol: str, bar_type: BarType) -> Path:
        suffix = "adj" if self._adjusted else "raw"
        return self._root / symbol.upper() / f"{bar_type.value}_{suffix}.parquet"

    def _write_symbol(
        self, symbol: str, bar_type: BarType, bars: list[BarEvent]
    ) -> None:
        path = self._path(symbol, bar_type)
        path.parent.mkdir(parents=True, exist_ok=True)

        new_df = _bars_to_df(bars)

        # Merge with any existing cached data
        if path.exists():
            try:
                existing = pd.read_parquet(path)
                combined = pd.concat([existing, new_df])
                # Keep the newest row when timestamps collide
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.sort_index(inplace=True)
                new_df = combined
            except Exception as exc:
                logger.warning(
                    "Could not merge with existing cache for %s (%s) — overwriting: %s",
                    symbol, bar_type.value, exc,
                )

        # Atomic write: temp file in same directory, then rename
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=path.parent, prefix=".tmp_", suffix=".parquet"
        )
        tmp_path = Path(tmp_path_str)
        try:
            os.close(tmp_fd)
            new_df.to_parquet(tmp_path)
            os.replace(tmp_path, path)  # os.replace is atomic on POSIX and Windows
        except Exception as exc:
            logger.error("Cache write failed for %s: %s", symbol, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ------------------------------------------------------------------
# DataFrame ↔ BarEvent conversion helpers
# ------------------------------------------------------------------

def _bars_to_df(bars: list[BarEvent]) -> pd.DataFrame:
    """Serialise a list of BarEvents to a DataFrame suitable for Parquet storage."""
    index = pd.DatetimeIndex(
        [b.timestamp for b in bars], name="timestamp", tz="UTC"
    )
    data = {
        "open":        [float(b.open)   for b in bars],
        "high":        [float(b.high)   for b in bars],
        "low":         [float(b.low)    for b in bars],
        "close":       [float(b.close)  for b in bars],
        "volume":      [b.volume        for b in bars],
        "source":      [b.source        for b in bars],
        "is_complete": [b.is_complete   for b in bars],
    }
    return pd.DataFrame(data, index=index)


def _df_to_bars(df: pd.DataFrame, symbol: str, bar_type: BarType) -> list[BarEvent]:
    """Deserialise a Parquet-sourced DataFrame back to BarEvents."""
    bars: list[BarEvent] = []
    for ts, row in df.iterrows():
        dt = _to_utc_datetime(ts)
        bars.append(
            BarEvent(
                timestamp=dt,
                symbol=symbol,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                bar_type=bar_type,
                source=str(row.get("source", "cache")),
                is_complete=bool(row.get("is_complete", True)),
            )
        )
    return bars


def _filter_date_range(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    dates = df.index.date  # type: ignore[attr-defined]
    return df.loc[(dates >= start) & (dates <= end)]


def _to_utc_datetime(ts: object) -> datetime:
    if isinstance(ts, pd.Timestamp):
        dt = ts.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.fromisoformat(str(ts)).replace(tzinfo=timezone.utc)

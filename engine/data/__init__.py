"""
engine.data — data pipeline for the trading engine.

The public entry point is DataPipeline. All other types are re-exported
here so callers never need to import from submodules directly.

    from engine.data import DataPipeline, YFinanceFetcher, Normalizer, BarCache
"""

from __future__ import annotations

import logging
from datetime import date

from engine.data.cache import BarCache
from engine.data.fetchers.base import AbstractFetcher, DataFetchError, RawData
from engine.data.fetchers.yfinance import YFinanceFetcher
from engine.data.normalizer import Normalizer
from engine.events.types import BarEvent, BarType

logger = logging.getLogger(__name__)


class DataPipeline:
    """
    Orchestrates fetch → cache → normalise → return.

    For backtest mode:
      Checks covers_range() per symbol. Cache hit → read from disk.
      Cache miss → fetch from source, write to cache, return bars.

    For live / paper mode:
      Always fetches the latest bar from the source. Writes to cache
      for persistence but does not read from it (per DATA_PIPELINE.md §6.3).

    Returns bars sorted by (timestamp, symbol) for deterministic replay.
    The EventBus, not this class, is responsible for publishing.
    """

    def __init__(
        self,
        fetcher: AbstractFetcher,
        normalizer: Normalizer,
        cache: BarCache | None = None,
        live_mode: bool = False,
    ) -> None:
        self._fetcher = fetcher
        self._normalizer = normalizer
        self._cache = cache
        self._live_mode = live_mode

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_bars(
        self, symbols: list[str], start: date, end: date
    ) -> list[BarEvent]:
        """
        Fetch and normalise bars for every symbol over [start, end] inclusive.

        Returns a flat list sorted by (timestamp, symbol). Symbols that
        return no data are silently excluded (a WARNING is logged by the
        fetcher / normalizer).
        """
        bar_type = self._normalizer.bar_type
        symbols_upper = [s.upper() for s in symbols]

        cached_bars: list[BarEvent] = []
        to_fetch: list[str] = []

        if self._live_mode or self._cache is None:
            to_fetch = symbols_upper
        else:
            for symbol in symbols_upper:
                if self._cache.covers_range(symbol, bar_type, start, end):
                    cached_bars.extend(
                        self._cache.read(symbol, bar_type, start, end)
                    )
                    logger.debug("Cache hit: %s [%s, %s]", symbol, start, end)
                else:
                    to_fetch.append(symbol)
                    logger.debug("Cache miss: %s [%s, %s]", symbol, start, end)

        fetched_bars: list[BarEvent] = []
        if to_fetch:
            raw_data = self._fetcher.fetch(to_fetch, start, end)
            fetched_bars = self._normalizer.normalize(raw_data)
            if self._cache is not None:
                self._cache.write(fetched_bars, bar_type)

        all_bars = cached_bars + fetched_bars
        all_bars.sort(key=lambda b: (b.timestamp, b.symbol))
        return all_bars

    # ------------------------------------------------------------------
    # Factory convenience
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        cache_dir: str = ".cache/bars",
        batch_size: int = 50,
        staleness_hours: int = 4,
        bar_close_utc_hour: int = 21,
        live_mode: bool = False,
        adjusted: bool = True,
    ) -> "DataPipeline":
        """
        Construct a DataPipeline with the default yfinance fetcher and
        an on-disk Parquet cache. Useful for quick setup in tests and scripts.

        """
        fetcher = YFinanceFetcher(batch_size=batch_size, adjusted=adjusted)
        normalizer = Normalizer(bar_close_utc_hour=bar_close_utc_hour)
        cache = BarCache(cache_dir=cache_dir, staleness_hours=staleness_hours, adjusted=adjusted)
        return cls(fetcher=fetcher, normalizer=normalizer, cache=cache, live_mode=live_mode)


__all__ = [
    "DataPipeline",
    "AbstractFetcher",
    "DataFetchError",
    "RawData",
    "YFinanceFetcher",
    "Normalizer",
    "BarCache",
]

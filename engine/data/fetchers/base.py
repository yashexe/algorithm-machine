"""
AbstractFetcher contract and the RawData type that flows between
the fetcher layer and the Normalizer.

RawData is a dict mapping uppercase symbol → single-symbol OHLCV
DataFrame. Each DataFrame has:
  - A UTC-aware DatetimeIndex
  - Lowercase columns: open, high, low, close, volume
  - Adjusted prices (split- and dividend-corrected)
  - Rows that are entirely NaN already removed

The fetcher is responsible for data delivery only — validation,
Decimal conversion, and BarEvent construction are the Normalizer's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

# One entry per symbol. Empty DataFrame means no data was available.
RawData = dict[str, pd.DataFrame]


class DataFetchError(RuntimeError):
    """Raised when a fetcher cannot retrieve data after exhausting retries."""


class AbstractFetcher(ABC):
    SOURCE_ID: str  # subclasses declare this as a class-level constant

    @abstractmethod
    def fetch(self, symbols: list[str], start: date, end: date) -> RawData:
        """
        Fetch OHLCV bars for every symbol over the closed interval [start, end].

        Returns a RawData dict. Symbols for which no data is available are
        omitted from the result (not a KeyError). Missing symbols are logged
        at WARNING by the concrete implementation.

        Raises DataFetchError on unrecoverable network or source failures.
        """

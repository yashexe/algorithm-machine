"""
YFinanceFetcher — wraps yfinance.download() into the RawData contract.

Responsibilities:
  - Batch symbols into groups of at most `batch_size` per network call.
  - Normalise the returned DataFrame (flat or MultiIndex columns) into the
    canonical dict[symbol → OHLCV DataFrame] shape.
  - Raise DataFetchError on network failure.

Not responsible for: price validation, Decimal conversion, BarEvent
construction, or caching. Those belong to Normalizer and BarCache.

Column structure notes (yfinance >= 0.2):
  Multi-ticker download → MultiIndex columns, names=['Price', 'Ticker'].
    Level 0 = price field ('Open', 'High', 'Low', 'Close', 'Volume')
    Level 1 = ticker symbol ('AAPL', 'MSFT', …)
  Single-ticker string download → flat columns ('Open', 'High', …).
  Both cases are handled below; see _split_by_symbol().
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from engine.data.fetchers.base import AbstractFetcher, DataFetchError, RawData

logger = logging.getLogger(__name__)

_OHLCV = {"open", "high", "low", "close", "volume"}


class YFinanceFetcher(AbstractFetcher):
    SOURCE_ID = "yfinance"

    def __init__(self, batch_size: int = 50, adjusted: bool = True) -> None:
        self._batch_size = batch_size
        self._adjusted = adjusted

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(self, symbols: list[str], start: date, end: date) -> RawData:
        symbols = [s.upper() for s in symbols]
        result: RawData = {}
        for i in range(0, len(symbols), self._batch_size):
            batch = symbols[i : i + self._batch_size]
            result.update(self._fetch_batch(batch, start, end))
        return result

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _fetch_batch(self, symbols: list[str], start: date, end: date) -> RawData:
        import yfinance as yf  # deferred so the rest of the engine works without yfinance installed

        # yfinance treats `end` as exclusive
        yf_end = end + timedelta(days=1)

        try:
            raw = yf.download(
                tickers=symbols,
                start=start.isoformat(),
                end=yf_end.isoformat(),
                interval="1d",
                auto_adjust=self._adjusted,
                progress=False,
                threads=False,
            )
        except Exception as exc:
            raise DataFetchError(
                f"yfinance download failed for {symbols} [{start}, {end}]: {exc}"
            ) from exc

        if raw is None or raw.empty:
            logger.warning(
                "yfinance returned no data for %s [%s, %s]", symbols, start, end
            )
            return {}

        return self._split_by_symbol(raw, symbols)

    def _split_by_symbol(self, df: pd.DataFrame, symbols: list[str]) -> RawData:
        if isinstance(df.columns, pd.MultiIndex):
            return self._from_multiindex(df, symbols)
        # Single-ticker string path: flat columns
        if len(symbols) == 1:
            cleaned = _clean_columns(df)
            cleaned = cleaned.dropna(how="all")
            if cleaned.empty:
                logger.warning("No usable data for %s", symbols[0])
                return {}
            return {symbols[0]: _ensure_utc(cleaned)}
        # Unexpected: multi-symbol but flat columns — shouldn't happen
        logger.error("Unexpected flat columns for multi-symbol download: %s", symbols)
        return {}

    def _from_multiindex(self, df: pd.DataFrame, symbols: list[str]) -> RawData:
        result: RawData = {}
        for symbol in symbols:
            sym_df = _extract_symbol(df, symbol)
            if sym_df is None or sym_df.empty:
                logger.warning("No data returned by yfinance for %s", symbol)
                continue
            sym_df = _clean_columns(sym_df).dropna(how="all")
            if sym_df.empty:
                logger.warning("All rows NaN for %s — skipping", symbol)
                continue
            result[symbol] = _ensure_utc(sym_df)
        return result


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _extract_symbol(df: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """
    Pull one symbol's columns from a MultiIndex DataFrame.
    Tries level=1 (standard: Price / Ticker) then level=0 as a fallback
    for alternative column orderings across yfinance versions.
    """
    for level in (1, 0):
        try:
            sym_df = df.xs(symbol, axis=1, level=level)
            if not sym_df.empty:
                return sym_df
        except KeyError:
            continue
    return None


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase column names and keep only OHLCV fields."""
    df = df.copy()
    df.columns = [c.lower() if isinstance(c, str) else str(c).lower() for c in df.columns]
    keep = [c for c in df.columns if c in _OHLCV]
    return df[keep]


def _ensure_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee the DatetimeIndex is UTC-aware."""
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    elif str(df.index.tz) != "UTC":
        df.index = df.index.tz_convert("UTC")
    return df

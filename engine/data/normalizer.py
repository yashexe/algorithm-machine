"""
Normalizer — stateless transformer from RawData to list[BarEvent].

Responsibilities (from DATA_PIPELINE.md §4):
  1. Timestamp standardisation  — YYYY-MM-DD HH:00:00 UTC per bar_close_utc_hour
  2. Float → Decimal conversion — via Decimal(str(value)), once, at this boundary
  3. Volume coercion            — int; NaN → 0 with WARNING
  4. Completeness flag          — always True for historical bars
  5. Validation + drop          — see _validate_row(); drops with WARNING, never silently

Validation rules (DATA_PIPELINE.md §4.2):
  • No null prices                  (open/high/low/close must all be non-null)
  • Price positivity                (all price fields > 0)
  • OHLC coherence                  (low ≤ open ≤ high  and  low ≤ close ≤ high)
  • Non-negative volume             (zero is WARNING but not a drop; negative → 0)
  • No duplicate (symbol, timestamp) pairs in the output set
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import pandas as pd

from engine.data.fetchers.base import RawData
from engine.events.types import BarEvent, BarType

logger = logging.getLogger(__name__)

_PRICE_FIELDS = ("open", "high", "low", "close")


class Normalizer:
    """
    Converts RawData → list[BarEvent].

    Stateless between calls: no instance state is mutated by normalize().
    The `seen` duplicate-detection set is local to each call.
    """

    def __init__(
        self,
        bar_type: BarType = BarType.DAILY,
        bar_close_utc_hour: int = 21,
        source_id: str = "yfinance",
    ) -> None:
        self.bar_type = bar_type
        self._utc_hour = bar_close_utc_hour
        self._source_id = source_id

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def normalize(self, raw_data: RawData) -> list[BarEvent]:
        """
        Convert all symbols in raw_data to BarEvents.

        Invalid rows are dropped with a WARNING log entry. The returned
        list contains only bars that passed all validation rules.
        """
        bars: list[BarEvent] = []
        seen: set[tuple[str, datetime]] = set()

        for symbol, df in raw_data.items():
            bars.extend(self._normalize_symbol(symbol.upper(), df, seen))

        return bars

    # ------------------------------------------------------------------
    # Per-symbol normalisation
    # ------------------------------------------------------------------

    def _normalize_symbol(
        self,
        symbol: str,
        df: pd.DataFrame,
        seen: set[tuple[str, datetime]],
    ) -> list[BarEvent]:
        bars: list[BarEvent] = []

        for raw_ts, row in df.iterrows():
            ts = self._to_bar_timestamp(raw_ts)

            # ── Duplicate check ──────────────────────────────────────
            key = (symbol, ts)
            if key in seen:
                logger.warning("Duplicate bar dropped: %s @ %s", symbol, ts.date())
                continue

            # ── Price validation + Decimal conversion ────────────────
            prices = _extract_prices(symbol, ts, row)
            if prices is None:
                continue  # already logged inside _extract_prices

            o, h, l, c = prices["open"], prices["high"], prices["low"], prices["close"]

            # ── OHLC coherence ───────────────────────────────────────
            # Epsilon absorbs float→Decimal conversion noise from yfinance adjusted prices
            # (e.g. close microscopically above high by ~2e-14). Only drop on material violations.
            _eps = max(Decimal("1e-6"), c * Decimal("1e-8"))
            if not (l - _eps <= o <= h + _eps and l - _eps <= c <= h + _eps):
                logger.warning(
                    "OHLC incoherence for %s @ %s: O=%s H=%s L=%s C=%s — bar dropped",
                    symbol, ts.date(), o, h, l, c,
                )
                continue

            # ── Volume ───────────────────────────────────────────────
            volume = _coerce_volume(symbol, ts, row)

            seen.add(key)
            bars.append(
                BarEvent(
                    timestamp=ts,
                    symbol=symbol,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=volume,
                    bar_type=self.bar_type,
                    source=self._source_id,
                    is_complete=True,
                )
            )

        return bars

    # ------------------------------------------------------------------
    # Timestamp helpers
    # ------------------------------------------------------------------

    def _to_bar_timestamp(self, raw: object) -> datetime:
        """
        Map a DataFrame index value to the canonical bar timestamp.
        Daily bars land at YYYY-MM-DD <utc_hour>:00:00 UTC.
        """
        if isinstance(raw, pd.Timestamp):
            d = raw.date()
        elif isinstance(raw, datetime):
            d = raw.date()
        else:
            d = pd.Timestamp(raw).date()  # type: ignore[arg-type]

        return datetime(d.year, d.month, d.day, self._utc_hour, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------
# Module-level helpers (stateless, no access to Normalizer internals)
# ------------------------------------------------------------------

def _extract_prices(
    symbol: str,
    ts: datetime,
    row: pd.Series,
) -> dict[str, Decimal] | None:
    """
    Validate and convert the four price fields to Decimal.
    Returns None (and logs a WARNING) on any failure.
    """
    prices: dict[str, Decimal] = {}
    for field in _PRICE_FIELDS:
        raw_val = row.get(field)

        if pd.isna(raw_val):
            logger.warning(
                "Null %s for %s @ %s — bar dropped", field, symbol, ts.date()
            )
            return None

        try:
            dec = Decimal(str(raw_val))
        except InvalidOperation:
            logger.warning(
                "Cannot convert %s=%r to Decimal for %s @ %s — bar dropped",
                field, raw_val, symbol, ts.date(),
            )
            return None

        if dec <= 0:
            logger.warning(
                "Non-positive %s=%s for %s @ %s — bar dropped",
                field, dec, symbol, ts.date(),
            )
            return None

        prices[field] = dec

    return prices


def _coerce_volume(symbol: str, ts: datetime, row: pd.Series) -> int:
    """
    Coerce volume to int.  NaN → 0 (WARNING).  Negative → 0 (WARNING).
    Zero is allowed (WARNING only — not a drop condition).
    """
    raw_vol = row.get("volume")

    if pd.isna(raw_vol):
        logger.warning("Null volume for %s @ %s — using 0", symbol, ts.date())
        return 0

    vol = int(raw_vol)

    if vol < 0:
        logger.warning(
            "Negative volume %d for %s @ %s — clamped to 0", vol, symbol, ts.date()
        )
        return 0

    if vol == 0:
        logger.warning("Zero volume for %s @ %s", symbol, ts.date())

    return vol

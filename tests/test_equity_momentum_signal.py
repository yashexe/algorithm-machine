"""
Unit tests for EquityMomentumStrategy._momentum()

Verifies that lookback prices are anchored at p_end (skip offset applied
consistently) rather than from prices[-1] (today).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from engine.events.bus import EventBus
from engine.events.types import BarEvent, BarType, PortfolioSnapshotEvent
from strategies.equity_momentum import EquityMomentumStrategy

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy(skip: int, lookback: int) -> EquityMomentumStrategy:
    """Construct a bare EquityMomentumStrategy via __new__, bypassing __init__."""
    strat = object.__new__(EquityMomentumStrategy)
    strat._lookback = lookback
    strat._skip = skip
    strat._price_history = {}
    return strat


# ---------------------------------------------------------------------------
# Test 1 — lookback anchors are offset by skip
# ---------------------------------------------------------------------------

def test_momentum_skip_anchored_at_skip_point():
    """
    With skip=20 and lookback=252, all lookback prices must be measured
    relative to p_end = prices[-21], not prices[-1].

    A monotonically increasing series makes any mis-anchoring produce a
    detectably different (larger) return, so an exact assertion catches the bug.
    """
    skip = 20
    lookback = 252

    # Build 300-bar monotonically increasing series: prices[i] = 100 + i * 0.5
    prices = [100 + i * 0.5 for i in range(300)]

    # Manually compute expected values using the CORRECT anchored indices
    p_end = prices[-21]         # prices[279] = 239.5
    p_3m  = prices[-84]         # prices[216] = 208.0
    p_6m  = prices[-147]        # prices[153] = 176.5
    p_12m = prices[-273]        # prices[27]  = 113.5

    expected = (
        (p_end - p_3m)  / p_3m
        + (p_end - p_6m)  / p_6m
        + (p_end - p_12m) / p_12m
    ) / 3.0

    strat = _make_strategy(skip=skip, lookback=lookback)
    strat._price_history = {"AAPL": deque(prices, maxlen=300)}

    result = strat._momentum("AAPL")

    assert result is not None, "_momentum() returned None unexpectedly"
    assert result == pytest.approx(expected, rel=1e-9), (
        f"Expected {expected!r}, got {result!r}. "
        "Lookback anchors are probably not offset by skip."
    )


# ---------------------------------------------------------------------------
# Test 2 — skip=0 uses today's price; result is finite for valid series
# ---------------------------------------------------------------------------

def test_momentum_zero_skip_uses_latest_price():
    """
    With skip=0, p_end should be prices[-1] (the very last bar).
    Use a 270-bar series where every bar is 100.0 except prices[0]=50.0.
    The old prices[0] is far outside any lookback window, so the momentum
    should be a finite float close to 0.0 (flat series in the lookback window).
    If the indexing were wrong and the cheap early bar crept into a window,
    the return would blow up — confirming the test is sensitive to mis-anchoring.
    """
    skip = 0
    lookback = 252

    prices = [100.0] * 270
    prices[0] = 50.0  # only affects bars well outside the 252-bar window

    strat = _make_strategy(skip=skip, lookback=lookback)
    strat._price_history = {"MSFT": deque(prices, maxlen=270)}

    result = strat._momentum("MSFT")

    assert result is not None, "_momentum() returned None for a 270-bar series with skip=0"
    assert math.isfinite(result), f"_momentum() returned non-finite value: {result!r}"
    # With a flat series over the lookback window the result must be exactly 0
    assert result == pytest.approx(0.0, abs=1e-12), (
        f"Expected 0.0 for a flat lookback window, got {result!r}. "
        "The wrong-index bar (prices[0]=50) may have leaked into the window."
    )


# ---------------------------------------------------------------------------
# Partial SELL fill — holdings tracking
# ---------------------------------------------------------------------------

class TestOnFillPartialSell:
    """on_fill must keep the symbol in _holdings until the position is fully closed."""

    def _make_strategy(self):
        from decimal import Decimal
        from datetime import datetime, timezone
        from uuid import uuid4
        from engine.events.types import BarEvent, BarType, FillEvent, Side
        from engine.events.bus import EventBus

        strat = object.__new__(EquityMomentumStrategy)
        strat.strategy_id = "test-strat"
        strat._symbols = {"AAPL"}
        strat._holdings = set()
        strat._qty_held = {}
        return strat, Decimal, datetime, timezone, uuid4, BarEvent, BarType, FillEvent, Side

    def _fill(self, strat_id, sym, side, qty, *, Decimal, datetime, timezone, uuid4, BarEvent, BarType, FillEvent, Side):
        ts = datetime(2024, 1, 2, tzinfo=timezone.utc)
        bar = BarEvent(
            timestamp=ts, symbol=sym,
            open=Decimal("100"), high=Decimal("110"), low=Decimal("95"), close=Decimal("105"),
            volume=1_000_000, bar_type=BarType.DAILY, source="test",
        )
        return FillEvent(
            timestamp=ts, symbol=sym, strategy_id=strat_id,
            order_id=uuid4(),
            side=side, filled_qty=Decimal(str(qty)),
            fill_price=Decimal("100"), commission=Decimal("0"),
            fill_bar=bar,
        )

    def test_full_sell_clears_holdings(self):
        strat, D, dt, tz, uid, BE, BT, FE, Side = self._make_strategy()
        buy = self._fill("test-strat", "AAPL", Side.BUY, "1000",
                         Decimal=D, datetime=dt, timezone=tz, uuid4=uid,
                         BarEvent=BE, BarType=BT, FillEvent=FE, Side=Side)
        strat.on_fill(buy)
        assert "AAPL" in strat._holdings
        assert strat._qty_held["AAPL"] == D("1000")

        sell = self._fill("test-strat", "AAPL", Side.SELL, "1000",
                          Decimal=D, datetime=dt, timezone=tz, uuid4=uid,
                          BarEvent=BE, BarType=BT, FillEvent=FE, Side=Side)
        strat.on_fill(sell)
        assert "AAPL" not in strat._holdings
        assert "AAPL" not in strat._qty_held

    def test_partial_sell_keeps_holdings(self):
        strat, D, dt, tz, uid, BE, BT, FE, Side = self._make_strategy()
        buy = self._fill("test-strat", "AAPL", Side.BUY, "1000",
                         Decimal=D, datetime=dt, timezone=tz, uuid4=uid,
                         BarEvent=BE, BarType=BT, FillEvent=FE, Side=Side)
        strat.on_fill(buy)

        partial_sell = self._fill("test-strat", "AAPL", Side.SELL, "400",
                                  Decimal=D, datetime=dt, timezone=tz, uuid4=uid,
                                  BarEvent=BE, BarType=BT, FillEvent=FE, Side=Side)
        strat.on_fill(partial_sell)

        # Position is still open — must stay in _holdings.
        assert "AAPL" in strat._holdings
        assert strat._qty_held["AAPL"] == D("600")

    def test_two_partial_sells_clear_holdings(self):
        strat, D, dt, tz, uid, BE, BT, FE, Side = self._make_strategy()
        buy = self._fill("test-strat", "AAPL", Side.BUY, "1000",
                         Decimal=D, datetime=dt, timezone=tz, uuid4=uid,
                         BarEvent=BE, BarType=BT, FillEvent=FE, Side=Side)
        strat.on_fill(buy)

        for qty in ("400", "600"):
            sell = self._fill("test-strat", "AAPL", Side.SELL, qty,
                              Decimal=D, datetime=dt, timezone=tz, uuid4=uid,
                              BarEvent=BE, BarType=BT, FillEvent=FE, Side=Side)
            strat.on_fill(sell)

        assert "AAPL" not in strat._holdings
        assert "AAPL" not in strat._qty_held

    def test_wrong_strategy_id_ignored(self):
        strat, D, dt, tz, uid, BE, BT, FE, Side = self._make_strategy()
        buy = self._fill("other-strat", "AAPL", Side.BUY, "1000",
                         Decimal=D, datetime=dt, timezone=tz, uuid4=uid,
                         BarEvent=BE, BarType=BT, FillEvent=FE, Side=Side)
        strat.on_fill(buy)
        # Fill from a different strategy must not affect this strategy's state.
        assert "AAPL" not in strat._holdings
        assert "AAPL" not in strat._qty_held


# ---------------------------------------------------------------------------
# Deferred rebalance — stale bars / same-day fill guard
# ---------------------------------------------------------------------------

@dataclass
class _Cfg:
    symbols: list[str]
    params: dict = field(default_factory=dict)


def _make_bar(symbol: str, ts: datetime, price: float = 100.0) -> BarEvent:
    p = Decimal(str(price))
    return BarEvent(
        timestamp=ts, symbol=symbol,
        open=p, high=p + Decimal("2"), low=p - Decimal("1"), close=p,
        volume=2_000_000, bar_type=BarType.DAILY, source="test",
    )


def _make_snapshot(equity: float = 100_000.0) -> PortfolioSnapshotEvent:
    e = Decimal(str(equity))
    return PortfolioSnapshotEvent(
        equity=e,
        cash=e,
        initial_cash=e,
        total_return_pct=Decimal("0"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        drawdown_pct=Decimal("0"),
        peak_equity=e,
        positions=(),
        num_positions=0,
    )


def _build_strategy(symbols=("AAPL", "SPY", "WMT")) -> EquityMomentumStrategy:
    bus = EventBus()
    cfg = _Cfg(symbols=list(symbols))
    strat = EquityMomentumStrategy("test", cfg, bus)
    strat.on_start(list(symbols))
    return strat


class TestDeferredRebalance:
    """
    Rebalance must fire in on_snapshot (after all daily bars are in), not during
    on_bar when SPY arrives. This prevents stale _latest_bars for post-SPY
    symbols from producing same-day fills instead of next-open fills.
    """

    def test_spy_bar_sets_flag_not_rebalance(self):
        """on_bar(spy) must set _pending_rebalance, not call _rebalance directly."""
        strat = _build_strategy()
        rebalance_calls: list[str] = []
        strat._rebalance = lambda: rebalance_calls.append("called")

        spy_ts = datetime(2024, 3, 1, tzinfo=UTC)
        strat._last_rebalance_date = date(2024, 1, 31)  # prior month → new period

        strat.on_bar(_make_bar("SPY", spy_ts))

        assert rebalance_calls == [], "on_bar(spy) must not call _rebalance() directly"
        assert strat._pending_rebalance is True

    def test_on_snapshot_executes_deferred_rebalance(self):
        """on_snapshot must consume _pending_rebalance and call _rebalance()."""
        strat = _build_strategy()
        rebalance_calls: list[str] = []
        strat._rebalance = lambda: rebalance_calls.append("called")

        strat._pending_rebalance = True
        strat.on_snapshot(_make_snapshot())

        assert rebalance_calls == ["called"]
        assert strat._pending_rebalance is False

    def test_latest_bars_are_todays_when_rebalance_fires(self):
        """
        After AAPL→SPY→WMT bars all arrive on the same day, on_snapshot must
        call _rebalance() with _latest_bars[WMT] equal to today's bar — not the
        stale yesterday bar that was in place when SPY triggered the period check.
        """
        strat = _build_strategy()
        yesterday = datetime(2024, 2, 29, tzinfo=UTC)
        today = datetime(2024, 3, 1, tzinfo=UTC)

        # Seed yesterday's data for all symbols
        for sym in ("AAPL", "SPY", "WMT"):
            strat._latest_bars[sym] = _make_bar(sym, yesterday)

        # Dispatch today's bars in alphabetical order (AAPL, SPY, WMT)
        strat._last_rebalance_date = date(2024, 1, 31)
        for sym in ("AAPL", "SPY", "WMT"):
            strat.on_bar(_make_bar(sym, today))

        # At this point _pending_rebalance=True and _latest_bars[WMT] is today's bar
        captured: dict = {}
        def _capture():
            captured["wmt_ts"] = strat._latest_bars["WMT"].timestamp

        strat._rebalance = _capture
        strat.on_snapshot(_make_snapshot())

        assert captured["wmt_ts"] == today, (
            "on_snapshot must fire _rebalance() after WMT's today-bar has arrived; "
            f"got {captured.get('wmt_ts')!r} but expected {today!r}"
        )

    def test_circuit_breaker_clears_pending_rebalance(self):
        """
        When the CB trips in on_snapshot the pending rebalance must be dropped
        so the strategy does not immediately re-enter after exiting all positions.
        """
        strat = _build_strategy()
        rebalance_calls: list[str] = []
        strat._rebalance = lambda: rebalance_calls.append("called")

        strat._cb_hwm = Decimal("100000")
        strat._pending_rebalance = True
        strat._holdings = {"AAPL"}
        strat._circuit_breaker_active = False

        # 30 % drawdown → CB fires
        strat.on_snapshot(_make_snapshot(equity=70_000.0))

        assert strat._circuit_breaker_active is True
        assert strat._pending_rebalance is False
        assert rebalance_calls == [], "Rebalance must NOT fire on the same snapshot that trips the CB"

    def test_no_pending_rebalance_on_non_period_bars(self):
        """on_bar(spy) within the same period must not set the flag."""
        strat = _build_strategy()
        rebalance_calls: list[str] = []
        strat._rebalance = lambda: rebalance_calls.append("called")

        today = datetime(2024, 3, 5, tzinfo=UTC)
        strat._last_rebalance_date = date(2024, 3, 1)  # same month → not a new period

        strat.on_bar(_make_bar("SPY", today))

        assert strat._pending_rebalance is False
        assert rebalance_calls == []


# ---------------------------------------------------------------------------
# Circuit-breaker residual retry
# ---------------------------------------------------------------------------

class TestCircuitBreakerResidualRetry:
    """
    When the CB is active and prior _exit_all SELLs only partially filled,
    _rebalance must call _exit_all again on the next rebalance so residual
    exposure is cleared. Without the fix, step 0 returned immediately when
    CB was active + regime still bearish, silently skipping the retry.
    """

    def _make(self) -> EquityMomentumStrategy:
        strat = object.__new__(EquityMomentumStrategy)
        strat.strategy_id = "test"
        strat._symbols = {"AAPL", "SPY"}
        strat._regime_symbol = "SPY"
        strat._ma_period = 200
        strat._lookback = 252
        strat._skip = 20
        strat._min_lookback = 200
        strat._top_n = 20
        strat._rank_exit_buffer = 5
        strat._min_volume = 500_000
        strat._max_daily_move = 0.15
        strat._rebalance_freq = "monthly"
        strat._vol_target_pct = 0.001
        strat._initial_cash = 100_000
        strat._max_new_entries = 10
        strat._circuit_breaker_dd = Decimal("0.20")
        strat._circuit_breaker_active = False
        strat._cb_hwm = Decimal("0")
        strat._cb_just_reset = False
        strat._pending_rebalance = False
        strat._price_history = {}
        strat._volume_history = {}
        strat._latest_bars = {}
        strat._holdings = set()
        strat._qty_held = {}
        strat._last_rebalance_date = None
        return strat

    def test_retry_fires_when_residual_holdings_remain(self):
        """
        CB active + regime still bearish + residual holdings → _exit_all retried.
        Prior bug: step 0 returned without calling _exit_all when CB was active.
        """
        strat = self._make()
        strat._circuit_breaker_active = True
        strat._holdings = {"AAPL"}
        strat._qty_held = {"AAPL": Decimal("50")}  # partial SELL left a residual

        exit_calls: list[str] = []
        strat._exit_all = lambda reason: exit_calls.append(reason)
        strat._regime_ok = lambda: False  # market still bearish

        strat._rebalance()

        assert len(exit_calls) == 1, "Expected one _exit_all retry call"
        assert "retry" in exit_calls[0].lower(), f"Unexpected reason: {exit_calls[0]!r}"

    def test_no_retry_when_fully_liquidated(self):
        """
        CB active + regime bearish + holdings empty → _exit_all must NOT be called.
        Avoids emitting zero-qty SELL intents on already-clean positions.
        """
        strat = self._make()
        strat._circuit_breaker_active = True
        strat._holdings = set()
        strat._qty_held = {}

        exit_calls: list[str] = []
        strat._exit_all = lambda reason: exit_calls.append(reason)
        strat._regime_ok = lambda: False

        strat._rebalance()

        assert exit_calls == [], "No retry should fire when holdings are already empty"

    def test_cb_deactivates_when_regime_recovers(self):
        """
        When regime recovers while CB is active, CB must be deactivated and
        _cb_just_reset set so on_snapshot re-anchors the HWM.
        """
        strat = self._make()
        strat._circuit_breaker_active = True
        strat._holdings = set()
        strat._qty_held = {}
        strat._regime_ok = lambda: True  # market recovered

        strat._rebalance()

        assert strat._circuit_breaker_active is False
        assert strat._cb_just_reset is True

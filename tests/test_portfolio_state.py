"""Unit tests for PortfolioState — position math, cash accounting, invariants."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from engine.events.types import BarEvent, BarType, FillEvent, Side
from engine.portfolio.position import PendingOrder
from engine.portfolio.state import PortfolioState

UTC = timezone.utc
D = Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(symbol="SPY", price=D("400"), ts=None):
    ts = ts or datetime(2024, 1, 2, tzinfo=UTC)
    return BarEvent(
        timestamp=ts,
        symbol=symbol,
        open=price, high=price, low=price, close=price,
        volume=1_000_000,
        bar_type=BarType.DAILY,
        source="test",
    )


def _fill(symbol="SPY", side=Side.BUY, qty=D("10"), price=D("100"),
          commission=D("1"), ts=None, strategy_id="test"):
    ts = ts or datetime(2024, 1, 2, tzinfo=UTC)
    return FillEvent(
        timestamp=ts,
        order_id=uuid4(),
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        filled_qty=qty,
        fill_price=price,
        commission=commission,
        fill_bar=_bar(symbol=symbol, price=price, ts=ts),
    )


def _fresh(initial_cash="10000") -> PortfolioState:
    return PortfolioState(initial_cash=D(initial_cash))


# ---------------------------------------------------------------------------
# BUY fills
# ---------------------------------------------------------------------------

class TestBuyFill:
    def test_creates_position(self):
        p = _fresh()
        p.on_fill(_fill(qty=D("10"), price=D("100"), commission=D("0")))
        assert "SPY" in p.positions
        assert p.positions["SPY"].quantity == D("10")
        assert p.positions["SPY"].avg_cost == D("100")

    def test_reduces_cash_including_commission(self):
        p = _fresh()
        p.on_fill(_fill(qty=D("10"), price=D("100"), commission=D("5")))
        # cost = 10 × 100 + 5 = 1005
        assert p.cash == D("10000") - D("1005")

    def test_vwac_on_second_buy(self):
        p = _fresh("20000")
        p.on_fill(_fill(qty=D("10"), price=D("100"), commission=D("0")))
        p.on_fill(_fill(qty=D("10"), price=D("200"), commission=D("0")))
        # VWAC = (10×100 + 10×200) / 20 = 150
        assert p.positions["SPY"].quantity == D("20")
        assert p.positions["SPY"].avg_cost == D("150")

    def test_price_cache_used_before_first_bar(self):
        p = _fresh()
        p.on_bar(_bar(price=D("110")))  # updates price cache
        p.on_fill(_fill(qty=D("5"), price=D("105"), commission=D("0")))
        # last_price should be 110 from cache, not fill price
        assert p.positions["SPY"].last_price == D("110")


# ---------------------------------------------------------------------------
# SELL fills
# ---------------------------------------------------------------------------

class TestSellFill:
    def test_full_close_removes_position(self):
        p = _fresh()
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_fill(_fill(side=Side.SELL, qty=D("10"), price=D("120"), commission=D("0")))
        assert "SPY" not in p.positions

    def test_proceeds_credited_minus_commission(self):
        p = _fresh()
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        cash_pre = p.cash
        p.on_fill(_fill(side=Side.SELL, qty=D("10"), price=D("120"), commission=D("5")))
        # proceeds = 10 × 120 - 5 = 1195
        assert p.cash == cash_pre + D("1195")

    def test_realized_pnl(self):
        p = _fresh()
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_fill(_fill(side=Side.SELL, qty=D("10"), price=D("130"), commission=D("0")))
        # realized = 10 × (130 - 100) = 300
        assert p.realized_pnl == D("300")

    def test_partial_close_leaves_remainder(self):
        p = _fresh()
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_fill(_fill(side=Side.SELL, qty=D("4"), price=D("110"), commission=D("0")))
        assert p.positions["SPY"].quantity == D("6")

    def test_sell_with_no_position_raises(self):
        p = _fresh()
        with pytest.raises(ValueError, match="no open position"):
            p.on_fill(_fill(side=Side.SELL))

    def test_sell_excess_quantity_raises(self):
        p = _fresh()
        p.on_fill(_fill(side=Side.BUY, qty=D("5"), price=D("100"), commission=D("0")))
        with pytest.raises(ValueError, match="exceeds position"):
            p.on_fill(_fill(side=Side.SELL, qty=D("10"), price=D("100"), commission=D("0")))


# ---------------------------------------------------------------------------
# Mark-to-market and metrics
# ---------------------------------------------------------------------------

class TestMTMAndMetrics:
    def test_on_bar_updates_last_price(self):
        p = _fresh()
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_bar(_bar(price=D("150")))
        assert p.positions["SPY"].last_price == D("150")

    def test_equity_includes_mtm(self):
        p = _fresh("10000")
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_bar(_bar(price=D("120")))
        # equity = cash(9000) + mtm(10 × 120 = 1200) = 10200
        assert p.equity == D("10200")

    def test_peak_equity_never_decreases(self):
        p = _fresh("10000")
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_bar(_bar(price=D("200")))
        peak = p.peak_equity
        p.on_bar(_bar(price=D("50")))
        assert p.peak_equity == peak

    def test_drawdown_pct(self):
        p = _fresh("10000")
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_bar(_bar(price=D("200")))   # equity = 11000 → new peak
        p.on_bar(_bar(price=D("50")))    # equity = 9500, peak = 11000
        expected_dd = (D("11000") - D("9500")) / D("11000")
        assert abs(p.drawdown_pct - expected_dd) < D("0.0001")

    def test_total_return_pct(self):
        p = _fresh("10000")
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_fill(_fill(side=Side.SELL, qty=D("10"), price=D("110"), commission=D("0")))
        assert p.total_return_pct == D("100") / D("10000")

    def test_unrealized_pnl(self):
        p = _fresh("10000")
        p.on_fill(_fill(side=Side.BUY, qty=D("10"), price=D("100"), commission=D("0")))
        p.on_bar(_bar(price=D("110")))
        # unrealized = 10 × (110 - 100) = 100
        assert p.unrealized_pnl == D("100")


# ---------------------------------------------------------------------------
# Pending orders and snapshots
# ---------------------------------------------------------------------------

class TestPendingOrders:
    def test_snapshot_includes_pending_order(self):
        p = _fresh()
        oid = uuid4()
        p.register_pending_order(PendingOrder(
            order_id=oid, symbol="SPY", side=Side.BUY,
            quantity=D("10"), reserved_cash=D("1000"),
        ))
        snap = p.snapshot()
        assert len(snap.open_orders) == 1
        assert snap.open_orders[0].order_id == oid
        assert snap.open_orders[0].reserved_cash == D("1000")

    def test_cancel_removes_pending_order(self):
        p = _fresh()
        oid = uuid4()
        p.register_pending_order(PendingOrder(
            order_id=oid, symbol="SPY", side=Side.BUY,
            quantity=D("10"), reserved_cash=D("1000"),
        ))
        p.cancel_pending_order(oid)
        assert len(p.snapshot().open_orders) == 0

    def test_fill_releases_pending_order(self):
        p = _fresh()
        oid = uuid4()
        p.register_pending_order(PendingOrder(
            order_id=oid, symbol="SPY", side=Side.BUY,
            quantity=D("10"), reserved_cash=D("1000"),
        ))
        fill = FillEvent(
            timestamp=datetime(2024, 1, 3, tzinfo=UTC),
            order_id=oid,
            strategy_id="test",
            symbol="SPY",
            side=Side.BUY,
            filled_qty=D("10"),
            fill_price=D("100"),
            commission=D("0"),
            fill_bar=_bar(),
        )
        p.on_fill(fill)
        assert len(p.snapshot().open_orders) == 0

    def test_snapshot_is_immutable_view(self):
        p = _fresh()
        snap1 = p.snapshot()
        p.on_bar(_bar(price=D("500")))
        snap2 = p.snapshot()
        assert snap1.equity == D("10000")   # unchanged
        assert snap2.equity == D("10000")   # cash-only portfolio, no positions

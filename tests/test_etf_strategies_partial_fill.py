"""
Unit tests for partial-fill handling and per-strategy SELL quantity isolation.

Covers three bugs:
  Bug 2 — SELL intent qty must equal this strategy's confirmed held qty,
           not a large sentinel that could liquidate another strategy's shares.
  Bug 3 — A partial broker fill must not clear the strategy's holding state;
           the holding is only cleared when the confirmed qty reaches zero.

Tests are deliberately free of the full broker stack — they call on_fill()
directly and inspect state, then test the emitted SELL qty via the bus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from engine.events.bus import EventBus
from engine.events.types import (
    BarEvent,
    BarType,
    FillEvent,
    OrderIntentEvent,
    Side,
)
from strategies.equity_momentum import EquityMomentumStrategy
from strategies.etf_mean_reversion import EtfMeanReversionStrategy
from strategies.etf_momentum import EtfMomentumStrategy

UTC = timezone.utc
D = Decimal


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _bar(symbol: str, ts: datetime | None = None) -> BarEvent:
    ts = ts or datetime(2024, 1, 2, tzinfo=UTC)
    return BarEvent(
        timestamp=ts, symbol=symbol,
        open=D("100"), high=D("110"), low=D("95"), close=D("105"),
        volume=1_000_000, bar_type=BarType.DAILY, source="test",
    )


def _fill_event(strat_id: str, sym: str, side: Side, qty: str) -> FillEvent:
    ts = datetime(2024, 1, 2, tzinfo=UTC)
    return FillEvent(
        timestamp=ts, symbol=sym, strategy_id=strat_id,
        order_id=uuid4(), side=side,
        filled_qty=D(qty), fill_price=D("100"), commission=D("0"),
        fill_bar=_bar(sym, ts),
    )


# ---------------------------------------------------------------------------
# EtfMomentumStrategy — partial-fill tracking (Bug 3)
# ---------------------------------------------------------------------------

class TestEtfMomentumPartialFill:
    """on_fill must keep _current_holding until the position is fully closed."""

    def _make(self) -> EtfMomentumStrategy:
        strat = object.__new__(EtfMomentumStrategy)
        strat.strategy_id = "test"
        strat._symbols = {"AAPL"}
        strat._current_holding = None
        strat._qty_held = {}
        return strat

    def test_buy_sets_holding_and_qty(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "AAPL", Side.BUY, "200"))
        assert strat._current_holding == "AAPL"
        assert strat._qty_held.get("AAPL", D("0")) == D("200")

    def test_full_sell_clears_holding(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "AAPL", Side.BUY, "200"))
        strat.on_fill(_fill_event("test", "AAPL", Side.SELL, "200"))
        assert strat._current_holding is None
        assert strat._qty_held.get("AAPL", D("0")) == D("0")

    def test_partial_sell_keeps_holding(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "AAPL", Side.BUY, "200"))
        strat.on_fill(_fill_event("test", "AAPL", Side.SELL, "80"))
        assert strat._current_holding == "AAPL"
        assert strat._qty_held.get("AAPL", D("0")) == D("120")

    def test_two_partial_sells_clear_holding(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "AAPL", Side.BUY, "200"))
        strat.on_fill(_fill_event("test", "AAPL", Side.SELL, "80"))
        strat.on_fill(_fill_event("test", "AAPL", Side.SELL, "120"))
        assert strat._current_holding is None
        assert strat._qty_held.get("AAPL", D("0")) == D("0")

    def test_wrong_strategy_id_ignored(self):
        strat = self._make()
        strat.on_fill(_fill_event("other", "AAPL", Side.BUY, "200"))
        assert strat._current_holding is None
        assert strat._qty_held.get("AAPL", D("0")) == D("0")

    def test_partial_rotation_tracks_two_symbols(self):
        """
        Partial rotation: SELL SPY partially fills, BUY QQQ fully fills.
        Both symbols must appear in _qty_held; _current_holding follows the BUY.
        """
        strat = object.__new__(EtfMomentumStrategy)
        strat.strategy_id = "test"
        strat._symbols = {"SPY", "QQQ"}
        strat._current_holding = None
        strat._qty_held = {}

        strat.on_fill(_fill_event("test", "SPY", Side.BUY, "200"))
        assert strat._qty_held.get("SPY", D("0")) == D("200")

        # Partial sell of SPY — 100 of 200 filled
        strat.on_fill(_fill_event("test", "SPY", Side.SELL, "100"))
        assert strat._qty_held.get("SPY", D("0")) == D("100")
        assert strat._current_holding == "SPY"

        # BUY QQQ fully fills
        strat.on_fill(_fill_event("test", "QQQ", Side.BUY, "200"))
        assert strat._qty_held.get("QQQ", D("0")) == D("200")
        assert strat._qty_held.get("SPY", D("0")) == D("100")
        assert strat._current_holding == "QQQ"


# ---------------------------------------------------------------------------
# EtfMeanReversionStrategy — partial-fill tracking (Bug 3)
# ---------------------------------------------------------------------------

class TestEtfMeanReversionPartialFill:
    """on_fill must track qty per leg and only clear the holding flag at zero."""

    def _make(self) -> EtfMeanReversionStrategy:
        strat = object.__new__(EtfMeanReversionStrategy)
        strat.strategy_id = "test"
        strat._symbols = {"SPY", "TLT"}
        strat._symbol_a = "SPY"
        strat._symbol_b = "TLT"
        strat._holding_a = False
        strat._holding_b = False
        strat._qty_a = D("0")
        strat._qty_b = D("0")
        return strat

    def test_buy_a_sets_holding_a(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "SPY", Side.BUY, "150"))
        assert strat._holding_a is True
        assert strat._qty_a == D("150")
        assert strat._holding_b is False

    def test_full_sell_a_clears_holding_a(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "SPY", Side.BUY, "150"))
        strat.on_fill(_fill_event("test", "SPY", Side.SELL, "150"))
        assert strat._holding_a is False
        assert strat._qty_a == D("0")

    def test_partial_sell_a_keeps_holding_a(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "SPY", Side.BUY, "150"))
        strat.on_fill(_fill_event("test", "SPY", Side.SELL, "60"))
        assert strat._holding_a is True
        assert strat._qty_a == D("90")

    def test_two_partial_sells_clear_holding_a(self):
        strat = self._make()
        strat.on_fill(_fill_event("test", "SPY", Side.BUY, "150"))
        strat.on_fill(_fill_event("test", "SPY", Side.SELL, "60"))
        strat.on_fill(_fill_event("test", "SPY", Side.SELL, "90"))
        assert strat._holding_a is False
        assert strat._qty_a == D("0")

    def test_legs_are_independent(self):
        """Selling SPY must not affect TLT tracking and vice versa."""
        strat = self._make()
        strat.on_fill(_fill_event("test", "SPY", Side.BUY, "150"))
        strat.on_fill(_fill_event("test", "TLT", Side.BUY, "100"))
        strat.on_fill(_fill_event("test", "SPY", Side.SELL, "150"))
        assert strat._holding_a is False
        assert strat._holding_b is True
        assert strat._qty_b == D("100")

    def test_wrong_strategy_id_ignored(self):
        strat = self._make()
        strat.on_fill(_fill_event("other", "SPY", Side.BUY, "150"))
        assert strat._holding_a is False
        assert strat._qty_a == D("0")


# ---------------------------------------------------------------------------
# Multi-strategy SELL qty isolation (Bug 2)
# ---------------------------------------------------------------------------

@dataclass
class _Cfg:
    symbols: list[str]
    params: dict = field(default_factory=dict)


class TestSellQtyIsolation:
    """
    Each strategy's SELL intent must carry only that strategy's confirmed held
    qty — not a large sentinel that a shared ShortSellingRule might cap to the
    total symbol position across all strategies.

    We test this by subscribing to OrderIntentEvent on the shared bus and
    asserting the qty on the emitted intent matches the per-strategy lot, not
    the global position size.
    """

    def _captured_intents(self, bus: EventBus) -> list[OrderIntentEvent]:
        return bus.get_history(OrderIntentEvent)

    # -- EtfMomentumStrategy --------------------------------------------------

    def test_etf_momentum_sell_qty_equals_confirmed_held(self):
        """
        SELL intent qty == _qty_held, not the config target qty.
        Strategy A holds 200 shares confirmed by fill; the intent must carry 200.
        """
        bus = EventBus()
        cfg = _Cfg(symbols=["AAPL"])
        strat = EtfMomentumStrategy("strat-a", cfg, bus)
        strat.on_start(["AAPL"])

        # Establish confirmed holding via on_fill
        strat.on_fill(_fill_event("strat-a", "AAPL", Side.BUY, "200"))
        assert strat._qty_held.get("AAPL", D("0")) == D("200")

        # Seed state needed by _emit_for
        strat._latest_bars = {"AAPL": _bar("AAPL")}
        strat._current_bar = _bar("AAPL")   # AbstractStrategy requirement

        # The strategy would emit a SELL via _rebalance when rotating out.
        # Test _emit_for directly with the confirmed qty.
        from engine.events.types import IntentType
        strat._emit_for("AAPL", Side.SELL, strat._qty_held.get("AAPL", D("0")), notes="test")

        intents = self._captured_intents(bus)
        sell_intents = [i for i in intents if i.side == Side.SELL]
        assert len(sell_intents) == 1
        assert sell_intents[0].quantity == D("200"), (
            f"Expected SELL qty 200 (confirmed held), got {sell_intents[0].quantity}"
        )

    # -- EquityMomentumStrategy._exit_all ------------------------------------

    def test_equity_momentum_exit_all_uses_qty_held(self):
        """
        _exit_all must emit SELL with _qty_held qty, not _LARGE_SELL_QTY.
        Confirms Bug 2 fix: a 1000-share confirmed lot produces a 1000-share intent.
        """
        bus = EventBus()
        cfg = _Cfg(symbols=["AAPL", "SPY"])
        strat = EquityMomentumStrategy("strat-em", cfg, bus)
        strat.on_start(["AAPL", "SPY"])

        # Simulate a confirmed holding
        strat.on_fill(_fill_event("strat-em", "AAPL", Side.BUY, "1000"))
        assert strat._qty_held["AAPL"] == D("1000")

        strat._latest_bars = {"AAPL": _bar("AAPL")}
        strat._current_bar = _bar("AAPL")

        strat._exit_all("test reason")

        intents = self._captured_intents(bus)
        sell_intents = [i for i in intents if i.side == Side.SELL and i.symbol == "AAPL"]
        assert len(sell_intents) == 1
        assert sell_intents[0].quantity == D("1000"), (
            f"Expected SELL qty 1000 (confirmed held), got {sell_intents[0].quantity}"
        )

    # -- EtfMeanReversionStrategy._emit_sell ---------------------------------

    def test_etf_mean_reversion_sell_uses_confirmed_qty(self):
        """
        _emit_sell must use _qty_a/_qty_b, not the old _LARGE_SELL_QTY sentinel.
        """
        bus = EventBus()
        cfg = _Cfg(symbols=["SPY", "TLT"], params={
            "symbol_a": "SPY", "symbol_b": "TLT",
            "lookback_days": 10, "entry_z": 1.5, "exit_z": 0.5,
            "quantity_pct": 0.10, "initial_cash": 100_000, "min_vol": 0.0001,
        })
        strat = EtfMeanReversionStrategy("strat-mr", cfg, bus)
        strat.on_start(["SPY", "TLT"])

        # Confirm a SPY holding via on_fill
        strat.on_fill(_fill_event("strat-mr", "SPY", Side.BUY, "150"))
        assert strat._qty_a == D("150")

        strat._latest_a = _bar("SPY")
        strat._current_bar = _bar("SPY")

        strat._emit_sell("SPY", strat._latest_a, notes="test")

        intents = self._captured_intents(bus)
        sell_intents = [i for i in intents if i.side == Side.SELL and i.symbol == "SPY"]
        assert len(sell_intents) == 1
        assert sell_intents[0].quantity == D("150"), (
            f"Expected SELL qty 150 (confirmed held), got {sell_intents[0].quantity}"
        )

    def test_emit_sell_with_zero_qty_emits_nothing(self):
        """_emit_sell must silently skip when no position is confirmed."""
        bus = EventBus()
        cfg = _Cfg(symbols=["SPY", "TLT"], params={
            "symbol_a": "SPY", "symbol_b": "TLT",
            "lookback_days": 10, "entry_z": 1.5, "exit_z": 0.5,
            "quantity_pct": 0.10, "initial_cash": 100_000, "min_vol": 0.0001,
        })
        strat = EtfMeanReversionStrategy("strat-mr", cfg, bus)
        strat.on_start(["SPY", "TLT"])
        strat._latest_a = _bar("SPY")
        strat._current_bar = _bar("SPY")

        # _qty_a == 0 (no confirmed holding) → no intent emitted
        strat._emit_sell("SPY", strat._latest_a, notes="should not emit")

        intents = self._captured_intents(bus)
        assert len(intents) == 0

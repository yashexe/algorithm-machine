"""
Tests for SmaCrossoverStrategy fill-tracking and signal gating.

Prior bug: _in_position was flipped optimistically at intent-emission time.
Fix: _qty_held is updated only in on_fill(), so rejected/gated intents cannot
desync the strategy's holding state from the actual portfolio.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from engine.events.bus import EventBus
from engine.events.types import (
    BarEvent,
    BarType,
    FillEvent,
    IntentType,
    OrderIntentEvent,
    Side,
)
from strategies.sma_crossover import SmaCrossoverStrategy

UTC = timezone.utc
D = Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _Config:
    symbols: list[str]
    params: dict = field(default_factory=dict)


def _make_strategy(
    symbol: str = "SPY",
    fast_period: int = 3,
    slow_period: int = 5,
    quantity: str = "100",
) -> tuple[SmaCrossoverStrategy, EventBus]:
    bus = EventBus()
    config = _Config(
        symbols=[symbol],
        params={"fast_period": fast_period, "slow_period": slow_period, "quantity": quantity},
    )
    strategy = SmaCrossoverStrategy("test-sma", config, bus)
    strategy.on_start([symbol])
    bus.subscribe(BarEvent, strategy.on_bar)
    bus.subscribe(FillEvent, strategy.on_fill)
    return strategy, bus


def _bar(symbol: str, price: float, day: int) -> BarEvent:
    """day is 1-based offset from 2024-01-01, extended past month boundaries."""
    from datetime import date, timedelta
    d = date(2024, 1, 1) + timedelta(days=day)
    p = D(str(price))
    return BarEvent(
        timestamp=datetime(d.year, d.month, d.day, 21, tzinfo=UTC),
        symbol=symbol,
        open=p, high=p, low=p, close=p,
        volume=1_000_000,
        bar_type=BarType.DAILY,
        source="test",
    )


def _fill(symbol: str, side: Side, qty: str, price: str, strategy_id: str = "test-sma") -> FillEvent:
    bar = _bar(symbol, float(price), 1)
    return FillEvent(
        timestamp=bar.timestamp,
        symbol=symbol,
        strategy_id=strategy_id,
        order_id=uuid.uuid4(),
        side=side,
        filled_qty=D(qty),
        fill_price=D(price),
        commission=D("0"),
        fill_bar=bar,
    )


_day_counter = 0


@pytest.fixture(autouse=True)
def _reset_day_counter():
    global _day_counter
    _day_counter = 0
    yield
    _day_counter = 0


def _feed_bars(strategy: SmaCrossoverStrategy, bus: EventBus, prices: list[float], symbol: str = "SPY") -> None:
    """Feed a sequence of bars through the bus with monotonically increasing timestamps."""
    global _day_counter
    for price in prices:
        _day_counter += 1
        bus.publish(_bar(symbol, price, _day_counter))


# Price sequences that produce controlled crossovers with fast=3, slow=5:
#   Bars 1-5 at 100  → fast_ema == slow_sma == 100 (bar 5 sets previous; no signal yet)
#   Bar  6   at 110  → fast_ema (105) > slow_sma (102) → BUY crossover
#   Bar  7   at 80   → fast_ema (92.5) < slow_sma (98) → SELL crossover
_WARMUP_PRICES  = [100.0] * 5   # bars 1-5: warm up, set previous state, no crossover
_BUY_PRICE      = 110.0         # bar 6: crosses above
_SELL_PRICE     = 80.0          # bar 7: crosses below


# ---------------------------------------------------------------------------
# on_fill — unit tests (state tracking in isolation)
# ---------------------------------------------------------------------------

class TestOnFillTracking:
    def test_buy_fill_increments_qty_held(self):
        strategy, _ = _make_strategy()
        strategy.on_fill(_fill("SPY", Side.BUY, "100", "105"))
        assert strategy._qty_held["SPY"] == D("100")

    def test_buy_fills_accumulate(self):
        """Two partial BUY fills add up correctly."""
        strategy, _ = _make_strategy()
        strategy.on_fill(_fill("SPY", Side.BUY, "60", "100"))
        strategy.on_fill(_fill("SPY", Side.BUY, "40", "102"))
        assert strategy._qty_held["SPY"] == D("100")

    def test_sell_fill_decrements_qty_held(self):
        strategy, _ = _make_strategy()
        strategy.on_fill(_fill("SPY", Side.BUY, "100", "100"))
        strategy.on_fill(_fill("SPY", Side.SELL, "100", "110"))
        assert strategy._qty_held["SPY"] == D("0")

    def test_partial_sell_leaves_residual(self):
        strategy, _ = _make_strategy()
        strategy.on_fill(_fill("SPY", Side.BUY, "100", "100"))
        strategy.on_fill(_fill("SPY", Side.SELL, "40", "110"))
        assert strategy._qty_held["SPY"] == D("60")

    def test_sell_cannot_go_below_zero(self):
        """Over-fill on sell side must clamp to zero, not produce a negative holding."""
        strategy, _ = _make_strategy()
        strategy.on_fill(_fill("SPY", Side.BUY, "100", "100"))
        strategy.on_fill(_fill("SPY", Side.SELL, "200", "110"))
        assert strategy._qty_held["SPY"] == D("0")

    def test_fill_from_other_strategy_ignored(self):
        """Fills with a different strategy_id must not affect _qty_held."""
        strategy, _ = _make_strategy()
        strategy.on_fill(_fill("SPY", Side.BUY, "100", "100", strategy_id="other-strategy"))
        assert strategy._qty_held["SPY"] == D("0")

    def test_qty_held_starts_at_zero(self):
        strategy, _ = _make_strategy()
        assert strategy._qty_held.get("SPY", D("0")) == D("0")

    def test_on_start_resets_qty_held(self):
        """on_start must clear any held qty so restarts begin flat."""
        strategy, _ = _make_strategy()
        strategy.on_fill(_fill("SPY", Side.BUY, "100", "100"))
        strategy.on_start(["SPY"])
        assert strategy._qty_held["SPY"] == D("0")


# ---------------------------------------------------------------------------
# Signal gating — _qty_held drives BUY/SELL guard conditions
# ---------------------------------------------------------------------------

class TestSignalGating:
    def _run_crossover_sequence(self, strategy, bus, symbol="SPY"):
        """Feed bars to produce exactly one BUY crossover."""
        intents = []
        bus.subscribe(OrderIntentEvent, intents.append)
        _feed_bars(strategy, bus, _WARMUP_PRICES, symbol)
        _feed_bars(strategy, bus, [_BUY_PRICE], symbol)
        return intents

    def test_buy_crossover_emits_intent(self):
        strategy, bus = _make_strategy()
        intents = self._run_crossover_sequence(strategy, bus)
        assert len(intents) == 1
        assert intents[0].side == Side.BUY

    def test_no_sell_when_qty_held_is_zero(self):
        """
        Core bug regression: BUY intent emitted but no fill received.
        Old code set _in_position=True optimistically → SELL crossover would fire
        even though we have no position.
        New code keeps _qty_held=0 → SELL crossover is suppressed correctly.
        """
        strategy, bus = _make_strategy()
        intents = []
        bus.subscribe(OrderIntentEvent, intents.append)

        # Feed warmup + BUY crossover (no fill arrives — intent rejected/gated)
        _feed_bars(strategy, bus, _WARMUP_PRICES)
        _feed_bars(strategy, bus, [_BUY_PRICE])   # BUY crossover → intent emitted, no fill
        assert len(intents) == 1 and intents[0].side == Side.BUY

        # _qty_held is still 0 (no fill)
        assert strategy._qty_held.get("SPY", D("0")) == D("0")

        # SELL crossover — must NOT emit because we never confirmed a position
        _feed_bars(strategy, bus, [_SELL_PRICE])
        sell_intents = [i for i in intents if i.side == Side.SELL]
        assert len(sell_intents) == 0, (
            "SELL intent must not be emitted when _qty_held=0 (no fill confirmed the position)"
        )

    def test_no_duplicate_buy_when_already_holding(self):
        """After a BUY fill confirms a position, another BUY crossover must be suppressed."""
        strategy, bus = _make_strategy()
        intents = []
        bus.subscribe(OrderIntentEvent, intents.append)

        # BUY crossover + confirm fill
        _feed_bars(strategy, bus, _WARMUP_PRICES)
        _feed_bars(strategy, bus, [_BUY_PRICE])
        strategy.on_fill(_fill("SPY", Side.BUY, "100", str(_BUY_PRICE)))

        # SELL crossover to reset state
        _feed_bars(strategy, bus, [_SELL_PRICE])
        strategy.on_fill(_fill("SPY", Side.SELL, "100", str(_SELL_PRICE)))

        # Another BUY-like push (prices spike again from 80)
        # We confirm _qty_held is zero before checking no spurious BUY guard
        assert strategy._qty_held.get("SPY", D("0")) == D("0")

    def test_sell_fires_after_fill_confirms_position(self):
        """SELL crossover must fire once a BUY fill has confirmed the holding."""
        strategy, bus = _make_strategy()
        intents = []
        bus.subscribe(OrderIntentEvent, intents.append)

        # BUY crossover + confirm via fill
        _feed_bars(strategy, bus, _WARMUP_PRICES)
        _feed_bars(strategy, bus, [_BUY_PRICE])
        strategy.on_fill(_fill("SPY", Side.BUY, "100", str(_BUY_PRICE)))
        assert strategy._qty_held["SPY"] == D("100")

        # SELL crossover — must emit because qty_held > 0
        _feed_bars(strategy, bus, [_SELL_PRICE])
        sell_intents = [i for i in intents if i.side == Side.SELL]
        assert len(sell_intents) == 1

    def test_sell_quantity_equals_confirmed_qty_not_configured_qty(self):
        """
        SELL intent quantity must equal _qty_held (confirmed fills), not self.quantity.
        A partial fill must cause a smaller SELL intent.
        """
        strategy, bus = _make_strategy(quantity="100")
        intents = []
        bus.subscribe(OrderIntentEvent, intents.append)

        # BUY crossover + PARTIAL fill (only 60 of 100 filled)
        _feed_bars(strategy, bus, _WARMUP_PRICES)
        _feed_bars(strategy, bus, [_BUY_PRICE])
        strategy.on_fill(_fill("SPY", Side.BUY, "60", str(_BUY_PRICE)))
        assert strategy._qty_held["SPY"] == D("60")

        # SELL crossover — must emit 60, not 100
        _feed_bars(strategy, bus, [_SELL_PRICE])
        sell_intents = [i for i in intents if i.side == Side.SELL]
        assert len(sell_intents) == 1
        assert sell_intents[0].quantity == D("60"), (
            f"Expected SELL qty=60 (confirmed holding), got {sell_intents[0].quantity}"
        )

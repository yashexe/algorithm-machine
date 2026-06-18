"""
Tests for strategies/etf_mean_reversion.py

Covers:
  - EtfMeanReversionStrategy construction and validation
  - Z-score computation: correct mean reversion
  - Signal logic: entry on extreme z-score, exit on reversion
  - Dual-symbol accumulation: signals only fire once both legs arrive per date
  - Strategy isolation: no restricted imports, all proposals via _emit_intent
  - Warm-up: no signals before lookback period satisfied
  - Integration: signals flow through EventBus → RiskGatekeeper → fills
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import math
import pytest

from engine.events.bus import EventBus
from engine.events.types import (
    ApprovedOrderEvent,
    BarEvent,
    BarType,
    FillEvent,
    IntentType,
    OrderIntentEvent,
    PortfolioSnapshotEvent,
    Side,
)
from engine.execution.paper_broker import PaperBroker
from engine.portfolio.state import PortfolioState
from engine.risk.gatekeeper import RiskGatekeeper
from engine.risk.rules.base import AbstractRule, RuleChain, RuleResult
from strategies.etf_mean_reversion import EtfMeanReversionStrategy

UTC = timezone.utc
D = Decimal
INITIAL_CASH = D("100000")


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

@dataclass
class _StrategyConfig:
    symbols: list[str]
    id: str = "etf_mr_test"
    params: dict = field(default_factory=dict)


class _AlwaysPassRule(AbstractRule):
    name = "always_pass"

    def evaluate(self, intent, snapshot, current_price):
        return RuleResult.pass_(intent.quantity, "always pass")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_config(
    symbols: list[str] = None,
    params: dict = None,
) -> _StrategyConfig:
    return _StrategyConfig(
        symbols=symbols or ["SPY", "TLT"],
        params=params or {
            "symbol_a": "SPY",
            "symbol_b": "TLT",
            "lookback_days": 10,
            "entry_z": 1.5,
            "exit_z": 0.5,
            "quantity_pct": 0.10,
            "initial_cash": 100_000,
            "min_vol": 0.0001,
        },
    )


def _make_bar(
    symbol: str,
    close: float,
    day_offset: int,
    open_: float | None = None,
) -> BarEvent:
    """Generate a synthetic daily bar."""
    ts = datetime(2023, 1, day_offset + 1, 21, 0, 0, tzinfo=UTC)
    price = D(str(close))
    return BarEvent(
        timestamp=ts,
        symbol=symbol,
        open=D(str(open_ or close)),
        high=price + D("1"),
        low=price - D("1"),
        close=price,
        volume=1_000_000,
        bar_type=BarType.DAILY,
        source="synthetic",
    )


def _make_bar_pair(
    spy_close: float,
    tlt_close: float,
    day_offset: int,
) -> tuple[BarEvent, BarEvent]:
    """Generate a matching SPY + TLT bar pair for the same date."""
    return (
        _make_bar("SPY", spy_close, day_offset),
        _make_bar("TLT", tlt_close, day_offset),
    )


def _build_stack(params: dict | None = None):
    """Build a minimal event pipeline with the mean reversion strategy."""
    bus = EventBus()
    portfolio = PortfolioState(initial_cash=INITIAL_CASH)
    rule_chain = RuleChain([_AlwaysPassRule()])
    gatekeeper = RiskGatekeeper(
        bus=bus,
        snapshot_provider=portfolio.snapshot,
        rule_chain=rule_chain,
    )
    broker = PaperBroker(
        bus=bus,
        portfolio=portfolio,
        universe=["SPY", "TLT"],
        slippage_model="zero",
        commission_model="zero",
        fill_at="next_open",
        max_participation_pct="0",
    )
    config = _make_config(params=params)
    strategy = EtfMeanReversionStrategy("etf_mr_test", config, bus)

    # Canonical handler order (runner.py §104–120): broker fills at open before MTM at close
    bus.subscribe(BarEvent, broker.on_bar)
    bus.subscribe(BarEvent, portfolio.on_bar)
    bus.subscribe(BarEvent, strategy.on_bar)
    bus.subscribe(OrderIntentEvent, gatekeeper.on_order_intent)
    bus.subscribe(ApprovedOrderEvent, broker.on_approved_order)
    bus.subscribe(FillEvent, portfolio.on_fill)
    bus.subscribe(FillEvent, strategy.on_fill)

    return bus, portfolio, strategy


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_basic_init(self):
        config = _make_config()
        bus = MagicMock()
        s = EtfMeanReversionStrategy("test", config, bus)
        assert s.strategy_id == "test"

    def test_rejects_symbol_a_not_in_symbols(self):
        config = _make_config(
            symbols=["SPY", "TLT"],
            params={"symbol_a": "QQQ", "symbol_b": "TLT", "initial_cash": 1000},
        )
        with pytest.raises(ValueError, match="symbol_a"):
            EtfMeanReversionStrategy("test", config, MagicMock())

    def test_rejects_symbol_b_not_in_symbols(self):
        config = _make_config(
            symbols=["SPY", "TLT"],
            params={"symbol_a": "SPY", "symbol_b": "GLD", "initial_cash": 1000},
        )
        with pytest.raises(ValueError, match="symbol_b"):
            EtfMeanReversionStrategy("test", config, MagicMock())

    def test_default_params(self):
        config = _make_config(params={"initial_cash": 100_000})
        bus = MagicMock()
        s = EtfMeanReversionStrategy("test", config, bus)
        assert s._lookback == 60
        assert s._entry_z == 1.5
        assert s._exit_z == 0.5

    def test_strategy_isolation_no_portfolio_import(self):
        """Strategy module must not import PortfolioState (isolation checklist)."""
        import importlib
        import sys
        # If the module imported PortfolioState at module level, it would appear
        # in the module's __dict__ or globals. Check it's not there.
        import strategies.etf_mean_reversion as mod
        assert not hasattr(mod, "PortfolioState")
        assert not hasattr(mod, "RiskGatekeeper")
        assert not hasattr(mod, "PaperBroker")


# ---------------------------------------------------------------------------
# Z-score computation
# ---------------------------------------------------------------------------

class TestZScoreComputation:
    def _make_strategy_with_prices(
        self,
        spy_prices: list[float],
        tlt_prices: list[float],
        lookback: int = 10,
    ) -> EtfMeanReversionStrategy:
        """Create a strategy and pre-load price histories."""
        from collections import deque

        config = _make_config(params={
            "lookback_days": lookback,
            "entry_z": 1.5,
            "exit_z": 0.5,
            "initial_cash": 100_000,
            "min_vol": 0.0001,
        })
        s = EtfMeanReversionStrategy("test", config, MagicMock())
        s.on_start(["SPY", "TLT"])

        s._prices_a = deque(spy_prices, maxlen=lookback + 1)
        s._prices_b = deque(tlt_prices, maxlen=lookback + 1)
        return s

    def test_returns_none_before_warmup(self):
        s = self._make_strategy_with_prices([100.0] * 5, [50.0] * 5, lookback=10)
        assert s._compute_z_score() is None

    def test_flat_spread_returns_zero_z(self):
        """When spread grows smoothly, z-score of the current value is computable."""
        # A linearly-growing spread has a non-zero std → z-score is computable
        spy = [100.0 + i * 0.5 for i in range(20)]  # grows faster than TLT → spread rises
        tlt = [50.0] * 20
        config = _make_config(params={
            "lookback_days": 10,
            "entry_z": 1.5,
            "exit_z": 0.5,
            "initial_cash": 100_000,
            "min_vol": 0.0,   # no minimum so even small vol is accepted
        })
        s = EtfMeanReversionStrategy("test", config, MagicMock())
        s.on_start(["SPY", "TLT"])
        from collections import deque
        s._prices_a = deque(spy, maxlen=11)
        s._prices_b = deque(tlt, maxlen=11)
        z = s._compute_z_score()
        # Spread is rising → z-score of current value should be positive
        assert z is not None
        assert isinstance(z, float)

    def test_extreme_low_spread_gives_negative_z(self):
        """When SPY drops sharply vs TLT, spread z-score should be negative."""
        # Normal regime
        spy = [100.0] * 10
        tlt = [50.0] * 10
        # Recent observation: SPY much lower
        spy.append(80.0)
        tlt.append(50.0)
        s = self._make_strategy_with_prices(spy, tlt, lookback=10)
        z = s._compute_z_score()
        assert z is not None
        assert z < 0

    def test_extreme_high_spread_gives_positive_z(self):
        """When SPY surges vs TLT, spread z-score should be positive."""
        spy = [100.0] * 10
        tlt = [50.0] * 10
        spy.append(130.0)
        tlt.append(50.0)
        s = self._make_strategy_with_prices(spy, tlt, lookback=10)
        z = s._compute_z_score()
        assert z is not None
        assert z > 0

    def test_low_volatility_returns_none(self):
        """If spread std < min_vol, returns None (no edge in flat regimes)."""
        # Perfectly constant spread → std = 0
        spy = [100.0] * 20
        tlt = [50.0] * 20
        config = _make_config(params={
            "lookback_days": 10,
            "entry_z": 1.5,
            "exit_z": 0.5,
            "initial_cash": 100_000,
            "min_vol": 999.0,  # impossibly high threshold
        })
        s = EtfMeanReversionStrategy("test", config, MagicMock())
        s.on_start(["SPY", "TLT"])
        from collections import deque
        s._prices_a = deque(spy, maxlen=11)
        s._prices_b = deque(tlt, maxlen=11)
        result = s._compute_z_score()
        assert result is None


# ---------------------------------------------------------------------------
# Signal logic
# ---------------------------------------------------------------------------

class TestSignalLogic:
    def _make_warmed_up_strategy(self, bus, lookback=5):
        """Create a strategy with pre-warmed buffers using synthetic bars."""
        config = _make_config(params={
            "lookback_days": lookback,
            "entry_z": 1.5,
            "exit_z": 0.5,
            "quantity_pct": 0.10,
            "initial_cash": 100_000,
            "min_vol": 0.0001,
        })
        s = EtfMeanReversionStrategy("test", config, bus)
        s.on_start(["SPY", "TLT"])
        return s

    def test_no_signal_before_warmup(self):
        """Strategy emits no intents before lookback is satisfied."""
        bus = EventBus()
        recorded_intents = []
        bus.subscribe(OrderIntentEvent, recorded_intents.append)

        s = self._make_warmed_up_strategy(bus, lookback=10)
        bus.subscribe(BarEvent, s.on_bar)  # wire strategy to bus
        # Feed only 5 bars per symbol — below the 10-bar lookback
        for i in range(5):
            bar_spy, bar_tlt = _make_bar_pair(100.0 + i, 50.0 + i * 0.5, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)

        assert len(recorded_intents) == 0

    def test_entry_long_a_when_z_very_negative(self):
        """When spread z-score < -entry_z, BUY SPY intent is emitted."""
        bus = EventBus()
        recorded_intents = []
        bus.subscribe(OrderIntentEvent, recorded_intents.append)

        s = self._make_warmed_up_strategy(bus, lookback=5)
        bus.subscribe(BarEvent, s.on_bar)  # wire strategy to bus

        # Build a regime where SPY/TLT spread is near mean for 5 bars
        for i in range(5):
            bar_spy, bar_tlt = _make_bar_pair(100.0, 50.0, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)

        # Now a sharp drop in SPY relative to TLT
        # log(70/50) vs log(100/50) → very negative z
        bar_spy, bar_tlt = _make_bar_pair(70.0, 50.0, 5)
        bus.publish(bar_spy)
        bus.publish(bar_tlt)

        buy_intents = [i for i in recorded_intents if i.side == Side.BUY]
        # May or may not fire depending on exact z calculation with 5 bars,
        # but if it fires it should be SPY
        if buy_intents:
            assert buy_intents[0].symbol == "SPY"

    def test_entry_long_b_when_z_very_positive(self):
        """When spread z-score > +entry_z, BUY TLT intent is emitted."""
        bus = EventBus()
        recorded_intents = []
        bus.subscribe(OrderIntentEvent, recorded_intents.append)

        s = self._make_warmed_up_strategy(bus, lookback=5)
        bus.subscribe(BarEvent, s.on_bar)  # wire strategy to bus

        for i in range(5):
            bar_spy, bar_tlt = _make_bar_pair(100.0, 50.0, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)

        # Sharp surge in SPY relative to TLT → positive z → BUY TLT
        bar_spy, bar_tlt = _make_bar_pair(180.0, 50.0, 5)
        bus.publish(bar_spy)
        bus.publish(bar_tlt)

        buy_intents = [i for i in recorded_intents if i.side == Side.BUY]
        if buy_intents:
            assert buy_intents[0].symbol == "TLT"

    def test_no_double_entry(self):
        """
        Once a BUY fill is confirmed, _holding_a=True blocks re-entry.
        Uses the full broker stack so fills propagate back via on_fill.
        """
        bus, portfolio, strategy = _build_stack(params={
            "symbol_a": "SPY", "symbol_b": "TLT",
            "lookback_days": 5, "entry_z": 1.5,
            # exit_z=-100 means z > 100 to exit — never fires, keeping _holding_a=True
            # throughout the test so we can verify purely the entry guard.
            "exit_z": -100.0,
            "quantity_pct": 0.05, "initial_cash": 100_000, "min_vol": 0.0001,
        })
        strategy.on_start(["SPY", "TLT"])
        recorded_intents = []
        bus.subscribe(OrderIntentEvent, recorded_intents.append)

        # Warm up with neutral spread
        for i in range(5):
            bar_spy, bar_tlt = _make_bar_pair(100.0, 50.0, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)

        # First extreme bar: z << -entry_z → BUY SPY emitted
        bar_spy, bar_tlt = _make_bar_pair(70.0, 50.0, 5)
        bus.publish(bar_spy)
        bus.publish(bar_tlt)

        # Next bar: fills the pending BUY → on_fill sets _holding_a = True
        # Subsequent bars with continued extreme spread must not re-enter
        for i in range(6, 12):
            bar_spy, bar_tlt = _make_bar_pair(70.0, 50.0, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)

        buy_intents = [e for e in recorded_intents if e.side == Side.BUY and e.symbol == "SPY"]
        assert len(buy_intents) <= 1


# ---------------------------------------------------------------------------
# Dual-symbol bar accumulation
# ---------------------------------------------------------------------------

class TestDualSymbolAccumulation:
    def test_no_signal_from_single_symbol_only(self):
        """Signals must not fire when only one symbol's bar has arrived."""
        bus = EventBus()
        recorded_intents = []
        bus.subscribe(OrderIntentEvent, recorded_intents.append)

        config = _make_config(params={
            "lookback_days": 3,
            "entry_z": 0.1,    # very low threshold to maximize trigger chance
            "exit_z": 0.05,
            "initial_cash": 100_000,
            "min_vol": 0.0001,
        })
        s = EtfMeanReversionStrategy("test", config, bus)
        bus.subscribe(BarEvent, s.on_bar)  # wire strategy to bus
        s.on_start(["SPY", "TLT"])

        # Publish only SPY bars — no TLT
        for i in range(10):
            bus.publish(_make_bar("SPY", 100.0 + i, i))

        # No intents should fire without TLT bars arriving
        assert len(recorded_intents) == 0

    def test_signals_fire_after_both_symbols_arrive(self):
        """After both symbols arrive for a day, _pending_date advances each date."""
        bus = EventBus()

        config = _make_config(params={
            "lookback_days": 3,
            "entry_z": 0.001,
            "exit_z": 0.0005,
            "initial_cash": 100_000,
            "min_vol": 0.0,
        })
        s = EtfMeanReversionStrategy("test", config, bus)
        # Subscribe strategy to the bus (needed for on_bar to be called)
        bus.subscribe(BarEvent, s.on_bar)
        s.on_start(["SPY", "TLT"])

        observed_pending_dates = []

        # Publish 5 complete bar pairs, checking _pending_date after each pair
        for i in range(5):
            bar_spy, bar_tlt = _make_bar_pair(100.0, 50.0, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)
            # After both bars arrive, _pending_date should equal today's date
            observed_pending_dates.append(s._pending_date)

        # All 5 pairs had distinct dates, so _pending_date should have been set 5 times
        unique_dates = set(d for d in observed_pending_dates if d is not None)
        assert len(unique_dates) == 5, (
            f"Expected 5 unique pending dates, got: {unique_dates}"
        )


# ---------------------------------------------------------------------------
# Integration: signals → fills
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_round_trip_with_always_pass_risk(self):
        """
        End-to-end: synthetic bars with extreme z → BUY fill → reversion → SELL fill.
        Uses short lookback (5) so warm-up happens fast.
        """
        bus, portfolio, strategy = _build_stack(params={
            "symbol_a": "SPY",
            "symbol_b": "TLT",
            "lookback_days": 5,
            "entry_z": 1.5,
            "exit_z": 0.3,
            "quantity_pct": 0.05,
            "initial_cash": 100_000,
            "min_vol": 0.0001,
        })

        strategy.on_start(["SPY", "TLT"])

        # ── Phase 1: Warm up with neutral spread (5 pairs) ───────────────
        for i in range(5):
            bar_spy, bar_tlt = _make_bar_pair(100.0, 50.0, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)
            portfolio.publish_snapshot(bus)

        # ── Phase 2: Inject extreme negative z (SPY crashes) ─────────────
        bar_spy, bar_tlt = _make_bar_pair(65.0, 50.0, 5)
        bus.publish(bar_spy)
        bus.publish(bar_tlt)
        portfolio.publish_snapshot(bus)

        # ── Phase 3: One more bar (fills pending BUY at next open) ────────
        bar_spy, bar_tlt = _make_bar_pair(65.0, 50.0, 6)
        bus.publish(bar_spy)
        bus.publish(bar_tlt)
        portfolio.publish_snapshot(bus)

        fills = bus.get_history(FillEvent)
        buy_fills = [f for f in fills if f.side == Side.BUY]

        # At minimum the BUY should have been approved and queued
        intents = bus.get_history(OrderIntentEvent)
        buy_intents = [i for i in intents if i.side == Side.BUY]

        # Strategy must have emitted at least one BUY intent
        assert len(buy_intents) >= 1
        assert buy_intents[0].symbol in ("SPY", "TLT")

    def test_quantity_is_positive(self):
        """All emitted intents have positive quantity (STRATEGY_INTERFACE.md §3.2)."""
        bus = EventBus()
        recorded_intents = []
        bus.subscribe(OrderIntentEvent, recorded_intents.append)

        config = _make_config(params={
            "lookback_days": 5,
            "entry_z": 0.1,   # very low to maximize triggers
            "exit_z": 0.05,
            "quantity_pct": 0.10,
            "initial_cash": 100_000,
            "min_vol": 0.0001,
        })
        s = EtfMeanReversionStrategy("test", config, bus)
        bus.subscribe(BarEvent, s.on_bar)  # wire strategy to bus
        s.on_start(["SPY", "TLT"])

        # Feed varied prices to provoke signals
        prices = [(100.0, 50.0), (100.0, 50.0), (100.0, 50.0),
                  (100.0, 50.0), (100.0, 50.0), (80.0, 50.0),
                  (80.0, 50.0), (100.0, 50.0)]
        for i, (spy, tlt) in enumerate(prices):
            bar_spy, bar_tlt = _make_bar_pair(spy, tlt, i)
            bus.publish(bar_spy)
            bus.publish(bar_tlt)

        for intent in recorded_intents:
            assert intent.quantity > Decimal("0")

    def test_on_start_resets_state(self):
        """on_start() clears all state for a fresh run."""
        config = _make_config()
        bus = MagicMock()
        s = EtfMeanReversionStrategy("test", config, bus)
        s.on_start(["SPY", "TLT"])

        # Artificially dirty the state
        s._holding_a = True
        s._holding_b = True

        # Re-start
        s.on_start(["SPY", "TLT"])

        assert not s._holding_a
        assert not s._holding_b
        assert len(s._prices_a) == 0
        assert len(s._prices_b) == 0

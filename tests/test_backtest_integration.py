"""
End-to-end integration test: synthetic bars → fills → MetricsEngine → BacktestResult.

Uses a deterministic fixed-signal strategy and an always-pass risk rule to
verify that the full event chain (bar → intent → approval → queue → fill →
snapshot → metrics) produces the expected trade log without touching the
data pipeline, config files, or live market data.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

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
from engine.backtest.metrics import MetricsEngine
from engine.backtest.runner import BacktestRunner
from engine.execution.paper_broker import PaperBroker
from engine.portfolio.state import PortfolioState
from engine.risk.gatekeeper import RiskGatekeeper
from engine.risk.rules.base import AbstractRule, RuleChain, RuleResult
from engine.strategy.base import AbstractStrategy

UTC = timezone.utc
D = Decimal

INITIAL_CASH = D("100000")
TRADE_QTY = D("10")


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _AlwaysPassRule(AbstractRule):
    name = "always_pass"

    def evaluate(self, intent, snapshot, current_price):
        return RuleResult.pass_(intent.quantity, "always pass")


@dataclass
class _StrategyConfig:
    symbols: list[str]
    id: str = "test-strategy"
    params: dict = field(default_factory=dict)


class _FixedSignalStrategy(AbstractStrategy):
    """
    Emits a single BUY intent on the buy_at-th bar and a single SELL intent
    on the sell_at-th bar. Counts bars per symbol independently.
    """

    def __init__(
        self,
        strategy_id: str,
        config: _StrategyConfig,
        bus: EventBus,
        *,
        buy_at: int = 2,
        sell_at: int = 6,
        qty: Decimal = TRADE_QTY,
    ) -> None:
        super().__init__(strategy_id, config, bus)
        self._buy_at = buy_at
        self._sell_at = sell_at
        self._qty = qty
        self._bought = False

    def on_bar(self, event: BarEvent) -> None:
        self._record_bar(event)
        n = self._bar_counts[event.symbol]
        if n == self._buy_at and not self._bought:
            self._emit_intent(event.symbol, Side.BUY, IntentType.MARKET, self._qty)
            self._bought = True
        elif n == self._sell_at and self._bought:
            self._emit_intent(event.symbol, Side.SELL, IntentType.MARKET, self._qty)
            self._bought = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_bars(n: int = 10, symbol: str = "SPY") -> list[BarEvent]:
    """Generate n consecutive daily bars with steadily rising prices."""
    bars = []
    for i in range(n):
        ts = datetime(2024, 1, i + 2, tzinfo=UTC)   # Jan 2, 3, ..., Jan 11
        price = D(str(100 + i))
        bars.append(BarEvent(
            timestamp=ts, symbol=symbol,
            open=price, high=price + D("2"), low=price - D("1"), close=price + D("1"),
            volume=1_000_000, bar_type=BarType.DAILY, source="synthetic",
        ))
    return bars


def _build_stack(buy_at=2, sell_at=6, scored_start: date | None = None):
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
        universe=["SPY"],
        slippage_model="zero",
        commission_model="zero",
        fill_at="next_open",
    )
    config = _StrategyConfig(symbols=["SPY"])
    strategy = _FixedSignalStrategy(
        "test-strategy", config, bus,
        buy_at=buy_at, sell_at=sell_at,
    )

    # Canonical handler order: broker fills at open before portfolio MTM at close
    bus.subscribe(BarEvent, broker.on_bar)
    bus.subscribe(BarEvent, portfolio.on_bar)
    bus.subscribe(BarEvent, strategy.on_bar)

    def _gated_intent(event: OrderIntentEvent) -> None:
        if scored_start is not None and event.signal_bar.timestamp.date() < scored_start:
            return
        gatekeeper.on_order_intent(event)

    bus.subscribe(OrderIntentEvent, _gated_intent)
    bus.subscribe(ApprovedOrderEvent, broker.on_approved_order)
    bus.subscribe(FillEvent, portfolio.on_fill)

    return bus, portfolio, strategy


def _run_replay(bus, portfolio, strategy, bars):
    strategy.on_start(["SPY"])
    bar_dates: list[date] = []
    for bar in bars:
        bus.publish(bar)
        portfolio.publish_snapshot(bus)
        bar_dates.append(bar.timestamp.date())
    strategy.on_stop()
    return bar_dates


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFullRoundTrip:
    def test_two_fills_produced(self):
        bus, portfolio, strategy = _build_stack(buy_at=2, sell_at=6)
        bars = _make_bars(10)
        _run_replay(bus, portfolio, strategy, bars)

        fills = bus.get_history(FillEvent)
        assert len(fills) == 2
        assert fills[0].side == Side.BUY
        assert fills[1].side == Side.SELL

    def test_buy_fills_at_bar3_open(self):
        """BUY intent emitted on bar 2 → fill at bar 3's open price."""
        bus, portfolio, strategy = _build_stack(buy_at=2, sell_at=6)
        bars = _make_bars(10)
        _run_replay(bus, portfolio, strategy, bars)

        fills = bus.get_history(FillEvent)
        buy_fill = fills[0]
        bar3_open = bars[2].open   # index 2 = 3rd bar
        assert buy_fill.fill_price == bar3_open

    def test_sell_fills_at_bar7_open(self):
        """SELL intent emitted on bar 6 → fill at bar 7's open price."""
        bus, portfolio, strategy = _build_stack(buy_at=2, sell_at=6)
        bars = _make_bars(10)
        _run_replay(bus, portfolio, strategy, bars)

        fills = bus.get_history(FillEvent)
        sell_fill = fills[1]
        bar7_open = bars[6].open   # index 6 = 7th bar
        assert sell_fill.fill_price == bar7_open

    def test_no_position_after_sell(self):
        bus, portfolio, strategy = _build_stack(buy_at=2, sell_at=6)
        _run_replay(bus, portfolio, strategy, _make_bars(10))
        assert "SPY" not in portfolio.positions

    def test_cash_increases_after_profitable_trade(self):
        """Prices rise so sell_open > buy_open → cash ends higher than start."""
        bus, portfolio, strategy = _build_stack(buy_at=2, sell_at=6)
        _run_replay(bus, portfolio, strategy, _make_bars(10))
        assert portfolio.cash > INITIAL_CASH


class TestMetricsEngineIntegration:
    def _compute(self, buy_at=2, sell_at=6, n_bars=10):
        bus, portfolio, strategy = _build_stack(buy_at=buy_at, sell_at=sell_at)
        bars = _make_bars(n_bars)
        bar_dates = _run_replay(bus, portfolio, strategy, bars)

        snapshots = bus.get_history(PortfolioSnapshotEvent)
        fills = bus.get_history(FillEvent)

        return MetricsEngine.compute(
            snapshots=snapshots,
            fills=fills,
            initial_cash=INITIAL_CASH,
            risk_free_rate=0.0,
            trading_day_count=n_bars,
            run_id="test-run",
            strategy_id="test-strategy",
            start_date=bars[0].timestamp.date(),
            end_date=bars[-1].timestamp.date(),
            bar_dates=bar_dates,
        )

    def test_one_trade_in_log(self):
        result = self._compute()
        assert result.num_trades == 1
        assert len(result.trade_log) == 1

    def test_trade_log_has_correct_entry_exit_prices(self):
        result = self._compute(buy_at=2, sell_at=6)
        bars = _make_bars(10)
        row = result.trade_log.iloc[0]
        assert row["entry_price"] == float(bars[2].open)   # bar 3 open
        assert row["exit_price"] == float(bars[6].open)    # bar 7 open

    def test_positive_total_return(self):
        result = self._compute()
        assert result.total_return_pct > 0.0

    def test_win_rate_one_winner(self):
        result = self._compute()
        assert result.win_rate == 1.0

    def test_equity_curve_length_matches_bars(self):
        result = self._compute(n_bars=10)
        assert len(result.equity_curve) == 10

    def test_equity_curve_starts_at_initial_cash(self):
        result = self._compute()
        assert result.equity_curve.iloc[0] == pytest.approx(float(INITIAL_CASH), rel=1e-3)

    def test_no_snapshot_returns_empty_result(self):
        result = MetricsEngine.compute(
            snapshots=[],
            fills=[],
            initial_cash=INITIAL_CASH,
            risk_free_rate=0.0,
            trading_day_count=0,
            run_id="empty",
            strategy_id="test",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
        assert result.num_trades == 0
        assert result.total_return_pct == 0.0


# ---------------------------------------------------------------------------
# FIFO partial-fill trade log
# ---------------------------------------------------------------------------

class TestPartialFillTradeLog:
    """_build_trade_log must correctly match lots when fills are partial."""

    def _fill(self, side, qty, price, ts_day, commission=D("0")):
        bar = BarEvent(
            timestamp=datetime(2024, 1, ts_day, tzinfo=UTC),
            symbol="SPY",
            open=D(str(price)), high=D(str(price)), low=D(str(price)), close=D(str(price)),
            volume=1_000_000, bar_type=BarType.DAILY, source="test",
        )
        return FillEvent(
            timestamp=bar.timestamp,
            symbol="SPY",
            strategy_id="test",
            order_id=__import__("uuid").uuid4(),
            side=side,
            filled_qty=D(str(qty)),
            fill_price=D(str(price)),
            commission=D(str(commission)),
            fill_bar=bar,
        )

    def test_full_fill_single_row(self):
        from engine.backtest.metrics import _build_trade_log
        fills = [
            self._fill(Side.BUY,  "1000", "100", 1),
            self._fill(Side.SELL, "1000", "110", 5),
        ]
        tl = _build_trade_log(fills)
        assert len(tl) == 1
        assert tl.iloc[0]["entry_qty"] == pytest.approx(1000.0)
        assert tl.iloc[0]["gross_pnl"] == pytest.approx(10_000.0)

    def test_partial_sell_splits_into_two_rows(self):
        """1000-share BUY → 400-SELL, 600-SELL → two trade rows, net PnL sums correctly."""
        from engine.backtest.metrics import _build_trade_log
        fills = [
            self._fill(Side.BUY,  "1000", "100", 1),
            self._fill(Side.SELL, "400",  "110", 5),
            self._fill(Side.SELL, "600",  "115", 8),
        ]
        tl = _build_trade_log(fills)
        assert len(tl) == 2
        # First lot: 400 shares × (110-100) = 4000
        assert tl.iloc[0]["entry_qty"] == pytest.approx(400.0)
        assert tl.iloc[0]["gross_pnl"] == pytest.approx(4_000.0)
        # Second lot: 600 shares × (115-100) = 9000
        assert tl.iloc[1]["entry_qty"] == pytest.approx(600.0)
        assert tl.iloc[1]["gross_pnl"] == pytest.approx(9_000.0)
        # Net PnL sums to total position gain
        assert tl["gross_pnl"].sum() == pytest.approx(13_000.0)

    def test_partial_buy_fills_cross_correctly(self):
        """Two BUY fills of 500 each, then one SELL of 1000 → two lot rows."""
        from engine.backtest.metrics import _build_trade_log
        fills = [
            self._fill(Side.BUY,  "500",  "100", 1),
            self._fill(Side.BUY,  "500",  "105", 2),
            self._fill(Side.SELL, "1000", "120", 6),
        ]
        tl = _build_trade_log(fills)
        assert len(tl) == 2
        # First lot: 500 shares × (120-100) = 10000
        assert tl.iloc[0]["gross_pnl"] == pytest.approx(10_000.0)
        # Second lot: 500 shares × (120-105) = 7500
        assert tl.iloc[1]["gross_pnl"] == pytest.approx(7_500.0)

    def test_residual_entry_not_lost(self):
        """Partial exit should not discard the remaining entry quantity."""
        from engine.backtest.metrics import _build_trade_log
        fills = [
            self._fill(Side.BUY,  "1000", "100", 1),
            self._fill(Side.SELL, "400",  "110", 5),
            # Only 400 sold — the remaining 600 never closes in this window.
        ]
        tl = _build_trade_log(fills)
        assert len(tl) == 1
        assert tl.iloc[0]["entry_qty"] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# Multi-strategy FIFO isolation — composite (strategy_id, symbol) key
# ---------------------------------------------------------------------------

class TestBuildTradeLogMultiStrategy:
    """
    _build_trade_log must not commingle fills from different strategies that
    trade the same symbol. The FIFO queue key must be (strategy_id, symbol),
    not just symbol.
    """

    def _fill(self, strategy_id: str, side, qty, price, ts_day):
        bar = BarEvent(
            timestamp=datetime(2024, 1, ts_day, tzinfo=UTC),
            symbol="SPY",
            open=D(str(price)), high=D(str(price)), low=D(str(price)), close=D(str(price)),
            volume=1_000_000, bar_type=BarType.DAILY, source="test",
        )
        return FillEvent(
            timestamp=bar.timestamp,
            symbol="SPY",
            strategy_id=strategy_id,
            order_id=__import__("uuid").uuid4(),
            side=side,
            filled_qty=D(str(qty)),
            fill_price=D(str(price)),
            commission=D("0"),
            fill_bar=bar,
        )

    def test_two_strategies_same_symbol_correct_entry_exit_pairing(self):
        """
        Canonical commingling scenario — 100 shares each:
          strategy-a: BUY @ 100 (t=1), SELL @ 120 (t=4) → entry=100, exit=120, pnl=+2000
          strategy-b: BUY @ 110 (t=2), SELL @  90 (t=3) → entry=110, exit=90,  pnl=-2000

        With a symbol-only FIFO key, strategy-b's SELL at t=3 would incorrectly
        match against strategy-a's BUY at 100, producing pnl=-1000/-1000 instead
        of +2000/-2000. The composite key prevents this.
        """
        from engine.backtest.metrics import _build_trade_log

        fills = [
            self._fill("strategy-a", Side.BUY,  "100", "100", 1),
            self._fill("strategy-b", Side.BUY,  "100", "110", 2),
            self._fill("strategy-b", Side.SELL, "100",  "90", 3),
            self._fill("strategy-a", Side.SELL, "100", "120", 4),
        ]
        tl = _build_trade_log(fills)

        assert len(tl) == 2
        # Sort by entry price to identify each strategy's row deterministically.
        tl_sorted = tl.sort_values("entry_price").reset_index(drop=True)
        # strategy-a row: entry=100, exit=120
        assert tl_sorted.iloc[0]["entry_price"] == pytest.approx(100.0)
        assert tl_sorted.iloc[0]["exit_price"]  == pytest.approx(120.0)
        assert tl_sorted.iloc[0]["gross_pnl"]   == pytest.approx(2000.0)
        # strategy-b row: entry=110, exit=90
        assert tl_sorted.iloc[1]["entry_price"] == pytest.approx(110.0)
        assert tl_sorted.iloc[1]["exit_price"]  == pytest.approx(90.0)
        assert tl_sorted.iloc[1]["gross_pnl"]   == pytest.approx(-2000.0)

    def test_single_strategy_behavior_unchanged(self):
        """Adding the composite key dimension must not break single-strategy PnL."""
        from engine.backtest.metrics import _build_trade_log

        fills = [
            self._fill("strat", Side.BUY,  "500", "100", 1),
            self._fill("strat", Side.SELL, "500", "110", 5),
        ]
        tl = _build_trade_log(fills)
        assert len(tl) == 1
        assert tl.iloc[0]["entry_price"] == pytest.approx(100.0)
        assert tl.iloc[0]["exit_price"]  == pytest.approx(110.0)
        assert tl.iloc[0]["gross_pnl"]   == pytest.approx(5000.0)


# ---------------------------------------------------------------------------
# Warmup suppression — no fills before scored_start
# ---------------------------------------------------------------------------

class TestWarmupSuppression:
    """
    scored_start gates OrderIntentEvent so bars before that date accumulate
    price history without opening any positions.

    The _FixedSignalStrategy emits a BUY on the buy_at-th bar. If scored_start
    is set to a date after the buy_at bar, the BUY must be silently discarded
    and no fill must occur — even though the strategy emitted the intent.
    """

    def test_no_fills_before_scored_start(self):
        """
        Warmup bars (before scored_start) produce no fills even if the strategy
        emits intents. Both the BUY (bar 2) and SELL (bar 4) fall before
        scored_start (bar 5's date), so both intents are gated and the portfolio
        must end at initial_cash with no fills.
        """
        bars = _make_bars(10)
        # scored_start = 6th bar (index 5); buy=bar3 and sell=bar5 are both in warmup
        scored_start = bars[5].timestamp.date()

        bus, portfolio, strategy = _build_stack(buy_at=2, sell_at=4, scored_start=scored_start)
        _run_replay(bus, portfolio, strategy, bars)

        fills = bus.get_history(FillEvent)
        assert len(fills) == 0, (
            f"Expected 0 fills (warmup suppressed), got {len(fills)}: {fills}"
        )
        assert portfolio.cash == INITIAL_CASH, (
            f"No fills should have occurred; expected cash={INITIAL_CASH}, got {portfolio.cash}"
        )

    def test_fills_allowed_on_or_after_scored_start(self):
        """
        Intents emitted on or after scored_start must pass through normally.
        buy_at=6 falls after scored_start (bar 5's date) — must produce a fill.
        """
        bars = _make_bars(10)
        scored_start = bars[4].timestamp.date()  # scored from bar 5 onward (index 4)

        bus, portfolio, strategy = _build_stack(buy_at=6, sell_at=9, scored_start=scored_start)
        _run_replay(bus, portfolio, strategy, bars)

        fills = bus.get_history(FillEvent)
        buy_fills = [f for f in fills if f.side == Side.BUY]
        assert len(buy_fills) == 1, f"Expected 1 BUY fill after scored_start, got {len(buy_fills)}"

    def test_no_scored_start_behavior_unchanged(self):
        """scored_start=None must not gate any intents — baseline behaviour preserved."""
        bus, portfolio, strategy = _build_stack(buy_at=2, sell_at=6, scored_start=None)
        _run_replay(bus, portfolio, strategy, _make_bars(10))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 2  # BUY + SELL as in the baseline tests


# ---------------------------------------------------------------------------
# BacktestRunner artifact saving
# ---------------------------------------------------------------------------

class TestBacktestRunnerArtifactSaving:
    def _make_config(self, tmp_path):
        from engine.config import load_config

        config = load_config("config/equity_momentum.yaml")
        config.engine.run_id = "artifact-test"
        config.engine.output_dir = str(tmp_path / "results")
        config.engine.log_dir = str(tmp_path / "logs")
        config.backtest.start_date = date(2024, 1, 2)
        config.backtest.end_date = date(2024, 1, 5)
        return config

    def _run_with_artifact_flag(self, config, *, save_artifacts: bool | None):
        strategy = MagicMock()
        strategy.strategy_id = "test-strategy"
        pipeline = MagicMock()
        pipeline.get_bars.return_value = []
        result = MagicMock()

        with patch("engine.backtest.runner.DataPipeline.build", return_value=pipeline), \
             patch("engine.backtest.runner._instantiate_strategy", return_value=strategy), \
             patch("engine.backtest.runner.MetricsEngine.compute", return_value=result):
            runner = BacktestRunner(config)
            if save_artifacts is None:
                returned = runner.run()
            else:
                returned = runner.run(save_artifacts=save_artifacts)

        assert returned is result
        return result

    def test_run_saves_artifacts_by_default(self, tmp_path):
        config = self._make_config(tmp_path)

        result = self._run_with_artifact_flag(config, save_artifacts=None)

        result.save.assert_called_once_with(tmp_path / "results" / "artifact-test")

    def test_run_can_skip_result_artifacts(self, tmp_path):
        config = self._make_config(tmp_path)

        result = self._run_with_artifact_flag(config, save_artifacts=False)

        result.save.assert_not_called()

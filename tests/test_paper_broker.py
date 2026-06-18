"""Unit tests for PaperBroker — queuing, fills, slippage, commission, edge cases."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from engine.events.bus import EventBus
from engine.events.types import (
    ApprovedOrderEvent,
    BarEvent,
    BarType,
    FillEvent,
    IntentType,
    OrderIntentEvent,
    Side,
)
from engine.execution.paper_broker import PaperBroker
from engine.portfolio.state import PortfolioState
from engine.risk.gatekeeper import RiskGatekeeper
from engine.risk.rules.base import AbstractRule, RuleChain, RuleResult

UTC = timezone.utc
D = Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _AlwaysPassRule(AbstractRule):
    name = "always_pass"

    def evaluate(self, intent, snapshot, current_price):
        return RuleResult.pass_(intent.quantity, "always pass")


def _bar(symbol="SPY", ts=None, open=D("100"), high=D("110"), low=D("95"), close=D("105")):
    ts = ts or datetime(2024, 1, 2, tzinfo=UTC)
    return BarEvent(
        timestamp=ts, symbol=symbol,
        open=open, high=high, low=low, close=close,
        volume=1_000_000, bar_type=BarType.DAILY, source="test",
    )


def _make_stack(
    initial_cash="100000",
    universe=("SPY",),
    slippage_model="zero",
    commission_model="zero",
    slippage_pct="0.001",
    commission_per_share="0.005",
    min_commission="1.00",
    max_participation_pct="0",
):
    bus = EventBus()
    portfolio = PortfolioState(initial_cash=D(initial_cash))
    rule_chain = RuleChain([_AlwaysPassRule()])
    gatekeeper = RiskGatekeeper(
        bus=bus,
        snapshot_provider=portfolio.snapshot,
        rule_chain=rule_chain,
    )
    broker = PaperBroker(
        bus=bus,
        portfolio=portfolio,
        universe=list(universe),
        slippage_model=slippage_model,
        slippage_pct=slippage_pct,
        commission_model=commission_model,
        commission_per_share=commission_per_share,
        min_commission=min_commission,
        fill_at="next_open",
        max_participation_pct=max_participation_pct,
    )
    bus.subscribe(BarEvent, broker.on_bar)
    bus.subscribe(BarEvent, portfolio.on_bar)
    bus.subscribe(OrderIntentEvent, gatekeeper.on_order_intent)
    bus.subscribe(ApprovedOrderEvent, broker.on_approved_order)
    bus.subscribe(FillEvent, portfolio.on_fill)
    return bus, portfolio, broker


def _send_intent(bus, symbol="SPY", side=Side.BUY, qty=D("10"), signal_bar=None):
    bar = signal_bar or _bar(symbol=symbol)
    bus.publish(OrderIntentEvent(
        timestamp=bar.timestamp,
        strategy_id="test",
        symbol=symbol,
        side=side,
        intent_type=IntentType.MARKET,
        quantity=qty,
        signal_bar=bar,
    ))
    return bar


def _send_limit_intent(bus, symbol="SPY", side=Side.BUY, qty=D("10"), limit_price=D("100"), signal_bar=None):
    bar = signal_bar or _bar(symbol=symbol)
    bus.publish(OrderIntentEvent(
        timestamp=bar.timestamp,
        strategy_id="test",
        symbol=symbol,
        side=side,
        intent_type=IntentType.LIMIT,
        quantity=qty,
        signal_bar=bar,
        limit_price=limit_price,
    ))
    return bar


T1 = datetime(2024, 1, 2, tzinfo=UTC)
T2 = datetime(2024, 1, 3, tzinfo=UTC)
T3 = datetime(2024, 1, 4, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Queuing and fill timing
# ---------------------------------------------------------------------------

class TestQueueAndFillTiming:
    def test_queue_on_submit(self):
        bus, _, broker = _make_stack()
        _send_intent(bus, signal_bar=_bar(ts=T1))
        assert len(broker.pending_orders) == 1

    def test_fill_at_next_bar_open(self):
        bus, _, broker = _make_stack()
        _send_intent(bus, signal_bar=_bar(ts=T1))

        bus.publish(_bar(ts=T2, open=D("102")))

        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].fill_price == D("102")   # zero slippage → open price
        assert len(broker.pending_orders) == 0

    def test_no_fill_same_bar_timestamp(self):
        bus, _, broker = _make_stack()
        _send_intent(bus, signal_bar=_bar(ts=T1))

        bus.publish(_bar(ts=T1, open=D("102")))  # same timestamp — should not fill

        assert len(bus.get_history(FillEvent)) == 0
        assert len(broker.pending_orders) == 1

    def test_fill_clears_pending_queue(self):
        bus, _, broker = _make_stack()
        _send_intent(bus, signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2))
        assert len(broker.pending_orders) == 0


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

class TestSlippage:
    def test_buy_slippage_increases_price(self):
        bus, _, _ = _make_stack(slippage_model="fixed_pct", slippage_pct="0.001")
        _send_intent(bus, signal_bar=_bar(ts=T1))

        bus.publish(_bar(ts=T2, open=D("100")))

        fills = bus.get_history(FillEvent)
        assert fills[0].fill_price == D("100") * (D("1") + D("0.001"))

    def test_sell_slippage_decreases_price(self):
        # BUY first, then SELL
        bus, _, _ = _make_stack(slippage_model="fixed_pct", slippage_pct="0.001")
        _send_intent(bus, side=Side.BUY, qty=D("5"), signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("100")))     # fills the BUY

        _send_intent(bus, side=Side.SELL, qty=D("5"), signal_bar=_bar(ts=T2))
        bus.publish(_bar(ts=T3, open=D("110")))     # fills the SELL

        fills = bus.get_history(FillEvent)
        sell_fill = next(f for f in fills if f.side == Side.SELL)
        assert sell_fill.fill_price == D("110") * (D("1") - D("0.001"))

    def test_zero_slippage_uses_open_exactly(self):
        bus, _, _ = _make_stack(slippage_model="zero")
        _send_intent(bus, signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("123.45")))
        fills = bus.get_history(FillEvent)
        assert fills[0].fill_price == D("123.45")


# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------

class TestCommission:
    def test_zero_commission_model(self):
        bus, _, _ = _make_stack(commission_model="zero")
        _send_intent(bus, signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("100")))
        fills = bus.get_history(FillEvent)
        assert fills[0].commission == D("0")

    def test_per_share_above_minimum(self):
        bus, _, _ = _make_stack(
            commission_model="per_share",
            commission_per_share="0.01",
            min_commission="1.00",
        )
        _send_intent(bus, qty=D("200"), signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("100")))
        fills = bus.get_history(FillEvent)
        # 200 × 0.01 = 2.00 > min 1.00
        assert fills[0].commission == D("2.00")

    def test_per_share_floored_at_minimum(self):
        bus, _, _ = _make_stack(
            commission_model="per_share",
            commission_per_share="0.005",
            min_commission="1.00",
        )
        _send_intent(bus, qty=D("10"), signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("100")))
        fills = bus.get_history(FillEvent)
        # 10 × 0.005 = 0.05 < min 1.00 → floored at 1.00
        assert fills[0].commission == D("1.00")

    def test_flat_commission(self):
        bus, _, _ = _make_stack(
            commission_model="flat",
            min_commission="2.50",
        )
        _send_intent(bus, qty=D("50"), signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("100")))
        fills = bus.get_history(FillEvent)
        assert fills[0].commission == D("2.50")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_unknown_symbol_dropped(self):
        bus, _, broker = _make_stack(universe=("SPY",))
        _send_intent(bus, symbol="AAPL")    # AAPL not in universe
        assert len(broker.pending_orders) == 0

    def test_buy_oversized_capped_at_submit_and_partially_filled(self):
        # cash = 1000, buy 10 @ close 200 = 2000 notional requested.
        # submit() caps reservation at available cash (no drop) so the order
        # is queued; fill handler resizes to max affordable qty (floor(1000/200)=5).
        bus, portfolio, broker = _make_stack(initial_cash="1000")
        _send_intent(bus, qty=D("10"), signal_bar=_bar(ts=T1, close=D("200")))
        assert len(broker.pending_orders) == 1   # queued with capped reservation
        bus.publish(_bar(ts=T2, open=D("200")))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].filled_qty == D("5")     # floor(1000 / 200) = 5

    def test_buy_cancelled_when_no_shares_affordable(self):
        # cash = 9 (less than price), so even 1 share is unaffordable → cancel.
        bus, portfolio, broker = _make_stack(initial_cash="9")
        _send_intent(bus, qty=D("1"), signal_bar=_bar(ts=T1, close=D("10")))
        bus.publish(_bar(ts=T2, open=D("10")))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 0
        assert len(broker.pending_orders) == 0   # cancelled at fill time

    def test_buy_gap_up_partially_filled(self):
        # Order approved at close=95; next open gaps up to 105.
        # cash=1000, qty=10, close=95 → reservation=950 ≤ 1000 (queued).
        # At fill: 10×105=1050 > 1000 → resize to floor(1000/105)=9 shares.
        bus, portfolio, broker = _make_stack(initial_cash="1000", slippage_model="zero")
        _send_intent(bus, qty=D("10"), signal_bar=_bar(ts=T1, close=D("95")))
        assert len(broker.pending_orders) == 1
        bus.publish(_bar(ts=T2, open=D("105"), close=D("105")))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].filled_qty == D("9")     # floor(1000 / 105) = 9
        assert len(broker.pending_orders) == 0

    def test_cancel_before_fill(self):
        bus, _, broker = _make_stack()
        _send_intent(bus, signal_bar=_bar(ts=T1))
        order_id = broker.pending_orders[0].order_id

        broker.cancel(order_id)

        assert len(broker.pending_orders) == 0
        bus.publish(_bar(ts=T2))
        assert len(bus.get_history(FillEvent)) == 0

    def test_cancel_idempotent_on_nonexistent(self):
        import uuid
        _, _, broker = _make_stack()
        broker.cancel(uuid.uuid4())   # should not raise

    def test_fill_produces_portfolio_position(self):
        bus, portfolio, _ = _make_stack()
        _send_intent(bus, qty=D("5"), signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("100")))
        assert "SPY" in portfolio.positions
        assert portfolio.positions["SPY"].quantity == D("5")


# ---------------------------------------------------------------------------
# Limit order fill semantics
# ---------------------------------------------------------------------------

class TestLimitOrderFills:
    def test_buy_limit_not_triggered_when_low_above_limit(self):
        """BUY limit order does not fill when bar.low > limit_price."""
        bus, _, broker = _make_stack(slippage_model="fixed_pct", slippage_pct="0.001")
        _send_limit_intent(bus, side=Side.BUY, limit_price=D("98"), signal_bar=_bar(ts=T1))
        # low=100 > 98 → no fill
        bus.publish(_bar(ts=T2, open=D("102"), high=D("110"), low=D("100"), close=D("105")))
        assert len(bus.get_history(FillEvent)) == 0
        assert len(broker.pending_orders) == 1

    def test_sell_limit_not_triggered_when_high_below_limit(self):
        """SELL limit order does not fill when bar.high < limit_price."""
        bus, portfolio, broker = _make_stack(slippage_model="fixed_pct", slippage_pct="0.001")
        # First acquire a position
        _send_intent(bus, side=Side.BUY, qty=D("5"), signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("100"), high=D("110"), low=D("95"), close=D("105")))
        # Now place a SELL limit above the high
        _send_limit_intent(bus, side=Side.SELL, qty=D("5"), limit_price=D("120"), signal_bar=_bar(ts=T2))
        bus.publish(_bar(ts=T3, open=D("115"), high=D("118"), low=D("112"), close=D("116")))
        fills = bus.get_history(FillEvent)
        sell_fills = [f for f in fills if f.side == Side.SELL]
        assert len(sell_fills) == 0

    def test_buy_limit_fill_price_never_exceeds_limit(self):
        """BUY limit fill price ≤ limit_price even after slippage is applied."""
        bus, _, _ = _make_stack(slippage_model="fixed_pct", slippage_pct="0.005")
        limit = D("100")
        _send_limit_intent(bus, side=Side.BUY, limit_price=limit, signal_bar=_bar(ts=T1))
        # bar.low=90 ≤ 100 so order should trigger; open=98 → base=min(98,100)=98
        # slippage: 98 * 1.005 = 98.49 ≤ 100 → clamped at 98.49 (no clamp needed)
        # Try with open=99 → base=min(99,100)=99 → slippage 99*1.005=99.495 ≤ 100 ✓
        bus.publish(_bar(ts=T2, open=D("99"), high=D("110"), low=D("90"), close=D("102")))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].fill_price <= limit

    def test_buy_limit_slippage_clamped_to_limit(self):
        """Slippage that would push BUY fill above limit is clamped to limit."""
        bus, _, _ = _make_stack(slippage_model="fixed_pct", slippage_pct="0.01")
        limit = D("100")
        _send_limit_intent(bus, side=Side.BUY, limit_price=limit, signal_bar=_bar(ts=T1))
        # open=100 → base=min(100,100)=100 → slippage 100*1.01=101 > 100 → clamped to 100
        bus.publish(_bar(ts=T2, open=D("100"), high=D("110"), low=D("90"), close=D("102")))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].fill_price == limit

    def test_sell_limit_slippage_clamped_to_limit(self):
        """Slippage that would push SELL fill below limit is clamped to limit."""
        bus, _, _ = _make_stack(slippage_model="fixed_pct", slippage_pct="0.01")
        limit = D("100")
        # Acquire position first
        _send_intent(bus, side=Side.BUY, qty=D("5"), signal_bar=_bar(ts=T1))
        bus.publish(_bar(ts=T2, open=D("80"), high=D("110"), low=D("75"), close=D("100")))
        # SELL limit=100; open=100 → base=max(100,100)=100 → slippage 100*0.99=99 < 100 → clamped to 100
        _send_limit_intent(bus, side=Side.SELL, qty=D("5"), limit_price=limit, signal_bar=_bar(ts=T2))
        bus.publish(_bar(ts=T3, open=D("100"), high=D("110"), low=D("90"), close=D("105")))
        fills = bus.get_history(FillEvent)
        sell_fills = [f for f in fills if f.side == Side.SELL]
        assert len(sell_fills) == 1
        assert sell_fills[0].fill_price >= limit


# ---------------------------------------------------------------------------
# Volume participation limits
# ---------------------------------------------------------------------------

class TestVolumeParticipation:
    def _bar_vol(self, volume, ts=T1):
        return BarEvent(
            timestamp=ts, symbol="SPY",
            open=D("100"), high=D("110"), low=D("95"), close=D("105"),
            volume=volume, bar_type=BarType.DAILY, source="test",
        )

    def test_fill_qty_capped_at_participation_pct(self):
        """Requested qty above vol cap is trimmed to vol_cap shares."""
        # volume=1000, participation=0.10 → cap=100; request 200 → fill 100
        bus, _, _ = _make_stack(max_participation_pct="0.10")
        _send_intent(bus, qty=D("200"), signal_bar=self._bar_vol(1000, ts=T1))
        bus.publish(self._bar_vol(1000, ts=T2))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].filled_qty == D("100")

    def test_order_cancelled_when_vol_too_thin(self):
        """Order is cancelled when participation cap yields < 1 share."""
        # volume=10, participation=0.05 → cap=floor(0.5)=0 → cancel
        bus, _, broker = _make_stack(max_participation_pct="0.05")
        _send_intent(bus, qty=D("10"), signal_bar=self._bar_vol(10, ts=T1))
        bus.publish(self._bar_vol(10, ts=T2))
        assert len(bus.get_history(FillEvent)) == 0
        assert len(broker.pending_orders) == 0

    def test_no_cap_when_participation_pct_zero(self):
        """max_participation_pct=0 disables the cap; full qty fills."""
        bus, _, _ = _make_stack(max_participation_pct="0")
        _send_intent(bus, qty=D("200"), signal_bar=self._bar_vol(100, ts=T1))
        bus.publish(self._bar_vol(100, ts=T2))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].filled_qty == D("200")

    def test_qty_within_cap_fills_unchanged(self):
        """Requested qty already below cap fills at full qty."""
        # volume=10000, participation=0.025 → cap=250; request 10 → fill 10
        bus, _, _ = _make_stack(max_participation_pct="0.025")
        _send_intent(bus, qty=D("10"), signal_bar=self._bar_vol(10000, ts=T1))
        bus.publish(self._bar_vol(10000, ts=T2))
        fills = bus.get_history(FillEvent)
        assert len(fills) == 1
        assert fills[0].filled_qty == D("10")

    def test_zero_volume_bar_cancels_order(self):
        """When bar.volume==0 and participation cap is enabled, order is cancelled."""
        bus, _, broker = _make_stack(max_participation_pct="0.025")
        _send_intent(bus, qty=D("10"), signal_bar=self._bar_vol(10000, ts=T1))
        # Publish a bar with volume=0 — no liquidity.
        bus.publish(self._bar_vol(0, ts=T2))
        assert len(bus.get_history(FillEvent)) == 0
        assert len(broker.pending_orders) == 0


# ---------------------------------------------------------------------------
# Handler order: fills at open precede MTM at close
# ---------------------------------------------------------------------------

class TestHandlerOrderPeakEquity:
    """
    Broker fills pending sells at today's open BEFORE portfolio MTM at today's close.

    Timeline (fill_at=next_open):
      T1: BUY intent queued (signal bar)
      T2: BUY fills at T2 open=$100; portfolio MTM at T2 close=$105
      T2: SELL intent queued (signal bar=T2 bar)
      T3: SELL fills at T3 open=$110 FIRST (broker.on_bar), then MTM at T3 close=$120
          → position is already gone when MTM runs; peak_equity stays at settled value

    Under the old wrong order (portfolio.on_bar first):
      T3: MTM runs at close=$120 with position still "open"
          → phantom peak_equity = cash(99000) + 10×120 = 100200
      Then fill fires at $110 → settled equity = cash = 100100
      Assertion: peak_equity(100200) <= settled_equity(100100) would FAIL, catching the bug.

    Under the correct order (broker.on_bar first):
      T3: fill fires at $110 → cash=100100, position closed
      MTM runs: no position → equity stays at 100100
      peak_equity = 100100 == settled_equity ✓
    """

    def test_peak_equity_matches_settled_not_phantom_close(self):
        T1 = datetime(2024, 1, 2, tzinfo=UTC)
        T2 = datetime(2024, 1, 3, tzinfo=UTC)
        T3 = datetime(2024, 1, 4, tzinfo=UTC)

        bus, portfolio, broker = _make_stack(initial_cash="100000")

        # Queue BUY intent on T1 signal bar
        t1_bar = _bar(symbol="SPY", ts=T1, open=D("100"), high=D("105"), low=D("95"), close=D("103"))
        _send_intent(bus, symbol="SPY", side=Side.BUY, qty=D("10"), signal_bar=t1_bar)

        # T2 bar: BUY fills at open=$100 (next bar after T1 intent)
        t2_bar = _bar(symbol="SPY", ts=T2, open=D("100"), high=D("108"), low=D("98"), close=D("105"))
        bus.publish(t2_bar)
        portfolio.publish_snapshot(bus)

        fills = bus.get_history(FillEvent)
        assert len(fills) == 1 and fills[0].side == Side.BUY, "BUY must fill on T2 bar"
        assert fills[0].fill_price == D("100")

        # Queue SELL intent on T2 signal bar
        _send_intent(bus, symbol="SPY", side=Side.SELL, qty=D("10"), signal_bar=t2_bar)

        # T3 bar: open=$110 (SELL fills here), close=$120 (higher — would inflate peak if MTM ran first)
        t3_bar = _bar(symbol="SPY", ts=T3, open=D("110"), high=D("125"), low=D("108"), close=D("120"))
        bus.publish(t3_bar)

        fills_after = bus.get_history(FillEvent)
        sell_fills = [f for f in fills_after if f.side == Side.SELL]
        assert len(sell_fills) == 1, "SELL must fill on T3 bar"
        assert sell_fills[0].fill_price == D("110"), f"Expected fill at open $110, got {sell_fills[0].fill_price}"

        portfolio.publish_snapshot(bus)
        snap = portfolio.snapshot()

        # Bought at 100, sold at 110 → cash = 100000 - 10*100 + 10*110 = 100100
        settled_equity = D("100100")
        assert snap.equity == settled_equity, f"Settled equity should be {settled_equity}, got {snap.equity}"

        # Under wrong handler order, MTM at $120 close before fill would produce
        # phantom peak_equity = 100000 - 1000 + 1200 = 100200. Assert it never happened.
        assert snap.peak_equity <= settled_equity, (
            f"peak_equity {snap.peak_equity} exceeds settled equity {settled_equity} — "
            "position was MTM'd at close=$120 before the open=$110 fill settled it"
        )

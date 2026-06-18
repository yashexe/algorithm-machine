"""
PortfolioState — the single source of truth for account state.

PortfolioState is a pure event consumer. It never generates signals or
makes execution decisions. Its two public event handlers are registered
on the EventBus by the engine runner:

    bus.subscribe(BarEvent,  portfolio.on_bar)
    bus.subscribe(FillEvent, portfolio.on_fill)

Publishing a snapshot is an explicit, separate action:

    portfolio.publish_snapshot(bus)

The runner calls this once per bar cycle, after all fills for that cycle
have been processed. This produces exactly one PortfolioSnapshotEvent per
trading day for the metrics and monitoring pipeline.

State-transition invariants (asserted after every mutation, disabled with
python -O):
  1. cash >= 0
  2. every position.quantity >= 0
  3. equity >= 0
  4. peak_equity >= equity  (watermark is monotonically non-decreasing)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from engine.events.types import (
    BarEvent,
    FillEvent,
    PortfolioSnapshotEvent,
    PositionSnapshot,
    Side,
)
from engine.portfolio.position import PendingOrder, Position

if TYPE_CHECKING:
    from engine.events.bus import EventBus

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# PortfolioSnapshot — immutable view passed to RiskGatekeeper
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class PortfolioSnapshot:
    """
    Deep-copy, immutable view of PortfolioState at a single point in time.

    The risk engine receives this object; it cannot mutate live portfolio state
    through it. positions and open_orders are immutable at the container level
    (dict values are frozen PositionSnapshots; open_orders is a tuple).

    Treat the positions dict as read-only — do not add or remove keys.
    """

    timestamp: datetime
    equity: Decimal
    cash: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal
    positions: dict[str, PositionSnapshot]  # treat as read-only
    open_orders: tuple[PendingOrder, ...]


# ---------------------------------------------------------------------------
# PortfolioState
# ---------------------------------------------------------------------------

class PortfolioState:
    """
    Mutable aggregate root for all account state.

    Thread-safety: not thread-safe. The synchronous EventBus guarantees
    single-threaded access in the MVP.
    """

    def __init__(self, initial_cash: Decimal, run_id: str = "") -> None:
        self.run_id = run_id
        self.initial_cash = initial_cash

        self.cash: Decimal = initial_cash
        self.positions: dict[str, Position] = {}
        self.peak_equity: Decimal = initial_cash
        self.realized_pnl: Decimal = _ZERO

        # Latest close price per symbol (updated by on_bar, used when opening
        # a new position before its first bar MTM update)
        self._price_cache: dict[str, Decimal] = {}

        # Immutable fill history — never mutated, only appended
        self._fill_history: list[FillEvent] = []

        # Pending (approved-but-unfilled) orders keyed by order_id
        self._pending_orders: dict[UUID, PendingOrder] = {}

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def equity(self) -> Decimal:
        return self.cash + sum(
            (p.market_value for p in self.positions.values()), _ZERO
        )

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum(
            (p.unrealized_pnl for p in self.positions.values()), _ZERO
        )

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity == _ZERO:
            return _ZERO
        dd = (self.peak_equity - self.equity) / self.peak_equity
        return max(_ZERO, dd)

    @property
    def total_return_pct(self) -> Decimal:
        if self.initial_cash == _ZERO:
            return _ZERO
        return (self.equity - self.initial_cash) / self.initial_cash

    # ------------------------------------------------------------------
    # Event handlers  (register these on the EventBus at engine startup)
    # ------------------------------------------------------------------

    def on_bar(self, event: BarEvent) -> None:
        """
        Update mark-to-market prices from the latest bar close.
        Must be registered *before* strategy handlers so that drawdown
        and position values are current when the risk engine evaluates.
        """
        self._price_cache[event.symbol] = event.close
        if event.symbol in self.positions:
            self.positions[event.symbol].last_price = event.close
        self._refresh_peak()

    def on_fill(self, event: FillEvent) -> None:
        """
        Apply a confirmed fill: update positions, cash, and realized P&L.
        Releases the pending-order cash reservation for this order_id.
        """
        self._fill_history.append(event)
        self._pending_orders.pop(event.order_id, None)

        if event.side == Side.BUY:
            self._apply_buy(event)
        else:
            self._apply_sell(event)

        self._refresh_peak()
        self._assert_invariants()

    # ------------------------------------------------------------------
    # Open-order accounting
    # ------------------------------------------------------------------

    def register_pending_order(self, order: PendingOrder) -> None:
        """
        Soft-reserve cash for a BUY order that has been approved but not
        yet filled. Called by PaperBroker immediately after it receives an
        ApprovedOrderEvent and before the next snapshot is taken.
        """
        self._pending_orders[order.order_id] = order

    def cancel_pending_order(self, order_id: UUID) -> None:
        """Release a reservation when an order is cancelled or expires."""
        self._pending_orders.pop(order_id, None)

    # ------------------------------------------------------------------
    # Snapshot for risk engine
    # ------------------------------------------------------------------

    def snapshot(self) -> PortfolioSnapshot:
        """
        Return a deep-copy, immutable view of current state.
        Called by RiskGatekeeper.evaluate() before each intent evaluation.
        The snapshot is taken at the *current* point in time — callers must
        not cache it across bar cycles.
        """
        return PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            equity=self.equity,
            cash=self.cash,
            peak_equity=self.peak_equity,
            drawdown_pct=self.drawdown_pct,
            positions={sym: pos.snapshot() for sym, pos in self.positions.items()},
            open_orders=tuple(self._pending_orders.values()),
        )

    # ------------------------------------------------------------------
    # Event publishing
    # ------------------------------------------------------------------

    def publish_snapshot(self, bus: "EventBus") -> None:
        """
        Publish the current portfolio state as a PortfolioSnapshotEvent.
        Called explicitly by the runner once per bar cycle, after all fills
        for that cycle have been applied.
        """
        event = PortfolioSnapshotEvent(
            equity=self.equity,
            cash=self.cash,
            initial_cash=self.initial_cash,
            total_return_pct=self.total_return_pct,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            drawdown_pct=self.drawdown_pct,
            peak_equity=self.peak_equity,
            positions=tuple(p.snapshot() for p in self.positions.values()),
            num_positions=len(self.positions),
        )
        bus.publish(event)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"PortfolioState("
            f"equity={self.equity:.2f}, "
            f"cash={self.cash:.2f}, "
            f"positions={len(self.positions)}, "
            f"drawdown={self.drawdown_pct:.1%}, "
            f"return={self.total_return_pct:+.1%})"
        )

    # ------------------------------------------------------------------
    # Private — fill application
    # ------------------------------------------------------------------

    def _apply_buy(self, fill: FillEvent) -> None:
        total_cost = fill.filled_qty * fill.fill_price + fill.commission
        sym = fill.symbol

        if sym in self.positions:
            pos = self.positions[sym]
            existing_basis = pos.quantity * pos.avg_cost
            new_qty = pos.quantity + fill.filled_qty
            pos.avg_cost = (existing_basis + fill.filled_qty * fill.fill_price) / new_qty
            pos.quantity = new_qty
        else:
            # Use cached price if available (may be prior bar's close);
            # on_bar will update last_price to today's close shortly after.
            last_price = self._price_cache.get(sym, fill.fill_price)
            self.positions[sym] = Position(
                symbol=sym,
                quantity=fill.filled_qty,
                avg_cost=fill.fill_price,
                last_price=last_price,
            )

        self.cash -= total_cost

    def _apply_sell(self, fill: FillEvent) -> None:
        sym = fill.symbol

        if sym not in self.positions:
            raise ValueError(
                f"SELL fill for {sym} but no open position exists. "
                f"Fill order_id={fill.order_id}."
            )

        pos = self.positions[sym]

        if pos.quantity < fill.filled_qty:
            raise ValueError(
                f"SELL fill qty {fill.filled_qty} exceeds position qty "
                f"{pos.quantity} for {sym}. Fill order_id={fill.order_id}."
            )

        proceeds = fill.filled_qty * fill.fill_price - fill.commission
        self.realized_pnl += fill.filled_qty * (fill.fill_price - pos.avg_cost)

        pos.quantity -= fill.filled_qty
        if pos.quantity == _ZERO:
            del self.positions[sym]

        self.cash += proceeds

    # ------------------------------------------------------------------
    # Private — helpers
    # ------------------------------------------------------------------

    def _refresh_peak(self) -> None:
        eq = self.equity
        if eq > self.peak_equity:
            self.peak_equity = eq

    def _assert_invariants(self) -> None:
        assert self.cash >= _ZERO, (
            f"Invariant violated: cash={self.cash} < 0. "
            "CashSolvencyRule should have prevented this."
        )
        for sym, pos in self.positions.items():
            assert pos.quantity >= _ZERO, (
                f"Invariant violated: {sym} quantity={pos.quantity} < 0. "
                "ShortSellingRule should have prevented this."
            )
        assert self.equity >= _ZERO, (
            f"Invariant violated: equity={self.equity} < 0."
        )
        assert self.peak_equity >= self.equity, (
            f"Invariant violated: peak_equity={self.peak_equity} < equity={self.equity}. "
            "peak_equity must be monotonically non-decreasing."
        )

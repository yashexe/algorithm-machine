"""
Position and PendingOrder models.

Position is mutable — PortfolioState updates its fields on every bar
(last_price) and every fill (quantity, avg_cost). It is never exposed
directly outside PortfolioState; callers receive a PositionSnapshot.

PendingOrder is frozen. It lives here (not in the execution layer) so
that PortfolioSnapshot can reference it without creating a circular
import between portfolio ← execution ← portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from engine.events.types import PositionSnapshot, Side

_ZERO = Decimal("0")


@dataclass
class Position:
    """
    Mutable record of one open long position.

    avg_cost is the volume-weighted average purchase price (updated on
    every BUY fill that adds to this position). last_price is the most
    recent mark-to-market close, updated by PortfolioState.on_bar.
    """

    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    last_price: Decimal

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.last_price

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.avg_cost

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.market_value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> Decimal:
        basis = self.cost_basis
        if basis == _ZERO:
            return _ZERO
        return self.unrealized_pnl / basis

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> PositionSnapshot:
        """Return an immutable copy for the risk engine and event bus."""
        return PositionSnapshot(
            symbol=self.symbol,
            quantity=self.quantity,
            avg_cost=self.avg_cost,
            last_price=self.last_price,
            market_value=self.market_value,
            unrealized_pnl=self.unrealized_pnl,
        )

    def __repr__(self) -> str:
        return (
            f"Position({self.symbol} "
            f"qty={self.quantity} "
            f"avg_cost={self.avg_cost:.4f} "
            f"last={self.last_price:.4f} "
            f"pnl={self.unrealized_pnl:+.2f})"
        )


@dataclass(frozen=True, kw_only=True)
class PendingOrder:
    """
    An approved order queued in the broker, awaiting fill at next-bar open.

    reserved_cash is the cash soft-reserved for BUY orders. The risk engine
    reads open_orders from PortfolioSnapshot to avoid approving orders whose
    combined cost would exceed available cash within the same bar cycle.

    SELL orders carry reserved_cash=0 (they generate cash, not consume it).
    """

    order_id: UUID
    symbol: str
    side: Side
    quantity: Decimal
    reserved_cash: Decimal

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from engine.events.types import ApprovedOrderEvent, IntentType, Side
from engine.portfolio.position import PendingOrder as PortfolioPendingOrder

_ZERO = Decimal("0")


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(kw_only=True)
class QueuedOrder:
    """
    Broker-side approved order waiting for execution.

    PortfolioState uses a smaller PendingOrder reservation model to avoid a
    circular dependency. This queue model keeps the full execution lifecycle
    state described in docs/EXECUTION_ENGINE.md.
    """

    origin_event: ApprovedOrderEvent
    order_id: UUID = field(default_factory=uuid.uuid4)
    status: OrderStatus = OrderStatus.PENDING

    @property
    def symbol(self) -> str:
        return self.origin_event.origin_intent.symbol

    @property
    def side(self) -> Side:
        return self.origin_event.approved_side

    @property
    def order_type(self) -> IntentType:
        return self.origin_event.approved_type

    @property
    def approved_qty(self) -> Decimal:
        return self.origin_event.approved_qty

    @property
    def limit_price(self) -> Decimal | None:
        return self.origin_event.origin_intent.limit_price

    @property
    def submitted_at(self) -> datetime:
        return self.origin_event.timestamp

    @property
    def signal_bar_timestamp(self) -> datetime:
        return self.origin_event.origin_intent.signal_bar.timestamp

    def __post_init__(self) -> None:
        if self.approved_qty <= _ZERO:
            raise ValueError("queued orders require a positive approved quantity")

    def mark_filled(self) -> None:
        self._require_pending("fill")
        self.status = OrderStatus.FILLED

    def mark_cancelled(self) -> None:
        self._require_pending("cancel")
        self.status = OrderStatus.CANCELLED

    def mark_expired(self) -> None:
        self._require_pending("expire")
        self.status = OrderStatus.EXPIRED

    def is_pending_for_next_bar(self, symbol: str, bar_timestamp: datetime) -> bool:
        return (
            self.status == OrderStatus.PENDING
            and self.symbol == symbol
            and self.signal_bar_timestamp < bar_timestamp
        )

    def to_portfolio_pending_order(
        self,
        reserved_cash: Decimal = _ZERO,
    ) -> PortfolioPendingOrder:
        if reserved_cash < _ZERO:
            raise ValueError("reserved_cash cannot be negative")
        return PortfolioPendingOrder(
            order_id=self.order_id,
            symbol=self.symbol,
            side=self.side,
            quantity=self.approved_qty,
            reserved_cash=reserved_cash if self.side == Side.BUY else _ZERO,
        )

    def _require_pending(self, action: str) -> None:
        if self.status != OrderStatus.PENDING:
            raise ValueError(f"cannot {action} order in {self.status.value} state")

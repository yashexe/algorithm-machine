from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from uuid import UUID

from engine.events.bus import EventBus
from engine.events.types import (
    ApprovedOrderEvent,
    BarEvent,
    FillEvent,
    IntentType,
    OrderSubmittedEvent,
    Side,
)
from engine.execution.base import AbstractBroker
from engine.execution.order import OrderStatus, QueuedOrder
from engine.portfolio import PortfolioState

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")


class PaperBroker(AbstractBroker):
    """
    Paper-trading broker that accepts only gatekeeper-approved orders.

    Default fills use the documented next-bar-open discipline. `prev_close`
    exists only for the optimistic comparison mode described in the execution
    config reference.
    """

    def __init__(
        self,
        bus: EventBus,
        portfolio: PortfolioState,
        universe: list[str] | tuple[str, ...] | set[str],
        slippage_model: str = "fixed_pct",
        slippage_pct: Decimal | str | float = Decimal("0.0005"),
        commission_model: str = "per_share",
        commission_per_share: Decimal | str | float = Decimal("0.005"),
        min_commission: Decimal | str | float = Decimal("1.00"),
        fill_at: str = "next_open",
        max_participation_pct: Decimal | str | float = Decimal("0.025"),
    ) -> None:
        self._bus = bus
        self._portfolio = portfolio
        self._universe = {symbol.upper() for symbol in universe}
        self._slippage_model = slippage_model
        self._slippage_pct = _decimal(slippage_pct)
        self._commission_model = commission_model
        self._commission_per_share = _decimal(commission_per_share)
        self._min_commission = _decimal(min_commission)
        self._fill_at = fill_at
        self._max_participation_pct = _decimal(max_participation_pct)
        self._pending: dict[str, list[QueuedOrder]] = {}
        self._orders_by_id: dict[UUID, QueuedOrder] = {}

        if self._slippage_pct < _ZERO:
            raise ValueError("slippage_pct must be non-negative")
        if self._commission_per_share < _ZERO:
            raise ValueError("commission_per_share must be non-negative")
        if self._min_commission < _ZERO:
            raise ValueError("min_commission must be non-negative")
        if self._max_participation_pct < _ZERO:
            raise ValueError("max_participation_pct must be non-negative")
        if self._slippage_model not in {"fixed_pct", "zero"}:
            raise ValueError("paper MVP supports only fixed_pct and zero slippage")
        if self._commission_model not in {"per_share", "flat", "zero"}:
            raise ValueError("unknown commission model")
        if self._fill_at not in {"next_open", "prev_close"}:
            raise ValueError("fill_at must be next_open or prev_close")

    def on_approved_order(self, event: ApprovedOrderEvent) -> None:
        self.submit(event)

    def submit(self, order: ApprovedOrderEvent) -> None:
        symbol = order.origin_intent.symbol
        if order.approved_qty <= _ZERO:
            logger.error("Dropping approved order with non-positive quantity: %s", order.event_id)
            return
        if symbol not in self._universe:
            logger.error("Dropping approved order for symbol outside universe: %s", symbol)
            return

        reservation_price = self._reservation_price(
            order.origin_intent.signal_bar.close,
            order.approved_side,
        )
        reserved_cash = self._required_cash(order.approved_qty, reservation_price)
        if order.approved_side == Side.BUY and reserved_cash > self._portfolio.cash:
            # Cap the reservation at available cash rather than dropping the order.
            # The slippage/min-commission gap between the risk-rule estimate and the
            # broker's reservation formula can cause valid small orders to be dropped.
            # The fill handler will resize to a partial fill or cancel if unaffordable.
            reserved_cash = self._portfolio.cash
            logger.debug(
                "BUY order %s: reservation capped at %s (slippage/commission gap; "
                "fill handler will resize if needed)",
                order.event_id,
                reserved_cash,
            )

        queued = self._new_queued_order(order)
        self._pending.setdefault(symbol, []).append(queued)
        self._orders_by_id[queued.order_id] = queued

        self._portfolio.register_pending_order(
            queued.to_portfolio_pending_order(reserved_cash=reserved_cash)
        )

        self._bus.publish(
            OrderSubmittedEvent(
                timestamp=order.timestamp,
                order_id=queued.order_id,
                origin_approval=order,
            )
        )

        if self._fill_at == "prev_close":
            self._fill_order(queued, order.origin_intent.signal_bar, use_close=True)

    def on_bar(self, event: BarEvent) -> None:
        pending = self._pending.get(event.symbol)
        if not pending:
            return

        remaining: list[QueuedOrder] = []
        for order in list(pending):
            if not order.is_pending_for_next_bar(event.symbol, event.timestamp):
                if order.status == OrderStatus.PENDING:
                    remaining.append(order)
                continue

            filled = self._fill_order(order, event, use_close=False)
            if not filled and order.status == OrderStatus.PENDING:
                remaining.append(order)

        if remaining:
            self._pending[event.symbol] = remaining
        else:
            self._pending.pop(event.symbol, None)

    def cancel(self, order_id: UUID) -> None:
        order = self._orders_by_id.get(order_id)
        if order is None or order.status != OrderStatus.PENDING:
            return
        order.mark_cancelled()
        self._portfolio.cancel_pending_order(order_id)
        self._remove_from_symbol_queue(order)

    @property
    def pending_orders(self) -> tuple[QueuedOrder, ...]:
        return tuple(
            order
            for orders in self._pending.values()
            for order in orders
            if order.status == OrderStatus.PENDING
        )

    def _new_queued_order(self, approval: ApprovedOrderEvent) -> QueuedOrder:
        for _ in range(10):
            order_id = uuid.uuid4()
            if order_id not in self._orders_by_id:
                return QueuedOrder(origin_event=approval, order_id=order_id)
        raise RuntimeError("could not generate unique broker order id")

    def _fill_order(self, order: QueuedOrder, bar: BarEvent, use_close: bool) -> bool:
        fill_price = self._fill_price(order, bar, use_close=use_close)
        if fill_price is None:
            return False

        fill_qty = order.approved_qty

        # Volume participation cap (0 = disabled)
        if self._max_participation_pct > _ZERO:
            if bar.volume == 0:
                logger.warning(
                    "Cancelling %s order %s for %s: bar volume is zero (no liquidity)",
                    order.side, order.order_id, order.symbol,
                )
                order.mark_cancelled()
                self._portfolio.cancel_pending_order(order.order_id)
                self._remove_from_symbol_queue(order)
                return True
            from decimal import ROUND_FLOOR
            vol_cap = (Decimal(bar.volume) * self._max_participation_pct).to_integral_value(
                rounding=ROUND_FLOOR
            )
            if vol_cap < _ONE:
                logger.warning(
                    "Cancelling %s order %s for %s: bar volume (%s) too thin for participation cap %.1f%%",
                    order.side, order.order_id, order.symbol, bar.volume,
                    float(self._max_participation_pct) * 100,
                )
                order.mark_cancelled()
                self._portfolio.cancel_pending_order(order.order_id)
                self._remove_from_symbol_queue(order)
                return True
            if fill_qty > vol_cap:
                logger.debug(
                    "Volume participation cap: %s %s qty %s→%s (bar vol=%s, max=%.1f%%)",
                    order.symbol, order.side, fill_qty, vol_cap,
                    bar.volume, float(self._max_participation_pct) * 100,
                )
                fill_qty = vol_cap

        commission = self._commission(fill_qty, fill_price)

        if order.side == Side.BUY and _fill_cash_required(fill_qty, fill_price, commission) > self._portfolio.cash:
            fill_qty = self._max_affordable_qty_at_price(fill_price)
            if fill_qty <= _ZERO:
                logger.warning(
                    "Cancelling BUY order %s for %s: insufficient cash (%s) at fill price %s",
                    order.order_id,
                    order.symbol,
                    self._portfolio.cash,
                    fill_price,
                )
                order.mark_cancelled()
                self._portfolio.cancel_pending_order(order.order_id)
                self._remove_from_symbol_queue(order)
                return True
            commission = self._commission(fill_qty, fill_price)
            logger.debug(
                "Partial fill: BUY order %s for %s resized %s→%s shares at %s (gap-up slippage)",
                order.order_id,
                order.symbol,
                order.approved_qty,
                fill_qty,
                fill_price,
            )

        order.mark_filled()
        self._bus.publish(
            FillEvent(
                timestamp=bar.timestamp,
                order_id=order.order_id,
                strategy_id=order.origin_event.origin_intent.strategy_id,
                symbol=order.symbol,
                side=order.side,
                filled_qty=fill_qty,
                fill_price=fill_price,
                commission=commission,
                fill_bar=bar,
            )
        )
        self._remove_from_symbol_queue(order)
        return True

    def _fill_price(
        self,
        order: QueuedOrder,
        bar: BarEvent,
        use_close: bool,
    ) -> Decimal | None:
        base_price = bar.close if use_close else bar.open

        if order.order_type == IntentType.LIMIT:
            if order.limit_price is None:
                logger.error("Limit order missing limit price: %s", order.order_id)
                return None
            if order.side == Side.BUY and bar.low > order.limit_price:
                return None
            if order.side == Side.SELL and bar.high < order.limit_price:
                return None
            base_price = min(base_price, order.limit_price) if order.side == Side.BUY else max(
                base_price,
                order.limit_price,
            )

        if self._slippage_model == "zero":
            fill_price = base_price
        elif order.side == Side.BUY:
            fill_price = base_price * (_ONE + self._slippage_pct)
        else:
            fill_price = base_price * (_ONE - self._slippage_pct)

        # Re-clamp to limit after slippage — limit price is a hard guarantee
        if order.order_type == IntentType.LIMIT and order.limit_price is not None:
            if order.side == Side.BUY:
                fill_price = min(fill_price, order.limit_price)
            else:
                fill_price = max(fill_price, order.limit_price)

        return fill_price

    def _required_cash(self, quantity: Decimal, price: Decimal) -> Decimal:
        return quantity * price + self._commission(quantity, price)

    def _reservation_price(self, price: Decimal, side: Side) -> Decimal:
        if side == Side.SELL or self._slippage_model == "zero":
            return price
        return price * (_ONE + self._slippage_pct)

    def _commission(self, quantity: Decimal, fill_price: Decimal) -> Decimal:
        if self._commission_model == "zero":
            return _ZERO
        if self._commission_model == "flat":
            return self._min_commission
        return max(self._min_commission, quantity * self._commission_per_share)

    def _max_affordable_qty_at_price(self, price: Decimal) -> Decimal:
        """Largest whole-share quantity affordable at ``price`` given current cash."""
        from decimal import ROUND_FLOOR

        def _floor(v: Decimal) -> Decimal:
            return v.to_integral_value(rounding=ROUND_FLOOR)

        cash = self._portfolio.cash
        if cash <= _ZERO or price <= _ZERO:
            return _ZERO

        if self._commission_model == "zero":
            return _floor(cash / price)

        if self._commission_model == "flat":
            net = cash - self._min_commission
            return _floor(net / price) if net > _ZERO else _ZERO

        # per_share: commission = max(qty * cps, min_commission)
        # Two cases: (1) min_commission binds, (2) per-share rate binds.
        if cash <= self._min_commission:
            return _ZERO
        qty_case1 = _floor((cash - self._min_commission) / price)
        if qty_case1 > _ZERO and qty_case1 * self._commission_per_share >= self._min_commission:
            # Per-share rate applies; solve: qty * price + qty * cps <= cash
            return _floor(cash / (price + self._commission_per_share))
        return qty_case1

    def _remove_from_symbol_queue(self, order: QueuedOrder) -> None:
        orders = self._pending.get(order.symbol)
        if not orders:
            return
        self._pending[order.symbol] = [
            pending for pending in orders if pending.order_id != order.order_id
        ]
        if not self._pending[order.symbol]:
            self._pending.pop(order.symbol, None)


def _decimal(value: Decimal | str | float | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _fill_cash_required(
    quantity: Decimal,
    fill_price: Decimal,
    commission: Decimal,
) -> Decimal:
    return quantity * fill_price + commission

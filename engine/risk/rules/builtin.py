from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from engine.events.types import ApprovedOrderEvent, OrderIntentEvent, Side
from engine.portfolio.position import PendingOrder
from engine.portfolio.state import PortfolioSnapshot
from engine.risk.rules.base import AbstractRule, RuleChain, RuleResult

_ZERO = Decimal("0")
_ONE = Decimal("1")


class MaxDrawdownRule(AbstractRule):
    name = "MaxDrawdownRule"

    def __init__(self, max_drawdown_pct: Decimal | str | float = Decimal("0.15")) -> None:
        self.max_drawdown_pct = _decimal(max_drawdown_pct)
        if self.max_drawdown_pct < _ZERO:
            raise ValueError("max_drawdown_pct must be non-negative")

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        if intent.side == Side.BUY and snapshot.drawdown_pct > self.max_drawdown_pct:
            return RuleResult.reject(
                intent.quantity,
                (
                    f"drawdown {snapshot.drawdown_pct:.2%} exceeds "
                    f"limit {self.max_drawdown_pct:.2%}"
                ),
            )
        return RuleResult.pass_(intent.quantity, "drawdown within limit")


class DailyOrderLimitRule(AbstractRule):
    name = "DailyOrderLimitRule"

    def __init__(self, max_orders_per_day: int = 10) -> None:
        if max_orders_per_day <= 0:
            raise ValueError("max_orders_per_day must be positive")
        self.max_orders_per_day = max_orders_per_day
        self._current_day: date | None = None
        self._approved_count = 0

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        day = intent.signal_bar.timestamp.date()
        if day != self._current_day:
            self._current_day = day
            self._approved_count = 0

        if self._approved_count >= self.max_orders_per_day:
            return RuleResult.reject(
                intent.quantity,
                f"daily approved order limit {self.max_orders_per_day} reached",
            )
        return RuleResult.pass_(intent.quantity, "daily order count within limit")

    def record_approval(
        self,
        intent: OrderIntentEvent,
        approval: ApprovedOrderEvent,
    ) -> None:
        day = intent.signal_bar.timestamp.date()
        if day != self._current_day:
            self._current_day = day
            self._approved_count = 0
        self._approved_count += 1


class ShortSellingRule(AbstractRule):
    name = "ShortSellingRule"

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        if intent.side == Side.BUY:
            return RuleResult.pass_(intent.quantity, "buy order cannot create a short")

        held_qty = _position_qty(snapshot, intent.symbol)
        pending_sell_qty = sum(
            order.quantity
            for order in snapshot.open_orders
            if order.symbol == intent.symbol and order.side == Side.SELL
        )
        sellable_qty = held_qty - pending_sell_qty
        if intent.quantity > sellable_qty:
            if sellable_qty <= _ZERO:
                return RuleResult.reject(
                    intent.quantity,
                    "no long position available to sell",
                )
            return RuleResult.resize(
                sellable_qty,
                f"sell quantity capped at available long position {sellable_qty}",
            )
        return RuleResult.pass_(intent.quantity, "sell quantity covered by long position")


class PositionSizeRule(AbstractRule):
    name = "PositionSizeRule"

    def __init__(self, max_position_pct: Decimal | str | float = Decimal("0.20")) -> None:
        self.max_position_pct = _decimal(max_position_pct)
        if self.max_position_pct <= _ZERO or self.max_position_pct > _ONE:
            raise ValueError("max_position_pct must be in (0.0, 1.0]")

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        if intent.side == Side.SELL:
            return RuleResult.pass_(intent.quantity, "position cap does not constrain sells")
        _require_positive_price(current_price)

        current_notional = _position_market_value(snapshot, intent.symbol)
        pending_notional = _pending_buy_notional(snapshot.open_orders, intent.symbol, current_price)
        proposed_notional = current_notional + pending_notional + intent.quantity * current_price
        max_notional = snapshot.equity * self.max_position_pct

        if proposed_notional <= max_notional:
            return RuleResult.pass_(intent.quantity, "position size within limit")

        allowed_additional = max_notional - current_notional - pending_notional
        if allowed_additional <= _ZERO:
            return RuleResult.reject(
                intent.quantity,
                "position is already at or above maximum allocation",
            )

        resized_qty = _floor_decimal(allowed_additional / current_price)
        if resized_qty <= _ZERO:
            return RuleResult.reject(
                intent.quantity,
                "position cap leaves no whole shares available",
            )
        return RuleResult.resize(
            resized_qty,
            f"position cap resized quantity from {intent.quantity} to {resized_qty}",
        )


class CashSolvencyRule(AbstractRule):
    name = "CashSolvencyRule"

    def __init__(
        self,
        commission_rate: Decimal | str | float | None = None,
        cash_buffer_pct: Decimal | str | float = Decimal("0"),
        slippage_pct: Decimal | str | float = Decimal("0.0005"),
        commission_model: str = "per_share",
        commission_per_share: Decimal | str | float = Decimal("0.005"),
        min_commission: Decimal | str | float = Decimal("1.00"),
    ) -> None:
        self.commission_rate = (
            _decimal(commission_rate) if commission_rate is not None else None
        )
        if self.commission_rate is not None and self.commission_rate < _ZERO:
            raise ValueError("commission_rate must be non-negative")
        self.cash_buffer_pct = _decimal(cash_buffer_pct)
        if not (_ZERO <= self.cash_buffer_pct < _ONE):
            raise ValueError("cash_buffer_pct must be in [0.0, 1.0)")
        self.slippage_pct = _decimal(slippage_pct)
        if self.slippage_pct < _ZERO:
            raise ValueError("slippage_pct must be non-negative")
        self.commission_model = commission_model
        if self.commission_model not in {"per_share", "flat", "zero"}:
            raise ValueError("unknown commission model")
        self.commission_per_share = _decimal(commission_per_share)
        if self.commission_per_share < _ZERO:
            raise ValueError("commission_per_share must be non-negative")
        self.min_commission = _decimal(min_commission)
        if self.min_commission < _ZERO:
            raise ValueError("min_commission must be non-negative")

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        if intent.side == Side.SELL:
            return RuleResult.pass_(intent.quantity, "sell order does not consume cash")
        _require_positive_price(current_price)

        gross_cash = snapshot.cash - _reserved_buy_cash(snapshot.open_orders)
        available_cash = gross_cash * (_ONE - self.cash_buffer_pct)
        reservation_price = current_price * (_ONE + self.slippage_pct)
        required_cash = self._required_cash(intent.quantity, reservation_price)
        if required_cash <= available_cash:
            return RuleResult.pass_(intent.quantity, "cash covers order")

        affordable_qty = self._max_affordable_qty(available_cash, reservation_price)
        if affordable_qty <= _ZERO:
            return RuleResult.reject(
                intent.quantity,
                "available cash cannot cover one share plus commission",
            )
        return RuleResult.resize(
            affordable_qty,
            f"cash resized quantity from {intent.quantity} to {affordable_qty}",
        )

    def _required_cash(self, quantity: Decimal, price: Decimal) -> Decimal:
        if self.commission_rate is not None:
            return quantity * price * (_ONE + self.commission_rate)
        return quantity * price + self._commission(quantity)

    def _commission(self, quantity: Decimal) -> Decimal:
        if self.commission_model == "zero":
            return _ZERO
        if self.commission_model == "flat":
            return self.min_commission
        return max(self.min_commission, quantity * self.commission_per_share)

    def _max_affordable_qty(self, available_cash: Decimal, price: Decimal) -> Decimal:
        if available_cash <= _ZERO:
            return _ZERO
        if self.commission_rate is not None:
            return _floor_decimal(
                available_cash / (price * (_ONE + self.commission_rate))
            )
        if self.commission_model == "zero":
            return _floor_decimal(available_cash / price)
        if self.commission_model == "flat":
            net_cash = available_cash - self.min_commission
            return _floor_decimal(net_cash / price) if net_cash > _ZERO else _ZERO

        candidates: list[Decimal] = []
        if available_cash > self.min_commission:
            candidates.append(_floor_decimal((available_cash - self.min_commission) / price))
        if self.commission_per_share > _ZERO:
            candidates.append(_floor_decimal(available_cash / (price + self.commission_per_share)))
        else:
            candidates.append(_floor_decimal((available_cash - self.min_commission) / price))

        affordable = _ZERO
        for quantity in candidates:
            if (
                quantity > affordable
                and self._required_cash(quantity, price) <= available_cash
            ):
                affordable = quantity
        return affordable


class ConcentrationRule(AbstractRule):
    name = "ConcentrationRule"

    def __init__(self, max_open_positions: int = 10) -> None:
        if max_open_positions <= 0:
            raise ValueError("max_open_positions must be positive")
        self.max_open_positions = max_open_positions

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        if intent.side == Side.SELL:
            return RuleResult.pass_(intent.quantity, "open-position cap does not constrain sells")

        open_symbols = {
            symbol
            for symbol, position in snapshot.positions.items()
            if position.quantity > _ZERO
        }
        open_symbols.update(
            order.symbol
            for order in snapshot.open_orders
            if order.side == Side.BUY and order.quantity > _ZERO
        )

        if intent.symbol not in open_symbols and len(open_symbols) >= self.max_open_positions:
            return RuleResult.reject(
                intent.quantity,
                f"max open positions {self.max_open_positions} reached",
            )
        return RuleResult.pass_(intent.quantity, "open-position count within limit")


class SectorExposureRule(AbstractRule):
    """
    Caps total notional exposure to any single GICS sector.

    Reads sector membership from engine.data.universe.SECTOR_MAP.
    Symbols not in the map are treated as their own sector ("Unknown"),
    so they are effectively unconstrained by this rule.
    """

    name = "SectorExposureRule"

    def __init__(self, max_sector_pct: Decimal | str | float = Decimal("0.30")) -> None:
        self.max_sector_pct = _decimal(max_sector_pct)
        if self.max_sector_pct <= _ZERO or self.max_sector_pct > _ONE:
            raise ValueError("max_sector_pct must be in (0.0, 1.0]")
        from engine.data.universe import SECTOR_MAP
        self._sector_map: dict[str, str] = SECTOR_MAP

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        if intent.side == Side.SELL:
            return RuleResult.pass_(intent.quantity, "sector cap does not constrain sells")
        _require_positive_price(current_price)

        sector = self._sector_map.get(intent.symbol.upper(), "Unknown")
        if sector == "Unknown":
            return RuleResult.pass_(intent.quantity, f"symbol {intent.symbol!r} has no sector mapping")

        # Current sector exposure from open positions
        sector_value = sum(
            pos.market_value
            for sym, pos in snapshot.positions.items()
            if self._sector_map.get(sym.upper(), "Unknown") == sector
        )
        # Pending exposure from queued BUY orders
        pending_sector = sum(
            order.reserved_cash
            for order in snapshot.open_orders
            if order.side == Side.BUY
            and self._sector_map.get(order.symbol.upper(), "Unknown") == sector
        )

        proposed = sector_value + pending_sector + intent.quantity * current_price
        max_value = snapshot.equity * self.max_sector_pct

        if proposed <= max_value:
            return RuleResult.pass_(
                intent.quantity,
                f"sector {sector!r} exposure within {self.max_sector_pct:.0%} limit",
            )

        allowed = max_value - sector_value - pending_sector
        if allowed <= _ZERO:
            return RuleResult.reject(
                intent.quantity,
                f"sector {sector!r} already at {self.max_sector_pct:.0%} cap",
            )

        resized = _floor_decimal(allowed / current_price)
        if resized <= _ZERO:
            return RuleResult.reject(
                intent.quantity,
                f"sector cap leaves no whole shares available for {sector!r}",
            )
        return RuleResult.resize(
            resized,
            f"sector {sector!r} cap resized qty {intent.quantity} → {resized}",
        )


RULE_REGISTRY: dict[str, type[AbstractRule]] = {
    MaxDrawdownRule.name: MaxDrawdownRule,
    DailyOrderLimitRule.name: DailyOrderLimitRule,
    ShortSellingRule.name: ShortSellingRule,
    PositionSizeRule.name: PositionSizeRule,
    CashSolvencyRule.name: CashSolvencyRule,
    ConcentrationRule.name: ConcentrationRule,
    SectorExposureRule.name: SectorExposureRule,
}


def create_rule(name: str, **params: Any) -> AbstractRule:
    rule_cls = RULE_REGISTRY.get(name)
    if rule_cls is None:
        known = ", ".join(sorted(RULE_REGISTRY))
        raise ValueError(f"unknown risk rule {name!r}; known rules: {known}")
    return rule_cls(**params)


def build_rule_chain(entries: list[dict[str, Any]]) -> RuleChain:
    rules: list[AbstractRule] = []
    for entry in entries:
        data = dict(entry)
        rule_name = data.pop("rule", None)
        if not rule_name:
            raise ValueError("risk rule config entries require a 'rule' key")
        rules.append(create_rule(str(rule_name), **data))
    return RuleChain(rules)


def _decimal(value: Decimal | str | float | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _floor_decimal(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


def _require_positive_price(price: Decimal) -> None:
    if price <= _ZERO:
        raise ValueError("current_price must be positive")


def _position_qty(snapshot: PortfolioSnapshot, symbol: str) -> Decimal:
    position = snapshot.positions.get(symbol)
    return position.quantity if position is not None else _ZERO


def _position_market_value(snapshot: PortfolioSnapshot, symbol: str) -> Decimal:
    position = snapshot.positions.get(symbol)
    return position.market_value if position is not None else _ZERO


def _pending_buy_notional(
    open_orders: tuple[PendingOrder, ...],
    symbol: str,
    current_price: Decimal,
) -> Decimal:
    return sum(
        (
            order.quantity * current_price
            for order in open_orders
            if order.symbol == symbol and order.side == Side.BUY
        ),
        _ZERO,
    )


def _reserved_buy_cash(open_orders: tuple[PendingOrder, ...]) -> Decimal:
    return sum(
        (
            order.reserved_cash
            for order in open_orders
            if order.side == Side.BUY
        ),
        _ZERO,
    )


def _required_cash(quantity: Decimal, price: Decimal, commission_rate: Decimal) -> Decimal:
    return quantity * price * (_ONE + commission_rate)

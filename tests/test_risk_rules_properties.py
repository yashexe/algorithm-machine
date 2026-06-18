from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from engine.events.types import BarEvent, BarType, IntentType, OrderIntentEvent, Side, Verdict
from engine.portfolio.state import PortfolioSnapshot
from engine.risk.rules.base import RuleChain
from engine.risk.rules.builtin import CashSolvencyRule, MaxDrawdownRule, PositionSizeRule

_BAR_TIME = datetime(2024, 1, 2, tzinfo=timezone.utc)
_ZERO = Decimal("0")
_ONE = Decimal("1")


cash_values = st.decimals(
    min_value=Decimal("1.00"),
    max_value=Decimal("1000000.00"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
prices = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000.00"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
quantities = st.integers(min_value=1, max_value=100000).map(Decimal)
signed_quantities = st.integers(min_value=-100000, max_value=100000).map(Decimal)
percentages = st.decimals(
    min_value=Decimal("0.0000"),
    max_value=Decimal("1.0000"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)
positive_percentages = st.decimals(
    min_value=Decimal("0.0001"),
    max_value=Decimal("1.0000"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)
commission_rates = st.decimals(
    min_value=Decimal("0.0000"),
    max_value=Decimal("0.1000"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)


def _bar(price: Decimal) -> BarEvent:
    return BarEvent(
        timestamp=_BAR_TIME,
        symbol="SPY",
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        bar_type=BarType.DAILY,
        source="test",
    )


def _intent(quantity: Decimal, price: Decimal, side: Side = Side.BUY) -> OrderIntentEvent:
    bar = _bar(price)
    return OrderIntentEvent(
        timestamp=bar.timestamp,
        strategy_id="property_test",
        symbol=bar.symbol,
        side=side,
        intent_type=IntentType.MARKET,
        quantity=quantity,
        signal_bar=bar,
    )


def _snapshot(
    equity: Decimal,
    cash: Decimal,
    drawdown_pct: Decimal = _ZERO,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=_BAR_TIME,
        equity=equity,
        cash=cash,
        peak_equity=max(equity, cash),
        drawdown_pct=drawdown_pct,
        positions={},
        open_orders=(),
    )


@settings(max_examples=100)
@given(equity=cash_values, max_pct=positive_percentages, price=prices, quantity=quantities)
def test_position_size_rule_never_approves_above_cap(
    equity: Decimal,
    max_pct: Decimal,
    price: Decimal,
    quantity: Decimal,
) -> None:
    rule = PositionSizeRule(max_position_pct=max_pct)
    result = rule.evaluate(_intent(quantity, price), _snapshot(equity, equity), price)

    if result.verdict != Verdict.REJECT:
        assert result.output_qty * price <= equity * max_pct


@settings(max_examples=100)
@given(cash=cash_values, price=prices, quantity=quantities, fee=commission_rates)
def test_cash_solvency_rule_never_approves_above_cash(
    cash: Decimal,
    price: Decimal,
    quantity: Decimal,
    fee: Decimal,
) -> None:
    rule = CashSolvencyRule(commission_rate=fee)
    result = rule.evaluate(_intent(quantity, price), _snapshot(cash, cash), price)

    if result.verdict != Verdict.REJECT:
        required_cash = result.output_qty * price * (_ONE + fee)
        assert required_cash <= cash


@settings(max_examples=100)
@given(threshold=percentages, drawdown=percentages, price=prices, quantity=quantities)
def test_max_drawdown_rule_rejects_buys_above_threshold(
    threshold: Decimal,
    drawdown: Decimal,
    price: Decimal,
    quantity: Decimal,
) -> None:
    assume(drawdown > threshold)

    rule = MaxDrawdownRule(max_drawdown_pct=threshold)
    result = rule.evaluate(
        _intent(quantity, price),
        _snapshot(Decimal("100000.00"), Decimal("100000.00"), drawdown),
        price,
    )

    assert result.verdict == Verdict.REJECT


@settings(max_examples=100)
@given(
    equity=cash_values,
    cash=cash_values,
    max_pct=positive_percentages,
    price=prices,
    quantity=quantities,
    fee=commission_rates,
)
def test_resized_rule_chain_is_idempotent(
    equity: Decimal,
    cash: Decimal,
    max_pct: Decimal,
    price: Decimal,
    quantity: Decimal,
    fee: Decimal,
) -> None:
    chain = RuleChain(
        [
            PositionSizeRule(max_position_pct=max_pct),
            CashSolvencyRule(commission_rate=fee),
        ]
    )
    snapshot = _snapshot(equity, cash)
    first = chain.evaluate(_intent(quantity, price), snapshot, price)
    assume(first.approved)

    second = chain.evaluate(_intent(first.output_qty, price), snapshot, price)

    assert second.approved
    assert second.output_qty == first.output_qty


@settings(max_examples=100)
@given(
    equity=cash_values,
    cash=cash_values,
    max_pct=positive_percentages,
    price=prices,
    quantity=signed_quantities,
    fee=commission_rates,
)
def test_rule_chain_never_outputs_negative_quantities(
    equity: Decimal,
    cash: Decimal,
    max_pct: Decimal,
    price: Decimal,
    quantity: Decimal,
    fee: Decimal,
) -> None:
    chain = RuleChain(
        [
            CashSolvencyRule(commission_rate=fee),
            PositionSizeRule(max_position_pct=max_pct),
        ]
    )
    result = chain.evaluate(_intent(quantity, price), _snapshot(equity, cash), price)

    assert result.output_qty >= _ZERO
    assert all(trace.output_qty >= _ZERO for trace in result.trace)

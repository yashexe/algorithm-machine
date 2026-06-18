from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from engine.events.types import ApprovedOrderEvent, OrderIntentEvent, RuleTrace, Verdict
from engine.portfolio.state import PortfolioSnapshot

_ZERO = Decimal("0")


@dataclass(frozen=True, kw_only=True)
class RuleResult:
    """
    Result returned by one risk rule.

    PASS leaves quantity unchanged, RESIZE reduces it, and REJECT terminates
    the chain. Concrete rules should use the class constructors below so the
    result shape stays consistent with docs/RISK_ENGINE.md.
    """

    verdict: Verdict
    output_qty: Decimal
    reason: str

    def __post_init__(self) -> None:
        if self.output_qty < _ZERO:
            raise ValueError("risk rules must never produce negative quantities")
        if not self.reason:
            raise ValueError("risk rule results require a human-readable reason")

    @classmethod
    def pass_(cls, quantity: Decimal, reason: str) -> "RuleResult":
        return cls(verdict=Verdict.PASS, output_qty=quantity, reason=reason)

    @classmethod
    def resize(cls, quantity: Decimal, reason: str) -> "RuleResult":
        if quantity <= _ZERO:
            raise ValueError("resize quantity must be positive; reject instead")
        return cls(verdict=Verdict.RESIZE, output_qty=quantity, reason=reason)

    @classmethod
    def reject(cls, quantity: Decimal, reason: str) -> "RuleResult":
        return cls(verdict=Verdict.REJECT, output_qty=quantity, reason=reason)


@dataclass(frozen=True, kw_only=True)
class ChainResult:
    """Final result of evaluating an ordered rule chain."""

    verdict: Verdict
    output_qty: Decimal
    trace: tuple[RuleTrace, ...]

    @property
    def approved(self) -> bool:
        return self.verdict in {Verdict.PASS, Verdict.RESIZE} and self.output_qty > _ZERO


class ApprovalObserver(Protocol):
    """Optional hook implemented by stateful rules such as DailyOrderLimitRule."""

    def record_approval(
        self,
        intent: OrderIntentEvent,
        approval: ApprovedOrderEvent,
    ) -> None:
        ...


class AbstractRule(ABC):
    """
    Base interface for all risk rules.

    Rules receive an immutable portfolio snapshot and the current working
    quantity. They never mutate portfolio state and never construct approval
    or rejection events themselves.
    """

    name: str

    @abstractmethod
    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> RuleResult:
        """Evaluate an intent at the current working quantity."""


class RuleChain:
    """Sequential risk-rule evaluator with full RuleTrace output."""

    def __init__(self, rules: list[AbstractRule]) -> None:
        if not rules:
            raise ValueError("RuleChain requires at least one rule")
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[AbstractRule, ...]:
        return self._rules

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
        current_price: Decimal,
    ) -> ChainResult:
        if intent.quantity <= _ZERO:
            trace = (
                RuleTrace(
                    rule_name="RuleChain",
                    verdict=Verdict.REJECT,
                    original_qty=intent.quantity,
                    output_qty=_ZERO,
                    reason="intent quantity must be positive",
                ),
            )
            return ChainResult(
                verdict=Verdict.REJECT,
                output_qty=_ZERO,
                trace=trace,
            )

        working_qty = intent.quantity
        final_verdict = Verdict.PASS
        trace: list[RuleTrace] = []

        for rule in self._rules:
            original_qty = working_qty
            working_intent = _intent_with_quantity(intent, working_qty)
            result = rule.evaluate(working_intent, snapshot, current_price)

            if result.verdict == Verdict.PASS and result.output_qty != original_qty:
                raise ValueError(f"{rule.name} returned PASS with changed quantity")
            if result.verdict == Verdict.RESIZE and result.output_qty >= original_qty:
                raise ValueError(f"{rule.name} returned RESIZE without reducing quantity")
            if result.verdict == Verdict.REJECT and result.output_qty != original_qty:
                raise ValueError(f"{rule.name} returned REJECT with changed quantity")

            trace.append(
                RuleTrace(
                    rule_name=rule.name,
                    verdict=result.verdict,
                    original_qty=original_qty,
                    output_qty=result.output_qty,
                    reason=result.reason,
                )
            )

            if result.verdict == Verdict.REJECT:
                return ChainResult(
                    verdict=Verdict.REJECT,
                    output_qty=original_qty,
                    trace=tuple(trace),
                )

            if result.verdict == Verdict.RESIZE:
                final_verdict = Verdict.RESIZE
            working_qty = result.output_qty

        return ChainResult(
            verdict=final_verdict,
            output_qty=working_qty,
            trace=tuple(trace),
        )

    def record_approval(
        self,
        intent: OrderIntentEvent,
        approval: ApprovedOrderEvent,
    ) -> None:
        for rule in self._rules:
            recorder = getattr(rule, "record_approval", None)
            if recorder is not None:
                recorder(intent, approval)


def _intent_with_quantity(
    intent: OrderIntentEvent,
    quantity: Decimal,
) -> OrderIntentEvent:
    if quantity == intent.quantity:
        return intent
    return replace(intent, quantity=quantity)

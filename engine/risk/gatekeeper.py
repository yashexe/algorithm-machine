from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from engine.events.bus import EventBus
from engine.events.types import (
    _APPROVAL_TOKEN,
    ApprovedOrderEvent,
    BaseEvent,
    OrderIntentEvent,
    RejectedOrderEvent,
    RuleTrace,
    Verdict,
)
from engine.portfolio.state import PortfolioSnapshot
from engine.risk.rules.base import ChainResult, RuleChain

_ZERO = Decimal("0")


class RiskGatekeeper:
    """
    Single authority that can approve, resize, or reject order intents.

    The gatekeeper is the only module that imports the approval token used by
    ApprovedOrderEvent. Strategies can propose; only this class can construct
    an approval event acceptable to the execution layer.
    """

    def __init__(
        self,
        bus: EventBus,
        snapshot_provider: Callable[[], PortfolioSnapshot],
        rule_chain: RuleChain,
        risk_log_path: str | Path | None = None,
    ) -> None:
        self._bus = bus
        self._snapshot_provider = snapshot_provider
        self._rule_chain = rule_chain
        self._risk_log_path = Path(risk_log_path) if risk_log_path else None
        if self._risk_log_path is not None:
            self._risk_log_path.parent.mkdir(parents=True, exist_ok=True)

    def on_order_intent(self, event: OrderIntentEvent) -> None:
        """
        EventBus handler for strategy intents.

        Snapshotting happens once at the start of evaluation, matching the
        purity requirement in docs/RISK_ENGINE.md.
        """
        snapshot = self._snapshot_provider()
        verdict_event = self.evaluate(event, snapshot)
        self._write_risk_log(event, verdict_event, snapshot)
        self._bus.publish(verdict_event)

    def evaluate(
        self,
        intent: OrderIntentEvent,
        snapshot: PortfolioSnapshot,
    ) -> ApprovedOrderEvent | RejectedOrderEvent:
        if intent.quantity <= _ZERO:
            trace = (
                RuleTrace(
                    rule_name="RiskGatekeeper",
                    verdict=Verdict.REJECT,
                    original_qty=intent.quantity,
                    output_qty=intent.quantity,
                    reason="intent quantity must be positive",
                ),
            )
            return self._build_rejection(intent, "intent quantity must be positive", trace)

        result = self._rule_chain.evaluate(
            intent=intent,
            snapshot=snapshot,
            current_price=intent.signal_bar.close,
        )

        if not result.approved:
            return self._build_rejection(
                intent,
                _rejection_reason(result),
                result.trace,
            )

        approval = self._build_approval(
            intent=intent,
            result=result,
        )
        self._rule_chain.record_approval(intent, approval)
        return approval

    def _build_approval(
        self,
        intent: OrderIntentEvent,
        result: ChainResult,
    ) -> ApprovedOrderEvent:
        return ApprovedOrderEvent(
            timestamp=intent.timestamp,
            origin_intent=intent,
            approved_qty=result.output_qty,
            approved_side=intent.side,
            approved_type=intent.intent_type,
            rule_trace=result.trace,
            notes=_approval_notes(intent.quantity, result),
            _token=_APPROVAL_TOKEN,
        )

    def _build_rejection(
        self,
        intent: OrderIntentEvent,
        reason: str,
        trace: tuple[RuleTrace, ...],
    ) -> RejectedOrderEvent:
        return RejectedOrderEvent(
            timestamp=intent.timestamp,
            origin_intent=intent,
            rejection_reason=reason,
            rule_trace=trace,
        )

    def _write_risk_log(
        self,
        intent: OrderIntentEvent,
        verdict_event: ApprovedOrderEvent | RejectedOrderEvent,
        snapshot: PortfolioSnapshot,
    ) -> None:
        if self._risk_log_path is None:
            return

        is_approval = isinstance(verdict_event, ApprovedOrderEvent)
        record = {
            "intent_id": intent.event_id,
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "intent_qty": intent.quantity,
            "verdict": "approved" if is_approval else "rejected",
            "approved_qty": verdict_event.approved_qty if is_approval else None,
            "rejection_reason": None if is_approval else verdict_event.rejection_reason,
            "full_rule_trace": verdict_event.rule_trace,
            "snapshot_equity": snapshot.equity,
            "snapshot_cash": snapshot.cash,
            "snapshot_drawdown_pct": snapshot.drawdown_pct,
            "timestamp": datetime.now(timezone.utc),
        }
        with self._risk_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_jsonable(record), sort_keys=True) + "\n")


def _approval_notes(original_qty: Decimal, result: ChainResult) -> str:
    if result.verdict == Verdict.RESIZE or result.output_qty != original_qty:
        reasons = [
            trace.reason
            for trace in result.trace
            if trace.verdict == Verdict.RESIZE
        ]
        return "resized: " + "; ".join(reasons)
    return "approved"


def _rejection_reason(result: ChainResult) -> str:
    for trace in reversed(result.trace):
        if trace.verdict == Verdict.REJECT:
            return trace.reason
    if result.output_qty <= _ZERO:
        return "risk rules produced zero approved quantity"
    return "risk rule rejected intent"


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseEvent):
        return _jsonable(asdict(value))
    if is_dataclass(value):
        return {
            key: _jsonable(item)
            for key, item in asdict(value).items()
            if not key.startswith("_")
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value

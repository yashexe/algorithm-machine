"""
engine.events — typed event system for the trading engine.

Import everything from here, not from the submodules directly, so
internal refactoring doesn't break call sites.

    from engine.events import EventBus, BarEvent, OrderIntentEvent, Side, ...

The approval sentinel (_APPROVAL_TOKEN) is intentionally not re-exported
here. Only engine.risk.gatekeeper should import it, directly from
engine.events.types.
"""

from engine.events.bus import EventBus
from engine.events.types import (
    # Enums
    BarType,
    IntentType,
    Side,
    Verdict,
    # Supporting types
    PositionSnapshot,
    RuleTrace,
    # Abstract / intermediate
    BaseEvent,
    ExecutionEvent,
    MarketEvent,
    PortfolioEvent,
    RiskEvent,
    SignalEvent,
    # Concrete events
    ApprovedOrderEvent,
    BarEvent,
    FillEvent,
    OrderIntentEvent,
    OrderSubmittedEvent,
    PortfolioSnapshotEvent,
    RejectedOrderEvent,
)

__all__ = [
    "EventBus",
    "BarType",
    "IntentType",
    "Side",
    "Verdict",
    "PositionSnapshot",
    "RuleTrace",
    "BaseEvent",
    "ExecutionEvent",
    "MarketEvent",
    "PortfolioEvent",
    "RiskEvent",
    "SignalEvent",
    "ApprovedOrderEvent",
    "BarEvent",
    "FillEvent",
    "OrderIntentEvent",
    "OrderSubmittedEvent",
    "PortfolioSnapshotEvent",
    "RejectedOrderEvent",
]

"""
All event and supporting types for the trading engine.

Events are frozen dataclasses — immutable once constructed. All timestamps
are UTC. All collections inside events are tuples, not lists, to preserve
true immutability.

The approval sentinel (_APPROVAL_TOKEN) is the runtime enforcement of the
core invariant: ApprovedOrderEvent can only be instantiated by
RiskGatekeeper._build_approval(). Import _APPROVAL_TOKEN only from
engine.risk.gatekeeper — do not reference it anywhere else.
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final
from uuid import UUID


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BarType(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class IntentType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class Verdict(Enum):
    PASS = "pass"
    RESIZE = "resize"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# Supporting dataclasses (not events — used as fields inside events)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class RuleTrace:
    """Record of a single rule's evaluation within a risk engine pass."""
    rule_name: str
    verdict: Verdict
    original_qty: Decimal
    output_qty: Decimal
    reason: str


@dataclass(frozen=True, kw_only=True)
class PositionSnapshot:
    """Immutable point-in-time view of a single position, embedded in PortfolioSnapshotEvent."""
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    last_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


# ---------------------------------------------------------------------------
# Approval sentinel
#
# _APPROVAL_TOKEN is the only instance of _ApprovalToken ever created, but
# possession of the token alone is not authority. ApprovedOrderEvent also checks
# that construction came through RiskGatekeeper._build_approval().
# ---------------------------------------------------------------------------

class _ApprovalToken:
    """Private sentinel type. Do not instantiate outside this module."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "<_ApprovalToken>"


_APPROVAL_TOKEN: Final[_ApprovalToken] = _ApprovalToken()
_APPROVAL_FACTORY_MODULE: Final[str] = "engine.risk.gatekeeper"
_APPROVAL_FACTORY_NAME: Final[str] = "_build_approval"


# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, kw_only=True)
class BaseEvent:
    """
    Root of the event hierarchy. All fields have defaults so subclasses can
    declare required fields freely without Python's no-default-after-default
    restriction (resolved by kw_only=True throughout the hierarchy).
    """
    schema_version: ClassVar[int] = 1
    timestamp: datetime = field(default_factory=_utcnow)
    event_id: UUID = field(default_factory=uuid.uuid4)


# ---------------------------------------------------------------------------
# Intermediate event categories — type markers for event-bus routing
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class MarketEvent(BaseEvent):
    """Events that carry market data."""


@dataclass(frozen=True, kw_only=True)
class SignalEvent(BaseEvent):
    """Events emitted by strategies."""


@dataclass(frozen=True, kw_only=True)
class RiskEvent(BaseEvent):
    """Events emitted by the risk engine."""


@dataclass(frozen=True, kw_only=True)
class ExecutionEvent(BaseEvent):
    """Events emitted by the broker / execution layer."""


@dataclass(frozen=True, kw_only=True)
class PortfolioEvent(BaseEvent):
    """Events emitted by portfolio state."""


# ---------------------------------------------------------------------------
# Concrete events
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class BarEvent(MarketEvent):
    """One complete OHLCV bar for one symbol."""
    schema_version: ClassVar[int] = 1

    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    bar_type: BarType
    source: str
    is_complete: bool = True


@dataclass(frozen=True, kw_only=True)
class OrderIntentEvent(SignalEvent):
    """
    A strategy's proposal to trade. An expression of desire, not authority.
    The risk engine may approve, resize, or reject it — the strategy has
    no guarantee its stated quantity will be executed.
    """
    schema_version: ClassVar[int] = 1

    strategy_id: str
    symbol: str
    side: Side
    intent_type: IntentType
    quantity: Decimal
    signal_bar: BarEvent
    limit_price: Decimal | None = None
    notes: str = ""


@dataclass(frozen=True, kw_only=True)
class ApprovedOrderEvent(RiskEvent):
    """
    A risk-engine-approved order. The only valid path to construct this is
    via RiskGatekeeper._build_approval(), which passes _token=_APPROVAL_TOKEN.
    Any other caller receives a RuntimeError from __post_init__.

    Note for serialisation: exclude the _token field (repr=False, compare=False).
    """
    schema_version: ClassVar[int] = 1

    origin_intent: OrderIntentEvent
    approved_qty: Decimal
    approved_side: Side
    approved_type: IntentType
    rule_trace: tuple[RuleTrace, ...]
    notes: str = ""
    _token: object = field(default=None, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self._token is not _APPROVAL_TOKEN or not _called_from_approval_factory():
            raise RuntimeError(
                "ApprovedOrderEvent must be created via RiskGatekeeper._build_approval(). "
                "Direct construction is not permitted."
            )


@dataclass(frozen=True, kw_only=True)
class RejectedOrderEvent(RiskEvent):
    """A risk-engine-rejected order intent. No order is created."""
    schema_version: ClassVar[int] = 1

    origin_intent: OrderIntentEvent
    rejection_reason: str
    rule_trace: tuple[RuleTrace, ...]


@dataclass(frozen=True, kw_only=True)
class OrderSubmittedEvent(ExecutionEvent):
    """Broker acknowledgement that an approved order entered its queue."""
    schema_version: ClassVar[int] = 1

    order_id: UUID
    origin_approval: ApprovedOrderEvent


@dataclass(frozen=True, kw_only=True)
class FillEvent(ExecutionEvent):
    """
    Confirmation that an order was filled. fill_price includes slippage.
    commission is the total for this fill (not per-share).
    fill_bar is the bar whose open was used as the base fill price.
    """
    schema_version: ClassVar[int] = 2

    order_id: UUID
    strategy_id: str
    symbol: str
    side: Side
    filled_qty: Decimal
    fill_price: Decimal
    commission: Decimal
    fill_bar: BarEvent


@dataclass(frozen=True, kw_only=True)
class PortfolioSnapshotEvent(PortfolioEvent):
    """
    Point-in-time portfolio state published at the end of each bar cycle,
    after all fills for that cycle have been processed.
    """
    schema_version: ClassVar[int] = 1

    equity: Decimal
    cash: Decimal
    initial_cash: Decimal
    total_return_pct: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    drawdown_pct: Decimal
    peak_equity: Decimal
    positions: tuple[PositionSnapshot, ...]
    num_positions: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Enums
    "BarType",
    "Side",
    "IntentType",
    "Verdict",
    # Supporting types
    "RuleTrace",
    "PositionSnapshot",
    # Base / intermediate
    "BaseEvent",
    "MarketEvent",
    "SignalEvent",
    "RiskEvent",
    "ExecutionEvent",
    "PortfolioEvent",
    # Concrete events
    "BarEvent",
    "OrderIntentEvent",
    "ApprovedOrderEvent",
    "RejectedOrderEvent",
    "OrderSubmittedEvent",
    "FillEvent",
    "PortfolioSnapshotEvent",
]


def _called_from_approval_factory() -> bool:
    for frame_info in inspect.stack()[2:7]:
        if (
            frame_info.function == _APPROVAL_FACTORY_NAME
            and frame_info.frame.f_globals.get("__name__") == _APPROVAL_FACTORY_MODULE
        ):
            return True
    return False

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from engine.events.types import ApprovedOrderEvent, BarEvent


class AbstractBroker(ABC):
    """
    Execution-layer interface.

    The type accepted by submit() is deliberately ApprovedOrderEvent, never
    OrderIntentEvent. That keeps the architecture's risk-gate invariant intact:
    execution can only receive orders that the gatekeeper approved.
    """

    @abstractmethod
    def submit(self, order: ApprovedOrderEvent) -> None:
        """Accept an approved order for routing or paper-simulation."""

    @abstractmethod
    def on_bar(self, event: BarEvent) -> None:
        """Receive the latest market bar for reconciliation or fill simulation."""

    @abstractmethod
    def cancel(self, order_id: UUID) -> None:
        """Cancel a pending order when the broker implementation supports it."""

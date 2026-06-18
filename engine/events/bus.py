"""
Synchronous in-process event bus.

Design constraints (from EVENT_SYSTEM.md §3):

  Ordering   — handlers fire in registration order, period. The engine
               runner is responsible for registering in the canonical
               sequence (portfolio MTM → strategy → risk → broker →
               portfolio fill → audit logger).

  Synchronous — publish() does not return until every matching handler
               has completed. No queue, no thread pool, no async.

  Re-entrant — a handler may call publish() (e.g. strategy.on_bar emits
               an OrderIntentEvent). The nested call runs the full downstream
               chain on the call stack before the outer publish() resumes
               its remaining handlers. This is correct and intentional.

  Fault-tolerant — if a handler raises, the bus logs the error, continues
               dispatching to all remaining handlers, then re-raises after
               the last handler completes. The audit logger (subscribed to
               BaseEvent, registered last) therefore always fires.

               Re-entrant note: an exception raised inside a nested
               publish() propagates to the outer publish() as a handler
               failure of the calling handler. The error is logged once
               by the inner bus (for the nested event) and again by the
               outer bus (for the calling handler). This is intentional —
               both failure points are real and both belong in the log.
"""

from __future__ import annotations

import logging
from typing import Callable, TypeVar

from engine.events.types import BaseEvent

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=BaseEvent)


class EventBus:
    """
    Typed synchronous publish/subscribe bus.

    Usage::

        bus = EventBus()
        bus.subscribe(BarEvent, portfolio.on_bar)
        bus.subscribe(BarEvent, strategy.on_bar)
        bus.subscribe(OrderIntentEvent, risk.on_order_intent)
        bus.subscribe(BaseEvent, audit_logger.on_any)   # fires for everything

        bus.publish(some_bar_event)  # dispatches to all three in order
    """

    def __init__(self) -> None:
        # Ordered list of (event_type, handler) pairs. Registration order
        # is dispatch order — do not sort or reorder this list.
        self._handlers: list[tuple[type[BaseEvent], Callable[[BaseEvent], None]]] = []
        self._history: list[BaseEvent] = []

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> None:
        """
        Register handler to be called for every published event that is an
        instance of event_type (including subclasses).

        Registering the same (event_type, handler) pair twice will call the
        handler twice per matching event — avoid duplicate registration.
        """
        self._handlers.append((event_type, handler))  # type: ignore[arg-type]

    def publish(self, event: BaseEvent) -> None:
        """
        Append event to history, then dispatch to all matching handlers
        in registration order.

        Matching is by isinstance: a handler registered for BaseEvent
        receives every event; one registered for BarEvent only receives
        BarEvent instances.

        All handlers run before this method returns. If any raise, dispatch
        still completes and exceptions are re-raised afterwards:
          - exactly one failure  → the original exception is re-raised as-is
          - multiple failures    → raised as ExceptionGroup
        """
        self._history.append(event)

        errors: list[Exception] = []

        # Snapshot the handler list so a handler that calls subscribe()
        # mid-dispatch does not affect the current publish round.
        for registered_type, handler in list(self._handlers):
            if not isinstance(event, registered_type):
                continue
            try:
                handler(event)  # type: ignore[arg-type]
            except Exception as exc:
                logger.error(
                    "Handler %s raised while processing %s(event_id=%s): %s",
                    _handler_name(handler),
                    type(event).__name__,
                    event.event_id,
                    exc,
                    exc_info=True,
                )
                errors.append(exc)

        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup(
                f"{len(errors)} handler(s) failed on {type(event).__name__}",
                errors,
            )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_history(self, event_type: type[E]) -> list[E]:
        """
        Return all published events that are instances of event_type,
        in the order they were published.

        Used by the backtest runner and tests — not intended for use
        inside handlers during a live run.
        """
        return [e for e in self._history if isinstance(e, event_type)]  # type: ignore[return-value]

    def clear_history(self) -> None:
        """Discard the event history. Called between backtest runs."""
        self._history.clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def subscriber_count(self, event_type: type[BaseEvent] | None = None) -> int:
        """
        Number of registered handlers.

        If event_type is provided, count only handlers that would fire
        when an event of that type is published (i.e. handlers registered
        for event_type or any of its base classes).
        """
        if event_type is None:
            return len(self._handlers)
        return sum(
            1 for registered_type, _ in self._handlers
            if issubclass(event_type, registered_type)
        )

    def __repr__(self) -> str:
        return (
            f"EventBus("
            f"handlers={len(self._handlers)}, "
            f"history={len(self._history)})"
        )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _handler_name(handler: Callable) -> str:
    """
    Return a readable identifier for a handler.

    Bound methods render as "ClassName.method_name".
    Plain functions render as their qualified name.
    """
    owner = getattr(handler, "__self__", None)
    if owner is not None:
        return f"{type(owner).__name__}.{handler.__name__}"
    return getattr(handler, "__qualname__", repr(handler))

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from decimal import Decimal
from typing import Any, Protocol

from engine.events.bus import EventBus
from engine.events.types import BarEvent, FillEvent, IntentType, OrderIntentEvent, PortfolioSnapshotEvent, Side

_ZERO = Decimal("0")


class StrategyConfigLike(Protocol):
    """Minimal strategy-config surface required by AbstractStrategy."""

    symbols: list[str]
    params: dict[str, Any]


class AbstractStrategy(ABC):
    """
    Base class for pure signal generators.

    The constructor accepts only strategy id, strategy config, and EventBus.
    It intentionally imports no portfolio, risk, broker, or order modules,
    preserving the isolation checklist in docs/STRATEGY_INTERFACE.md.
    """

    def __init__(
        self,
        strategy_id: str,
        config: StrategyConfigLike,
        bus: EventBus,
    ) -> None:
        self.strategy_id = strategy_id
        self.config = config
        self._bus = bus
        self._symbols = {symbol.upper() for symbol in config.symbols}
        self._bar_counts: dict[str, int] = defaultdict(int)
        self._current_bar: BarEvent | None = None

        if not self.strategy_id:
            raise ValueError("strategy_id must be non-empty")
        if not self._symbols:
            raise ValueError("strategy config must contain at least one symbol")

    def on_start(self, universe: list[str]) -> None:
        unknown = self._symbols - {symbol.upper() for symbol in universe}
        if unknown:
            raise ValueError(
                f"strategy {self.strategy_id!r} references symbols outside universe: "
                f"{sorted(unknown)}"
            )
        self._bar_counts.clear()
        self._current_bar = None

    @abstractmethod
    def on_bar(self, event: BarEvent) -> None:
        """
        Handle one market bar.

        Subclasses should call self._record_bar(event) before indicator logic
        so _is_warmed_up() and _emit_intent() use the current bar.
        """

    def on_snapshot(self, event: PortfolioSnapshotEvent) -> None:
        """Override to react to portfolio state changes (e.g. circuit-breaker liquidation)."""

    def on_fill(self, event: FillEvent) -> None:
        """
        Override to reconcile internal state from confirmed broker fills.

        This is the ONLY correct point for strategies to update holdings or
        position tracking. Updating state at intent-emission time is wrong:
        the broker may cancel, resize, or drop the order between approval and
        fill. See docs/STRATEGY_INTERFACE.md §5.3.
        """

    def on_stop(self) -> None:
        self._current_bar = None

    def _record_bar(self, event: BarEvent) -> None:
        if event.symbol not in self._symbols:
            return
        self._current_bar = event
        self._bar_counts[event.symbol] += 1

    def _is_warmed_up(self, symbol: str, required_bars: int) -> bool:
        if required_bars <= 0:
            raise ValueError("required_bars must be positive")
        return self._bar_counts[symbol.upper()] >= required_bars

    def _emit_intent(
        self,
        symbol: str,
        side: Side,
        intent_type: IntentType,
        quantity: Decimal,
        limit_price: Decimal | None = None,
        notes: str = "",
    ) -> None:
        symbol = symbol.upper()
        if symbol not in self._symbols:
            raise ValueError(
                f"strategy {self.strategy_id!r} cannot emit intents for {symbol!r}"
            )
        if quantity <= _ZERO:
            raise ValueError("strategy intent quantity must be positive")
        if intent_type == IntentType.LIMIT and limit_price is None:
            raise ValueError("limit intents require limit_price")
        if self._current_bar is None or self._current_bar.symbol != symbol:
            raise RuntimeError("_emit_intent() requires _record_bar(event) for the same symbol")

        self._bus.publish(
            OrderIntentEvent(
                timestamp=self._current_bar.timestamp,
                strategy_id=self.strategy_id,
                symbol=symbol,
                side=side,
                intent_type=intent_type,
                quantity=quantity,
                limit_price=limit_price,
                signal_bar=self._current_bar,
                notes=notes,
            )
        )

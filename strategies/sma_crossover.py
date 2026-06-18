from __future__ import annotations

from collections import deque
from decimal import Decimal

from engine.events.bus import EventBus
from engine.events.types import BarEvent, FillEvent, IntentType, Side
from engine.strategy.base import AbstractStrategy, StrategyConfigLike

_ZERO = Decimal("0")
_ONE = Decimal("1")


class SmaCrossoverStrategy(AbstractStrategy):
    """
    Reference fast-EMA / slow-SMA crossover strategy.

    This strategy is intentionally a pure signal generator. It imports no
    portfolio, risk, broker, or execution-order modules and emits trade
    proposals only through AbstractStrategy._emit_intent().
    """

    def __init__(
        self,
        strategy_id: str,
        config: StrategyConfigLike,
        bus: EventBus,
    ) -> None:
        super().__init__(strategy_id, config, bus)
        params = config.params
        self.fast_period = int(params.get("fast_period", 10))
        self.slow_period = int(params.get("slow_period", 30))
        self.quantity = Decimal(str(params.get("quantity", "100")))

        if self.fast_period <= 0:
            raise ValueError("fast_period must be positive")
        if self.slow_period <= 0:
            raise ValueError("slow_period must be positive")
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")
        if self.quantity <= _ZERO:
            raise ValueError("quantity must be positive")

        self._price_history: dict[str, deque[Decimal]] = {}
        self._previous_signal_state: dict[str, tuple[Decimal, Decimal]] = {}
        self._qty_held: dict[str, Decimal] = {}

    def on_start(self, universe: list[str]) -> None:
        super().on_start(universe)
        self._price_history = {
            symbol: deque(maxlen=self.slow_period) for symbol in self._symbols
        }
        self._previous_signal_state.clear()
        self._qty_held = {symbol: _ZERO for symbol in self._symbols}

    def on_bar(self, event: BarEvent) -> None:
        if event.symbol not in self._symbols:
            return

        self._record_bar(event)
        history = self._price_history.setdefault(
            event.symbol,
            deque(maxlen=self.slow_period),
        )
        history.append(event.close)

        if not self._is_warmed_up(event.symbol, self.slow_period):
            return

        closes = list(history)
        fast_ema = _ema(closes[-self.fast_period:])
        slow_sma = sum(closes, _ZERO) / Decimal(len(closes))

        previous = self._previous_signal_state.get(event.symbol)
        self._previous_signal_state[event.symbol] = (fast_ema, slow_sma)
        if previous is None:
            return

        prev_fast, prev_slow = previous
        qty_held = self._qty_held.get(event.symbol, _ZERO)

        if prev_fast <= prev_slow and fast_ema > slow_sma and qty_held == _ZERO:
            self._emit_intent(
                symbol=event.symbol,
                side=Side.BUY,
                intent_type=IntentType.MARKET,
                quantity=self.quantity,
                notes=(
                    f"fast EMA({self.fast_period}) crossed above "
                    f"slow SMA({self.slow_period})"
                ),
            )
        elif prev_fast >= prev_slow and fast_ema < slow_sma and qty_held > _ZERO:
            self._emit_intent(
                symbol=event.symbol,
                side=Side.SELL,
                intent_type=IntentType.MARKET,
                quantity=qty_held,
                notes=(
                    f"fast EMA({self.fast_period}) crossed below "
                    f"slow SMA({self.slow_period})"
                ),
            )

    def on_fill(self, event: FillEvent) -> None:
        if event.strategy_id != self.strategy_id:
            return
        symbol = event.symbol.upper()
        if event.side == Side.BUY:
            self._qty_held[symbol] = self._qty_held.get(symbol, _ZERO) + event.filled_qty
        else:
            self._qty_held[symbol] = max(_ZERO, self._qty_held.get(symbol, _ZERO) - event.filled_qty)


def _ema(values: list[Decimal]) -> Decimal:
    if not values:
        return _ZERO
    alpha = Decimal("2") / Decimal(len(values) + 1)
    ema = values[0]
    for value in values[1:]:
        ema = value * alpha + ema * (_ONE - alpha)
    return ema

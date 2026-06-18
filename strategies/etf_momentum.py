"""
ETF Momentum Rotation Strategy

Monthly rotation across a configurable ETF universe. Each month, ranks all
symbols by their lookback-period price return and holds the top-ranked one.
Moves to cash when the top-ranked symbol has negative momentum (absolute
momentum safety filter — avoids holding anything in a broad bear market).
"""

from __future__ import annotations

from collections import deque
from datetime import date
from decimal import Decimal

from engine.events.types import BarEvent, FillEvent, IntentType, Side
from engine.strategy.base import AbstractStrategy, StrategyConfigLike

_DEFAULT_LOOKBACK = 63   # ≈ 3 calendar months of trading days
_DEFAULT_QTY = 200


class EtfMomentumStrategy(AbstractStrategy):
    """
    Monthly ETF momentum rotation with absolute momentum safety filter.

    On the first trading day of each calendar month, ranks all configured
    symbols by their lookback-period return. Rotates into the top-ranked
    symbol. If that symbol's return is negative, exits to cash instead.

    Config params:
        lookback_days (int): momentum lookback in trading days (default 63)
        quantity      (int): shares per position (default 200; risk rules resize)
    """

    def __init__(
        self,
        strategy_id: str,
        config: StrategyConfigLike,
        bus,
    ) -> None:
        super().__init__(strategy_id, config, bus)
        self._lookback: int = int(config.params.get("lookback_days", _DEFAULT_LOOKBACK))
        self._qty: Decimal = Decimal(str(config.params.get("quantity", _DEFAULT_QTY)))

        # Initialised properly in on_start; set here for safety
        self._price_history: dict[str, deque] = {}
        self._latest_bars: dict[str, BarEvent] = {}
        self._current_holding: str | None = None
        self._qty_held: dict[str, Decimal] = {}  # symbol → confirmed held qty
        self._last_rebalance_month: int = -1
        self._bars_today: set[str] = set()
        self._last_bar_date: date | None = None

    def on_start(self, universe: list[str]) -> None:
        super().on_start(universe)
        self._price_history = {
            s: deque(maxlen=self._lookback + 1) for s in self._symbols
        }
        self._latest_bars = {}
        self._current_holding = None
        self._qty_held = {}
        self._last_rebalance_month = -1
        self._bars_today = set()
        self._last_bar_date = None

    def on_stop(self) -> None:
        super().on_stop()
        self._current_holding = None
        self._qty_held = {}

    def on_fill(self, event: FillEvent) -> None:
        if event.strategy_id != self.strategy_id:
            return
        sym = event.symbol.upper()
        if sym not in self._symbols:
            return
        if event.side == Side.BUY:
            self._qty_held[sym] = self._qty_held.get(sym, Decimal("0")) + event.filled_qty
            self._current_holding = sym
        else:
            remaining = max(Decimal("0"), self._qty_held.get(sym, Decimal("0")) - event.filled_qty)
            if remaining <= Decimal("0"):
                self._qty_held.pop(sym, None)
                if self._current_holding == sym:
                    self._current_holding = None
            else:
                self._qty_held[sym] = remaining

    def on_bar(self, event: BarEvent) -> None:
        self._record_bar(event)
        sym = event.symbol.upper()
        if sym not in self._symbols:
            return

        bar_date = event.timestamp.date()

        # Reset daily tracking when the date rolls over
        if bar_date != self._last_bar_date:
            self._bars_today = set()
            self._last_bar_date = bar_date

        self._price_history[sym].append(float(event.close))
        self._latest_bars[sym] = event
        self._bars_today.add(sym)

        # Fire rebalance logic only after all symbols have reported for today
        # and only on the first day of each new calendar month
        if self._bars_today < self._symbols:
            return
        if bar_date.month == self._last_rebalance_month:
            return

        self._rebalance(bar_date)

    # ------------------------------------------------------------------
    # Rebalance logic
    # ------------------------------------------------------------------

    def _rebalance(self, today: date) -> None:
        # Need a full lookback window for every symbol before first trade
        for sym in self._symbols:
            if len(self._price_history.get(sym, [])) < self._lookback + 1:
                return

        self._last_rebalance_month = today.month

        # Rank by lookback-period return, best first
        ranking = sorted(
            [(sym, self._momentum(sym)) for sym in self._symbols],
            key=lambda x: x[1],
            reverse=True,
        )
        top_sym, top_mom = ranking[0]

        # Absolute momentum filter: go to cash if even the best is negative
        target = top_sym if top_mom > 0 else None

        # No-op: already cleanly in the target with no residual non-target lots
        residuals = [sym for sym, qty in self._qty_held.items() if sym != target and qty > Decimal("0")]
        if target == self._current_holding and not residuals:
            return

        # Exit all non-target confirmed positions — covers residual partial lots
        # left over from a previous rotation that only partially filled.
        for sym in list(self._qty_held):
            qty = self._qty_held[sym]
            if qty > Decimal("0") and sym != target:
                self._emit_for(sym, Side.SELL, qty, notes=f"rotation out of {sym}")

        # Enter new position only if we don't already hold it
        if target is not None and target not in self._qty_held:
            self._emit_for(
                target,
                Side.BUY,
                self._qty,
                notes=f"rotation into {target} (mom={top_mom:+.2%})",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _momentum(self, symbol: str) -> float:
        h = self._price_history[symbol]
        if len(h) < self._lookback + 1:
            return float("-inf")
        return (h[-1] - h[-self._lookback]) / h[-self._lookback]

    def _emit_for(self, symbol: str, side: Side, qty: Decimal, notes: str = "") -> None:
        """Temporarily point _current_bar at the right symbol before emitting."""
        saved = self._current_bar
        self._current_bar = self._latest_bars[symbol]
        self._emit_intent(symbol, side, IntentType.MARKET, qty, notes=notes)
        self._current_bar = saved

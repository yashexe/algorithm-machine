"""
ETF Mean Reversion Strategy — SPY / TLT Pairs Trading

Trades the statistical relationship between equities (SPY) and long-duration
Treasuries (TLT). The two assets are historically negatively correlated: when
equities fall, investors flee to bonds, pushing TLT up. When equities rally,
TLT tends to underperform.

This creates a mean-reverting spread: log(SPY/TLT) tends to revert to its
rolling mean. We buy the undervalued leg when the z-score is extreme.

Since this is a long-only engine (ShortSellingRule enforced), we express each
trade as a BUY into whichever leg is undervalued:
    - z-score < -entry_z  → SPY is cheap relative to TLT  → BUY SPY
    - z-score > +entry_z  → TLT is cheap relative to SPY  → BUY TLT
    - z-score reverts to ±exit_z                           → SELL the held leg

Architecture compliance (STRATEGY_INTERFACE.md §7):
    [x] Only inherits from AbstractStrategy
    [x] __init__ accepts only (strategy_id, config, bus)
    [x] No import of PortfolioState, RiskGatekeeper, PaperBroker, or Order
    [x] All proposals via ._emit_intent(), never direct OrderIntentEvent construction
    [x] Warm-up check gates all signal logic
    [x] State is re-initialized in on_start() — stateless across stop/start
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal

from engine.events.types import BarEvent, FillEvent, IntentType, Side
from engine.strategy.base import AbstractStrategy, StrategyConfigLike



class EtfMeanReversionStrategy(AbstractStrategy):
    """
    Z-score mean reversion strategy on the SPY/TLT log spread.

    Config params (all optional with defaults):
        symbol_a         (str)   first symbol in the pair, default "SPY"
        symbol_b         (str)   second symbol in the pair, default "TLT"
        lookback_days    (int)   rolling window for spread z-score, default 60
        entry_z          (float) z-score magnitude to enter a position, default 1.5
        exit_z           (float) z-score magnitude to exit a position, default 0.5
        quantity_pct     (float) target portfolio weight per leg (0.0–1.0), default 0.15
        initial_cash     (float) used for quantity sizing denominator, default 100000
        min_vol          (float) minimum spread volatility to enter (avoids flat regimes)

    Multi-strategy note (STRATEGY_INTERFACE.md §6):
        When running alongside equity_momentum, both strategies emit intents to the
        shared RiskGatekeeper. If both emit BUY SPY simultaneously, the gatekeeper
        evaluates each against the same portfolio snapshot. The PositionSizeRule and
        CashSolvencyRule jointly cap combined exposure without any strategy-level
        coordination — this is the correct centralized risk architecture.
    """

    def __init__(
        self,
        strategy_id: str,
        config: StrategyConfigLike,
        bus,
    ) -> None:
        super().__init__(strategy_id, config, bus)
        p = config.params

        self._symbol_a = str(p.get("symbol_a", "SPY")).upper()
        self._symbol_b = str(p.get("symbol_b", "TLT")).upper()
        self._lookback = int(p.get("lookback_days", 60))
        self._entry_z = float(p.get("entry_z", 1.5))
        self._exit_z = float(p.get("exit_z", 0.5))
        self._quantity_pct = float(p.get("quantity_pct", 0.15))
        self._initial_cash = float(p.get("initial_cash", 100_000))
        self._min_vol = float(p.get("min_vol", 0.001))  # min spread daily vol

        # Validate symbols are in the strategy universe
        if self._symbol_a not in self._symbols:
            raise ValueError(
                f"EtfMeanReversionStrategy: symbol_a={self._symbol_a!r} "
                f"not in strategy symbols {sorted(self._symbols)}"
            )
        if self._symbol_b not in self._symbols:
            raise ValueError(
                f"EtfMeanReversionStrategy: symbol_b={self._symbol_b!r} "
                f"not in strategy symbols {sorted(self._symbols)}"
            )

        # State — all re-initialized in on_start()
        self._prices_a: deque[float] = deque(maxlen=self._lookback + 1)
        self._prices_b: deque[float] = deque(maxlen=self._lookback + 1)
        self._latest_a: BarEvent | None = None
        self._latest_b: BarEvent | None = None

        # Position tracking — confirmed by fills, not optimistic estimates
        self._holding_a: bool = False  # currently long symbol_a (SPY)
        self._holding_b: bool = False  # currently long symbol_b (TLT)
        self._qty_a: Decimal = Decimal("0")  # confirmed filled qty for symbol_a
        self._qty_b: Decimal = Decimal("0")  # confirmed filled qty for symbol_b

        # Signals are only evaluated once both symbols have arrived for the same bar
        self._pending_date = None  # date for which we're accumulating bars

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self, universe: list[str]) -> None:
        super().on_start(universe)
        self._prices_a = deque(maxlen=self._lookback + 1)
        self._prices_b = deque(maxlen=self._lookback + 1)
        self._latest_a = None
        self._latest_b = None
        self._holding_a = False
        self._holding_b = False
        self._qty_a = Decimal("0")
        self._qty_b = Decimal("0")
        self._pending_date = None

    def on_stop(self) -> None:
        super().on_stop()
        self._holding_a = False
        self._holding_b = False
        self._qty_a = Decimal("0")
        self._qty_b = Decimal("0")

    def on_fill(self, event: FillEvent) -> None:
        if event.strategy_id != self.strategy_id:
            return
        sym = event.symbol.upper()
        if event.side == Side.BUY:
            if sym == self._symbol_a:
                self._qty_a += event.filled_qty
                self._holding_a = True
            elif sym == self._symbol_b:
                self._qty_b += event.filled_qty
                self._holding_b = True
        elif event.side == Side.SELL:
            if sym == self._symbol_a:
                self._qty_a -= event.filled_qty
                if self._qty_a <= Decimal("0"):
                    self._qty_a = Decimal("0")
                    self._holding_a = False
            elif sym == self._symbol_b:
                self._qty_b -= event.filled_qty
                if self._qty_b <= Decimal("0"):
                    self._qty_b = Decimal("0")
                    self._holding_b = False

    # ------------------------------------------------------------------
    # Bar handler
    # ------------------------------------------------------------------

    def on_bar(self, event: BarEvent) -> None:
        """
        Accumulate bars for both legs. Evaluate signals only once both
        symbols have arrived for the current bar date. This prevents
        look-ahead by ensuring both prices are from the same date.
        """
        self._record_bar(event)
        sym = event.symbol.upper()

        if sym not in self._symbols:
            return

        if sym == self._symbol_a:
            self._prices_a.append(float(event.close))
            self._latest_a = event
        elif sym == self._symbol_b:
            self._prices_b.append(float(event.close))
            self._latest_b = event
        else:
            return  # symbol not relevant to this pair

        # Evaluate only when both symbols have been updated for this date
        bar_date = event.timestamp.date()
        if bar_date != self._pending_date:
            # New date — reset pending
            self._pending_date = bar_date
            self._seen_today = {sym}
        else:
            self._seen_today.add(sym)

        both_ready = (
            self._symbol_a in self._seen_today
            and self._symbol_b in self._seen_today
        )
        if not both_ready:
            return

        # Both symbols have arrived for today — run signal logic
        self._evaluate_signals()

    # ------------------------------------------------------------------
    # Signal logic
    # ------------------------------------------------------------------

    def _evaluate_signals(self) -> None:
        """Compute z-score and emit intents when thresholds are breached."""
        if not self._is_warmed_up(self._symbol_a, self._lookback):
            return
        if not self._is_warmed_up(self._symbol_b, self._lookback):
            return

        z_score = self._compute_z_score()
        if z_score is None:
            return

        # ── Exit logic ─────────────────────────────────────────────────
        # Exit long-A (SPY) position when spread reverts upward
        if self._holding_a and z_score > -self._exit_z:
            self._emit_sell(self._symbol_a, self._latest_a, notes=f"exit SPY z={z_score:.2f}")

        # Exit long-B (TLT) position when spread reverts downward
        if self._holding_b and z_score < self._exit_z:
            self._emit_sell(self._symbol_b, self._latest_b, notes=f"exit TLT z={z_score:.2f}")

        # ── Entry logic ────────────────────────────────────────────────
        # SPY unusually cheap vs TLT → buy SPY
        if not self._holding_a and z_score < -self._entry_z:
            qty = self._target_qty(self._latest_a)
            if qty > Decimal("0"):
                self._emit_for(self._symbol_a, Side.BUY, qty, notes=f"enter SPY z={z_score:.2f}")

        # TLT unusually cheap vs SPY → buy TLT
        elif not self._holding_b and z_score > self._entry_z:
            qty = self._target_qty(self._latest_b)
            if qty > Decimal("0"):
                self._emit_for(self._symbol_b, Side.BUY, qty, notes=f"enter TLT z={z_score:.2f}")

    # ------------------------------------------------------------------
    # Spread and z-score computation
    # ------------------------------------------------------------------

    def _compute_z_score(self) -> float | None:
        """
        Compute the z-score of the log price spread: log(price_a / price_b).

        Returns None if prices are unavailable or spread volatility is too low.
        """
        if len(self._prices_a) < self._lookback or len(self._prices_b) < self._lookback:
            return None

        # Build spread series using the rolling window
        len_a = len(self._prices_a)
        len_b = len(self._prices_b)
        n = min(len_a, len_b, self._lookback)

        prices_a = list(self._prices_a)[-n:]
        prices_b = list(self._prices_b)[-n:]

        spread = [
            math.log(pa / pb)
            for pa, pb in zip(prices_a, prices_b)
            if pa > 0 and pb > 0
        ]
        if len(spread) < self._lookback // 2:
            return None

        mean = sum(spread) / len(spread)
        variance = sum((s - mean) ** 2 for s in spread) / len(spread)
        std = math.sqrt(variance)

        if std < self._min_vol or std == 0.0:
            return None  # spread is too quiet — no edge in flat regimes

        current_spread = spread[-1]
        return (current_spread - mean) / std

    # ------------------------------------------------------------------
    # Order emission helpers
    # ------------------------------------------------------------------

    def _target_qty(self, bar: BarEvent | None) -> Decimal:
        """Compute share quantity targeting quantity_pct of initial_cash."""
        if bar is None or float(bar.close) <= 0:
            return Decimal("1")
        price = float(bar.close)
        notional = self._quantity_pct * self._initial_cash
        raw = int(notional / price)
        return max(Decimal("1"), Decimal(str(raw)))

    def _emit_for(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        notes: str = "",
    ) -> None:
        """Emit an intent using the bar that belongs to the target symbol."""
        bar = self._latest_a if symbol == self._symbol_a else self._latest_b
        if bar is None:
            return
        saved = self._current_bar
        self._current_bar = bar
        self._emit_intent(symbol, side, IntentType.MARKET, qty, notes=notes)
        self._current_bar = saved

    def _emit_sell(
        self,
        symbol: str,
        bar: BarEvent | None,
        notes: str = "",
    ) -> None:
        """Emit a SELL intent for the confirmed held qty in this strategy's lot."""
        if bar is None:
            return
        qty = self._qty_a if symbol == self._symbol_a else self._qty_b
        if qty <= Decimal("0"):
            return
        saved = self._current_bar
        self._current_bar = bar
        self._emit_intent(symbol, Side.SELL, IntentType.MARKET, qty, notes=notes)
        self._current_bar = saved

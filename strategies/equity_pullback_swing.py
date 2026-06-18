"""
Equity Pullback Swing Strategy

Long-only pullback continuation strategy for swing holding periods (days to weeks).

Entry logic:
  - Market regime bullish: SPY above its regime_ma_period-day MA
  - Stock in uptrend: close above trend_ma_period-day MA, positive RS, above min volume
  - Short-term RSI <= entry_rsi (stock has pulled back into oversold territory)

Exit logic (any one condition):
  - RSI >= exit_rsi (bounce complete)
  - close >= entry_price * (1 + take_profit_pct)
  - close <= entry_price * (1 - stop_loss_pct)
  - bars held >= max_hold_bars

Sizing: floor(initial_cash * position_pct / close). Risk rules may resize further.
"""

from __future__ import annotations

from collections import deque
from datetime import date
from decimal import Decimal
from math import floor

from engine.events.types import BarEvent, FillEvent, IntentType, PortfolioSnapshotEvent, Side
from engine.strategy.base import AbstractStrategy, StrategyConfigLike

_ZERO = Decimal("0")


class EquityPullbackSwingStrategy(AbstractStrategy):
    """
    Long-only pullback continuation swing strategy.

    Config params (all optional with defaults):
        regime_symbol           (str)   market regime symbol, default SPY
        regime_ma_period        (int)   MA period for regime filter, default 200
        trend_ma_period         (int)   MA period for per-stock uptrend filter, default 50
        rs_lookback_days        (int)   relative-strength lookback in trading days, default 63
        rsi_period              (int)   RSI period, default 5
        entry_rsi               (float) enter when RSI <= this value, default 35.0
        exit_rsi                (float) exit when RSI >= this value, default 55.0
        max_hold_bars           (int)   maximum bars before forced exit, default 10
        stop_loss_pct           (float) stop loss below entry price, default 0.05
        take_profit_pct         (float) take profit above entry price, default 0.10
        position_pct            (float) fraction of initial_cash per position, default 0.05
        initial_cash            (float) sizing denominator, default 100000
        max_open_positions      (int)   maximum concurrent positions, default 5
        max_new_entries_per_day (int)   entry turnover cap per bar date, default 3
        min_volume_20d          (float) minimum 20-day avg daily volume, default 1_000_000
    """

    def __init__(
        self,
        strategy_id: str,
        config: StrategyConfigLike,
        bus,
    ) -> None:
        super().__init__(strategy_id, config, bus)
        p = config.params

        self._regime_symbol      = str(p.get("regime_symbol", "SPY")).upper()
        self._regime_ma_period   = int(p.get("regime_ma_period", 200))
        self._trend_ma_period    = int(p.get("trend_ma_period", 50))
        self._rs_lookback        = int(p.get("rs_lookback_days", 63))
        self._rsi_period         = int(p.get("rsi_period", 5))
        self._entry_rsi          = float(p.get("entry_rsi", 35.0))
        self._exit_rsi           = float(p.get("exit_rsi", 55.0))
        self._max_hold_bars      = int(p.get("max_hold_bars", 30))
        self._stop_loss_pct      = float(p.get("stop_loss_pct", 0.05))
        self._take_profit_pct    = float(p.get("take_profit_pct", 0.15))
        self._trail_activation_pct = float(p.get("trail_activation_pct", 0.05))
        self._trail_ma_period    = int(p.get("trail_ma_period", 20))
        self._position_pct       = float(p.get("position_pct", 0.05))
        self._initial_cash       = float(p.get("initial_cash", 100_000))
        self._max_new_entries    = int(p.get("max_new_entries_per_day", 3))
        self._min_volume         = float(p.get("min_volume_20d", 1_000_000))
        self._current_equity     = self._initial_cash  # updated via on_snapshot each bar cycle

        # Cap max_open_positions so that position_pct * max_open_positions <= 1.0.
        # _size_qty commits initial_cash * position_pct per slot, so exceeding this
        # ratio guarantees cash exhaustion and cascading broker cancellations.
        _capacity_cap = max(1, int(1.0 / self._position_pct))
        self._max_open_positions = min(int(p.get("max_open_positions", 5)), _capacity_cap)

        # Validation
        if self._regime_ma_period <= 0:
            raise ValueError("regime_ma_period must be positive")
        if self._trend_ma_period <= 0:
            raise ValueError("trend_ma_period must be positive")
        if self._rs_lookback <= 0:
            raise ValueError("rs_lookback_days must be positive")
        if self._rsi_period <= 0:
            raise ValueError("rsi_period must be positive")
        if self._entry_rsi >= self._exit_rsi:
            raise ValueError(f"entry_rsi ({self._entry_rsi}) must be less than exit_rsi ({self._exit_rsi})")
        if self._stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive")
        if self._take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive")
        if self._trail_ma_period <= 0:
            raise ValueError("trail_ma_period must be positive")
        if self._trail_activation_pct <= 0:
            raise ValueError("trail_activation_pct must be positive")
        if self._trail_activation_pct >= self._take_profit_pct:
            raise ValueError("trail_activation_pct must be less than take_profit_pct")
        if self._position_pct <= 0:
            raise ValueError("position_pct must be positive")
        if self._max_open_positions <= 0:
            raise ValueError("max_open_positions must be positive")
        if self._max_new_entries <= 0:
            raise ValueError("max_new_entries_per_day must be positive")
        if self._initial_cash <= 0:
            raise ValueError("initial_cash must be positive")

        # Close history needs to cover the longest lookback requirement.
        _buf = max(self._regime_ma_period, self._trend_ma_period, self._rs_lookback + 1, self._trail_ma_period) + self._rsi_period + 5
        self._close_buf_size = _buf

        # State — reset in on_start
        self._close_history:    dict[str, deque] = {}
        self._volume_history:   dict[str, deque] = {}
        self._latest_bars:      dict[str, BarEvent] = {}
        self._qty_held:         dict[str, Decimal] = {}
        self._entry_price:      dict[str, Decimal] = {}   # VWAC per symbol
        self._entry_bar_index:  dict[str, int] = {}       # _bar_counts[sym] at fill time
        self._last_bar_date:    date | None = None
        self._new_entries_today: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self, universe: list[str]) -> None:
        super().on_start(universe)
        self._close_history   = {s: deque(maxlen=self._close_buf_size) for s in self._symbols}
        self._volume_history  = {s: deque(maxlen=21) for s in self._symbols}
        self._latest_bars     = {}
        self._qty_held        = {}
        self._entry_price     = {}
        self._entry_bar_index = {}
        self._last_bar_date   = None
        self._new_entries_today = 0
        self._current_equity  = self._initial_cash

    def on_stop(self) -> None:
        super().on_stop()
        self._qty_held.clear()
        self._entry_price.clear()
        self._entry_bar_index.clear()

    def on_snapshot(self, event: PortfolioSnapshotEvent) -> None:
        self._current_equity = float(event.equity)

    # ------------------------------------------------------------------
    # Bar handler
    # ------------------------------------------------------------------

    def on_bar(self, event: BarEvent) -> None:
        self._record_bar(event)
        sym = event.symbol.upper()
        if sym not in self._symbols:
            return

        self._close_history[sym].append(float(event.close))
        self._volume_history[sym].append(float(event.volume))
        self._latest_bars[sym] = event

        # Reset per-day entry counter when the date rolls over
        bar_date = event.timestamp.date()
        if bar_date != self._last_bar_date:
            self._last_bar_date = bar_date
            self._new_entries_today = 0

        # Regime symbol is used only as a filter, not traded
        if sym == self._regime_symbol:
            return

        # Exit check takes priority; skip entry logic on the same bar
        if sym in self._qty_held:
            self._check_exit(sym, event)
            return

        # Capacity gates before computing any indicators
        if len(self._qty_held) >= self._max_open_positions:
            return
        if self._new_entries_today >= self._max_new_entries:
            return

        self._check_entry(sym, event)

    # ------------------------------------------------------------------
    # Entry / exit
    # ------------------------------------------------------------------

    def _check_entry(self, sym: str, event: BarEvent) -> None:
        if not self._regime_ok():
            return
        if not self._trend_ok(sym):
            return
        rsi = self._rsi(sym)
        if rsi is None or rsi > self._entry_rsi:
            return
        qty = self._size_qty(event)
        if qty <= _ZERO:
            return
        rs = self._rs(sym)
        self._emit_intent(
            sym, Side.BUY, IntentType.MARKET, qty,
            notes=f"entry pullback rsi={rsi:.1f} rs={rs:+.1%}",
        )
        self._new_entries_today += 1

    def _check_exit(self, sym: str, event: BarEvent) -> None:
        qty = self._qty_held.get(sym, _ZERO)
        if qty <= _ZERO:
            return
        entry_price = self._entry_price.get(sym)
        if entry_price is None:
            return

        close     = float(event.close)
        entry_f   = float(entry_price)
        bars_held = self._bar_counts[sym] - self._entry_bar_index.get(sym, 0)
        rsi       = self._rsi(sym)
        pnl_pct   = close / entry_f - 1.0

        reason: str | None = None

        # Hard exits — always apply
        if close >= entry_f * (1.0 + self._take_profit_pct):
            reason = f"exit take profit {pnl_pct:+.1%}"
        elif close <= entry_f * (1.0 - self._stop_loss_pct):
            reason = f"exit stop loss {pnl_pct:+.1%}"
        elif bars_held >= self._max_hold_bars:
            reason = f"exit max hold {bars_held} bars"
        else:
            trail_ma = self._trail_ma(sym)
            # Trend-continuation mode: position has gained enough to trail the 20-day MA
            # instead of exiting on RSI — lets winners develop into multi-week trends.
            if trail_ma is not None and pnl_pct >= self._trail_activation_pct:
                if close < trail_ma:
                    reason = f"exit trail MA break {pnl_pct:+.1%}"
            elif rsi is not None and rsi >= self._exit_rsi:
                reason = f"exit rsi={rsi:.1f}"

        if reason is not None:
            self._emit_intent(sym, Side.SELL, IntentType.MARKET, qty, notes=reason)

    # ------------------------------------------------------------------
    # Filters and indicators
    # ------------------------------------------------------------------

    def _regime_ok(self) -> bool:
        prices = list(self._close_history.get(self._regime_symbol, []))
        if len(prices) < self._regime_ma_period:
            return False
        ma = sum(prices[-self._regime_ma_period:]) / self._regime_ma_period
        return prices[-1] > ma

    def _trend_ok(self, sym: str) -> bool:
        prices = list(self._close_history.get(sym, []))
        min_len = max(self._trend_ma_period, self._rs_lookback + 1)
        if len(prices) < min_len:
            return False
        # Above trend MA
        ma = sum(prices[-self._trend_ma_period:]) / self._trend_ma_period
        if prices[-1] <= ma:
            return False
        # Positive return over rs_lookback
        if self._rs(sym) <= 0.0:
            return False
        # Minimum volume
        vols = list(self._volume_history.get(sym, []))
        if len(vols) < 20:
            return False
        return (sum(vols[-20:]) / 20) >= self._min_volume

    def _rs(self, sym: str) -> float:
        """Return over rs_lookback_days (positive = stock has been rising)."""
        prices = list(self._close_history.get(sym, []))
        if len(prices) < self._rs_lookback + 1:
            return 0.0
        p_past = prices[-(self._rs_lookback + 1)]
        if p_past <= 0.0:
            return 0.0
        return (prices[-1] - p_past) / p_past

    def _trail_ma(self, sym: str) -> float | None:
        """Simple MA over trail_ma_period bars used as a trailing stop in trend mode."""
        prices = list(self._close_history.get(sym, []))
        if len(prices) < self._trail_ma_period:
            return None
        return sum(prices[-self._trail_ma_period:]) / self._trail_ma_period

    def _rsi(self, sym: str) -> float | None:
        """Cutler RSI over rsi_period bars. Returns None when insufficient history."""
        prices = list(self._close_history.get(sym, []))
        n = self._rsi_period
        if len(prices) < n + 1:
            return None
        changes = [prices[-(n - i)] - prices[-(n - i) - 1] for i in range(n)]
        avg_gain = sum(max(0.0, c) for c in changes) / n
        avg_loss = sum(abs(min(0.0, c)) for c in changes) / n
        if avg_loss == 0.0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def _size_qty(self, event: BarEvent) -> Decimal:
        close = float(event.close)
        if close <= 0.0:
            return _ZERO
        shares = floor(self._current_equity * self._position_pct / close)
        return Decimal(str(shares)) if shares > 0 else _ZERO

    # ------------------------------------------------------------------
    # Fill handler — the only place position state is updated
    # ------------------------------------------------------------------

    def on_fill(self, event: FillEvent) -> None:
        if event.strategy_id != self.strategy_id:
            return
        sym = event.symbol.upper()
        if sym not in self._symbols:
            return

        if event.side == Side.BUY:
            prev_qty = self._qty_held.get(sym, _ZERO)
            add_qty  = event.filled_qty
            new_qty  = prev_qty + add_qty
            if prev_qty > _ZERO:
                # Update weighted average entry price for add-ons
                prev_price = self._entry_price.get(sym, _ZERO)
                self._entry_price[sym] = (
                    prev_price * prev_qty + event.fill_price * add_qty
                ) / new_qty
            else:
                self._entry_price[sym]     = event.fill_price
                self._entry_bar_index[sym] = self._bar_counts[sym]
            self._qty_held[sym] = new_qty

        elif event.side == Side.SELL:
            remaining = self._qty_held.get(sym, _ZERO) - event.filled_qty
            if remaining <= _ZERO:
                self._qty_held.pop(sym, None)
                self._entry_price.pop(sym, None)
                self._entry_bar_index.pop(sym, None)
            else:
                self._qty_held[sym] = remaining

"""
Cross-Sectional Equity Momentum Strategy

Monthly rotation across a large-cap equity universe. Each month:
  1. Check market regime (SPY vs 200-day MA) — go to cash if bearish
  2. Rank universe by skip-month momentum (12M - 1M returns)
  3. Apply pre-filters: liquidity, gap risk, 200-day MA per stock
  4. Hold top N ranked names; exit names that fall below rank threshold
  5. Size each position by inverse volatility targeting

The risk gatekeeper has final say on every order. This module only proposes.
"""

from __future__ import annotations

from collections import deque
from datetime import date
from decimal import Decimal

from engine.events.types import BarEvent, FillEvent, IntentType, PortfolioSnapshotEvent, Side
from engine.strategy.base import AbstractStrategy, StrategyConfigLike

_LARGE_SELL_QTY = Decimal("999999")   # ShortSellingRule will cap to actual holding


class EquityMomentumStrategy(AbstractStrategy):
    """
    Cross-sectional momentum with market regime filter and volatility sizing.

    Config params (all optional with defaults):
        momentum_lookback        (int)   trading-day lookback for momentum, default 252
        skip_recent_days         (int)   skip-recent-month reversal filter, default 20
        min_lookback             (int)   min bars to rank a symbol, default 200
        top_n                    (int)   number of positions to hold, default 20
        rank_exit_buffer         (int)   exit if rank > top_n + buffer, default 5
        ma_period                (int)   MA period for regime + stock filter, default 200
        regime_symbol            (str)   symbol for market regime check, default SPY
        vol_target_pct           (float) target daily vol per position / equity, default 0.001
        min_volume_30d           (float) min 30-day avg daily volume, default 500000
        max_daily_move_pct       (float) reject stocks with 30-day max move above this, default 0.15
        rebalance_freq           (str)   monthly | weekly, default monthly
        initial_cash             (float) used for vol-sizing denominator, default 100000
        max_new_entries          (int)   turnover cap: max new buys per rebalance, default 10
    """

    def __init__(
        self,
        strategy_id: str,
        config: StrategyConfigLike,
        bus,
    ) -> None:
        super().__init__(strategy_id, config, bus)
        p = config.params
        self._lookback         = int(p.get("momentum_lookback", 252))
        self._skip             = int(p.get("skip_recent_days", 20))
        self._min_lookback     = int(p.get("min_lookback", 200))
        self._top_n            = int(p.get("top_n", 20))
        self._rank_exit_buffer = int(p.get("rank_exit_buffer", 5))
        self._ma_period        = int(p.get("ma_period", 200))
        self._regime_symbol    = str(p.get("regime_symbol", "SPY")).upper()
        self._vol_target_pct   = float(p.get("vol_target_pct", 0.001))
        self._min_volume       = float(p.get("min_volume_30d", 500_000))
        self._max_daily_move   = float(p.get("max_daily_move_pct", 0.15))
        self._rebalance_freq        = str(p.get("rebalance_freq", "monthly")).lower()
        self._initial_cash          = float(p.get("initial_cash", 100_000))
        self._max_new_entries       = int(p.get("max_new_entries", 10))
        self._circuit_breaker_dd    = Decimal(str(p.get("circuit_breaker_drawdown", "0.20")))
        self._circuit_breaker_active = False
        self._cb_hwm: Decimal        = Decimal("0")  # strategy-owned HWM; resets on CB reset
        self._cb_just_reset: bool    = False           # defers HWM baseline reset to on_snapshot
        self._pending_rebalance: bool = False          # set by on_bar, executed by on_snapshot

        # State — properly reset in on_start
        self._price_history:  dict[str, deque] = {}
        self._volume_history: dict[str, deque] = {}
        self._latest_bars:    dict[str, BarEvent] = {}
        self._holdings:       set[str] = set()
        self._qty_held:       dict[str, Decimal] = {}
        self._last_rebalance_date: date | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self, universe: list[str]) -> None:
        super().on_start(universe)
        buf = self._skip + self._lookback + 2
        self._price_history  = {s: deque(maxlen=buf) for s in self._symbols}
        self._volume_history = {s: deque(maxlen=31)  for s in self._symbols}
        self._latest_bars    = {}
        self._holdings       = set()
        self._qty_held       = {}
        self._last_rebalance_date = None
        self._circuit_breaker_active = False
        self._cb_hwm = Decimal("0")
        self._cb_just_reset = False
        self._pending_rebalance = False

    def on_snapshot(self, event: PortfolioSnapshotEvent) -> None:
        # On the day the CB resets, defer the HWM baseline to the current equity
        # so the CB doesn't immediately re-fire against the pre-crash peak.
        if self._cb_just_reset:
            self._cb_hwm = event.equity
            self._cb_just_reset = False
            return

        if not self._circuit_breaker_active:
            if event.equity > self._cb_hwm:
                self._cb_hwm = event.equity
            if self._cb_hwm > Decimal("0"):
                dd = (self._cb_hwm - event.equity) / self._cb_hwm
                if dd >= self._circuit_breaker_dd and self._holdings:
                    self._circuit_breaker_active = True
                    self._pending_rebalance = False
                    self._exit_all(f"circuit breaker: drawdown {dd:.2%} >= {self._circuit_breaker_dd:.2%}")

        if self._pending_rebalance:
            self._pending_rebalance = False
            self._rebalance()

    def on_stop(self) -> None:
        super().on_stop()
        self._holdings.clear()

    # ------------------------------------------------------------------
    # Bar handler
    # ------------------------------------------------------------------

    def on_bar(self, event: BarEvent) -> None:
        self._record_bar(event)
        sym = event.symbol.upper()
        if sym not in self._symbols:
            return

        self._price_history[sym].append(float(event.close))
        self._volume_history[sym].append(float(event.volume))
        self._latest_bars[sym] = event

        # Only the regime symbol (SPY) triggers the rebalance clock
        if sym != self._regime_symbol:
            return

        bar_date = event.timestamp.date()
        if not self._is_new_period(bar_date):
            return

        self._last_rebalance_date = bar_date
        self._pending_rebalance = True

    # ------------------------------------------------------------------
    # Rebalance logic
    # ------------------------------------------------------------------

    def _rebalance(self) -> None:
        # ── 0. Circuit breaker ────────────────────────────────────────
        # Reset when regime turns bullish again (SPY recrosses 200 MA).
        # We set _cb_just_reset so on_snapshot re-anchors the HWM to current equity,
        # preventing an immediate re-fire against the pre-crash all-time peak.
        if self._circuit_breaker_active:
            if self._regime_ok():
                self._circuit_breaker_active = False
                self._cb_just_reset = True
            else:
                # CB still active — retry any positions not yet fully liquidated
                # (e.g. prior _exit_all intents that only partially filled).
                if self._holdings:
                    self._exit_all("circuit breaker retry: residual exposure")
                return

        # ── 1. Market regime filter ───────────────────────────────────
        if not self._regime_ok():
            if self._holdings:
                self._exit_all("regime: SPY below 200-day MA — moving to cash")
            return

        # ── 2. Rank universe ─────────────────────────────────────────
        ranked = self._rank_universe()
        if not ranked:
            return

        top_symbols    = {sym for sym, _ in ranked[:self._top_n]}
        exit_threshold = self._top_n + self._rank_exit_buffer

        # ── 3. Exits ──────────────────────────────────────────────
        for sym in list(self._holdings):
            rank = next((i for i, (s, _) in enumerate(ranked) if s == sym), len(ranked))
            if rank >= exit_threshold or sym not in top_symbols or not self._above_ma(sym):
                qty = self._qty_held.get(sym, _LARGE_SELL_QTY)
                if qty <= Decimal("0"):
                    continue
                self._emit_for(sym, Side.SELL, qty, notes=f"exit rank={rank + 1}")
                # Do NOT discard from _holdings here. The SELL intent may be resized,
                # cancelled, or rejected by the broker. _holdings is updated in on_fill()
                # once the SELL actually confirms. Until then we must not lose track of
                # the real position.

        # ── 4. New entries (turnover cap) ──────────────────────────────
        new_count = 0
        for sym, mom in ranked[:self._top_n]:
            if new_count >= self._max_new_entries:
                break
            if sym in self._holdings:
                continue
            if not self._above_ma(sym):
                continue
            qty = self._vol_sized_qty(sym)
            self._emit_for(sym, Side.BUY, qty, notes=f"entry mom={mom:+.2%}")
            # Do NOT add to _holdings here. The BUY may be cancelled, dropped, or
            # rejected. _holdings is updated in on_fill() once the BUY confirms.
            new_count += 1

    # ------------------------------------------------------------------
    # Filters and signals
    # ------------------------------------------------------------------

    def _is_new_period(self, bar_date: date) -> bool:
        if self._last_rebalance_date is None:
            return True
        if self._rebalance_freq == "weekly":
            return (
                bar_date.isocalendar()[1] != self._last_rebalance_date.isocalendar()[1]
                or bar_date.year != self._last_rebalance_date.year
            )
        # Monthly (default)
        return (
            bar_date.month != self._last_rebalance_date.month
            or bar_date.year != self._last_rebalance_date.year
        )

    def _regime_ok(self) -> bool:
        prices = list(self._price_history.get(self._regime_symbol, []))
        if len(prices) < self._ma_period:
            return False
            
        current_price = prices[-1]
        
        # Calculate Slow MA (200-day)
        ma_slow = sum(prices[-self._ma_period:]) / self._ma_period
        
        # Calculate Fast MA (50-day)
        fast_period = 50
        ma_fast = sum(prices[-fast_period:]) / fast_period
        
        # Bull Market: Price is above the 200-day MA
        is_bull_market = current_price > ma_slow
        
        # Recovery Mode: Price is below 200-day, but has surged above the 50-day MA
        is_recovery = (current_price < ma_slow) and (current_price > ma_fast)
        
        return is_bull_market or is_recovery

    def _rank_universe(self) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for sym in self._symbols:
            if sym == self._regime_symbol:
                continue   # regime symbol is not traded
            mom = self._momentum(sym)
            if mom is None:
                continue
            if not self._is_liquid(sym):
                continue
            if self._is_gappy(sym):
                continue
            scored.append((sym, mom))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def _momentum(self, sym: str) -> float | None:
        prices = list(self._price_history.get(sym, []))
        
        # Ensure we have enough bars for the full lookback window plus the skip offset
        if len(prices) < self._skip + 1 + self._lookback:
            return None

        # The 'current' price, respecting the skip-month reversal filter
        p_end = prices[-(self._skip + 1)] if self._skip > 0 else prices[-1]

        # Get historical prices for 3M (63 days), 6M (126 days), and 12M (252 days)
        # All anchored relative to p_end (skip offset applied consistently)
        p_3m  = prices[-(self._skip + 1 + 63)]
        p_6m  = prices[-(self._skip + 1 + 126)]
        p_12m = prices[-(self._skip + 1 + self._lookback)]
        
        if p_3m <= 0 or p_6m <= 0 or p_12m <= 0:
            return None
            
        # Calculate returns
        ret_3m  = (p_end - p_3m) / p_3m
        ret_6m  = (p_end - p_6m) / p_6m
        ret_12m = (p_end - p_12m) / p_12m
        
        # Return the blended average
        return (ret_3m + ret_6m + ret_12m) / 3.0

    def _above_ma(self, sym: str) -> bool:
        prices = list(self._price_history.get(sym, []))
        if len(prices) < self._ma_period:
            return False
        ma = sum(prices[-self._ma_period:]) / self._ma_period
        return prices[-1] > ma

    def _is_liquid(self, sym: str) -> bool:
        vols = list(self._volume_history.get(sym, []))
        if len(vols) < 20:
            return False
        return (sum(vols[-20:]) / 20) >= self._min_volume

    def _is_gappy(self, sym: str) -> bool:
        prices = list(self._price_history.get(sym, []))
        n = min(30, len(prices) - 1)
        if n < 5:
            return True
        max_move = max(
            abs(prices[-i] - prices[-i - 1]) / prices[-i - 1]
            for i in range(1, n + 1)
        )
        return max_move > self._max_daily_move

    def _vol_sized_qty(self, sym: str) -> Decimal:
        prices = list(self._price_history.get(sym, []))
        if len(prices) < 22 or sym not in self._latest_bars:
            return Decimal("1")
        # 20-day RMS daily return
        rets = [(prices[-i] - prices[-i - 1]) / prices[-i - 1] for i in range(1, 21)]
        daily_vol = (sum(r * r for r in rets) / 20) ** 0.5
        if daily_vol <= 0:
            return Decimal("1")
        price = float(self._latest_bars[sym].close)
        if price <= 0:
            return Decimal("1")
        # shares × price × daily_vol = vol_target_pct × initial_cash
        raw_qty = (self._vol_target_pct * self._initial_cash) / (price * daily_vol)
        return max(Decimal("1"), Decimal(str(int(raw_qty))))

    def _exit_all(self, reason: str) -> None:
        for sym in list(self._holdings):
            qty = self._qty_held.get(sym, _LARGE_SELL_QTY)
            if qty <= Decimal("0"):
                continue
            self._emit_for(sym, Side.SELL, qty, notes=reason)
        # Do NOT clear _holdings here. The SELLs may be resized or partially
        # rejected. Holdings are removed in on_fill() as each SELL confirms.

    def on_fill(self, event: FillEvent) -> None:
        """
        Reconcile _holdings and _qty_held from confirmed broker fills.

        Tracks actual filled quantity so a partial SELL leaves the symbol in
        _holdings until the position is fully closed.
        """
        if event.strategy_id != self.strategy_id:
            return
        sym = event.symbol.upper()
        if sym not in self._symbols:
            return
        if event.side == Side.BUY:
            self._qty_held[sym] = self._qty_held.get(sym, Decimal(0)) + event.filled_qty
            self._holdings.add(sym)
        elif event.side == Side.SELL:
            remaining = self._qty_held.get(sym, Decimal(0)) - event.filled_qty
            if remaining <= Decimal(0):
                self._holdings.discard(sym)
                self._qty_held.pop(sym, None)
            else:
                self._qty_held[sym] = remaining

    def _emit_for(self, symbol: str, side: Side, qty: Decimal, notes: str = "") -> None:
        if symbol not in self._latest_bars:
            return
        saved = self._current_bar
        self._current_bar = self._latest_bars[symbol]
        self._emit_intent(symbol, side, IntentType.MARKET, qty, notes=notes)
        self._current_bar = saved

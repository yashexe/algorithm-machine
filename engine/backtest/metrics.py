"""
Backtest performance metrics.

BacktestResult is the value object returned by BacktestRunner.run().
MetricsEngine.compute() is a pure function that builds it from event history.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from engine.events.types import FillEvent, PortfolioSnapshotEvent, Side

_TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# BacktestResult
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    """All outputs from a completed backtest run."""

    # Run identity
    run_id: str
    strategy_id: str
    start_date: date
    end_date: date
    initial_cash: Decimal

    # Return metrics
    total_return_pct: float
    annualized_return_pct: float
    volatility_annualized: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    # Risk metrics
    max_drawdown_pct: float
    max_drawdown_duration_days: int

    # Trade metrics
    win_rate: float
    profit_factor: float
    num_trades: int
    num_buy_fills: int
    num_sell_fills: int
    avg_days_held: float
    exposure_pct: float

    # Benchmark (None when no benchmark is configured)
    alpha: float | None
    beta: float | None
    correlation: float | None
    information_ratio: float | None

    # Time series
    equity_curve: pd.Series     # DatetimeIndex → float equity
    drawdown_curve: pd.Series   # DatetimeIndex → float drawdown pct
    daily_returns: pd.Series    # DatetimeIndex → float daily return

    # Trade log — one row per completed round-trip
    trade_log: pd.DataFrame

    # Benchmark equity curve (None when no benchmark configured)
    benchmark_equity: pd.Series | None = None

    # Position count per snapshot date; used to recompute exposure after WFO trimming
    position_count_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype="int64"))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        """Print formatted summary table to stdout."""
        n_days = (self.end_date - self.start_date).days
        final_equity = float(self.initial_cash) * (1 + self.total_return_pct)
        trading_days_count = len(self.equity_curve)

        print(f"\n=== Backtest Results: {self.strategy_id} ===")
        print(f"Period:          {self.start_date} → {self.end_date} ({trading_days_count} trading days)")
        print(f"Initial Capital: ${float(self.initial_cash):,.2f}")
        print(f"Final Equity:    ${final_equity:,.2f}")
        print()
        print("── Returns ──────────────────────────────")
        print(f"Total Return:        {self.total_return_pct:+.2%}")
        print(f"Annualized Return:   {self.annualized_return_pct:+.2%}")
        if self.alpha is not None:
            print(f"Alpha:               {self.alpha:+.2%}")
        print()
        print("── Risk ─────────────────────────────────")
        print(f"Volatility (ann.):   {self.volatility_annualized:.2%}")
        dd_days = (
            f"  (recovered in {self.max_drawdown_duration_days} days)"
            if self.max_drawdown_duration_days > 0
            else ""
        )
        print(f"Max Drawdown:        {-self.max_drawdown_pct:.2%}{dd_days}")
        print(f"Sharpe Ratio:        {self.sharpe_ratio:.2f}")
        print(f"Sortino Ratio:       {self.sortino_ratio:.2f}")
        if self.beta is not None:
            print(f"Beta:                {self.beta:.2f}")
        print()
        print("── Trades ───────────────────────────────")
        print(f"Total Trades:        {self.num_trades}")
        print(f"Buy Fills: {self.num_buy_fills}  Sell Fills: {self.num_sell_fills}")
        print(f"Win Rate:            {self.win_rate:.1%}")
        print(f"Profit Factor:       {self.profit_factor:.2f}")
        print(f"Avg Holding Period:  {self.avg_days_held:.1f} days")
        print(f"Time in Market:      {self.exposure_pct:.1%}")

    def save(self, output_dir: str | Path) -> None:
        """Write summary.json, equity_curve.csv, drawdown_curve.csv, and trade_log.csv."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        summary: dict[str, Any] = {
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "initial_cash": str(self.initial_cash),
            "total_return_pct": self.total_return_pct,
            "annualized_return_pct": self.annualized_return_pct,
            "volatility_annualized": self.volatility_annualized,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "calmar_ratio": self.calmar_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_duration_days": self.max_drawdown_duration_days,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "num_trades": self.num_trades,
            "num_buy_fills": self.num_buy_fills,
            "num_sell_fills": self.num_sell_fills,
            "avg_days_held": self.avg_days_held,
            "exposure_pct": self.exposure_pct,
            "alpha": self.alpha,
            "beta": self.beta,
            "correlation": self.correlation,
            "information_ratio": self.information_ratio,
        }
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )

        self.equity_curve.to_frame("equity").to_csv(out / "equity_curve.csv")
        self.drawdown_curve.to_frame("drawdown_pct").to_csv(out / "drawdown_curve.csv")
        if not self.trade_log.empty:
            self.trade_log.to_csv(out / "trade_log.csv", index=False)


# ---------------------------------------------------------------------------
# MetricsEngine
# ---------------------------------------------------------------------------

class MetricsEngine:
    """Pure computation: PortfolioSnapshotEvent + FillEvent history → BacktestResult."""

    @classmethod
    def compute(
        cls,
        *,
        snapshots: list[PortfolioSnapshotEvent],
        fills: list[FillEvent],
        initial_cash: Decimal,
        risk_free_rate: float,
        trading_day_count: int,
        run_id: str,
        strategy_id: str,
        start_date: date,
        end_date: date,
        benchmark_returns: pd.Series | None = None,
        bar_dates: list[date] | None = None,
    ) -> BacktestResult:
        if not snapshots:
            return _empty_result(
                run_id=run_id,
                strategy_id=strategy_id,
                start_date=start_date,
                end_date=end_date,
                initial_cash=initial_cash,
            )

        # -- Equity and drawdown curves --
        equity_curve, drawdown_curve, daily_returns, position_count_curve = _build_curves(
            snapshots, initial_cash, bar_dates
        )

        # -- Return metrics --
        n = len(daily_returns)
        total_return = float((equity_curve.iloc[-1] - float(initial_cash)) / float(initial_cash))

        if n >= 2:
            ann_return = (1 + total_return) ** (_TRADING_DAYS_PER_YEAR / n) - 1
            volatility = float(daily_returns.std()) * math.sqrt(_TRADING_DAYS_PER_YEAR)
        else:
            ann_return = total_return
            volatility = 0.0

        sharpe = _safe_div(ann_return - risk_free_rate, volatility)

        neg_returns = daily_returns[daily_returns < 0]
        downside_std = float(neg_returns.std()) * math.sqrt(_TRADING_DAYS_PER_YEAR) if len(neg_returns) >= 2 else 0.0
        sortino = _safe_div(ann_return - risk_free_rate, downside_std)

        max_dd = float(drawdown_curve.max())
        calmar = _safe_div(ann_return, max_dd)
        max_dd_duration = _max_drawdown_duration(drawdown_curve)

        # -- Exposure --
        exposure = float((position_count_curve > 0).mean()) if not position_count_curve.empty else 0.0

        # -- Trade metrics --
        trade_log = _build_trade_log(fills)
        num_trades = len(trade_log)

        if num_trades > 0:
            win_rate = float((trade_log["net_pnl"] > 0).mean())
            gross_wins = trade_log.loc[trade_log["net_pnl"] > 0, "net_pnl"].sum()
            gross_losses = trade_log.loc[trade_log["net_pnl"] < 0, "net_pnl"].abs().sum()
            profit_factor = _safe_div(float(gross_wins), float(gross_losses))
            avg_days_held = float(trade_log["holding_days"].mean())
        else:
            win_rate = 0.0
            profit_factor = 0.0
            avg_days_held = 0.0

        num_buy_fills = sum(1 for f in fills if f.side == Side.BUY)
        num_sell_fills = sum(1 for f in fills if f.side == Side.SELL)

        # -- Benchmark metrics --
        alpha: float | None = None
        beta: float | None = None
        correlation: float | None = None
        information_ratio: float | None = None
        benchmark_equity: pd.Series | None = None

        if benchmark_returns is not None and n >= 2:
            # Both series have DatetimeIndex of plain dates; align directly.
            dr_dates = daily_returns.copy()
            bm_dates = benchmark_returns.copy()

            aligned = pd.concat([dr_dates, bm_dates], axis=1).dropna()
            if len(aligned) >= 2:
                aligned.columns = ["strategy", "benchmark"]
                s_ret = aligned["strategy"]
                b_ret = aligned["benchmark"]

                correlation = float(s_ret.corr(b_ret))
                bench_var = float(b_ret.var())
                beta = _safe_div(float(s_ret.cov(b_ret)), bench_var)

                bm_n = len(aligned)
                bm_total = float((1 + b_ret).prod()) - 1
                bm_ann = (1 + bm_total) ** (_TRADING_DAYS_PER_YEAR / bm_n) - 1
                alpha = ann_return - risk_free_rate - beta * (bm_ann - risk_free_rate)

                tracking_diff = s_ret - b_ret
                tracking_error = float(tracking_diff.std()) * math.sqrt(_TRADING_DAYS_PER_YEAR)
                information_ratio = _safe_div(ann_return - bm_ann, tracking_error)

                benchmark_equity = (
                    (1 + bm_dates).cumprod() * float(initial_cash)
                ).rename("benchmark_equity")

        return BacktestResult(
            run_id=run_id,
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
            initial_cash=initial_cash,
            total_return_pct=total_return,
            annualized_return_pct=ann_return,
            volatility_annualized=volatility,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown_pct=max_dd,
            max_drawdown_duration_days=max_dd_duration,
            win_rate=win_rate,
            profit_factor=profit_factor,
            num_trades=num_trades,
            num_buy_fills=num_buy_fills,
            num_sell_fills=num_sell_fills,
            avg_days_held=avg_days_held,
            exposure_pct=exposure,
            alpha=alpha,
            beta=beta,
            correlation=correlation,
            information_ratio=information_ratio,
            benchmark_equity=benchmark_equity,
            position_count_curve=position_count_curve,
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            daily_returns=daily_returns,
            trade_log=trade_log,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_curves(
    snapshots: list[PortfolioSnapshotEvent],
    initial_cash: Decimal,
    bar_dates: list[date] | None = None,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    sorted_snaps = sorted(snapshots, key=lambda s: s.timestamp)
    equities = [float(s.equity) for s in sorted_snaps]
    position_counts = [s.num_positions for s in sorted_snaps]

    if bar_dates is not None and len(bar_dates) == len(equities):
        index: pd.DatetimeIndex = pd.DatetimeIndex(
            [pd.Timestamp(d) for d in bar_dates]
        )
    else:
        # Fallback: snapshot wall-clock timestamps (not historical dates)
        index = pd.DatetimeIndex([s.timestamp for s in sorted_snaps])

    equity_curve = pd.Series(equities, index=index, name="equity")
    position_count_curve = pd.Series(position_counts, index=index, name="num_positions")
    running_peak = equity_curve.cummax()
    drawdown_curve = ((running_peak - equity_curve) / running_peak).rename("drawdown_pct")
    daily_returns = equity_curve.pct_change().dropna().rename("daily_return")

    return equity_curve, drawdown_curve, daily_returns, position_count_curve


def _build_trade_log(fills: list[FillEvent]) -> pd.DataFrame:
    # Each entry is [FillEvent, Decimal remaining_qty] — mutable inner list for FIFO matching.
    # Key is (strategy_id, symbol) so fills from different strategies on the same symbol
    # are never commingled into the same FIFO queue.
    open_positions: dict[tuple[str, str], list[list]] = {}
    rows: list[dict] = []
    _zero = Decimal(0)

    for fill in sorted(fills, key=lambda f: f.timestamp):
        sym = fill.symbol
        key = (fill.strategy_id, sym)
        if fill.side == Side.BUY:
            open_positions.setdefault(key, []).append([fill, fill.filled_qty])
        else:
            queue = open_positions.get(key, [])
            remaining_sell = fill.filled_qty

            while remaining_sell > _zero and queue:
                entry_fill, entry_remaining = queue[0]
                matched_qty = min(remaining_sell, entry_remaining)

                holding = (
                    fill.fill_bar.timestamp.date() - entry_fill.fill_bar.timestamp.date()
                ).days
                gross_pnl = float(matched_qty * (fill.fill_price - entry_fill.fill_price))
                # Prorate commissions by the fraction of each lot consumed.
                entry_commission = float(
                    entry_fill.commission * matched_qty / entry_fill.filled_qty
                )
                exit_commission = float(
                    fill.commission * matched_qty / fill.filled_qty
                )
                commission = entry_commission + exit_commission

                rows.append({
                    "symbol": sym,
                    "entry_date": entry_fill.fill_bar.timestamp.date(),
                    "entry_price": float(entry_fill.fill_price),
                    "entry_qty": float(matched_qty),
                    "exit_date": fill.fill_bar.timestamp.date(),
                    "exit_price": float(fill.fill_price),
                    "exit_qty": float(matched_qty),
                    "holding_days": holding,
                    "gross_pnl": gross_pnl,
                    "commission": commission,
                    "net_pnl": gross_pnl - commission,
                })

                remaining_sell -= matched_qty
                queue[0][1] -= matched_qty
                if queue[0][1] <= _zero:
                    queue.pop(0)

    if not rows:
        return pd.DataFrame(columns=[
            "symbol", "entry_date", "entry_price", "entry_qty",
            "exit_date", "exit_price", "exit_qty",
            "holding_days", "gross_pnl", "commission", "net_pnl",
        ])
    return pd.DataFrame(rows)


def _max_drawdown_duration(drawdown_series: pd.Series) -> int:
    """Longest consecutive run of trading days with drawdown > 0."""
    max_run = 0
    current = 0
    for val in drawdown_series:
        if val > 0:
            current += 1
            if current > max_run:
                max_run = current
        else:
            current = 0
    return max_run


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator != 0 and not math.isnan(denominator) else default


def _empty_result(
    *,
    run_id: str,
    strategy_id: str,
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
) -> BacktestResult:
    empty_series = pd.Series(dtype=float)
    empty_df = pd.DataFrame(columns=[
        "symbol", "entry_date", "entry_price", "entry_qty",
        "exit_date", "exit_price", "exit_qty",
        "holding_days", "gross_pnl", "commission", "net_pnl",
    ])
    return BacktestResult(
        run_id=run_id,
        strategy_id=strategy_id,
        start_date=start_date,
        end_date=end_date,
        initial_cash=initial_cash,
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_annualized=0.0,
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        max_drawdown_pct=0.0,
        max_drawdown_duration_days=0,
        win_rate=0.0,
        profit_factor=0.0,
        num_trades=0,
        num_buy_fills=0,
        num_sell_fills=0,
        avg_days_held=0.0,
        exposure_pct=0.0,
            alpha=None,
            beta=None,
            correlation=None,
            information_ratio=None,
            position_count_curve=pd.Series(dtype="int64", name="num_positions"),
            equity_curve=empty_series,
            drawdown_curve=empty_series,
            daily_returns=empty_series,
        trade_log=empty_df,
    )

"""
Four-panel backtest performance chart (requires matplotlib).

Panels:
  1. Equity curve (log scale) with optional benchmark
  2. Drawdown
  3. Monthly returns heatmap
  4. Rolling 252-day Sharpe ratio
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from engine.backtest.metrics import BacktestResult


def plot_results(result: "BacktestResult") -> None:
    """Display a 4-panel chart. Blocks until the window is closed."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(
        f"Backtest Results — {result.strategy_id}  "
        f"({result.start_date} → {result.end_date})",
        fontsize=13,
        fontweight="bold",
    )

    _panel_equity(axes[0, 0], result)
    _panel_drawdown(axes[0, 1], result)
    _panel_monthly_heatmap(axes[1, 0], result)
    _panel_rolling_sharpe(axes[1, 1], result)

    plt.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# Individual panels
# ---------------------------------------------------------------------------

def _panel_equity(ax, result: "BacktestResult") -> None:
    import matplotlib.dates as mdates

    eq = result.equity_curve
    ax.semilogy(eq.index, eq.values, color="steelblue", linewidth=1.5, label=result.strategy_id)

    bench = getattr(result, "benchmark_equity", None)
    if bench is not None and not bench.empty:
        ax.semilogy(bench.index, bench.values, color="gray", linewidth=1.0,
                    linestyle="--", label="Benchmark", alpha=0.8)
        ax.legend(fontsize=8)

    ann = result.annualized_return_pct
    ax.set_title(f"Equity Curve  (CAGR {ann:+.1%})", fontsize=10)
    ax.set_ylabel("Equity ($)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(axis="x", labelsize=8)
    ax.grid(True, alpha=0.3)


def _panel_drawdown(ax, result: "BacktestResult") -> None:
    import matplotlib.dates as mdates

    dd = result.drawdown_curve * 100
    ax.fill_between(dd.index, 0, -dd.values, color="firebrick", alpha=0.55)
    ax.plot(dd.index, -dd.values, color="firebrick", linewidth=0.8)
    ax.set_title(f"Drawdown  (max {result.max_drawdown_pct:.1%})", fontsize=10)
    ax.set_ylabel("Drawdown (%)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(axis="x", labelsize=8)
    ax.grid(True, alpha=0.3)


def _panel_monthly_heatmap(ax, result: "BacktestResult") -> None:
    if result.equity_curve.empty or len(result.equity_curve) < 2:
        ax.set_title("Monthly Returns  (insufficient data)", fontsize=10)
        return

    monthly = result.equity_curve.resample("ME").last().pct_change().dropna() * 100
    pivot = _monthly_pivot(monthly)

    if pivot.empty:
        ax.set_title("Monthly Returns  (insufficient data)", fontsize=10)
        return

    import matplotlib.colors as mcolors
    import numpy as np

    vals = pivot.values
    finite = vals[~np.isnan(vals)]
    vmax = max(abs(finite.min()), abs(finite.max()), 1.0) if len(finite) else 5.0
    norm = mcolors.TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

    im = ax.imshow(vals, cmap="RdYlGn", norm=norm, aspect="auto")
    im.format_cursor_data = lambda data: ""   # suppress NaN hover overflow in matplotlib
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    ax.set_xticks(range(12))
    ax.set_xticklabels(months, fontsize=7)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([str(y) for y in pivot.index], fontsize=8)
    ax.set_title("Monthly Returns (%)", fontsize=10)

    for r in range(len(pivot.index)):
        for c in range(12):
            v = vals[r, c]
            if not np.isnan(v):
                ax.text(c, r, f"{v:.1f}", ha="center", va="center",
                        fontsize=6, color="black" if abs(v) < vmax * 0.6 else "white")


def _panel_rolling_sharpe(ax, result: "BacktestResult") -> None:
    import matplotlib.dates as mdates

    dr = result.daily_returns
    window = 252
    if len(dr) < window:
        ax.set_title("Rolling Sharpe  (insufficient data)", fontsize=10)
        return

    rolling_sharpe = (
        dr.rolling(window).mean() / dr.rolling(window).std() * math.sqrt(252)
    ).dropna()

    ax.plot(rolling_sharpe.index, rolling_sharpe.values, color="darkorange", linewidth=1.2)
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.fill_between(rolling_sharpe.index, 0, rolling_sharpe.values,
                    where=rolling_sharpe.values >= 0, color="green", alpha=0.2)
    ax.fill_between(rolling_sharpe.index, 0, rolling_sharpe.values,
                    where=rolling_sharpe.values < 0, color="red", alpha=0.2)
    ax.set_title("Rolling 252-day Sharpe Ratio", fontsize=10)
    ax.set_ylabel("Sharpe")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(axis="x", labelsize=8)
    ax.grid(True, alpha=0.3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _monthly_pivot(monthly: pd.Series) -> pd.DataFrame:
    df = monthly.to_frame("ret")
    df["year"] = df.index.year
    df["month"] = df.index.month
    pivot = df.pivot(index="year", columns="month", values="ret")
    pivot = pivot.reindex(columns=range(1, 13))
    return pivot

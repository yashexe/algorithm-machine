#!/usr/bin/env python3
"""
Walk-Forward Validation CLI for algorithm machine.

Partitions the full date range into rolling IS/OOS windows, optimizes
strategy parameters in-sample, and evaluates them blindly out-of-sample.
If the strategy beats SPY out-of-sample across multiple folds, the edge is
statistically proven, not hindsight-fitted.

Usage:
    .venv/bin/python run_walk_forward.py --config config/equity_pullback_swing.yaml
    .venv/bin/python run_walk_forward.py --config config/equity_momentum.yaml --is-years 3 --oos-years 1
    .venv/bin/python run_walk_forward.py --config config/equity_pullback_swing.yaml --plot
    .venv/bin/python run_walk_forward.py --config config/equity_pullback_swing.yaml \\
        --grid '{"entry_rsi": [30, 35], "exit_rsi": [55, 60], "position_pct": [0.10, 0.12]}'

Parameter grid:
    Defined in the config YAML under the top-level key `wfo_grid:`.
    Each key must match a strategy param name; values are lists to sweep.
    Override for a single run with --grid (JSON dict of lists).
    If no grid is defined anywhere, runs a single fixed-param WFO.

Runtime: ~5–15 minutes depending on grid size and cache warmth.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Algorithm Machine — walk-forward validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--config", metavar="PATH",
        default="config/equity_pullback_swing.yaml",
        help="base YAML config (default: config/equity_pullback_swing.yaml)",
    )
    p.add_argument(
        "--is-years", metavar="N", type=int, default=3, dest="is_years",
        help="in-sample window in calendar years (default: 3)",
    )
    p.add_argument(
        "--oos-years", metavar="N", type=int, default=1, dest="oos_years",
        help="out-of-sample window in calendar years (default: 1)",
    )
    p.add_argument(
        "--metric", metavar="NAME", default="sharpe_ratio",
        dest="optimize_metric",
        choices=["sharpe_ratio", "annualized_return_pct", "sortino_ratio", "calmar_ratio"],
        help=(
            "BacktestResult attribute to maximize on IS data "
            "(default: sharpe_ratio). "
            "Options: sharpe_ratio, annualized_return_pct, sortino_ratio, calmar_ratio"
        ),
    )
    p.add_argument(
        "--output-dir", metavar="DIR", dest="output_dir",
        help="directory for WFO output (default: results/wfo_<run_id>)",
    )
    p.add_argument(
        "--plot", action="store_true",
        help="display stitched OOS equity curve vs benchmark after run",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="enable DEBUG logging (shows per-combo progress)",
    )
    p.add_argument(
        "--grid", metavar="JSON", dest="grid", default=None,
        help=(
            "JSON override for the wfo_grid from config "
            "(e.g. '{\"entry_rsi\": [30, 35], \"position_pct\": [0.10, 0.12]}'). "
            "If omitted, uses the wfo_grid section from the config YAML. "
            "Each key must be a valid strategy param name."
        ),
    )
    p.add_argument(
        "--warmup-bars", metavar="N", type=int, default=400, dest="warmup_bars",
        help=(
            "calendar days of burn-in before each OOS window so the strategy "
            "can satisfy its lookback requirement "
            "(default: 400; covers a 200-day MA ≈ 290 calendar days + buffer)"
        ),
    )
    p.add_argument(
        "--workers", metavar="N", type=int, default=None, dest="max_workers",
        help=(
            "parallel worker processes for IS grid search "
            "(default: min(cpu_count, 6) to leave P-cores free for interactive use). "
            "Set to 1 to disable parallelism."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.is_years < 1:
        print(f"Error: --is-years must be >= 1, got {args.is_years}", file=sys.stderr)
        return 1
    if args.oos_years < 1:
        print(f"Error: --oos-years must be >= 1, got {args.oos_years}", file=sys.stderr)
        return 1
    if args.warmup_bars < 0:
        print(f"Error: --warmup-bars must be >= 0, got {args.warmup_bars}", file=sys.stderr)
        return 1

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=[logging.StreamHandler()],
    )

    import json
    import yaml
    from engine.config import load_config
    from engine.backtest.walk_forward import WalkForwardValidator

    # Load base config
    config = load_config(args.config)

    # Build parameter grid: YAML wfo_grid section, overrideable with --grid JSON
    if args.grid is not None:
        try:
            param_grid = json.loads(args.grid)
        except json.JSONDecodeError as exc:
            print(f"Error: --grid is not valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(param_grid, dict):
            print("Error: --grid must be a JSON object (dict of lists)", file=sys.stderr)
            return 1
    else:
        with open(args.config) as _f:
            param_grid = yaml.safe_load(_f).get("wfo_grid") or {}

    total_combos = 1
    for vals in param_grid.values():
        total_combos *= len(vals)

    print(f"\nWalk-Forward Validation")
    print(f"Config:     {args.config}")
    print(f"IS window:  {args.is_years} year(s)")
    print(f"OOS window: {args.oos_years} year(s)")
    print(f"Optimize:   {args.optimize_metric}")
    if param_grid:
        print(f"Grid size:  {total_combos} combinations per fold")
        for k, v in param_grid.items():
            print(f"  {k}: {v}")
    else:
        print("Grid:       (none — fixed-param WFO using config defaults)")
    print()

    # Set output directory
    import uuid
    run_id = uuid.uuid4().hex[:8]
    output_dir = Path(args.output_dir) if args.output_dir else Path("results") / f"wfo_{run_id}"

    # Run walk-forward
    validator = WalkForwardValidator(config)
    wf_result = validator.run(
        param_grid=param_grid,
        is_years=args.is_years,
        oos_years=args.oos_years,
        optimize_metric=args.optimize_metric,
        output_dir=output_dir,
        silent_folds=not args.verbose,
        warmup_bars=args.warmup_bars,
        max_workers=args.max_workers,
        config_file=args.config,
    )

    # Print results table
    wf_result.print_summary()
    print(f"\nResults saved to: {output_dir}/")

    # Plot if requested
    if args.plot:
        try:
            _plot_wfo(wf_result, output_dir)
        except ImportError as exc:
            print(f"\nCannot show plot: {exc}", file=sys.stderr)

    return 0


def _plot_wfo(wf_result, output_dir: Path) -> None:
    """Generate a 3-panel WFO plot: equity curve, fold Sharpes, param stability."""
    import matplotlib
    matplotlib.use("Agg")  # non-interactive for reliability; switch to TkAgg for GUI
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np

    fig, axes = plt.subplots(3, 1, figsize=(14, 12))
    fig.suptitle(
        f"Walk-Forward Validation — {wf_result.strategy_id}\n"
        f"IS={wf_result.is_years}yr / OOS={wf_result.oos_years}yr  "
        f"({wf_result.full_start} → {wf_result.full_end})",
        fontsize=13, fontweight="bold",
    )

    # ── Panel 1: Stitched OOS equity curve vs benchmark ────────────────
    ax1 = axes[0]
    if not wf_result.oos_equity_curve.empty:
        ax1.plot(
            wf_result.oos_equity_curve.index,
            wf_result.oos_equity_curve.values,
            label="OOS Strategy (stitched)", color="#4f81bd", linewidth=2,
        )
    if wf_result.benchmark_equity is not None and not wf_result.benchmark_equity.empty:
        ax1.plot(
            wf_result.benchmark_equity.index,
            wf_result.benchmark_equity.values,
            label="SPY Benchmark", color="#c0504d", linewidth=1.5, linestyle="--",
        )

    # Shade OOS periods
    for fold in wf_result.folds:
        ax1.axvspan(
            str(fold.oos_start), str(fold.oos_end),
            alpha=0.05, color="green",
        )
    ax1.set_ylabel("Portfolio Equity ($)")
    ax1.set_title("Stitched Out-of-Sample Equity Curve")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    # ── Panel 2: IS vs OOS Sharpe per fold ─────────────────────────────
    ax2 = axes[1]
    fold_labels = [f"Fold {f.fold_index}" for f in wf_result.folds]
    is_sharpes = [f.best_is_sharpe for f in wf_result.folds]
    oos_sharpes = [f.oos_result.sharpe_ratio for f in wf_result.folds]
    x = np.arange(len(fold_labels))
    width = 0.35
    ax2.bar(x - width / 2, is_sharpes, width, label="IS Sharpe (optimized)", color="#4f81bd", alpha=0.8)
    ax2.bar(x + width / 2, oos_sharpes, width, label="OOS Sharpe (blind)", color="#9bbb59", alpha=0.8)
    ax2.axhline(0, color="black", linewidth=0.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels(fold_labels)
    ax2.set_ylabel("Sharpe Ratio")
    ax2.set_title("In-Sample (Optimized) vs Out-of-Sample (Blind) Sharpe by Fold")
    ax2.legend()
    ax2.grid(True, alpha=0.3, axis="y")

    # ── Panel 3: Parameter stability across folds ───────────────────────
    ax3 = axes[2]
    if wf_result.best_params_by_fold:
        param_keys = list(wf_result.best_params_by_fold[0].keys())
        colors = ["#4f81bd", "#c0504d", "#9bbb59", "#8064a2"]
        for i, key in enumerate(param_keys[:4]):  # max 4 params on one chart
            vals = [p.get(key, 0) for p in wf_result.best_params_by_fold]
            ax3.plot(
                fold_labels, vals,
                label=key, color=colors[i % len(colors)],
                marker="o", linewidth=2,
            )
        ax3.set_ylabel("Parameter Value")
        ax3.set_title("Best Parameter Selection Stability Across Folds")
        ax3.legend(loc="upper left")
        ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    plot_path = output_dir / "wfo_results.png"
    plt.savefig(plot_path, dpi=150, bbox_inches="tight")
    print(f"\nPlot saved to: {plot_path}")
    import matplotlib
    if matplotlib.get_backend().lower() not in {"tkagg", "qt5agg", "qt4agg", "wxagg", "macosx"}:
        return
    try:
        plt.show()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())

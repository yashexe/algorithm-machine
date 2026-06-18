#!/usr/bin/env python3
"""
Command-line entrypoint for algorithm machine backtests.

Usage:
    .venv/bin/python run_backtest.py
    .venv/bin/python run_backtest.py --config config/default.yaml
    .venv/bin/python run_backtest.py --start 2022-01-01 --end 2023-12-31
    .venv/bin/python run_backtest.py --cash 50000 --plot
"""

from __future__ import annotations

import argparse
import sys
import logging
from datetime import date, timedelta
from decimal import Decimal


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Algorithm Machine — backtest runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--config", metavar="PATH",
        default="config/default.yaml",
        help="path to YAML config (default: config/default.yaml)",
    )
    p.add_argument(
        "--start", metavar="YYYY-MM-DD",
        type=date.fromisoformat,
        help="override backtest.start_date",
    )
    p.add_argument(
        "--end", metavar="YYYY-MM-DD",
        type=date.fromisoformat,
        help="override backtest.end_date",
    )
    p.add_argument(
        "--cash", metavar="AMOUNT",
        type=float,
        help="override backtest.initial_cash",
    )
    p.add_argument(
        "--params", metavar="KEY=VALUE,...", dest="params",
        help=(
            "override strategy params (comma-separated), e.g. "
            "vol_target_pct=0.005,top_n=15,ma_period=150"
        ),
    )
    p.add_argument(
        "--output-dir", metavar="DIR",
        dest="output_dir",
        help="override engine.output_dir",
    )
    p.add_argument(
        "--plot", action="store_true",
        help="display chart after run (requires matplotlib)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    from engine.config import load_config
    from engine.backtest import BacktestRunner

    config = load_config(args.config)

    if args.start:
        config.backtest.start_date = args.start
    if args.end:
        config.backtest.end_date = args.end
    if args.cash is not None:
        config.backtest.initial_cash = Decimal(str(args.cash))
    if args.output_dir:
        config.engine.output_dir = args.output_dir

    if args.params and config.strategies:
        for pair in args.params.split(","):
            k, v = pair.split("=", 1)
            k = k.strip(); v = v.strip()
            try:
                config.strategies[0].params[k] = int(v)
            except ValueError:
                try:
                    config.strategies[0].params[k] = float(v)
                except ValueError:
                    config.strategies[0].params[k] = v

    # Guard: cap end_date at holdout_start - 1 day when the holdout boundary is set.
    # Running through the holdout window contaminates the one-shot final evaluation.
    # To disable: remove holdout_start from the config, or use run_holdout.py for
    # the final evaluation instead.
    _log = logging.getLogger(__name__)
    if config.backtest.holdout_start is not None:
        holdout_start = config.backtest.holdout_start
        if config.backtest.end_date >= holdout_start:
            capped = holdout_start - timedelta(days=1)
            _log.warning(
                "Holdout guard: end_date capped %s → %s "
                "(holdout_start=%s is reserved — use run_holdout.py for final evaluation, "
                "or remove holdout_start from the config to disable this guard).",
                config.backtest.end_date, capped, holdout_start,
            )
            config.backtest.end_date = capped

    result = BacktestRunner(config).run()
    result.print_summary()

    if args.plot:
        try:
            from engine.backtest.plot import plot_results
            plot_results(result)
        except ImportError as exc:
            print(f"\nCannot show plot: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

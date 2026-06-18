#!/usr/bin/env python3
"""
Holdout evaluation for algorithm machine strategies.

Runs a single backtest over the reserved holdout window
(backtest.holdout_start → backtest.end_date) and reports metrics trimmed to
that window. The backtest fetches warmup_bars of prior data so the strategy
is fully warmed up by holdout_start, but those warmup bars are not included
in any reported metric.

WARNING: The holdout window is a one-shot final test. Run it at most once per
strategy version with frozen parameters. Running it iteratively to tune
converts the holdout into another OOS window and defeats its purpose.

Usage:
    # Explicit frozen params (recommended):
    .venv/bin/python run_holdout.py --config config/equity_momentum.yaml \\
        --params vol_target_pct=0.003,top_n=15,ma_period=150

    # Read modal (most-common) params from a WFO summary automatically:
    .venv/bin/python run_holdout.py --config config/equity_momentum.yaml \\
        --wfo-results results/wfo_abc123/wfo_summary.json

    # Use whatever params are already in the config:
    .venv/bin/python run_holdout.py --config config/equity_momentum.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Algorithm Machine — holdout evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--config", metavar="PATH",
        default="config/equity_momentum.yaml",
        help="base YAML config (must have backtest.holdout_start set)",
    )
    p.add_argument(
        "--params", metavar="KEY=VALUE,...",
        help=(
            "comma-separated frozen strategy params, e.g. "
            "vol_target_pct=0.003,top_n=15,ma_period=150"
        ),
    )
    p.add_argument(
        "--wfo-results", metavar="PATH", dest="wfo_results",
        help=(
            "path to wfo_summary.json; modal (most-common) params across folds "
            "are used as frozen params (overridden by --params if both given)"
        ),
    )
    p.add_argument(
        "--warmup-bars", metavar="N", type=int, default=400, dest="warmup_bars",
        help=(
            "calendar days of data fetched before holdout_start for strategy "
            "warm-up (not included in reported metrics; default: 400)"
        ),
    )
    p.add_argument(
        "--output-dir", metavar="DIR", dest="output_dir",
        help="save holdout results here (default: results/holdout_<date>)",
    )
    p.add_argument(
        "--plot", action="store_true",
        help="display equity chart after run (requires matplotlib)",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="enable DEBUG logging",
    )
    return p


def _modal_params(best_params_by_fold: list[dict]) -> dict:
    """Return the most-common param combination across WFO folds."""
    from collections import Counter
    counts = Counter(tuple(sorted(p.items())) for p in best_params_by_fold)
    return dict(counts.most_common(1)[0][0])


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=[logging.StreamHandler()],
    )

    from dateutil.relativedelta import relativedelta

    from engine.config import load_config
    from engine.backtest import BacktestRunner
    from engine.backtest.walk_forward import _trim_result_to_oos

    config = load_config(args.config)

    if config.backtest.holdout_start is None:
        print(
            "ERROR: backtest.holdout_start is not set in the config. "
            "Add holdout_start to the backtest section of your YAML.",
            file=sys.stderr,
        )
        return 1

    holdout_start: date = config.backtest.holdout_start
    holdout_end: date = config.backtest.end_date

    # ── Resolve frozen params ────────────────────────────────────────────────
    frozen_params: dict | None = None

    if args.wfo_results:
        import json
        wfo_path = Path(args.wfo_results)
        if not wfo_path.exists():
            print(f"ERROR: WFO results file not found: {wfo_path}", file=sys.stderr)
            return 1
        with wfo_path.open() as f:
            wfo_data = json.load(f)
        frozen_params = _modal_params(wfo_data["best_params_by_fold"])
        print(f"WFO modal params (from {wfo_path.name}): {frozen_params}")

    if args.params:
        if frozen_params is None:
            frozen_params = {}
        for pair in args.params.split(","):
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            try:
                frozen_params[k] = int(v)
            except ValueError:
                try:
                    frozen_params[k] = float(v)
                except ValueError:
                    frozen_params[k] = v

    if frozen_params and config.strategies:
        config.strategies[0].params.update(frozen_params)

    # ── Print holdout header ─────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  HOLDOUT EVALUATION — ONE-SHOT FINAL TEST")
    print("=" * 60)
    print(f"  Config:        {args.config}")
    print(f"  Holdout window: {holdout_start} → {holdout_end}")
    if frozen_params:
        print(f"  Frozen params: {frozen_params}")
    else:
        params = config.strategies[0].params if config.strategies else {}
        print(f"  Params (from config): {params}")
    print()
    print("  *** Do not re-run this to tune. ***")
    print("=" * 60)
    print()

    # ── Run backtest with warmup prepended ───────────────────────────────────
    data_start = holdout_start - relativedelta(days=args.warmup_bars)
    config.backtest.start_date = data_start
    config.backtest.end_date = holdout_end
    config.backtest.scored_start = holdout_start  # warmup-only before holdout window

    result = BacktestRunner(config).run()

    # ── Trim to holdout window only and recompute all metrics ────────────────
    result = _trim_result_to_oos(
        result,
        holdout_start,
        float(config.backtest.risk_free_rate),
    )

    result.print_summary()

    # ── Save results ─────────────────────────────────────────────────────────
    import uuid
    run_id = uuid.uuid4().hex[:8]
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path("results") / f"holdout_{holdout_start.isoformat()}_{run_id}"
    )
    result.save(output_dir)
    print(f"\nHoldout results saved to: {output_dir}/")

    if args.plot:
        try:
            from engine.backtest.plot import plot_results
            plot_results(result)
        except ImportError as exc:
            print(f"\nCannot show plot: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

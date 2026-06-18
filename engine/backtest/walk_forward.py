"""
Walk-Forward Validation Engine.

Implements the extension described in BACKTEST_HARNESS.md §8:

    Walk-forward partitions the date range into in-sample (IS) optimization
    windows and out-of-sample (OOS) test windows. The BacktestRunner is called
    independently for each IS/OOS fold. Strategy parameters are optimized on IS
    data (via grid search) and evaluated on OOS data.

The BacktestRunner already accepts arbitrary start_date / end_date pairs, so
walk-forward only requires a loop around runner.run().

Architecture compliance:
    - Does NOT modify any engine component.
    - Uses AppConfig deep-copy with mutated dates and params per fold.
    - Pre-fetches all data once into the Parquet cache; IS grid-search workers
      read from the warm cache so network I/O only happens once.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from dateutil.relativedelta import relativedelta

from engine.backtest.calendar import trading_days
from engine.backtest.metrics import BacktestResult
from engine.backtest.runner import BacktestRunner
from engine.config.schema import AppConfig
from engine.data import DataPipeline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class WalkForwardFold:
    """Results for one IS→OOS fold."""

    fold_index: int
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date

    # Best parameter set chosen on IS data
    best_params: dict[str, Any]
    best_is_sharpe: float

    # Out-of-sample result using those locked parameters
    oos_result: BacktestResult

    # Benchmark comparison for the OOS window (None if no benchmark in config)
    benchmark_ann_return: float | None = None
    excess_return: float | None = None

    # Number of IS grid-search combos that raised an exception (0 = clean run)
    is_combo_failures: int = 0


@dataclass
class WalkForwardResult:
    """
    Aggregate output from a complete walk-forward run.

    The primary proof of edge is oos_equity_curve — a continuous equity
    series stitched from successive OOS periods, each using parameters
    locked from the prior IS window.
    """

    run_id: str
    strategy_id: str
    full_start: date
    full_end: date
    is_years: int
    oos_years: int

    folds: list[WalkForwardFold] = field(default_factory=list)

    # Aggregate OOS metrics (computed from stitched equity curve)
    aggregate_oos_return: float = 0.0
    aggregate_oos_sharpe: float = 0.0
    aggregate_oos_max_dd: float = 0.0
    aggregate_oos_ann_return: float = 0.0

    # Aggregate benchmark annualized return (from stitched benchmark equity)
    aggregate_benchmark_ann_return: float | None = None

    # Best params selected in each fold — tracks parameter stability
    best_params_by_fold: list[dict[str, Any]] = field(default_factory=list)

    # Continuous OOS equity curve (stitched across folds)
    oos_equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    # Benchmark equity curve for comparison (uses same benchmark as config)
    benchmark_equity: pd.Series | None = None

    # Run provenance for reproducibility
    provenance: dict = field(default_factory=dict)

    def print_summary(self) -> None:
        """Print walk-forward results table to stdout."""
        print(f"\n=== Walk-Forward Validation: {self.strategy_id} ===")
        print(f"Period:    {self.full_start} → {self.full_end}")
        print(f"Folds:     {len(self.folds)} × (IS={self.is_years}yr / OOS={self.oos_years}yr)")
        print()
        print(
            f"{'Fold':<6} {'IS Period':<25} {'OOS Period':<25} "
            f"{'IS Sharpe':>10} {'OOS Ret':>9} {'OOS Sharpe':>11} "
            f"{'OOS MaxDD':>10} {'SPY Ret':>9} {'Excess':>8} {'Best Params'}"
        )
        print("─" * 140)
        for fold in self.folds:
            oos = fold.oos_result
            param_str = ", ".join(f"{k}={v}" for k, v in fold.best_params.items())
            spy_str = (
                f"{fold.benchmark_ann_return:>+9.2%}"
                if fold.benchmark_ann_return is not None else f"{'N/A':>9}"
            )
            excess_str = (
                f"{fold.excess_return:>+8.2%}"
                if fold.excess_return is not None else f"{'N/A':>8}"
            )
            print(
                f"{fold.fold_index:<6} "
                f"{fold.is_start!s}→{fold.is_end!s:<12} "
                f"{fold.oos_start!s}→{fold.oos_end!s:<12} "
                f"{fold.best_is_sharpe:>10.2f} "
                f"{oos.annualized_return_pct:>+9.2%} "
                f"{oos.sharpe_ratio:>11.2f} "
                f"{-oos.max_drawdown_pct:>10.2%} "
                f"{spy_str} {excess_str} "
                f"{param_str}"
            )
        print("─" * 140)
        agg_spy_str = (
            f"{self.aggregate_benchmark_ann_return:>+9.2%}"
            if self.aggregate_benchmark_ann_return is not None else f"{'N/A':>9}"
        )
        agg_excess = (
            (self.aggregate_oos_ann_return - self.aggregate_benchmark_ann_return)
            if self.aggregate_benchmark_ann_return is not None else None
        )
        agg_excess_str = (
            f"{agg_excess:>+8.2%}" if agg_excess is not None else f"{'N/A':>8}"
        )
        print(
            f"{'AGGREGATE OOS':<6} {'':25} {'':25} "
            f"{'':>10} "
            f"{self.aggregate_oos_ann_return:>+9.2%} "
            f"{self.aggregate_oos_sharpe:>11.2f} "
            f"{-self.aggregate_oos_max_dd:>10.2%} "
            f"{agg_spy_str} {agg_excess_str}"
        )
        total_failures = sum(f.is_combo_failures for f in self.folds)
        if total_failures > 0:
            print(
                f"\n  WARNING: {total_failures} IS grid-search combo(s) failed across "
                f"{sum(1 for f in self.folds if f.is_combo_failures > 0)} fold(s) "
                f"(excluded from parameter selection; see logs for details)"
            )

    def save(self, output_dir: str | Path) -> None:
        """Write wfo_summary.json and per-fold subdirectories."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        agg_excess = (
            self.aggregate_oos_ann_return - self.aggregate_benchmark_ann_return
            if self.aggregate_benchmark_ann_return is not None else None
        )
        summary: dict[str, Any] = {
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "full_start": self.full_start.isoformat(),
            "full_end": self.full_end.isoformat(),
            "is_years": self.is_years,
            "oos_years": self.oos_years,
            "num_folds": len(self.folds),
            "aggregate_oos_ann_return": self.aggregate_oos_ann_return,
            "aggregate_oos_sharpe": self.aggregate_oos_sharpe,
            "aggregate_oos_max_dd": self.aggregate_oos_max_dd,
            "aggregate_oos_total_return": self.aggregate_oos_return,
            "aggregate_benchmark_ann_return": self.aggregate_benchmark_ann_return,
            "aggregate_excess_return": agg_excess,
            "best_params_by_fold": self.best_params_by_fold,
            "provenance": self.provenance,
            "folds": [
                {
                    "fold_index": f.fold_index,
                    "is_start": f.is_start.isoformat(),
                    "is_end": f.is_end.isoformat(),
                    "oos_start": f.oos_start.isoformat(),
                    "oos_end": f.oos_end.isoformat(),
                    "best_is_sharpe": f.best_is_sharpe,
                    "best_params": f.best_params,
                    "oos_ann_return": f.oos_result.annualized_return_pct,
                    "oos_sharpe": f.oos_result.sharpe_ratio,
                    "oos_max_dd": f.oos_result.max_drawdown_pct,
                    "oos_num_trades": f.oos_result.num_trades,
                    "oos_buy_fills": f.oos_result.num_buy_fills,
                    "oos_sell_fills": f.oos_result.num_sell_fills,
                    "oos_benchmark_ann_return": f.benchmark_ann_return,
                    "oos_excess_return": f.excess_return,
                    "is_combo_failures": f.is_combo_failures,
                }
                for f in self.folds
            ],
        }

        (out / "wfo_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )

        if not self.oos_equity_curve.empty:
            self.oos_equity_curve.to_frame("equity").to_csv(out / "oos_equity_curve.csv")

        logger.info("WFO results saved to %s", out)


# ---------------------------------------------------------------------------
# Walk-Forward Validator
# ---------------------------------------------------------------------------

class WalkForwardValidator:
    """
    Runs a rolling walk-forward optimization over an AppConfig.

    Usage::

        config = load_config("config/equity_momentum.yaml")
        param_grid = {
            "vol_target_pct": [0.001, 0.002, 0.003, 0.005],
            "top_n":          [10, 15, 20],
            "ma_period":      [150, 200, 250],
        }
        wf = WalkForwardValidator(config)
        result = wf.run(
            param_grid=param_grid,
            is_years=3,
            oos_years=1,
            optimize_metric="sharpe_ratio",
        )
        result.print_summary()

    Parameters
    ----------
    config : AppConfig
        Base configuration. Dates and strategy params are overridden per fold.
        The config itself is not mutated — deep copies are used for each run.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def run(
        self,
        param_grid: dict[str, list[Any]],
        is_years: int = 3,
        oos_years: int = 1,
        optimize_metric: str = "sharpe_ratio",
        output_dir: str | Path | None = None,
        silent_folds: bool = True,
        warmup_bars: int = 400,
        max_workers: int | None = None,
        config_file: str | None = None,
    ) -> WalkForwardResult:
        """
        Execute the walk-forward loop.

        Parameters
        ----------
        param_grid : dict[str, list[Any]]
            Maps strategy param name → list of candidate values.
            All combinations (Cartesian product) are evaluated on each IS window.
        is_years : int
            In-sample optimization window in calendar years.
        oos_years : int
            Out-of-sample test window in calendar years.
        optimize_metric : str
            Attribute name on BacktestResult to maximize during IS optimization.
            Default "sharpe_ratio". Use "annualized_return_pct" for return-only opt.
        output_dir : str | Path | None
            If provided, saves per-fold results under output_dir/fold_N/.
        silent_folds : bool
            If True, suppresses INFO logging during individual fold runs to
            keep console output readable.
        warmup_bars : int
            Number of calendar days to prepend before each OOS window so the
            strategy can satisfy its lookback warm-up requirement.
            The extra bars are burn-in only: equity stitching trims each OOS
            equity curve to start from the true oos_start date.
            Default 400 (covers ma_period=250 ≈ 362 calendar days + buffer).
        max_workers : int | None
            Number of parallel processes for the IS grid search.
            None (default) auto-detects via os.cpu_count().
            Set to 1 to disable parallelism (useful for debugging).
        """
        _VALID_METRICS = {"sharpe_ratio", "annualized_return_pct", "sortino_ratio", "calmar_ratio"}
        if optimize_metric not in _VALID_METRICS:
            raise ValueError(
                f"optimize_metric {optimize_metric!r} is not a valid BacktestResult attribute; "
                f"choose from {sorted(_VALID_METRICS)}"
            )

        import uuid
        run_id = uuid.uuid4().hex
        cfg = self._config

        full_start: date = cfg.backtest.start_date
        full_end: date = cfg.backtest.end_date

        # Enforce holdout boundary — WFO must never touch holdout data
        if cfg.backtest.holdout_start is not None:
            holdout_start = cfg.backtest.holdout_start
            if full_end >= holdout_start:
                capped = holdout_start - relativedelta(days=1)
                logger.warning(
                    "WFO holdout guard: full_end capped %s → %s "
                    "(holdout_start=%s is reserved — use run_holdout.py for final evaluation)",
                    full_end, capped, holdout_start,
                )
                full_end = capped

        strategy_id = cfg.strategies[0].id if cfg.strategies else "unknown"

        logger.info(
            "WFO start — strategy=%s period=%s→%s IS=%dyr OOS=%dyr grid=%d combos",
            strategy_id, full_start, full_end, is_years, oos_years,
            _grid_size(param_grid),
        )

        # Build fold date ranges
        folds_dates = _build_fold_dates(full_start, full_end, is_years, oos_years)
        if not folds_dates:
            raise ValueError(
                f"No folds fit in {full_start}→{full_end} with IS={is_years}yr OOS={oos_years}yr. "
                f"Need at least {is_years + oos_years} years of data."
            )

        logger.info("WFO folds: %d", len(folds_dates))

        # Build all param combinations once
        param_combos = _expand_grid(param_grid)
        logger.info("WFO param combinations per fold: %d", len(param_combos))

        # Effective worker count: cap at grid size; default to 6 to leave P-cores
        # free for interactive use (stays responsive while WFO runs in background).
        n_workers = min(
            max_workers if max_workers is not None else min(os.cpu_count() or 1, 6),
            len(param_combos),
        )
        logger.info("WFO IS grid search workers: %d", n_workers)

        # Warm the Parquet cache once for the full date range (including warmup
        # headroom) so IS grid-search workers never need to hit the network.
        _prefetch_bars(cfg, full_start, full_end, warmup_bars)

        # Mute runner logging during grid search if requested
        original_log_level = cfg.engine.log_level
        if silent_folds:
            _suppress_runner_logging()

        completed_folds: list[WalkForwardFold] = []

        # Create the worker pool once and reuse it across all folds to avoid
        # repeated process-spawn overhead (macOS uses 'spawn', not 'fork').
        _pool = ProcessPoolExecutor(max_workers=n_workers) if n_workers > 1 else None

        for fold_idx, (is_start, is_end, oos_start, oos_end) in enumerate(folds_dates):
            logger.info(
                "Fold %d/%d  IS=%s→%s  OOS=%s→%s  (searching %d combos)",
                fold_idx + 1, len(folds_dates),
                is_start, is_end, oos_start, oos_end,
                len(param_combos),
            )

            # ── IS grid search ──────────────────────────────────────────
            best_params, best_is_sharpe, is_failures = _optimize_is(
                base_config=cfg,
                param_combos=param_combos,
                is_start=is_start,
                is_end=is_end,
                optimize_metric=optimize_metric,
                fold_idx=fold_idx,
                output_dir=output_dir,
                executor=_pool,
                n_workers=n_workers,
                warmup_bars=warmup_bars,
            )

            logger.info(
                "Fold %d: best IS %s=%.3f  params=%s",
                fold_idx + 1, optimize_metric, best_is_sharpe, best_params,
            )

            # ── OOS evaluation with locked params ───────────────────────
            # Extend OOS data fetch backwards by warmup_bars calendar days so
            # the strategy can satisfy its lookback requirement. The equity
            # stitcher trims to oos_start so the burn-in bars don't skew metrics.
            oos_data_start = oos_start - relativedelta(days=warmup_bars)
            oos_cfg = _build_fold_config(cfg, best_params, oos_data_start, oos_end)
            oos_cfg.backtest.scored_start = oos_start  # warmup-only before OOS window
            if silent_folds:
                oos_cfg.engine.log_level = "WARNING"

            oos_result = BacktestRunner(oos_cfg).run(save_artifacts=False, write_log_files=False)

            # Trim equity curve to the true OOS window and recompute all metrics
            oos_result = _trim_result_to_window(
                oos_result, oos_start, float(cfg.backtest.risk_free_rate)
            )

            # Compute per-fold benchmark metrics for the fold table
            bench_ann: float | None = None
            if oos_result.benchmark_equity is not None and len(oos_result.benchmark_equity) >= 2:
                bm = oos_result.benchmark_equity
                bm_rets = bm.pct_change().dropna()
                bm_n = len(bm_rets)
                bm_total = float((bm.iloc[-1] - bm.iloc[0]) / bm.iloc[0])
                bench_ann = (1 + bm_total) ** (252 / bm_n) - 1 if bm_n >= 2 else bm_total
            fold_excess = (
                oos_result.annualized_return_pct - bench_ann
                if bench_ann is not None else None
            )

            if output_dir:
                fold_out = Path(output_dir) / f"fold_{fold_idx + 1}" / "oos"
                oos_result.save(fold_out)

            logger.info(
                "Fold %d OOS: ann_ret=%+.2f%% sharpe=%.2f max_dd=%.2f%%",
                fold_idx + 1,
                oos_result.annualized_return_pct * 100,
                oos_result.sharpe_ratio,
                oos_result.max_drawdown_pct * 100,
            )

            completed_folds.append(
                WalkForwardFold(
                    fold_index=fold_idx + 1,
                    is_start=is_start,
                    is_end=is_end,
                    oos_start=oos_start,
                    oos_end=oos_end,
                    best_params=best_params,
                    best_is_sharpe=best_is_sharpe,
                    oos_result=oos_result,
                    benchmark_ann_return=bench_ann,
                    excess_return=fold_excess,
                    is_combo_failures=is_failures,
                )
            )

        if _pool is not None:
            _pool.shutdown(wait=False)

        if silent_folds:
            _restore_runner_logging(original_log_level)

        # ── Stitch OOS equity curves ────────────────────────────────────
        oos_equity = _stitch_equity_curves(
            completed_folds,
            float(cfg.backtest.initial_cash),
        )

        # ── Aggregate OOS metrics ───────────────────────────────────────
        agg = _aggregate_oos_metrics(oos_equity, float(cfg.backtest.risk_free_rate))

        # ── Build benchmark comparison equity ───────────────────────────
        bench_equity = _stitch_benchmark(completed_folds, float(cfg.backtest.initial_cash))

        agg_bench_ann: float | None = None
        if bench_equity is not None and len(bench_equity) >= 2:
            bm_rets = bench_equity.pct_change().dropna()
            bm_n = len(bm_rets)
            bm_total = float((bench_equity.iloc[-1] - bench_equity.iloc[0]) / bench_equity.iloc[0])
            agg_bench_ann = (1 + bm_total) ** (252 / bm_n) - 1 if bm_n >= 2 else bm_total

        wf_result = WalkForwardResult(
            run_id=run_id,
            strategy_id=strategy_id,
            full_start=full_start,
            full_end=full_end,
            is_years=is_years,
            oos_years=oos_years,
            folds=completed_folds,
            aggregate_oos_return=agg["total_return"],
            aggregate_oos_ann_return=agg["ann_return"],
            aggregate_oos_sharpe=agg["sharpe"],
            aggregate_oos_max_dd=agg["max_dd"],
            aggregate_benchmark_ann_return=agg_bench_ann,
            best_params_by_fold=[f.best_params for f in completed_folds],
            oos_equity_curve=oos_equity,
            benchmark_equity=bench_equity,
        )
        wf_result.provenance = _run_manifest(cfg, config_file=config_file)

        if output_dir:
            wf_result.save(Path(output_dir))

        return wf_result


# ---------------------------------------------------------------------------
# Helpers — fold date generation
# ---------------------------------------------------------------------------

def _build_fold_dates(
    full_start: date,
    full_end: date,
    is_years: int,
    oos_years: int,
) -> list[tuple[date, date, date, date]]:
    """
    Build rolling IS/OOS fold boundaries.

    Each fold advances by oos_years (rolling window, not expanding).
    Returns list of (is_start, is_end, oos_start, oos_end).
    """
    folds: list[tuple[date, date, date, date]] = []
    fold_start = full_start
    while True:
        is_end = fold_start + relativedelta(years=is_years) - relativedelta(days=1)
        oos_start = is_end + relativedelta(days=1)
        oos_end = oos_start + relativedelta(years=oos_years) - relativedelta(days=1)

        if oos_end > full_end:
            break

        folds.append((fold_start, is_end, oos_start, oos_end))
        # Rolling: advance by oos_years
        fold_start = fold_start + relativedelta(years=oos_years)

    return folds


# ---------------------------------------------------------------------------
# Helpers — data pre-fetch
# ---------------------------------------------------------------------------

def _prefetch_bars(
    cfg: AppConfig,
    full_start: date,
    full_end: date,
    warmup_bars: int,
) -> None:
    """
    Warm the Parquet cache for the full WFO date range before the fold loop.

    Subsequent BacktestRunner.run() calls (both IS grid search and OOS
    evaluation) find cache hits and skip network I/O entirely. The prefetch
    window extends warmup_bars calendar days before full_start so OOS runners
    that reach back for a warm-up period also find cache hits.
    """
    prefetch_start = full_start - relativedelta(days=warmup_bars)
    data_cfg = cfg.data
    pipeline = DataPipeline.build(
        cache_dir=data_cfg.cache_dir,
        batch_size=data_cfg.batch_size,
        staleness_hours=data_cfg.cache_staleness_hours,
        bar_close_utc_hour=data_cfg.bar_close_utc_hour,
        adjusted=data_cfg.adjusted,
    )
    symbols = list(cfg.universe.symbols)
    if cfg.universe.benchmark and cfg.universe.benchmark not in symbols:
        symbols.append(cfg.universe.benchmark)

    logger.info(
        "Pre-fetching bars: %d symbols  %s → %s",
        len(symbols), prefetch_start, full_end,
    )
    pipeline.get_bars(symbols, prefetch_start, full_end)
    logger.info("Pre-fetch complete.")


# ---------------------------------------------------------------------------
# Helpers — IS optimization
# ---------------------------------------------------------------------------

def _run_is_combo(
    args: tuple,
) -> tuple[dict[str, Any], float, str | None]:
    """
    Run one IS grid-search combo and return (params, metric_val, error_summary).

    Must be a module-level function so ProcessPoolExecutor can pickle it.
    Workers read bars from the warm Parquet cache populated by _prefetch_bars.

    On success error_summary is None. On failure metric_val is -inf and
    error_summary contains "<ExcType>: <message>" for the caller to aggregate.
    """
    (
        base_config, params, is_start, is_end, optimize_metric,
        fold_idx, combo_idx, output_dir, warmup_bars,
    ) = args
    data_start = is_start - relativedelta(days=warmup_bars)
    is_cfg = _build_fold_config(base_config, params, data_start, is_end)
    is_cfg.backtest.scored_start = is_start  # warmup-only before IS window
    is_cfg.engine.log_level = "WARNING"

    try:
        is_result = BacktestRunner(is_cfg).run(save_artifacts=False, write_log_files=False)
    except Exception as exc:  # noqa: BLE001
        return dict(params), float("-inf"), f"{type(exc).__name__}: {exc}"

    # Trim warmup bars from the IS result so the optimizer scores only the true
    # IS window, not the flat burn-in period.
    is_result = _trim_result_to_window(is_result, is_start, float(base_config.backtest.risk_free_rate))
    metric_val = float(getattr(is_result, optimize_metric))

    if output_dir is not None:
        combo_out = Path(output_dir) / f"fold_{fold_idx + 1}" / "is" / f"combo_{combo_idx}"
        is_result.save(combo_out)

    return dict(params), metric_val, None


def _optimize_is(
    base_config: AppConfig,
    param_combos: list[dict[str, Any]],
    is_start: date,
    is_end: date,
    optimize_metric: str,
    fold_idx: int,
    output_dir: str | Path | None,
    executor: "ProcessPoolExecutor | None" = None,
    n_workers: int = 1,
    warmup_bars: int = 400,
) -> tuple[dict[str, Any], float, int]:
    """
    Run all param combinations on the IS window; return (best_params, best_metric, failed_count).

    executor, if provided, is a persistent ProcessPoolExecutor created by the caller
    (avoids per-fold spawn overhead). Workers share the warm Parquet cache written by
    _prefetch_bars, so each worker only reads from disk — no redundant network I/O.

    Raises RuntimeError if every combo fails (prevents selecting a -inf "best" combo
    and silently producing a bad OOS fold).
    """
    worker_args = [
        (base_config, params, is_start, is_end, optimize_metric, fold_idx, combo_idx, output_dir, warmup_bars)
        for combo_idx, params in enumerate(param_combos)
    ]

    if executor is not None:
        chunksize = max(1, math.ceil(len(worker_args) / n_workers))
        raw = list(executor.map(_run_is_combo, worker_args, chunksize=chunksize))
    else:
        raw = [_run_is_combo(a) for a in worker_args]

    # Separate successes from failures
    failures = [(p, err) for p, _, err in raw if err is not None]
    n_failed = len(failures)
    n_total = len(raw)

    if n_failed == n_total:
        raise RuntimeError(
            f"Fold {fold_idx + 1}: all {n_total} IS grid-search combos failed. "
            f"First error: {failures[0][1]}"
        )
    if n_failed > 0:
        logger.warning(
            "Fold %d: %d/%d IS combos failed (excluded from selection). First error: %s",
            fold_idx + 1, n_failed, n_total, failures[0][1],
        )

    best_params: dict[str, Any] = {}
    best_metric: float = float("-inf")
    for params, metric_val, _ in raw:
        if metric_val > best_metric:
            best_metric = metric_val
            best_params = params

    return best_params, best_metric, n_failed


# ---------------------------------------------------------------------------
# Helpers — config building
# ---------------------------------------------------------------------------

def _build_fold_config(
    base: AppConfig,
    params: dict[str, Any],
    start: date,
    end: date,
) -> AppConfig:
    """
    Deep-copy AppConfig and apply fold-specific dates and strategy params.

    Only the strategy params listed in `params` are overridden. All other
    strategy params retain their base config values. This means the IS and OOS
    runs inherit the full equity_momentum.yaml configuration and only vary the
    parameters being optimized.
    """
    cfg = copy.deepcopy(base)
    cfg.backtest.start_date = start
    cfg.backtest.end_date = end
    # Assign a new run_id per fold so log files don't collide
    cfg.engine.run_id = None  # will be auto-generated by runner

    # Apply params to the first strategy (the one being optimized)
    if cfg.strategies:
        cfg.strategies[0].params.update(params)
        # vol-sizing needs initial_cash to match backtest cash
        if "initial_cash" not in params:
            cfg.strategies[0].params["initial_cash"] = float(cfg.backtest.initial_cash)

    return cfg


# ---------------------------------------------------------------------------
# Helpers — param grid
# ---------------------------------------------------------------------------

def _expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of all parameter lists → list of dicts."""
    if not grid:
        return [{}]
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _grid_size(grid: dict[str, list[Any]]) -> int:
    size = 1
    for v in grid.values():
        size *= len(v)
    return size


def _run_manifest(cfg: AppConfig, config_file: str | None = None) -> dict:
    """Capture run provenance for reproducibility."""
    import hashlib
    import platform
    import sys
    from datetime import datetime, timezone

    manifest: dict = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "platform": platform.system(),
    }
    try:
        import yfinance
        manifest["yfinance_version"] = yfinance.__version__
    except ImportError:
        manifest["yfinance_version"] = "unknown"

    if config_file:
        manifest["config_file"] = config_file
        try:
            content = Path(config_file).read_bytes()
            manifest["config_sha256"] = hashlib.sha256(content).hexdigest()[:16]
        except OSError:
            pass

    manifest["symbol_count"] = len(cfg.universe.symbols)
    manifest["benchmark"] = cfg.universe.benchmark
    manifest["backtest_start"] = str(cfg.backtest.start_date)
    manifest["backtest_end"] = str(cfg.backtest.end_date)
    manifest["initial_cash"] = str(cfg.backtest.initial_cash)
    manifest["slippage_pct"] = str(cfg.execution.slippage_pct)
    manifest["commission_model"] = cfg.execution.commission_model
    return manifest


# ---------------------------------------------------------------------------
# Helpers — equity stitching
# ---------------------------------------------------------------------------

def _stitch_equity_curves(
    folds: list[WalkForwardFold],
    initial_cash: float,
) -> pd.Series:
    """
    Concatenate OOS equity curves, scaling each fold so it starts where the
    previous fold ended. This produces a realistic continuous equity curve as
    if the strategy was deployed live with no parameter adjustments visible.
    """
    if not folds:
        return pd.Series(dtype=float, name="equity")

    segments: list[pd.Series] = []
    running_equity = initial_cash

    for fold in folds:
        curve = fold.oos_result.equity_curve
        if curve.empty:
            continue
        # Scale: fold starts at `running_equity`, not initial_cash
        scale = running_equity / float(curve.iloc[0])
        scaled = curve * scale
        segments.append(scaled)
        running_equity = float(scaled.iloc[-1])

    if not segments:
        return pd.Series(dtype=float, name="equity")

    stitched = pd.concat(segments)
    stitched.name = "equity"
    return stitched


def _stitch_benchmark(
    folds: list[WalkForwardFold],
    initial_cash: float,
) -> pd.Series | None:
    """Stitch benchmark equity curves across folds."""
    segments: list[pd.Series] = []
    running_equity = initial_cash

    for fold in folds:
        bench = fold.oos_result.benchmark_equity
        if bench is None or bench.empty:
            continue
        scale = running_equity / float(bench.iloc[0])
        scaled = bench * scale
        segments.append(scaled)
        running_equity = float(scaled.iloc[-1])

    if not segments:
        return None

    stitched = pd.concat(segments)
    stitched.name = "benchmark_equity"
    return stitched


# ---------------------------------------------------------------------------
# Helpers — aggregate metrics
# ---------------------------------------------------------------------------

def _aggregate_oos_metrics(equity: pd.Series, risk_free_rate: float = 0.0) -> dict[str, float]:
    """Compute CAGR, Sharpe, and max drawdown from the stitched OOS equity curve."""
    if equity.empty or len(equity) < 2:
        return {"total_return": 0.0, "ann_return": 0.0, "sharpe": 0.0, "max_dd": 0.0}

    rets = equity.pct_change().dropna()
    n = len(rets)
    total_return = float((equity.iloc[-1] - equity.iloc[0]) / equity.iloc[0])
    ann_return = (1 + total_return) ** (252 / n) - 1 if n >= 2 else total_return
    vol = float(rets.std()) * math.sqrt(252)
    sharpe = (ann_return - risk_free_rate) / vol if vol > 0 else 0.0

    running_peak = equity.cummax()
    dd_series = (running_peak - equity) / running_peak
    max_dd = float(dd_series.max())

    return {
        "total_return": total_return,
        "ann_return": ann_return,
        "sharpe": sharpe,
        "max_dd": max_dd,
    }


# ---------------------------------------------------------------------------
# Helpers — logging control
# ---------------------------------------------------------------------------

def _suppress_runner_logging() -> None:
    """Quieten the engine loggers during grid search to keep output readable."""
    for name in (
        "engine.backtest.runner",
        "engine.data",
        "engine.risk",
        "engine.execution.paper_broker",   # suppress cash-cancellation noise
    ):
        logging.getLogger(name).setLevel(logging.ERROR)


def _restore_runner_logging(level: str) -> None:
    numeric = getattr(logging, level, logging.INFO)
    for name in (
        "engine.backtest.runner",
        "engine.data",
        "engine.risk",
        "engine.execution.paper_broker",
    ):
        logging.getLogger(name).setLevel(numeric)


# ---------------------------------------------------------------------------
# Helpers — OOS equity trimming
# ---------------------------------------------------------------------------

def _oos_max_dd_duration(drawdown_series: pd.Series) -> int:
    """Longest consecutive run of bars in drawdown (value > 0)."""
    max_run = current = 0
    for val in drawdown_series:
        if val > 0:
            current += 1
            if current > max_run:
                max_run = current
        else:
            current = 0
    return max_run


def _trim_result_to_window(
    result: BacktestResult,
    window_start: date,
    risk_free_rate: float = 0.0,
) -> BacktestResult:
    """
    Trim BacktestResult to a window starting at window_start and recompute all metrics.

    Used for both IS (trim warmup so optimizer sees only the true IS signal) and
    OOS (trim warmup so reported metrics reflect only the OOS window).

    The equity curve is sliced from window_start onward — warmup bars are dropped.
    The trade log keeps only trades with exit_date >= window_start so per-trade
    metrics (win rate, profit factor) align with P&L already captured in the
    trimmed equity curve — including trades entered before the window but closed
    inside it.
    """
    import copy as _copy
    r = _copy.copy(result)

    cutoff = pd.Timestamp(window_start)
    r.start_date = window_start  # align reported period with trimmed window

    # ── Trim and recompute equity-curve metrics ──────────────────────────────
    if result.equity_curve.empty:
        raise ValueError(
            f"cannot trim run {getattr(result, 'run_id', 'unknown')} to {window_start}: "
            "equity_curve is empty"
        )

    trimmed = result.equity_curve[result.equity_curve.index >= cutoff]
    if trimmed.empty:
        raise ValueError(
            f"cannot trim run {getattr(result, 'run_id', 'unknown')} to {window_start}: "
            f"no equity points remain after cutoff; original range "
            f"{result.equity_curve.index.min()} → {result.equity_curve.index.max()}"
        )
    if len(trimmed) < 2:
        raise ValueError(
            f"cannot trim run {getattr(result, 'run_id', 'unknown')} to {window_start}: "
            f"fewer than 2 equity points remain after cutoff ({len(trimmed)})"
        )
    r.equity_curve = trimmed

    if len(r.equity_curve) >= 2:
        running_peak = r.equity_curve.cummax()
        r.drawdown_curve = ((running_peak - r.equity_curve) / running_peak).rename("drawdown_pct")
        r.daily_returns = r.equity_curve.pct_change().dropna().rename("daily_return")

        n = len(r.daily_returns)
        start_eq = float(r.equity_curve.iloc[0])
        end_eq = float(r.equity_curve.iloc[-1])
        total_return = (end_eq - start_eq) / start_eq
        ann_return = (1 + total_return) ** (252 / n) - 1 if n >= 2 else total_return
        volatility = float(r.daily_returns.std()) * math.sqrt(252)
        max_dd = float(r.drawdown_curve.max())

        r.total_return_pct = total_return
        r.annualized_return_pct = ann_return
        r.volatility_annualized = volatility
        r.sharpe_ratio = (ann_return - risk_free_rate) / volatility if volatility != 0 else 0.0
        neg_rets = r.daily_returns[r.daily_returns < 0]
        downside_std = float(neg_rets.std()) * math.sqrt(252) if len(neg_rets) >= 2 else 0.0
        r.sortino_ratio = (ann_return - risk_free_rate) / downside_std if downside_std != 0 else 0.0
        r.max_drawdown_pct = max_dd
        r.calmar_ratio = ann_return / max_dd if max_dd != 0 else 0.0
        r.max_drawdown_duration_days = _oos_max_dd_duration(r.drawdown_curve)

    # ── Trim daily position counts and recompute exposure ─────────────────────
    position_counts = getattr(result, "position_count_curve", None)
    if isinstance(position_counts, pd.Series) and not position_counts.empty:
        trimmed_positions = position_counts[position_counts.index >= cutoff]
        if not trimmed_positions.empty:
            r.position_count_curve = trimmed_positions.rename("num_positions")
            r.exposure_pct = float((r.position_count_curve > 0).mean())

    # ── Trim trade log and recompute trade metrics ───────────────────────────
    if not result.trade_log.empty:
        tl = result.trade_log
        oos_tl = tl[pd.to_datetime(tl["exit_date"]) >= cutoff].copy()
        r.trade_log = oos_tl.reset_index(drop=True)
        nt = len(r.trade_log)
        if nt > 0:
            r.num_trades = nt
            r.win_rate = float((r.trade_log["net_pnl"] > 0).mean())
            gross_wins = r.trade_log.loc[r.trade_log["net_pnl"] > 0, "net_pnl"].sum()
            gross_losses = r.trade_log.loc[r.trade_log["net_pnl"] < 0, "net_pnl"].abs().sum()
            r.profit_factor = float(gross_wins) / float(gross_losses) if gross_losses > 0 else 0.0
            r.avg_days_held = float(r.trade_log["holding_days"].mean())
        else:
            r.num_trades = 0
            r.win_rate = 0.0
            r.profit_factor = 0.0
            r.avg_days_held = 0.0

    # ── Trim benchmark and recompute benchmark metrics ───────────────────────
    if result.benchmark_equity is not None:
        if result.benchmark_equity.empty:
            r.benchmark_equity = result.benchmark_equity
        else:
            r.benchmark_equity = result.benchmark_equity[
                result.benchmark_equity.index >= cutoff
            ]

        if len(r.equity_curve) >= 2 and len(r.benchmark_equity) >= 2:
            bm_returns = r.benchmark_equity.pct_change().dropna()
            aligned = pd.concat([r.daily_returns, bm_returns], axis=1).dropna()
            if len(aligned) >= 2:
                aligned.columns = ["strategy", "benchmark"]
                s_ret = aligned["strategy"]
                b_ret = aligned["benchmark"]
                cov = float(s_ret.cov(b_ret))
                bench_var = float(b_ret.var())
                r.beta = cov / bench_var if bench_var != 0 else 0.0
                r.correlation = float(s_ret.corr(b_ret))
                bm_n = len(aligned)
                bm_total = float((1 + b_ret).prod()) - 1
                bm_ann = (1 + bm_total) ** (252 / bm_n) - 1
                r.alpha = r.annualized_return_pct - risk_free_rate - r.beta * (bm_ann - risk_free_rate)
                tracking_error = float((s_ret - b_ret).std()) * math.sqrt(252)
                r.information_ratio = (
                    (r.annualized_return_pct - bm_ann) / tracking_error
                    if tracking_error != 0 else 0.0
                )
            else:
                r.alpha = None
                r.beta = None
                r.correlation = None
                r.information_ratio = None
        else:
            r.alpha = None
            r.beta = None
            r.correlation = None
            r.information_ratio = None

    return r


# Backward-compat alias used by run_holdout.py and external callers.
_trim_result_to_oos = _trim_result_to_window

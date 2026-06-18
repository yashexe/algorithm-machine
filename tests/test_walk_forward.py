"""
Tests for engine/backtest/walk_forward.py

Covers:
  - _build_fold_dates: fold generation with rolling windows
  - _expand_grid: Cartesian product of parameter grids
  - _stitch_equity_curves: continuity of stitched equity
  - _aggregate_oos_metrics: correct CAGR / Sharpe computation
  - WalkForwardValidator.run: smoke test with minimal grid using synthetic data
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from engine.backtest.metrics import BacktestResult, MetricsEngine
from engine.events.types import PortfolioSnapshotEvent
from engine.backtest.walk_forward import (
    WalkForwardFold,
    WalkForwardResult,
    WalkForwardValidator,
    _aggregate_oos_metrics,
    _build_fold_dates,
    _expand_grid,
    _stitch_equity_curves,
    _trim_result_to_window,
)


def _snapshot(
    timestamp: datetime,
    equity: Decimal,
    num_positions: int,
) -> PortfolioSnapshotEvent:
    return PortfolioSnapshotEvent(
        timestamp=timestamp,
        equity=equity,
        cash=equity,
        initial_cash=Decimal("100000"),
        total_return_pct=(equity - Decimal("100000")) / Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        drawdown_pct=Decimal("0"),
        peak_equity=max(equity, Decimal("100000")),
        positions=(),
        num_positions=num_positions,
    )


# ---------------------------------------------------------------------------
# _build_fold_dates
# ---------------------------------------------------------------------------

class TestBuildFoldDates:
    def test_standard_3plus1_folds(self):
        """10-year period with 3+1 rolling windows yields 7 folds."""
        folds = _build_fold_dates(
            full_start=date(2015, 1, 1),
            full_end=date(2024, 12, 31),
            is_years=3,
            oos_years=1,
        )
        assert len(folds) == 7

    def test_fold_structure(self):
        """IS ends the day before OOS starts; OOS is exactly oos_years long."""
        folds = _build_fold_dates(
            full_start=date(2015, 1, 1),
            full_end=date(2024, 12, 31),
            is_years=3,
            oos_years=1,
        )
        is_start, is_end, oos_start, oos_end = folds[0]
        assert is_start == date(2015, 1, 1)
        assert oos_start == is_end + pd.Timedelta(days=1)

    def test_rolling_advance(self):
        """Each fold advances by oos_years."""
        folds = _build_fold_dates(
            full_start=date(2015, 1, 1),
            full_end=date(2024, 12, 31),
            is_years=3,
            oos_years=1,
        )
        from dateutil.relativedelta import relativedelta
        starts = [f[0] for f in folds]
        for i in range(1, len(starts)):
            expected = starts[i - 1] + relativedelta(years=1)
            assert starts[i] == expected

    def test_no_folds_if_period_too_short(self):
        """Returns empty list if IS + OOS > total period."""
        folds = _build_fold_dates(
            full_start=date(2020, 1, 1),
            full_end=date(2021, 12, 31),
            is_years=3,
            oos_years=1,
        )
        assert folds == []

    def test_2plus1_folds(self):
        """4-year period with 2+1 windows yields 2 folds."""
        folds = _build_fold_dates(
            full_start=date(2018, 1, 1),
            full_end=date(2021, 12, 31),
            is_years=2,
            oos_years=1,
        )
        assert len(folds) == 2

    def test_oos_end_within_full_end(self):
        """No fold exceeds the full_end date."""
        folds = _build_fold_dates(
            full_start=date(2015, 1, 1),
            full_end=date(2024, 12, 31),
            is_years=3,
            oos_years=1,
        )
        for _, _, _, oos_end in folds:
            assert oos_end <= date(2024, 12, 31)


# ---------------------------------------------------------------------------
# _expand_grid
# ---------------------------------------------------------------------------

class TestExpandGrid:
    def test_cartesian_product(self):
        grid = {"a": [1, 2], "b": [10, 20, 30]}
        combos = _expand_grid(grid)
        assert len(combos) == 6
        assert {"a": 1, "b": 10} in combos
        assert {"a": 2, "b": 30} in combos

    def test_empty_grid(self):
        combos = _expand_grid({})
        assert combos == [{}]

    def test_single_param(self):
        combos = _expand_grid({"x": [5, 10, 15]})
        assert len(combos) == 3
        assert all(len(c) == 1 for c in combos)

    def test_preserves_all_values(self):
        grid = {"p": [0.001, 0.003], "q": [10, 20]}
        combos = _expand_grid(grid)
        all_p = {c["p"] for c in combos}
        all_q = {c["q"] for c in combos}
        assert all_p == {0.001, 0.003}
        assert all_q == {10, 20}


# ---------------------------------------------------------------------------
# _stitch_equity_curves
# ---------------------------------------------------------------------------

class TestStitchEquityCurves:
    def _make_fold(self, values: list[float], start_date: str) -> WalkForwardFold:
        """Build a minimal WalkForwardFold with a synthetic equity curve."""
        dates = pd.date_range(start=start_date, periods=len(values), freq="B")
        equity = pd.Series(values, index=dates, name="equity")

        oos_result = MagicMock()
        oos_result.equity_curve = equity
        oos_result.benchmark_equity = None

        return WalkForwardFold(
            fold_index=1,
            is_start=date(2020, 1, 1),
            is_end=date(2022, 12, 31),
            oos_start=date(2023, 1, 1),
            oos_end=date(2023, 12, 31),
            best_params={},
            best_is_sharpe=1.0,
            oos_result=oos_result,
        )

    def test_single_fold_passthrough(self):
        """Single fold: stitched curve scales to initial_cash."""
        fold = self._make_fold([100_000, 101_000, 102_000], "2023-01-01")
        stitched = _stitch_equity_curves([fold], initial_cash=100_000)
        assert abs(float(stitched.iloc[0]) - 100_000) < 1

    def test_two_folds_continuity(self):
        """Second fold starts exactly where first fold ended."""
        fold1 = self._make_fold([100_000, 110_000, 120_000], "2023-01-01")
        fold2 = self._make_fold([100_000, 105_000, 115_000], "2024-01-01")
        fold1.fold_index = 1
        fold2.fold_index = 2
        stitched = _stitch_equity_curves([fold1, fold2], initial_cash=100_000)

        # The last value of fold 1 and first value of fold 2 should be continuous
        # (within floating point tolerance)
        fold1_end = float(stitched.iloc[2])   # last of fold 1
        fold2_start = float(stitched.iloc[3]) # first of fold 2
        # fold2_start = fold1_end * (100_000/100_000) = fold1_end
        assert abs(fold2_start - fold1_end) < 1.0

    def test_empty_folds(self):
        """Empty fold list returns empty series."""
        result = _stitch_equity_curves([], initial_cash=100_000)
        assert result.empty

    def test_preserves_index_length(self):
        """Total length equals sum of all fold lengths."""
        fold1 = self._make_fold([100_000, 110_000], "2023-01-01")
        fold2 = self._make_fold([100_000, 95_000, 100_000], "2024-01-01")
        stitched = _stitch_equity_curves([fold1, fold2], initial_cash=100_000)
        assert len(stitched) == 5


# ---------------------------------------------------------------------------
# _aggregate_oos_metrics
# ---------------------------------------------------------------------------

class TestAggregateOosMetrics:
    def test_positive_return_series(self):
        """Steadily growing equity curve → positive CAGR and positive Sharpe."""
        equity = pd.Series(
            [100_000 * (1 + 0.0003) ** i for i in range(252)],
            index=pd.date_range("2023-01-01", periods=252, freq="B"),
        )
        metrics = _aggregate_oos_metrics(equity)
        assert metrics["ann_return"] > 0
        assert metrics["max_dd"] >= 0

    def test_empty_series(self):
        """Empty series returns all zeros."""
        metrics = _aggregate_oos_metrics(pd.Series(dtype=float))
        assert metrics["ann_return"] == 0.0
        assert metrics["sharpe"] == 0.0
        assert metrics["max_dd"] == 0.0

    def test_flat_series(self):
        """Flat equity → zero return."""
        equity = pd.Series(
            [100_000.0] * 50,
            index=pd.date_range("2023-01-01", periods=50, freq="B"),
        )
        metrics = _aggregate_oos_metrics(equity)
        assert abs(metrics["total_return"]) < 1e-9

    def test_max_dd_captured(self):
        """Drawdown is computed from peak to trough."""
        values = [100_000, 110_000, 90_000, 95_000]
        equity = pd.Series(values, index=pd.date_range("2023-01-01", periods=4, freq="B"))
        metrics = _aggregate_oos_metrics(equity)
        # Peak 110_000, trough 90_000 → max_dd = 20/110 ≈ 0.1818
        assert abs(metrics["max_dd"] - (20_000 / 110_000)) < 0.001


# ---------------------------------------------------------------------------
# WalkForwardValidator smoke test (mocked BacktestRunner)
# ---------------------------------------------------------------------------

class TestWalkForwardValidatorSmoke:
    """
    Smoke test: verifies the validator orchestration without hitting yfinance.
    BacktestRunner.run() is mocked to return a synthetic BacktestResult.
    """

    def _make_mock_result(
        self,
        cash: float = 100_000,
        start: date = date(2018, 1, 1),
        end: date | None = None,
    ) -> MagicMock:
        """Return a BacktestResult mock with realistic attributes."""
        result = MagicMock()
        result.run_id = "mock"
        result.sharpe_ratio = 0.8
        result.annualized_return_pct = 0.10
        result.max_drawdown_pct = 0.05
        result.num_trades = 10
        result.num_buy_fills = 10
        result.num_sell_fills = 10
        result.alpha = 0.02
        dates = (
            pd.date_range(start, end, freq="B")
            if end is not None
            else pd.date_range(start, periods=252, freq="B")
        )
        result.equity_curve = pd.Series(
            [cash * (1 + 0.0003) ** i for i in range(len(dates))],
            index=dates, name="equity",
        )
        result.benchmark_equity = pd.Series(
            [cash * (1 + 0.0002) ** i for i in range(len(dates))],
            index=dates, name="benchmark_equity",
        )
        result.trade_log = pd.DataFrame()
        result.position_count_curve = pd.Series(
            [1] * len(dates),
            index=dates,
            name="num_positions",
        )
        return result

    def _make_mock_runner(self, config) -> MagicMock:
        runner = MagicMock()
        runner.run.return_value = self._make_mock_result(
            start=config.backtest.start_date,
            end=config.backtest.end_date,
        )
        return runner

    def _make_config(self):
        """Build a minimal AppConfig pointing to equity_momentum strategy."""
        from engine.config import load_config
        return load_config("config/equity_momentum.yaml")

    def test_fold_count(self):
        """Validator produces the expected number of folds."""
        config = self._make_config()

        with patch("engine.backtest.walk_forward.BacktestRunner", side_effect=self._make_mock_runner), \
             patch("engine.backtest.walk_forward._prefetch_bars"):
            validator = WalkForwardValidator(config)
            result = validator.run(
                param_grid={"vol_target_pct": [0.002, 0.003], "top_n": [15, 20]},
                is_years=3,
                oos_years=1,
                silent_folds=True,
                max_workers=1,
            )

        # holdout_start=2024-01-01 caps full_end to 2023-12-31 → 6 folds
        assert len(result.folds) == 6

    def test_best_params_selected(self):
        """Best params are a subset of the grid values."""
        config = self._make_config()
        grid = {"vol_target_pct": [0.001, 0.003], "top_n": [10, 20]}

        call_count = 0

        def varying_runner(config):
            nonlocal call_count
            r = self._make_mock_result(
                start=config.backtest.start_date,
                end=config.backtest.end_date,
            )
            # Make Sharpe vary so optimizer can pick a best
            r.sharpe_ratio = 0.5 + (call_count % 4) * 0.1
            call_count += 1
            runner = MagicMock()
            runner.run.return_value = r
            return runner

        with patch("engine.backtest.walk_forward.BacktestRunner", side_effect=varying_runner), \
             patch("engine.backtest.walk_forward._prefetch_bars"):
            validator = WalkForwardValidator(config)
            result = validator.run(
                param_grid=grid,
                is_years=3,
                oos_years=1,
                silent_folds=True,
                max_workers=1,
            )

        for params in result.best_params_by_fold:
            assert params["vol_target_pct"] in [0.001, 0.003]
            assert params["top_n"] in [10, 20]

    def test_oos_equity_stitched(self):
        """OOS equity curve is non-empty after a successful run."""
        config = self._make_config()

        with patch("engine.backtest.walk_forward.BacktestRunner", side_effect=self._make_mock_runner), \
             patch("engine.backtest.walk_forward._prefetch_bars"):
            validator = WalkForwardValidator(config)
            result = validator.run(
                param_grid={"vol_target_pct": [0.003]},
                is_years=3,
                oos_years=1,
                silent_folds=True,
                max_workers=1,
            )

        assert not result.oos_equity_curve.empty
        assert len(result.oos_equity_curve) == sum(
            len(fold.oos_result.equity_curve) for fold in result.folds
        )

    def test_wfo_result_save(self, tmp_path):
        """WalkForwardResult.save() writes wfo_summary.json without error."""
        import json

        config = self._make_config()

        with patch("engine.backtest.walk_forward.BacktestRunner", side_effect=self._make_mock_runner), \
             patch("engine.backtest.walk_forward._prefetch_bars"):
            validator = WalkForwardValidator(config)
            result = validator.run(
                param_grid={"vol_target_pct": [0.003]},
                is_years=3,
                oos_years=1,
                output_dir=tmp_path,
                silent_folds=True,
                max_workers=1,
            )

        summary_path = tmp_path / "wfo_summary.json"
        assert summary_path.exists()

        with open(summary_path) as f:
            data = json.load(f)

        assert data["num_folds"] == 6
        assert "aggregate_oos_ann_return" in data
        assert "best_params_by_fold" in data

    def test_internal_wfo_runs_skip_root_artifact_saves(self):
        """IS combo and OOS fold runners should save only trimmed WFO artifacts."""
        config = self._make_config()
        runners: list[MagicMock] = []

        def runner_factory(cfg):
            runner = self._make_mock_runner(cfg)
            runners.append(runner)
            return runner

        with patch("engine.backtest.walk_forward.BacktestRunner", side_effect=runner_factory), \
             patch("engine.backtest.walk_forward._prefetch_bars"):
            validator = WalkForwardValidator(config)
            validator.run(
                param_grid={"vol_target_pct": [0.003]},
                is_years=3,
                oos_years=1,
                silent_folds=True,
                max_workers=1,
            )

        assert runners
        for runner in runners:
            runner.run.assert_called_once_with(save_artifacts=False)


# ---------------------------------------------------------------------------
# IS warmup — _run_is_combo builds config with data_start = is_start - warmup
# ---------------------------------------------------------------------------

class TestISWarmup:
    """_run_is_combo must prepend warmup_bars to the IS window and trim the
    result before extracting the optimization metric."""

    def _make_config(self):
        from engine.config import load_config
        return load_config("config/equity_momentum.yaml")

    def test_is_config_start_date_prepended_by_warmup(self):
        """_run_is_combo uses data_start = is_start - warmup_bars as the backtest start."""
        from datetime import date
        from dateutil.relativedelta import relativedelta
        from unittest.mock import MagicMock, patch, call
        from engine.backtest.walk_forward import _run_is_combo

        config = self._make_config()
        is_start = date(2018, 1, 1)
        is_end   = date(2020, 12, 31)
        warmup   = 200

        captured_configs = []

        def fake_runner(cfg):
            m = MagicMock()
            m.run.return_value = MagicMock(
                equity_curve=pd.Series(
                    [100_000.0 * (1 + 0.0003) ** i for i in range(252)],
                    index=pd.date_range(is_start, periods=252, freq="B"),
                    name="equity",
                ),
                sharpe_ratio=1.0,
                daily_returns=pd.Series([0.0003] * 252),
                drawdown_curve=pd.Series([0.0] * 252),
                trade_log=pd.DataFrame(),
                benchmark_equity=None,
            )
            captured_configs.append(cfg)
            return m

        with patch("engine.backtest.walk_forward.BacktestRunner", side_effect=fake_runner):
            _run_is_combo((
                config, {"vol_target_pct": 0.002}, is_start, is_end,
                "sharpe_ratio", 0, 0, None, warmup,
            ))

        assert len(captured_configs) == 1
        built_cfg = captured_configs[0]
        expected_data_start = is_start - relativedelta(days=warmup)
        assert built_cfg.backtest.start_date == expected_data_start
        assert built_cfg.backtest.end_date == is_end

    def test_is_metric_reflects_trimmed_window(self):
        """Metric returned by _run_is_combo is from the trimmed IS window, not the warmup."""
        from datetime import date
        from unittest.mock import MagicMock, patch
        from engine.backtest.walk_forward import _run_is_combo

        config = self._make_config()
        is_start = date(2018, 1, 1)
        is_end   = date(2020, 12, 31)

        # Mock result covers a longer period (warmup included); after trimming to
        # is_start the equity curve starts at is_start. We verify the test doesn't
        # blow up and returns a finite metric.
        mock_result = MagicMock()
        mock_result.equity_curve = pd.Series(
            [100_000.0 * (1 + 0.0003) ** i for i in range(756)],
            index=pd.date_range("2017-03-10", periods=756, freq="B"),
            name="equity",
        )
        mock_result.trade_log = pd.DataFrame()
        mock_result.benchmark_equity = None

        with patch("engine.backtest.walk_forward.BacktestRunner") as MockRunner:
            MockRunner.return_value.run.return_value = mock_result
            params, metric_val, err = _run_is_combo((
                config, {"vol_target_pct": 0.002}, is_start, is_end,
                "sharpe_ratio", 0, 0, None, 200,
            ))

        assert err is None
        import math
        assert math.isfinite(metric_val)


# ---------------------------------------------------------------------------
# Exposure metrics — position-count curve is the source of truth
# ---------------------------------------------------------------------------

class TestExposureMetrics:
    def _make_result_with_positions(self, counts: list[int]) -> BacktestResult:
        dates = pd.date_range("2023-01-02", periods=len(counts), freq="B")
        values = [100_000.0 * (1.0002 ** i) for i in range(len(counts))]
        equity = pd.Series(values, index=dates, name="equity")
        daily_rets = equity.pct_change().dropna().rename("daily_return")
        running_peak = equity.cummax()
        drawdown = ((running_peak - equity) / running_peak).rename("drawdown_pct")
        position_count_curve = pd.Series(counts, index=dates, name="num_positions")

        return BacktestResult(
            run_id="test",
            strategy_id="test",
            start_date=dates[0].date(),
            end_date=dates[-1].date(),
            initial_cash=Decimal("100000"),
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
            exposure_pct=float((position_count_curve > 0).mean()),
            alpha=None,
            beta=None,
            correlation=None,
            information_ratio=None,
            equity_curve=equity,
            drawdown_curve=drawdown,
            daily_returns=daily_rets,
            trade_log=pd.DataFrame(),
            benchmark_equity=None,
            position_count_curve=position_count_curve,
        )

    def test_metrics_engine_exposure_uses_snapshot_num_positions(self):
        """Exposure comes from PortfolioSnapshotEvent.num_positions, per docs."""
        dates = pd.date_range("2023-01-02", periods=5, freq="B")
        counts = [0, 1, 2, 0, 3]
        snapshots = [
            _snapshot(
                datetime(2023, 1, 2, tzinfo=timezone.utc) + timedelta(days=i),
                Decimal("100000") + Decimal(i * 100),
                counts[i],
            )
            for i in range(len(counts))
        ]

        result = MetricsEngine.compute(
            snapshots=snapshots,
            fills=[],
            initial_cash=Decimal("100000"),
            risk_free_rate=0.0,
            trading_day_count=len(dates),
            run_id="test",
            strategy_id="test",
            start_date=dates[0].date(),
            end_date=dates[-1].date(),
            bar_dates=[d.date() for d in dates],
        )

        assert result.exposure_pct == pytest.approx(3 / 5)
        assert result.position_count_curve.tolist() == counts

    def test_trim_result_recomputes_exposure_from_trimmed_position_counts(self):
        """Warmup bars should not dilute exposure after WFO trimming."""
        result = self._make_result_with_positions([0, 0, 0, 1, 2, 1])
        cutoff = result.position_count_curve.index[3].date()

        trimmed = _trim_result_to_window(result, cutoff)

        assert trimmed.position_count_curve.tolist() == [1, 2, 1]
        assert trimmed.exposure_pct == pytest.approx(1.0)

    def test_trim_result_keeps_exposure_when_position_counts_are_absent(self):
        """Older/minimal BacktestResult objects without position counts still trim safely."""
        result = self._make_result_with_positions([0, 1, 0, 1])
        result.position_count_curve = pd.Series(dtype="int64")
        result.exposure_pct = 0.25

        trimmed = _trim_result_to_window(result, result.equity_curve.index[2].date())

        assert trimmed.exposure_pct == pytest.approx(0.25)

    def test_trim_result_raises_when_equity_curve_is_empty(self):
        """A trim with no equity source data must fail loudly."""
        result = self._make_result_with_positions([0, 1, 1])
        result.equity_curve = pd.Series(dtype=float, name="equity")

        with pytest.raises(ValueError, match="equity_curve is empty"):
            _trim_result_to_window(result, date(2023, 1, 2))

    def test_trim_result_raises_when_cutoff_removes_all_equity_points(self):
        """The trim helper must not fall back to the untrimmed warmup curve."""
        result = self._make_result_with_positions([0, 1, 1])

        with pytest.raises(ValueError, match="no equity points remain"):
            _trim_result_to_window(result, date(2030, 1, 1))

    def test_trim_result_raises_when_only_one_equity_point_remains(self):
        """One post-cutoff equity point cannot produce valid returns or risk metrics."""
        result = self._make_result_with_positions([0, 1, 1])
        cutoff = result.equity_curve.index[-1].date()

        with pytest.raises(ValueError, match="fewer than 2 equity points"):
            _trim_result_to_window(result, cutoff)

    def test_trim_result_does_not_fallback_to_untrimmed_benchmark(self):
        """Benchmark-relative metrics must not compare OOS equity to warmup benchmark data."""
        result = self._make_result_with_positions([1, 1, 1, 1])
        result.benchmark_equity = pd.Series(
            [100_000.0, 100_100.0],
            index=pd.date_range("2022-12-01", periods=2, freq="B"),
            name="benchmark_equity",
        )
        result.alpha = 0.1
        result.beta = 0.5
        result.correlation = 0.7
        result.information_ratio = 0.2

        trimmed = _trim_result_to_window(result, result.equity_curve.index[1].date())

        assert trimmed.benchmark_equity is not None
        assert trimmed.benchmark_equity.empty
        assert trimmed.alpha is None
        assert trimmed.beta is None
        assert trimmed.correlation is None
        assert trimmed.information_ratio is None


# ---------------------------------------------------------------------------
# Bug 6a — _trim_result_to_window: trade log filtered by exit_date not entry_date
# ---------------------------------------------------------------------------

class TestTrimResultTradeLogFilter:
    """
    Trades whose entry predates the window but whose exit falls inside it have
    P&L already captured in the trimmed equity curve.  The trade log must
    include those trades (exit_date >= cutoff) so trade stats match the equity.
    """

    def _make_result(self, trade_rows):
        """Build a minimal BacktestResult with the given trade log rows."""
        from decimal import Decimal
        from engine.backtest.metrics import BacktestResult

        values = [100_000.0 * (1.0003 ** i) for i in range(10)]
        dates = pd.date_range("2023-01-01", periods=10, freq="B")
        equity = pd.Series(values, index=dates)
        daily_rets = equity.pct_change().dropna()
        running_peak = equity.cummax()
        drawdown = ((running_peak - equity) / running_peak).rename("drawdown_pct")
        tl = pd.DataFrame(trade_rows) if trade_rows else pd.DataFrame()
        return BacktestResult(
            run_id="test", strategy_id="test",
            start_date=dates[0].date(), end_date=dates[-1].date(),
            initial_cash=Decimal("100000"),
            total_return_pct=0.0, annualized_return_pct=0.0,
            volatility_annualized=0.0, sharpe_ratio=0.0,
            sortino_ratio=0.0, calmar_ratio=0.0,
            max_drawdown_pct=0.0, max_drawdown_duration_days=0,
            win_rate=0.0, profit_factor=0.0,
            num_trades=0, num_buy_fills=0, num_sell_fills=0,
            avg_days_held=0.0, exposure_pct=0.0,
            alpha=None, beta=None, correlation=None, information_ratio=None,
            equity_curve=equity, drawdown_curve=drawdown,
            daily_returns=daily_rets, trade_log=tl, benchmark_equity=None,
        )

    def test_trade_entered_before_but_exited_after_cutoff_is_included(self):
        """A trade with entry before the window but exit inside it must appear in the trimmed log."""
        rows = [
            {
                "entry_date": "2022-12-28",   # before cutoff
                "exit_date":  "2023-01-03",   # inside window
                "entry_price": 100.0, "exit_price": 110.0,
                "entry_qty": 10.0,
                "gross_pnl": 100.0, "net_pnl": 100.0,
                "commission": 0.0, "holding_days": 6,
            },
        ]
        result = self._make_result(rows)
        cutoff = date(2023, 1, 2)
        trimmed = _trim_result_to_window(result, cutoff)
        assert len(trimmed.trade_log) == 1, (
            "Trade closed inside the window should be kept when filtering by exit_date"
        )

    def test_trade_entered_and_exited_before_cutoff_is_excluded(self):
        """A trade fully before the window must not appear in the trimmed log."""
        rows = [
            {
                "entry_date": "2022-12-20",
                "exit_date":  "2022-12-29",   # both before cutoff
                "entry_price": 100.0, "exit_price": 105.0,
                "entry_qty": 10.0,
                "gross_pnl": 50.0, "net_pnl": 50.0,
                "commission": 0.0, "holding_days": 9,
            },
        ]
        result = self._make_result(rows)
        cutoff = date(2023, 1, 2)
        trimmed = _trim_result_to_window(result, cutoff)
        assert len(trimmed.trade_log) == 0, (
            "Trade fully before the window should be excluded"
        )

    def test_mixed_trades_correctly_partitioned(self):
        """One pre-window trade and one straddling trade — only the straddler survives."""
        rows = [
            {
                "entry_date": "2022-12-15", "exit_date": "2022-12-28",
                "entry_price": 100.0, "exit_price": 102.0,
                "entry_qty": 10.0, "gross_pnl": 20.0, "net_pnl": 20.0,
                "commission": 0.0, "holding_days": 13,
            },
            {
                "entry_date": "2022-12-28", "exit_date": "2023-01-04",
                "entry_price": 102.0, "exit_price": 108.0,
                "entry_qty": 10.0, "gross_pnl": 60.0, "net_pnl": 60.0,
                "commission": 0.0, "holding_days": 7,
            },
        ]
        result = self._make_result(rows)
        cutoff = date(2023, 1, 2)
        trimmed = _trim_result_to_window(result, cutoff)
        assert len(trimmed.trade_log) == 1
        assert trimmed.trade_log.iloc[0]["gross_pnl"] == pytest.approx(60.0)

    def test_num_buy_fill_and_sell_fill_not_overridden(self):
        """num_buy_fills and num_sell_fills must NOT be set to num_trades after trimming."""
        rows = [
            {
                "entry_date": "2023-01-02", "exit_date": "2023-01-05",
                "entry_price": 100.0, "exit_price": 105.0,
                "entry_qty": 10.0, "gross_pnl": 50.0, "net_pnl": 50.0,
                "commission": 0.0, "holding_days": 3,
            },
        ]
        result = self._make_result(rows)
        result.num_buy_fills = 3
        result.num_sell_fills = 3
        cutoff = date(2023, 1, 2)
        trimmed = _trim_result_to_window(result, cutoff)
        assert trimmed.num_buy_fills == 3, "num_buy_fills must not be overridden by num_trades"
        assert trimmed.num_sell_fills == 3, "num_sell_fills must not be overridden by num_trades"


# ---------------------------------------------------------------------------
# Bug 6b — _aggregate_oos_metrics uses the risk_free_rate parameter
# ---------------------------------------------------------------------------

class TestAggregateOosMetricsRiskFreeRate:
    def _growing_equity(self, daily_return: float = 0.0003, periods: int = 252) -> pd.Series:
        return pd.Series(
            [100_000.0 * (1 + daily_return) ** i for i in range(periods)],
            index=pd.date_range("2023-01-01", periods=periods, freq="B"),
        )

    def test_zero_rate_gives_higher_sharpe_than_positive_rate(self):
        """Sharpe with risk_free_rate=0 must exceed Sharpe with risk_free_rate=0.05."""
        equity = self._growing_equity()
        sharpe_zero = _aggregate_oos_metrics(equity, risk_free_rate=0.0)["sharpe"]
        sharpe_rfr = _aggregate_oos_metrics(equity, risk_free_rate=0.05)["sharpe"]
        assert sharpe_zero > sharpe_rfr, (
            "Higher risk-free rate should reduce Sharpe for the same equity curve"
        )

    def test_default_rate_is_zero(self):
        """Calling without risk_free_rate uses 0.0 (not 0.05)."""
        equity = self._growing_equity()
        default_sharpe = _aggregate_oos_metrics(equity)["sharpe"]
        explicit_zero_sharpe = _aggregate_oos_metrics(equity, risk_free_rate=0.0)["sharpe"]
        assert default_sharpe == pytest.approx(explicit_zero_sharpe)

    def test_sharpe_numerator_uses_rate(self):
        """Verify Sharpe numerator is (ann_return - rate), not (ann_return - 0.05)."""
        equity = self._growing_equity(daily_return=0.0001, periods=252)
        m_zero = _aggregate_oos_metrics(equity, risk_free_rate=0.0)
        m_five = _aggregate_oos_metrics(equity, risk_free_rate=0.05)
        # ann_return − vol × sharpe = rate
        # Rearranged: rate ≈ ann_return − vol × sharpe
        ann = m_zero["ann_return"]
        vol_zero = m_zero["sharpe"] and (ann / m_zero["sharpe"]) if m_zero["sharpe"] else None
        if vol_zero:
            implied_rate_zero = ann - vol_zero * m_zero["sharpe"]
            implied_rate_five = ann - vol_zero * m_five["sharpe"]
            assert implied_rate_zero == pytest.approx(0.0, abs=1e-6)
            assert implied_rate_five == pytest.approx(0.05, abs=1e-3)


# ---------------------------------------------------------------------------
# Bug 6c — optimize_metric validation at WalkForwardValidator.run() entry
# ---------------------------------------------------------------------------

class TestOptimizeMetricValidation:
    def _make_config(self):
        from engine.config import load_config
        return load_config("config/equity_momentum.yaml")

    def test_invalid_metric_raises_value_error(self):
        """An unrecognized optimize_metric must raise ValueError immediately."""
        config = self._make_config()
        validator = WalkForwardValidator(config)
        with pytest.raises(ValueError, match="optimize_metric"):
            validator.run(
                param_grid={"vol_target_pct": [0.003]},
                optimize_metric="total_pnl",   # not a valid BacktestResult attribute
            )

    def test_valid_metrics_do_not_raise_at_entry(self):
        """All four documented metrics must pass the entry-point validation guard."""
        from unittest.mock import patch, MagicMock
        valid_metrics = ["sharpe_ratio", "annualized_return_pct", "sortino_ratio", "calmar_ratio"]
        config = self._make_config()

        for metric in valid_metrics:
            validator = WalkForwardValidator(config)
            # We abort immediately after validation passes by making _build_fold_dates return []
            with patch(
                "engine.backtest.walk_forward._build_fold_dates", return_value=[]
            ):
                with pytest.raises(ValueError, match="No folds"):
                    validator.run(
                        param_grid={"vol_target_pct": [0.003]},
                        optimize_metric=metric,
                    )

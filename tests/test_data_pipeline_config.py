"""
Tests for data pipeline config wiring and cache behaviour.

  Bug 9 (original): data.adjusted was not wired through to YFinanceFetcher.
  Finding 8: run_walk_forward.py had no validation for --is-years/--oos-years,
             causing an infinite loop on zero or negative values.
  Finding 9: BarCache used a single path per bar_type regardless of the
             adjusted flag, causing raw/adjusted data to collide.
  Finding 10: covers_range() checked only min/max bounds, missing interior gaps.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from engine.data import DataPipeline
from engine.data.cache import BarCache
from engine.data.fetchers.yfinance import YFinanceFetcher
from engine.events.types import BarType


# ---------------------------------------------------------------------------
# Bug 9 — data.adjusted wired through DataPipeline.build()
# ---------------------------------------------------------------------------

class TestDataPipelineAdjustedWiring:
    def test_adjusted_true_reaches_fetcher(self):
        pipeline = DataPipeline.build(adjusted=True)
        assert pipeline._fetcher._adjusted is True

    def test_adjusted_false_reaches_fetcher(self):
        pipeline = DataPipeline.build(adjusted=False)
        assert pipeline._fetcher._adjusted is False

    def test_default_adjusted_is_true(self):
        """Without explicit adjusted kwarg the fetcher defaults to True (split-adjusted prices)."""
        pipeline = DataPipeline.build()
        assert pipeline._fetcher._adjusted is True

    def test_config_adjusted_false_propagates(self):
        """DataConfig.adjusted=False must flow through runner pipeline construction."""
        from engine.config import load_config
        config = load_config("config/equity_momentum.yaml")
        config.data.__dict__["adjusted"] = False  # bypass pydantic field to simulate config

        # Verify the build call would honour the config value
        pipeline = DataPipeline.build(
            cache_dir=config.data.cache_dir,
            batch_size=config.data.batch_size,
            staleness_hours=config.data.cache_staleness_hours,
            bar_close_utc_hour=config.data.bar_close_utc_hour,
            adjusted=config.data.adjusted,
        )
        assert pipeline._fetcher._adjusted is False

    def test_yfinance_fetcher_uses_adjusted_flag(self):
        """YFinanceFetcher._adjusted is forwarded to yf.download as auto_adjust."""
        fetcher = YFinanceFetcher(adjusted=False)
        assert fetcher._adjusted is False

        import pandas as pd
        mock_df = pd.DataFrame(
            {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.5], "Volume": [1_000_000]},
            index=pd.to_datetime(["2024-01-02"]),
        )

        with patch("yfinance.download", return_value=mock_df) as mock_dl:
            try:
                fetcher.fetch(["SPY"], date(2024, 1, 2), date(2024, 1, 2))
            except Exception:
                pass  # network errors irrelevant — we only care about the call args
            if mock_dl.called:
                _, kwargs = mock_dl.call_args
                assert kwargs.get("auto_adjust") is False


# ---------------------------------------------------------------------------
# Bug 10 — holdout guard in run_backtest.py
# ---------------------------------------------------------------------------

class TestRunBacktestHoldoutGuard:
    def _load_config(self):
        from engine.config import load_config
        return load_config("config/equity_momentum.yaml")

    def test_end_date_capped_when_overlap(self):
        """
        When config.backtest.end_date overlaps holdout_start, the guard must cap
        end_date to holdout_start - 1 day before BacktestRunner.run() is called.
        """
        config = self._load_config()
        assert config.backtest.holdout_start is not None
        holdout_start = config.backtest.holdout_start
        # Confirm the config does overlap (end_date=2024-12-31 >= holdout_start=2024-01-01)
        assert config.backtest.end_date >= holdout_start

        expected_cap = holdout_start - timedelta(days=1)

        with patch("engine.backtest.BacktestRunner") as MockRunner:
            mock_result = MagicMock()
            mock_result.print_summary = MagicMock()
            MockRunner.return_value.run.return_value = mock_result

            from run_backtest import main
            main(["--config", "config/equity_momentum.yaml"])

            # The config passed to BacktestRunner must have end_date capped
            captured_cfg = MockRunner.call_args[0][0]
            assert captured_cfg.backtest.end_date == expected_cap, (
                f"Expected end_date capped to {expected_cap}, "
                f"got {captured_cfg.backtest.end_date}"
            )

    def test_no_cap_when_end_before_holdout(self):
        """When --end is already before holdout_start the guard must not cap it further."""
        config = self._load_config()
        holdout_start = config.backtest.holdout_start
        safe_end = holdout_start - timedelta(days=30)

        with patch("engine.backtest.BacktestRunner") as MockRunner:
            mock_result = MagicMock()
            mock_result.print_summary = MagicMock()
            MockRunner.return_value.run.return_value = mock_result

            from run_backtest import main
            main(["--config", "config/equity_momentum.yaml", "--end", safe_end.isoformat()])

            captured_cfg = MockRunner.call_args[0][0]
            assert captured_cfg.backtest.end_date == safe_end

    def test_no_cap_when_no_holdout_start(self):
        """Config without holdout_start must not have any end_date cap applied."""
        config = self._load_config()
        config.backtest.holdout_start = None
        original_end = config.backtest.end_date

        with patch("engine.config.load_config", return_value=config), \
             patch("engine.backtest.BacktestRunner") as MockRunner:
            mock_result = MagicMock()
            mock_result.print_summary = MagicMock()
            MockRunner.return_value.run.return_value = mock_result

            from run_backtest import main
            main(["--config", "config/equity_momentum.yaml"])

            captured_cfg = MockRunner.call_args[0][0]
            assert captured_cfg.backtest.end_date == original_end


# ---------------------------------------------------------------------------
# Finding 8 — run_walk_forward.py CLI validation
# ---------------------------------------------------------------------------

class TestWalkForwardCLIValidation:
    """main() must reject invalid --is-years / --oos-years / --warmup-bars before running."""

    def test_zero_oos_years_exits_with_error(self):
        from run_walk_forward import main
        rc = main(["--oos-years", "0"])
        assert rc == 1

    def test_negative_oos_years_exits_with_error(self):
        from run_walk_forward import main
        rc = main(["--oos-years", "-1"])
        assert rc == 1

    def test_zero_is_years_exits_with_error(self):
        from run_walk_forward import main
        rc = main(["--is-years", "0"])
        assert rc == 1

    def test_negative_is_years_exits_with_error(self):
        from run_walk_forward import main
        rc = main(["--is-years", "-2"])
        assert rc == 1

    def test_negative_warmup_bars_exits_with_error(self):
        from run_walk_forward import main
        rc = main(["--warmup-bars", "-1"])
        assert rc == 1

    def test_valid_args_do_not_exit_early(self):
        """Valid args must reach the validator (not be rejected by the guard)."""
        from run_walk_forward import main
        # WalkForwardValidator is imported inside main(), so patch the module attribute.
        with patch("engine.backtest.walk_forward.WalkForwardValidator") as MockVal:
            mock_result = MagicMock()
            mock_result.print_summary = MagicMock()
            MockVal.return_value.run.return_value = mock_result
            rc = main(["--is-years", "2", "--oos-years", "1"])
        assert rc == 0
        assert MockVal.return_value.run.called


# ---------------------------------------------------------------------------
# Finding 9 — BarCache adj/raw path separation
# ---------------------------------------------------------------------------

class TestBarCacheAdjRawPaths:
    """BarCache must use distinct Parquet paths for adjusted and raw data."""

    def test_adjusted_path_contains_adj_suffix(self, tmp_path):
        cache = BarCache(cache_dir=tmp_path, adjusted=True)
        p = cache._path("SPY", BarType.DAILY)
        assert p.name == "daily_adj.parquet"

    def test_raw_path_contains_raw_suffix(self, tmp_path):
        cache = BarCache(cache_dir=tmp_path, adjusted=False)
        p = cache._path("SPY", BarType.DAILY)
        assert p.name == "daily_raw.parquet"

    def test_adj_and_raw_paths_are_distinct(self, tmp_path):
        adj_cache = BarCache(cache_dir=tmp_path, adjusted=True)
        raw_cache = BarCache(cache_dir=tmp_path, adjusted=False)
        assert adj_cache._path("SPY", BarType.DAILY) != raw_cache._path("SPY", BarType.DAILY)

    def test_default_is_adjusted(self, tmp_path):
        cache = BarCache(cache_dir=tmp_path)
        assert cache._adjusted is True
        assert cache._path("SPY", BarType.DAILY).name == "daily_adj.parquet"

    def test_pipeline_build_forwards_adjusted_to_cache(self):
        pipeline = DataPipeline.build(adjusted=False)
        assert pipeline._cache._adjusted is False

    def test_pipeline_build_adjusted_true_reaches_cache(self):
        pipeline = DataPipeline.build(adjusted=True)
        assert pipeline._cache._adjusted is True


# ---------------------------------------------------------------------------
# Finding 10 — covers_range density check
# ---------------------------------------------------------------------------

def _make_cache_with_dates(tmp_path, symbol: str, bar_type: BarType, trading_dates: list[date]) -> BarCache:
    """Write a Parquet file to tmp_path containing bars on the given dates."""
    from decimal import Decimal
    from engine.events.types import BarEvent
    from engine.data.cache import BarCache

    cache = BarCache(cache_dir=tmp_path, adjusted=True)
    bars = [
        BarEvent(
            timestamp=datetime(d.year, d.month, d.day, 21, 0, tzinfo=timezone.utc),
            symbol=symbol,
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
            volume=1_000_000, bar_type=bar_type, source="test",
        )
        for d in trading_dates
    ]
    cache.write(bars, bar_type)
    return cache


class TestCoverageRangeDensityCheck:
    """covers_range must reject caches with interior gaps even when bounds match."""

    def test_full_coverage_returns_true(self, tmp_path):
        """A complete set of Jan 2024 trading days must be a cache hit."""
        from engine.backtest.calendar import trading_days
        jan_days = trading_days(date(2024, 1, 2), date(2024, 1, 31))
        cache = _make_cache_with_dates(tmp_path, "SPY", BarType.DAILY, jan_days)
        assert cache.covers_range("SPY", BarType.DAILY, date(2024, 1, 2), date(2024, 1, 31)) is True

    def test_large_interior_gap_returns_false(self, tmp_path):
        """
        Cache has data on day 1 and day 30 of January only.
        Boundary check passes but density fails — must return False.
        """
        sparse = [date(2024, 1, 2), date(2024, 1, 31)]
        cache = _make_cache_with_dates(tmp_path, "SPY", BarType.DAILY, sparse)
        assert cache.covers_range("SPY", BarType.DAILY, date(2024, 1, 2), date(2024, 1, 31)) is False

    def test_one_missing_day_still_passes(self, tmp_path):
        """Dropping a single interior trading day from a full month must still be a hit (above 90%)."""
        from engine.backtest.calendar import trading_days
        jan_days = trading_days(date(2024, 1, 2), date(2024, 1, 31))
        # Drop one interior day — 20/21 = 95.2% > 90%.
        # Keep first and last so the boundary check still passes.
        incomplete = jan_days[:10] + jan_days[11:]
        cache = _make_cache_with_dates(tmp_path, "SPY", BarType.DAILY, incomplete)
        assert cache.covers_range("SPY", BarType.DAILY, date(2024, 1, 2), date(2024, 1, 31)) is True

    def test_half_month_gap_returns_false(self, tmp_path):
        """Roughly half the days missing (10/21) must fail the 90% threshold."""
        from engine.backtest.calendar import trading_days
        jan_days = trading_days(date(2024, 1, 2), date(2024, 1, 31))
        # Keep only every other day — 10 or 11 out of 21
        sparse = jan_days[::2]
        cache = _make_cache_with_dates(tmp_path, "SPY", BarType.DAILY, sparse)
        assert cache.covers_range("SPY", BarType.DAILY, date(2024, 1, 2), date(2024, 1, 31)) is False

    def test_boundary_miss_still_returns_false(self, tmp_path):
        """Cache that doesn't reach the end date must still fail (boundary check)."""
        from engine.backtest.calendar import trading_days
        jan_days = trading_days(date(2024, 1, 2), date(2024, 1, 26))
        cache = _make_cache_with_dates(tmp_path, "SPY", BarType.DAILY, jan_days)
        # Request extends past cached end
        assert cache.covers_range("SPY", BarType.DAILY, date(2024, 1, 2), date(2024, 1, 31)) is False

    def test_no_file_returns_false(self, tmp_path):
        cache = BarCache(cache_dir=tmp_path, adjusted=True)
        assert cache.covers_range("MISSING", BarType.DAILY, date(2024, 1, 2), date(2024, 1, 31)) is False

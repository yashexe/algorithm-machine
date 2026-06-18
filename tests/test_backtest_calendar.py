"""Unit tests for the U.S. trading calendar."""

from datetime import date

import pytest

from engine.backtest.calendar import is_trading_day, trading_days


# ---------------------------------------------------------------------------
# Weekends
# ---------------------------------------------------------------------------

class TestWeekends:
    def test_saturday_not_trading(self):
        assert not is_trading_day(date(2024, 1, 6))   # Saturday

    def test_sunday_not_trading(self):
        assert not is_trading_day(date(2024, 1, 7))   # Sunday

    def test_monday_is_trading(self):
        assert is_trading_day(date(2024, 1, 8))       # regular Monday


# ---------------------------------------------------------------------------
# 2024 holidays (all dates verified against NYSE holiday list)
# ---------------------------------------------------------------------------

class TestHolidays2024:
    def test_new_years_day(self):
        assert not is_trading_day(date(2024, 1, 1))   # Monday

    def test_mlk_day(self):
        assert not is_trading_day(date(2024, 1, 15))  # 3rd Monday of Jan

    def test_presidents_day(self):
        assert not is_trading_day(date(2024, 2, 19))  # 3rd Monday of Feb

    def test_good_friday(self):
        assert not is_trading_day(date(2024, 3, 29))  # Easter = Mar 31

    def test_memorial_day(self):
        assert not is_trading_day(date(2024, 5, 27))  # last Monday of May

    def test_independence_day(self):
        assert not is_trading_day(date(2024, 7, 4))   # Thursday

    def test_labor_day(self):
        assert not is_trading_day(date(2024, 9, 2))   # 1st Monday of Sep

    def test_thanksgiving(self):
        assert not is_trading_day(date(2024, 11, 28)) # 4th Thursday of Nov

    def test_christmas(self):
        assert not is_trading_day(date(2024, 12, 25)) # Wednesday

    # Adjacent trading days should be open
    def test_day_after_thanksgiving_is_trading(self):
        assert is_trading_day(date(2024, 11, 29))     # Black Friday (open)

    def test_day_before_christmas_is_trading(self):
        assert is_trading_day(date(2024, 12, 24))     # Christmas Eve (open)


# ---------------------------------------------------------------------------
# Observed holiday rules
# ---------------------------------------------------------------------------

class TestObservedHolidays:
    def test_christmas_on_sunday_observed_monday(self):
        # Dec 25, 2022 is Sunday → observed Dec 26 (Monday)
        assert not is_trading_day(date(2022, 12, 26))
        assert is_trading_day(date(2022, 12, 27))     # Tuesday is open

    def test_independence_day_on_saturday_observed_friday(self):
        # Jul 4, 2020 is Saturday → observed Jul 3 (Friday)
        assert not is_trading_day(date(2020, 7, 3))
        assert is_trading_day(date(2020, 7, 6))       # Monday is open

    def test_new_years_on_saturday_observed_dec31(self):
        # Jan 1, 2022 is Saturday → Dec 31, 2021 is the observed holiday
        assert not is_trading_day(date(2021, 12, 31))
        assert is_trading_day(date(2022, 1, 3))       # First trading day of 2022


# ---------------------------------------------------------------------------
# trading_days() range function
# ---------------------------------------------------------------------------

class TestTradingDaysRange:
    def test_empty_when_start_after_end(self):
        assert trading_days(date(2024, 2, 1), date(2024, 1, 1)) == []

    def test_single_day_holiday(self):
        assert trading_days(date(2024, 1, 1), date(2024, 1, 1)) == []

    def test_single_trading_day(self):
        result = trading_days(date(2024, 1, 2), date(2024, 1, 2))
        assert result == [date(2024, 1, 2)]

    def test_skips_weekend_and_holiday(self):
        # Week of Jan 15, 2024 (MLK Day Monday)
        result = trading_days(date(2024, 1, 13), date(2024, 1, 19))
        # Sat 13, Sun 14 excluded; Mon 15 = MLK Day excluded
        assert date(2024, 1, 13) not in result
        assert date(2024, 1, 14) not in result
        assert date(2024, 1, 15) not in result
        # Tue–Fri are trading days
        assert date(2024, 1, 16) in result
        assert date(2024, 1, 17) in result
        assert date(2024, 1, 18) in result
        assert date(2024, 1, 19) in result
        assert len(result) == 4

    def test_result_is_sorted(self):
        result = trading_days(date(2024, 1, 2), date(2024, 1, 31))
        assert result == sorted(result)

    def test_approx_trading_days_per_year(self):
        # NYSE typically has 251–253 trading days per year
        count = len(trading_days(date(2024, 1, 1), date(2024, 12, 31)))
        assert 250 <= count <= 254

"""
Simplified U.S. equity trading calendar.

Covers NYSE/NASDAQ exchange holidays. Half-days are not modeled — every
trading day uses its full bar (per BACKTEST_HARNESS.md §4).
"""

from __future__ import annotations

from datetime import date, timedelta


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm for Easter Sunday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = (h + el - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _good_friday(year: int) -> date:
    return _easter(year) - timedelta(days=2)


def _nth_monday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    first = d + timedelta(days=(0 - d.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def _nth_thursday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    first = d + timedelta(days=(3 - d.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def _last_monday(year: int, month: int) -> date:
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    return last_day - timedelta(days=last_day.weekday())


def _observe(d: date) -> date:
    """Saturday → prior Friday; Sunday → next Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _us_holidays(year: int) -> frozenset[date]:
    return frozenset({
        _observe(date(year, 1, 1)),     # New Year's Day
        _nth_monday(year, 1, 3),        # MLK Day — 3rd Monday of January
        _nth_monday(year, 2, 3),        # Presidents' Day — 3rd Monday of February
        _good_friday(year),             # Good Friday
        _last_monday(year, 5),          # Memorial Day — last Monday of May
        _observe(date(year, 7, 4)),     # Independence Day
        _nth_monday(year, 9, 1),        # Labor Day — 1st Monday of September
        _nth_thursday(year, 11, 4),     # Thanksgiving — 4th Thursday of November
        _observe(date(year, 12, 25)),   # Christmas Day
    })


_CACHE: dict[int, frozenset[date]] = {}


def is_trading_day(d: date) -> bool:
    """Return True if d is a U.S. equity exchange trading day."""
    if d.weekday() >= 5:
        return False
    if d.year not in _CACHE:
        _CACHE[d.year] = _us_holidays(d.year)
    if d in _CACHE[d.year]:
        return False
    # Dec 31 can be an observed New Year's holiday for the following year
    # (when Jan 1 of year+1 falls on Saturday, Dec 31 is the observed holiday).
    if d.month == 12 and d.day == 31:
        next_year = d.year + 1
        if next_year not in _CACHE:
            _CACHE[next_year] = _us_holidays(next_year)
        if d in _CACHE[next_year]:
            return False
    return True


def trading_days(start: date, end: date) -> list[date]:
    """Return sorted list of trading days in [start, end] inclusive."""
    result: list[date] = []
    current = start
    while current <= end:
        if is_trading_day(current):
            result.append(current)
        current += timedelta(days=1)
    return result

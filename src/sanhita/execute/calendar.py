"""Turning a deadline into a date.

This is the smallest module in the execute stage and the one most able to be
quietly wrong. "T+5 day" is not a date. It becomes a date only once two things
are decided: whether the count is working days or calendar days, and, if working
days, which days the market was shut.

Sanhita refuses to guess either.

  The working-versus-calendar question is settled before this module ever runs.
  ``DayCount.UNSPECIFIED`` blocks certification, so an uncertified rule never
  reaches the engine and a certified one always carries a human's answer. If a
  rule somehow arrives here still unspecified, this module raises rather than
  picking a convention.

  The holiday question is answered by whoever supplies the calendar. The default
  calendar knows weekends and nothing else, and it says so. A gap report
  computed against it states which calendar it used, so nobody reads a due date
  as more precise than the inputs allow.

An exchange holiday list is a published fact we do not have, so we do not
pretend to. Load one and the same rules produce exact answers; the arithmetic
does not change.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

__all__ = ["TradingCalendar", "WEEKENDS_ONLY", "DayCountUnresolved"]


class DayCountUnresolved(ValueError):
    """A deadline reached the engine without its day-count convention settled."""


@dataclass(frozen=True)
class TradingCalendar:
    """Which days count as working days.

    ``holidays`` is an explicit set of dates the market was shut. ``name`` is
    printed on every report that used this calendar, so a reader can tell
    whether the due dates in front of them were computed against a real exchange
    holiday list or against weekends alone.
    """

    name: str
    holidays: frozenset[_dt.date] = field(default_factory=frozenset)
    #: Saturday and Sunday. Indices match ``date.weekday()``.
    weekend: frozenset[int] = frozenset({5, 6})

    @property
    def knows_holidays(self) -> bool:
        return bool(self.holidays)

    @property
    def covers(self) -> tuple[_dt.date, _dt.date] | None:
        """The range the holiday list actually spans, or None if it is empty."""
        if not self.holidays:
            return None
        return (min(self.holidays), max(self.holidays))

    def is_working_day(self, day: _dt.date) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays

    def covers_date(self, day: _dt.date) -> bool:
        """Whether a due date falls inside the range the holiday list describes.

        Outside that range the calendar still computes, but only weekends are
        known, so the answer is weaker. The report says which findings this
        applies to instead of averaging the doubt away.
        """
        span = self.covers
        if span is None:
            return False
        return span[0] <= day <= span[1]

    # ------------------------------------------------------------- arithmetic

    def add_working_days(self, start: _dt.date, days: int) -> _dt.date:
        """T+n counted in working days.

        The anchor day itself is day zero whether or not it was a working day,
        which is how "T+1 working day" is read: the day after T that the market
        was open. ``days=0`` therefore returns the anchor unchanged.
        """
        if days < 0:
            raise ValueError("a deadline offset cannot be negative")
        day = start
        remaining = days
        while remaining > 0:
            day += _dt.timedelta(days=1)
            if self.is_working_day(day):
                remaining -= 1
        return day

    def add_calendar_days(self, start: _dt.date, days: int) -> _dt.date:
        if days < 0:
            raise ValueError("a deadline offset cannot be negative")
        return start + _dt.timedelta(days=days)

    def add_months(self, start: _dt.date, months: int) -> _dt.date:
        """Calendar months, clamped to the end of the target month.

        Two months from 31 December is 28 February, not 3 March. Rolling into
        the next month would give the entity a deadline the regulation did not.
        """
        if months < 0:
            raise ValueError("a deadline offset cannot be negative")
        total = start.month - 1 + months
        year = start.year + total // 12
        month = total % 12 + 1
        last = _month_length(year, month)
        return _dt.date(year, month, min(start.day, last))

    def end_of_period(self, start: _dt.date, period: str) -> _dt.date:
        """The close of the named period containing ``start``."""
        name = period.strip().upper()
        if name == "DAY":
            return start
        if name == "WEEK":
            return start + _dt.timedelta(days=6 - start.weekday())
        if name == "MONTH":
            return _dt.date(start.year, start.month, _month_length(start.year, start.month))
        if name == "QUARTER":
            end_month = ((start.month - 1) // 3 + 1) * 3
            return _dt.date(start.year, end_month, _month_length(start.year, end_month))
        if name == "HALF_YEAR":
            end_month = 6 if start.month <= 6 else 12
            return _dt.date(start.year, end_month, _month_length(start.year, end_month))
        if name == "YEAR":
            return _dt.date(start.year, 12, 31)
        raise ValueError(f"unknown period {period!r}")


def _month_length(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (_dt.date(year, month + 1, 1) - _dt.timedelta(days=1)).day


#: The calendar you get when nobody supplied one. It knows that Saturday and
#: Sunday are not working days and it knows nothing else. Every report computed
#: against it says so on its face.
WEEKENDS_ONLY = TradingCalendar(name="weekends only, no exchange holiday list loaded")

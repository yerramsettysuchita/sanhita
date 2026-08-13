"""What is due, and when.

A certified rulebook is a description. A compliance officer's Monday morning
needs a list. This turns the first into the second: given the certified rules
and a window, it says which duties fall due on which dates, and cites the clause
for each.

Only recurring duties can be placed on a calendar without evidence. A rule that
runs from a trade or a breach has no date until that event happens, so it is not
scheduled here; it is listed separately as something that will fire when its
trigger does. Putting a made-up date on it would be inventing a deadline.
"""

from __future__ import annotations

import calendar as _cal
import datetime as _dt
from dataclasses import dataclass, field

from sanhita.ir.enums import DeadlineKind, Modality, RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["Due", "Schedule", "build_schedule"]

#: How a named period maps onto the dates it ends on.
_PERIODS = {"DAY", "WEEK", "MONTH", "QUARTER", "HALF_YEAR", "YEAR"}


@dataclass(frozen=True)
class Due:
    """One occasion on which a duty falls due."""

    on: _dt.date
    obligation_id: str
    clause_id: str
    page: int
    actor: str
    requirement: str
    period: str
    certified_by: str

    def to_json(self) -> dict:
        return {
            "on": self.on.isoformat(),
            "obligation_id": self.obligation_id,
            "clause_id": self.clause_id,
            "page": self.page,
            "actor": self.actor,
            "requirement": self.requirement,
            "period": self.period,
            "certified_by": self.certified_by,
        }


@dataclass
class Schedule:
    start: _dt.date
    end: _dt.date
    certified: int = 0
    due: list[Due] = field(default_factory=list)
    #: Certified duties with no fixed date, because they run from an event.
    event_driven: list[tuple[str, str]] = field(default_factory=list)
    #: Duties owed every day. Listed once rather than repeated on every date:
    #: enumerating twenty daily duties across ninety days produced eighteen
    #: hundred rows that buried the monthly and quarterly ones a person
    #: actually has to plan around.
    daily: list[tuple[str, str]] = field(default_factory=list)
    #: Rules left out because their clause is one of the flattened tables.
    excluded_rules: int = 0
    excluded_clauses: list[str] = field(default_factory=list)

    def by_date(self) -> dict[_dt.date, list[Due]]:
        out: dict[_dt.date, list[Due]] = {}
        for item in sorted(self.due, key=lambda d: (d.on, d.clause_id)):
            out.setdefault(item.on, []).append(item)
        return out

    def next_due(self) -> Due | None:
        upcoming = sorted(self.due, key=lambda d: (d.on, d.clause_id))
        return upcoming[0] if upcoming else None

    def caveats(self) -> list[str]:
        notes = [
            f"Only rules a person has certified appear here. {self.certified} "
            "are certified in total.",
            f"{len(self.event_driven)} certified duty(ies) run from an event "
            "rather than a calendar, so they have no date until the event "
            "happens and are listed separately. Giving them one would be "
            "inventing a deadline.",
            "Dates are the close of the period the regulation names. Where a "
            "clause adds an offset in working days, the exchange holiday "
            "calendar has not been loaded, so those dates may be a day or two "
            "early.",
        ]
        if self.daily:
            notes.append(
                f"{len(self.daily)} duty(ies) are owed every single day. They "
                "are listed once rather than repeated on every date, because "
                "printing them daily would bury everything else."
            )
        if self.excluded_clauses:
            notes.append(
                f"{self.excluded_rules} rule(s) from "
                + ", ".join(self.excluded_clauses)
                + " were left out. Those clauses are summary tables the parser "
                "flattened into one node, and the duties drawn from them are "
                "fragments rather than obligations."
            )
        return notes

    def to_json(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "certified": self.certified,
            "occasions": len(self.due),
            "event_driven": len(self.event_driven),
            "caveats": self.caveats(),
            "due": [d.to_json() for d in sorted(self.due, key=lambda d: d.on)],
        }


def _period_ends(period: str, start: _dt.date, end: _dt.date) -> list[_dt.date]:
    """Every date in the window on which the named period closes."""
    name = period.strip().upper()
    if name not in _PERIODS:
        return []

    out: list[_dt.date] = []
    day = start
    while day <= end:
        if name == "DAY":
            out.append(day)
            day += _dt.timedelta(days=1)
            continue
        if name == "WEEK":
            closes = day + _dt.timedelta(days=6 - day.weekday())
            if closes >= start and closes <= end and closes not in out:
                out.append(closes)
            day = closes + _dt.timedelta(days=1)
            continue

        last = _cal.monthrange(day.year, day.month)[1]
        month_end = _dt.date(day.year, day.month, last)
        closes = None
        if name == "MONTH":
            closes = month_end
        elif name == "QUARTER" and day.month % 3 == 0:
            closes = month_end
        elif name == "HALF_YEAR" and day.month in (6, 12):
            closes = month_end
        elif name == "YEAR" and day.month == 12:
            closes = month_end
        if closes is not None and start <= closes <= end and closes not in out:
            out.append(closes)
        day = month_end + _dt.timedelta(days=1)
    return out


def build_schedule(
    obligations: list[Obligation],
    *,
    start: _dt.date,
    days: int = 90,
) -> Schedule:
    """The next ``days`` of certified, recurring duties."""
    from sanhita.analyse.conflicts import TABLE_LIKE_CHARS

    end = start + _dt.timedelta(days=days)
    all_certified = [o for o in obligations if o.status is RuleStatus.CERTIFIED]

    # The same flattened tables that poison conflict detection produce sentence
    # fragments here: "password protected" is not a duty anybody schedules.
    oversized = {
        o.source.clause_id
        for o in all_certified
        if len(o.source.verbatim_text) > TABLE_LIKE_CHARS
    }
    certified = [o for o in all_certified if o.source.clause_id not in oversized]

    schedule = Schedule(
        start=start,
        end=end,
        certified=len(certified),
        excluded_rules=len(all_certified) - len(certified),
        excluded_clauses=sorted(oversized),
    )

    for o in sorted(certified, key=lambda o: o.id):
        if o.modality not in (Modality.MUST, Modality.SHOULD):
            continue
        deadline = o.deadline
        if deadline is None:
            continue

        requirement = f"{o.action.verb} {o.action.object}".strip()
        who = (
            o.certification.certified_by if o.certification else "unknown"
        )

        if deadline.kind is DeadlineKind.ABSOLUTE and deadline.absolute_date:
            if start <= deadline.absolute_date <= end:
                schedule.due.append(
                    Due(
                        on=deadline.absolute_date,
                        obligation_id=o.id,
                        clause_id=o.source.clause_id,
                        page=o.source.page,
                        actor=o.actor.value,
                        requirement=requirement,
                        period="fixed date",
                        certified_by=who,
                    )
                )
            continue

        if deadline.kind is DeadlineKind.END_OF_PERIOD and deadline.period:
            # A daily duty is a standing instruction, not a diary entry.
            if deadline.period.strip().upper() == "DAY":
                schedule.daily.append((o.source.clause_id, requirement))
                continue
            for on in _period_ends(deadline.period, start, end):
                schedule.due.append(
                    Due(
                        on=on,
                        obligation_id=o.id,
                        clause_id=o.source.clause_id,
                        page=o.source.page,
                        actor=o.actor.value,
                        requirement=requirement,
                        period=deadline.period.lower(),
                        certified_by=who,
                    )
                )
            continue

        # Everything else runs from an event. It has no date until the event
        # happens, and this module will not invent one.
        schedule.event_driven.append((o.source.clause_id, requirement))

    return schedule

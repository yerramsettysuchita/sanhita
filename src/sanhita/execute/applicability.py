"""Whether an obligation was owed at all, decided before evidence is looked at.

The engine used to skip any certified rule with no matching events. The comment
said a rule with no evidence is not a breach, which is true of *some* rules and
dangerously false of others:

* A rule that never applied to this firm has no events and no breach. Skipping
  it is right.
* A rule that applied every month for a year, and produced nothing, has no
  events either. Skipping it means the firm that ignored a duty completely
  looks exactly like the firm that never owed it.

The second case is the one an inspector cares about most, and it was invisible.

Fixing it needs a decision the engine could not previously make: **was this
obligation owed, in this window, by this firm?** That is what this module
answers, and it answers it from the rule's own trigger and deadline rather than
from whether evidence happens to exist.

Three verdicts, and the middle one is the point:

``NOT_APPLICABLE``   the rule cannot have fallen due here, so silence is correct
``EXPECTED``         it fell due a computable number of times, so silence is a gap
``UNDETERMINED``     applicability cannot be decided without inventing something

``UNDETERMINED`` is not a soft ``NOT_APPLICABLE``. An event-driven duty fires
when a trade or a breach happens, and how often that happened is a fact about
the firm's business that Sanhita does not hold. Guessing would either invent
breaches or hide them, so the verdict is recorded, surfaced, and left for a
person. The one thing it must never do is quietly become a pass.
"""

from __future__ import annotations

import calendar as _cal
import datetime as _dt
from dataclasses import dataclass
from enum import Enum

from sanhita.ir.enums import DeadlineKind, Modality, RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["Applicability", "Verdict", "assess_applicability", "expected_occasions"]

#: How many times a named period comes round in a year. Trading days for
#: daily duties, matching the burden metric so two screens cannot disagree.
_PER_YEAR = {"DAY": 250, "WEEK": 52, "MONTH": 12, "QUARTER": 4, "HALF_YEAR": 2, "YEAR": 1}


class Verdict(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    EXPECTED = "EXPECTED"
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class Applicability:
    """Whether a rule was owed in a window, how often, and on what grounds."""

    obligation_id: str
    clause_id: str
    verdict: Verdict
    #: How many times it fell due inside the window. Zero unless EXPECTED.
    occasions: int
    #: Why, in words a compliance officer can argue with.
    reason: str

    @property
    def is_expected(self) -> bool:
        return self.verdict is Verdict.EXPECTED


def _period_ends(period: str, start: _dt.date, end: _dt.date) -> list[_dt.date]:
    """The dates on which a named period closes, inside a window."""
    name = (period or "").strip().upper()
    if name not in _PER_YEAR:
        return []

    out: list[_dt.date] = []
    day = start
    while day <= end:
        if name == "DAY":
            # Weekends are not trading days. The engine's calendar does the
            # authoritative version of this; here it only needs to be close
            # enough to say "this fell due repeatedly".
            if day.weekday() < 5:
                out.append(day)
            day += _dt.timedelta(days=1)
            continue
        if name == "WEEK":
            closes = day + _dt.timedelta(days=6 - day.weekday())
            if start <= closes <= end and closes not in out:
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


def expected_occasions(
    obligation: Obligation, start: _dt.date, end: _dt.date
) -> list[_dt.date]:
    """The dates this obligation fell due inside the window.

    Only recurring duties produce dates. An event-driven duty has no date until
    the event happens, and this module will not invent one.
    """
    deadline = obligation.deadline
    if deadline is None or deadline.kind is not DeadlineKind.END_OF_PERIOD:
        return []
    if not deadline.period:
        return []
    return _period_ends(deadline.period, start, end)


def assess_applicability(
    obligation: Obligation,
    *,
    start: _dt.date,
    end: _dt.date,
) -> Applicability:
    """Decide whether this rule was owed in this window, before any evidence."""
    ident = obligation.obligation_id if hasattr(obligation, "obligation_id") else obligation.id
    common = {"obligation_id": ident, "clause_id": obligation.source.clause_id}

    if obligation.status is not RuleStatus.CERTIFIED:
        return Applicability(
            **common,
            verdict=Verdict.NOT_APPLICABLE,
            occasions=0,
            reason="the rule is not certified, so nothing is owed under it yet",
        )

    # A prohibition is breached by doing the thing, not by failing to file a
    # document. Absence of evidence is the expected state and is not a gap.
    if obligation.modality is Modality.MUST_NOT:
        return Applicability(
            **common,
            verdict=Verdict.NOT_APPLICABLE,
            occasions=0,
            reason=(
                "this clause forbids something rather than requiring it. There is "
                "no artifact whose absence would be a breach"
            ),
        )
    if obligation.modality is Modality.MAY:
        return Applicability(
            **common,
            verdict=Verdict.NOT_APPLICABLE,
            occasions=0,
            reason="this clause permits something rather than requiring it",
        )

    if not obligation.evidence:
        return Applicability(
            **common,
            verdict=Verdict.NOT_APPLICABLE,
            occasions=0,
            reason=(
                "the clause names no artifact to retain, so there is nothing whose "
                "absence could be detected"
            ),
        )

    deadline = obligation.deadline
    if deadline is None:
        return Applicability(
            **common,
            verdict=Verdict.UNDETERMINED,
            occasions=0,
            reason=(
                "a standing requirement with no deadline. It is always in force, "
                "so there is no date on which it can be said to have been missed"
            ),
        )

    if deadline.kind is DeadlineKind.ON_DEMAND:
        return Applicability(
            **common,
            verdict=Verdict.UNDETERMINED,
            occasions=0,
            reason=(
                "the clock starts on a demand from the regulator or a client. "
                "Whether any demand was made in this window is a fact about the "
                "firm that Sanhita does not hold"
            ),
        )

    if deadline.kind is DeadlineKind.END_OF_PERIOD and deadline.period:
        dates = expected_occasions(obligation, start, end)
        if not dates:
            return Applicability(
                **common,
                verdict=Verdict.NOT_APPLICABLE,
                occasions=0,
                reason=(
                    f"recurs every {deadline.period.lower()}, and no such period "
                    "closed inside this window"
                ),
            )
        return Applicability(
            **common,
            verdict=Verdict.EXPECTED,
            occasions=len(dates),
            reason=(
                f"recurs every {deadline.period.lower()}. "
                f"{len(dates)} period(s) closed between "
                f"{start.isoformat()} and {end.isoformat()}, so that many "
                "occasions were owed"
            ),
        )

    # Everything else runs from an event: a trade, a breach, a client request.
    trigger = obligation.trigger.kind if obligation.trigger else None
    return Applicability(
        **common,
        verdict=Verdict.UNDETERMINED,
        occasions=0,
        reason=(
            "fires on an event"
            + (f" ({trigger.value.lower()})" if trigger else "")
            + " rather than on a calendar. How many times that event occurred is "
            "a fact about the firm's business, so whether anything was owed here "
            "cannot be decided from the regulation alone"
        ),
    )

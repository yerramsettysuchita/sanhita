"""What is about to be missed, while there is still time to do something.

Problem Statement 2 puts the word "before" in the requirement:

    "...identifying and remediating compliance gaps before they become
     regulatory findings."

The gap report answers the retrospective question: what did this firm fail to
do. That is what an inspection produces, and by the time it is produced the
finding already exists. This module answers the other one. Given what falls due
in the next few weeks, and given how this firm has handled the same duty every
previous time it came round, which of those are going to be missed?

**The signal is the firm's own track record, not a model.** For every duty
falling due in the window, the evidence store already knows how many times that
obligation came round before and how many of those occasions produced the
artifact. A duty that has never once been evidenced is not a prediction, it is
an observation with a date attached to it.

Three tiers, in the order a compliance officer should read them:

``NEVER_EVIDENCED``  The duty has come round before and nothing was ever filed.
``OFTEN_MISSED``     Filed sometimes. More than half the occasions produced nothing.
``ON_RECORD``        Filed every time, or every time but one.

A duty with no history at all is reported separately as ``UNPROVEN`` rather than
being called clean, because an empty record is not a good record.

Nothing here calls a model. Same rules and same evidence in, same forecast out.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum

from sanhita.analyse.calendar import build_schedule
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["Forecast", "Outlook", "UpcomingDuty", "build_forecast"]

#: Miss more than this share of past occasions and the duty is at risk.
MISS_RATE_AT_RISK = 0.5

#: How far ahead to look, by default. Long enough to act, short enough that the
#: list is a to-do rather than a rulebook.
DEFAULT_HORIZON_DAYS = 30


class Outlook(str, Enum):
    NEVER_EVIDENCED = "NEVER_EVIDENCED"
    OFTEN_MISSED = "OFTEN_MISSED"
    UNPROVEN = "UNPROVEN"
    ON_RECORD = "ON_RECORD"

    @property
    def at_risk(self) -> bool:
        return self in (Outlook.NEVER_EVIDENCED, Outlook.OFTEN_MISSED)


@dataclass
class UpcomingDuty:
    """One duty falling due inside the window, with how it has gone before."""

    obligation_id: str
    clause_id: str
    page: int
    actor: str
    requirement: str
    period: str
    due_on: _dt.date | None
    #: True where the duty is owed every day, so it has no single next date.
    daily: bool = False

    occasions_before: int = 0
    filed_before: int = 0
    last_filed_on: _dt.date | None = None
    outlook: Outlook = Outlook.UNPROVEN

    @property
    def missed_before(self) -> int:
        return self.occasions_before - self.filed_before

    @property
    def miss_rate(self) -> float | None:
        if not self.occasions_before:
            return None
        return round(self.missed_before / self.occasions_before, 4)

    @property
    def days_away(self) -> int | None:
        if self.due_on is None:
            return None
        return (self.due_on - _dt.date.today()).days

    def to_json(self) -> dict:
        return {
            "obligation_id": self.obligation_id,
            "clause_id": self.clause_id,
            "page": self.page,
            "actor": self.actor,
            "requirement": self.requirement,
            "due_on": self.due_on.isoformat() if self.due_on else None,
            "daily": self.daily,
            "occasions_before": self.occasions_before,
            "filed_before": self.filed_before,
            "miss_rate": self.miss_rate,
            "outlook": self.outlook.value,
        }


@dataclass
class Forecast:
    start: _dt.date
    end: _dt.date
    evidence_label: str = ""
    duties: list[UpcomingDuty] = field(default_factory=list)
    certified: int = 0
    #: Rules dropped for coming from a flattened summary table.
    excluded_rules: int = 0

    @property
    def at_risk(self) -> list[UpcomingDuty]:
        return [d for d in self.duties if d.outlook.at_risk]

    @property
    def never_evidenced(self) -> list[UpcomingDuty]:
        return [d for d in self.duties if d.outlook is Outlook.NEVER_EVIDENCED]

    @property
    def often_missed(self) -> list[UpcomingDuty]:
        return [d for d in self.duties if d.outlook is Outlook.OFTEN_MISSED]

    @property
    def unproven(self) -> list[UpcomingDuty]:
        return [d for d in self.duties if d.outlook is Outlook.UNPROVEN]

    @property
    def on_record(self) -> list[UpcomingDuty]:
        return [d for d in self.duties if d.outlook is Outlook.ON_RECORD]

    def caveats(self) -> list[str]:
        notes = [
            "This looks forward from a track record, not from a model. A duty "
            "listed as at risk has come round before and produced nothing; that "
            "is an observation, not a guess.",
            f"Evidence read from: {self.evidence_label or 'no evidence store'}.",
            "Only certified rules appear. A proposed rule has not been signed "
            "by anybody, so nothing is owed under it yet.",
        ]
        if self.unproven:
            notes.append(
                f"{len(self.unproven)} duty(ies) have no history at all in this "
                "evidence store. They are listed separately rather than counted "
                "as clean, because an empty record is not a good record."
            )
        if any(d.daily for d in self.duties):
            notes.append(
                "Duties owed every day are listed once rather than repeated on "
                "every date in the window."
            )
        return notes

    def to_json(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "evidence_label": self.evidence_label,
            "at_risk": len(self.at_risk),
            "never_evidenced": len(self.never_evidenced),
            "often_missed": len(self.often_missed),
            "unproven": len(self.unproven),
            "on_record": len(self.on_record),
            "duties": [d.to_json() for d in self.duties],
            "caveats": self.caveats(),
        }


def build_forecast(
    obligations: list[Obligation],
    evidence=None,
    *,
    start: _dt.date | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> Forecast:
    """What falls due next, and which of it this firm has never managed before."""
    begin = start or _dt.date.today()
    end = begin + _dt.timedelta(days=horizon_days)

    schedule = build_schedule(obligations, start=begin, days=horizon_days)
    forecast = Forecast(
        start=begin,
        end=end,
        evidence_label=getattr(evidence, "label", "") if evidence else "",
        certified=schedule.certified,
        excluded_rules=schedule.excluded_rules,
    )

    certified = {
        o.id: o for o in obligations if o.status is RuleStatus.CERTIFIED
    }

    # One row per obligation, at its earliest occasion in the window. A monthly
    # duty falling due twice inside a 60 day horizon is still one duty with one
    # track record, and listing it twice would double-count the risk.
    earliest: dict[str, UpcomingDuty] = {}

    for due in schedule.due:
        existing = earliest.get(due.obligation_id)
        if existing is not None and existing.due_on and due.on >= existing.due_on:
            continue
        earliest[due.obligation_id] = UpcomingDuty(
            obligation_id=due.obligation_id,
            clause_id=due.clause_id,
            page=due.page,
            actor=due.actor,
            requirement=due.requirement,
            period=due.period,
            due_on=due.on,
        )

    # Daily duties are not placed on individual dates by the scheduler, and the
    # list it does keep is indexed by clause rather than by rule. Rebuilding
    # them from the obligations keeps one row per rule, which is what a track
    # record attaches to.
    daily_clauses = {clause_id for clause_id, _ in schedule.daily}
    for obligation in certified.values():
        if obligation.id in earliest:
            continue
        if obligation.source.clause_id not in daily_clauses:
            continue
        deadline = obligation.deadline
        if deadline is None or (deadline.period or "").strip().upper() != "DAY":
            continue
        earliest[obligation.id] = UpcomingDuty(
            obligation_id=obligation.id,
            clause_id=obligation.source.clause_id,
            page=obligation.source.page,
            actor=obligation.actor.value,
            requirement=f"{obligation.action.verb} {obligation.action.object}",
            period="DAY",
            due_on=None,
            daily=True,
        )

    for duty in earliest.values():
        if evidence is not None:
            past = [
                event
                for event in evidence.for_obligation(duty.obligation_id)
                if event.occurred_on < begin
            ]
            duty.occasions_before = len(past)
            filed = [e for e in past if e.filed_on is not None]
            duty.filed_before = len(filed)
            if filed:
                duty.last_filed_on = max(e.filed_on for e in filed)

        if not duty.occasions_before:
            duty.outlook = Outlook.UNPROVEN
        elif duty.filed_before == 0:
            duty.outlook = Outlook.NEVER_EVIDENCED
        elif (duty.miss_rate or 0) > MISS_RATE_AT_RISK:
            duty.outlook = Outlook.OFTEN_MISSED
        else:
            duty.outlook = Outlook.ON_RECORD

    order = {
        Outlook.NEVER_EVIDENCED: 0,
        Outlook.OFTEN_MISSED: 1,
        Outlook.UNPROVEN: 2,
        Outlook.ON_RECORD: 3,
    }
    forecast.duties = sorted(
        earliest.values(),
        key=lambda d: (
            order[d.outlook],
            d.due_on or _dt.date.max,
            d.clause_id,
        ),
    )
    return forecast

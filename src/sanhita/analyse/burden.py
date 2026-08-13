"""How much this regulation actually asks of each kind of firm.

Everybody in the market has an opinion about whether compliance load is
proportionate. Nobody has the number, because getting it means reading 399 pages
and counting, and the answer changes every time the circular is reissued.

It is arithmetic once the rules are typed. Every obligation names the actor it
binds, and a recurring deadline names how often it comes round. Multiply and
sum, and you have filings per year per intermediary type, with every figure
tracing to a clause.

**What this is not.** It is not a measure of effort. Filing a one-line
confirmation and building a cyber-resilience framework both count as one duty
here, and they are not remotely the same work. It counts *occasions on which the
regulation requires something*, which is a real quantity and a narrow one. The
screen says so, because a burden figure quoted without that caveat would deserve
to be taken apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sanhita.ir.enums import DeadlineKind, Modality, RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["ActorBurden", "BurdenReport", "measure_burden"]

#: How many times a year a named period comes round.
_PER_YEAR = {
    "DAY": 250,  # trading days, not calendar days
    "WEEK": 52,
    "MONTH": 12,
    "QUARTER": 4,
    "HALF_YEAR": 2,
    "YEAR": 1,
}


@dataclass
class ActorBurden:
    actor: str
    #: Distinct duties this actor carries.
    duties: int = 0
    certified: int = 0
    #: Duties that recur on a calendar, by period.
    recurring: dict[str, int] = field(default_factory=dict)
    #: Duties that fire on an event, so their count depends on business volume.
    event_driven: int = 0
    #: Duties with no deadline at all: standing requirements.
    standing: int = 0
    prohibitions: int = 0
    permissions: int = 0
    #: Clauses this actor's duties come from.
    clauses: set[str] = field(default_factory=set)

    @property
    def filings_per_year(self) -> int:
        """Occasions a year on which a calendar-driven duty comes round."""
        return sum(_PER_YEAR.get(p, 0) * n for p, n in self.recurring.items())

    @property
    def recurring_duties(self) -> int:
        return sum(self.recurring.values())

    def to_json(self) -> dict:
        return {
            "actor": self.actor,
            "duties": self.duties,
            "certified": self.certified,
            "clauses": len(self.clauses),
            "recurring": dict(sorted(self.recurring.items())),
            "recurring_duties": self.recurring_duties,
            "filings_per_year": self.filings_per_year,
            "event_driven": self.event_driven,
            "standing": self.standing,
            "prohibitions": self.prohibitions,
            "permissions": self.permissions,
        }


@dataclass
class BurdenReport:
    rules_counted: int = 0
    certified_only: bool = False
    actors: list[ActorBurden] = field(default_factory=list)

    @property
    def heaviest(self) -> ActorBurden | None:
        return self.actors[0] if self.actors else None

    def caveats(self) -> list[str]:
        basis = (
            "Only rules a person has certified are counted."
            if self.certified_only
            else "Every proposed and certified rule is counted, so this includes "
            "extraction the extractor has not had checked."
        )
        return [
            basis,
            "This counts occasions on which the regulation requires something. "
            "It is not a measure of effort: a one-line confirmation and a "
            "cyber-resilience framework each count as one duty, and they are not "
            "the same work.",
            "A daily duty is counted as 250 occasions a year, on trading days "
            "rather than calendar days. Weekly is 52, monthly 12, quarterly 4.",
            "Event-driven duties are excluded from the yearly figure entirely, "
            "because how often they fire depends on a firm's business volume "
            "rather than on the regulation.",
            "An actor appears only where a clause names it. Where the "
            "regulation writes a duty without naming who owes it, the extractor "
            "produces nothing and this undercounts.",
        ]

    def to_json(self) -> dict:
        return {
            "rules_counted": self.rules_counted,
            "certified_only": self.certified_only,
            "caveats": self.caveats(),
            "actors": [a.to_json() for a in self.actors],
        }


def measure_burden(
    obligations: list[Obligation], *, certified_only: bool = False
) -> BurdenReport:
    """Count what the regulation asks of each kind of firm."""
    live = [
        o
        for o in obligations
        if o.status in (RuleStatus.PROPOSED, RuleStatus.CERTIFIED)
    ]
    if certified_only:
        live = [o for o in live if o.status is RuleStatus.CERTIFIED]

    report = BurdenReport(rules_counted=len(live), certified_only=certified_only)
    buckets: dict[str, ActorBurden] = {}

    for o in live:
        actor = buckets.setdefault(o.actor.value, ActorBurden(actor=o.actor.value))
        actor.clauses.add(o.source.clause_id)

        if o.modality is Modality.MUST_NOT:
            actor.prohibitions += 1
            continue
        if o.modality is Modality.MAY:
            actor.permissions += 1
            continue

        actor.duties += 1
        if o.status is RuleStatus.CERTIFIED:
            actor.certified += 1

        deadline = o.deadline
        if deadline is None:
            actor.standing += 1
        elif deadline.kind is DeadlineKind.END_OF_PERIOD and deadline.period:
            period = deadline.period.strip().upper()
            actor.recurring[period] = actor.recurring.get(period, 0) + 1
        else:
            actor.event_driven += 1

    report.actors = sorted(
        buckets.values(), key=lambda a: (-a.duties, a.actor)
    )
    return report

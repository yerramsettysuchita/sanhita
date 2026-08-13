"""Is this firm's evidence still arriving, or did it stop in March?

An assessment is a photograph. It tells a firm where it stood against the
records it had on the day somebody ran it, and it is silent about everything
that happened since. That silence is the failure mode compliance software
actually has: the register was uploaded once during onboarding, the quarterly
filings kept happening in the real world, nobody uploaded them, and the product
either says nothing or keeps showing a green number from four months ago.

This module asks a different question from the engine's. The engine asks
whether a duty was discharged. This asks whether the firm's **records** are in
a state where that question can be answered at all.

The distinction matters and the screens must keep it. A rule with no records is
not a breach. It is very often a duty discharged perfectly on paper that nobody
uploaded, and calling it a breach would be exactly the fabrication the product
exists to avoid. What can be said, and is worth saying loudly, is that nothing
in this system knows either way.

Everything here is arithmetic over dates. No model runs, nothing is inferred,
and every signal names the rule and the dates it was computed from.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum

from sanhita.execute.applicability import expected_occasions
from sanhita.ir.enums import DeadlineKind, RuleStatus

__all__ = ["Signal", "RuleHealth", "EvidenceHealth", "assess_evidence_health"]

#: How far back to look. Long enough that a yearly duty shows up at all,
#: short enough that a firm is not judged on records from before it existed.
DEFAULT_LOOKBACK_DAYS = 400


class Signal(str, Enum):
    """What is true of one certified rule's records.

    None of these is a compliance verdict. They are statements about a filing
    cabinet, and the wording of each one is chosen so it cannot be read as a
    statement about the firm's conduct.
    """

    #: Records are arriving on schedule. The most recent occasion that has
    #: fallen due has a record against it.
    CURRENT = "CURRENT"
    #: Records exist, but the last one is older than the duty's own period, so
    #: at least one occasion has come round with nothing recorded.
    GONE_QUIET = "GONE_QUIET"
    #: This duty has fallen due inside the window and has no record at all.
    NEVER_RECORDED = "NEVER_RECORDED"
    #: Records exist for occasions that were never filed. The engine will call
    #: these breaches; here they are counted so the two screens agree.
    UNFILED = "UNFILED"
    #: An event-driven duty. It has no schedule, so nothing can be said about
    #: whether its records are late. Reported rather than hidden, because a
    #: silent omission looks like a clean bill of health.
    NO_SCHEDULE = "NO_SCHEDULE"

    @property
    def label(self) -> str:
        return {
            Signal.CURRENT: "Records up to date",
            Signal.GONE_QUIET: "Records have stopped",
            Signal.NEVER_RECORDED: "Never recorded",
            Signal.UNFILED: "Recorded but not filed",
            Signal.NO_SCHEDULE: "No schedule to judge against",
        }[self]

    @property
    def needs_attention(self) -> bool:
        return self in (Signal.GONE_QUIET, Signal.NEVER_RECORDED, Signal.UNFILED)

    @property
    def rank(self) -> int:
        """Worst first, and clerical last."""
        return {
            Signal.GONE_QUIET: 0,
            Signal.NEVER_RECORDED: 1,
            Signal.UNFILED: 2,
            Signal.NO_SCHEDULE: 3,
            Signal.CURRENT: 4,
        }[self]


@dataclass(frozen=True)
class RuleHealth:
    """The state of one certified rule's records, with the dates behind it."""

    obligation_id: str
    clause_id: str
    requirement: str
    signal: Signal
    #: How many times this duty fell due inside the window.
    occasions_due: int = 0
    #: How many of those have a record of any kind.
    occasions_recorded: int = 0
    #: Records that exist but name no filing date.
    unfiled: int = 0
    last_recorded_on: _dt.date | None = None
    #: The most recent occasion that fell due. The gap between this and
    #: ``last_recorded_on`` is the whole of the "gone quiet" judgement.
    last_due_on: _dt.date | None = None
    period: str = ""
    #: Who inside the firm this belongs to, where a binding says.
    process: str = ""
    function: str = ""

    @property
    def days_quiet(self) -> int | None:
        if self.last_recorded_on is None or self.last_due_on is None:
            return None
        return max(0, (self.last_due_on - self.last_recorded_on).days)

    @property
    def is_owned(self) -> bool:
        return bool(self.function or self.process)

    def describe(self) -> str:
        """One sentence about the records, never about the firm's conduct."""
        where = f" It belongs to {self.function}." if self.function else ""
        if self.signal is Signal.NEVER_RECORDED:
            return (
                f"This duty fell due {self.occasions_due} time(s) in the window "
                f"and no record of any of them has been uploaded. That is not a "
                f"finding: nothing here knows whether it was done.{where}"
            )
        if self.signal is Signal.GONE_QUIET:
            last = self.last_recorded_on.isoformat() if self.last_recorded_on else "never"
            due = self.last_due_on.isoformat() if self.last_due_on else "unknown"
            return (
                f"The last record is dated {last}. The duty has fallen due again "
                f"since, most recently on {due}, with nothing recorded against "
                f"it.{where}"
            )
        if self.signal is Signal.UNFILED:
            return (
                f"{self.unfiled} recorded occasion(s) name no filing date, so "
                f"the register itself says the artifact was never produced."
                f"{where}"
            )
        if self.signal is Signal.NO_SCHEDULE:
            return (
                "This duty is triggered by an event rather than by a calendar, "
                "so there is no schedule to judge its records against. Whether "
                "records are missing depends on whether the event happened."
            )
        return (
            f"{self.occasions_recorded} of {self.occasions_due} occasion(s) in "
            f"the window have a record, including the most recent."
        )


@dataclass
class EvidenceHealth:
    """Everything that can be said about one firm's records, as of one date."""

    as_of: _dt.date
    since: _dt.date
    #: The evidence store's own label, carried through so a report built on
    #: generated events can never be mistaken for one built on a firm's books.
    source: str = ""
    rules: list[RuleHealth] = field(default_factory=list)
    #: Records that arrived and are still waiting for a person to say which
    #: duty they discharge. Not a rule-level signal, because until somebody
    #: maps them they belong to no rule.
    awaiting_mapping: int = 0
    #: Total records read, and the span they cover.
    records: int = 0
    earliest_record: _dt.date | None = None
    latest_record: _dt.date | None = None
    #: When the position on the overview was last recorded, and whether the
    #: records have moved since.
    assessed_on: _dt.datetime | None = None
    assessment_is_stale: bool = False

    def of(self, signal: Signal | str) -> list[RuleHealth]:
        """Accepts the name as well as the member, so a template can ask."""
        wanted = Signal(signal)
        return [r for r in self.rules if r.signal is wanted]

    @property
    def watched(self) -> int:
        return len(self.rules)

    @property
    def needing_attention(self) -> int:
        return sum(1 for r in self.rules if r.signal.needs_attention)

    @property
    def current(self) -> int:
        return len(self.of(Signal.CURRENT))

    @property
    def days_since_last_record(self) -> int | None:
        if self.latest_record is None:
            return None
        return (self.as_of - self.latest_record).days

    @property
    def is_quiet(self) -> bool:
        """Nothing has arrived for a month. The single most useful alarm."""
        days = self.days_since_last_record
        return days is not None and days > 31

    def by_function(self) -> dict[str, list[RuleHealth]]:
        """Grouped the way the firm is staffed, because somebody has to chase."""
        grouped: dict[str, list[RuleHealth]] = {}
        for rule in self.rules:
            if rule.signal.needs_attention:
                grouped.setdefault(rule.function or "Not yet mapped", []).append(rule)
        return dict(
            sorted(grouped.items(), key=lambda kv: (kv[0] == "Not yet mapped", kv[0]))
        )

    def headline(self) -> str:
        """What a person should be told first, in one sentence."""
        if not self.records:
            return "No compliance records have been uploaded for this firm."
        if self.is_quiet:
            return (
                f"Nothing has been recorded for {self.days_since_last_record} days. "
                "The most recent record is dated "
                f"{self.latest_record.isoformat()}."
            )
        if self.needing_attention:
            return (
                f"{self.needing_attention} of {self.watched} certified duties have "
                "records that are missing, stopped or unfiled."
            )
        return (
            f"All {self.watched} certified duties with a schedule have a record "
            "against their most recent occasion."
        )


def assess_evidence_health(
    obligations,
    evidence,
    *,
    as_of: _dt.date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    controls=None,
    awaiting_mapping: int = 0,
    assessed_on: _dt.datetime | None = None,
    assessment_is_stale: bool = False,
) -> EvidenceHealth:
    """Read a firm's records and say what state they are in.

    Only certified rules are looked at. A proposal nobody signed places no duty
    on anybody, so its records cannot be missing.
    """
    today = as_of or _dt.date.today()
    since = today - _dt.timedelta(days=lookback_days)
    report = EvidenceHealth(
        as_of=today,
        since=since,
        source=getattr(evidence, "label", "") if evidence is not None else "",
        awaiting_mapping=awaiting_mapping,
        assessed_on=assessed_on,
        assessment_is_stale=assessment_is_stale,
    )

    if evidence is not None:
        report.records = len(evidence)
        window = evidence.window
        if window is not None:
            report.earliest_record, report.latest_record = window

    for obligation in obligations:
        if obligation.status is not RuleStatus.CERTIFIED:
            continue
        if not obligation.evidence:
            # Nothing was ever required of the firm's filing cabinet here, so
            # there is nothing about its records to report.
            continue
        report.rules.append(_health_of(obligation, evidence, since, today, controls))

    report.rules.sort(key=lambda r: (r.signal.rank, r.clause_id))
    return report


def _health_of(obligation, evidence, since, today, controls) -> RuleHealth:
    binding = controls.get(obligation.id) if controls is not None else None
    events = evidence.for_obligation(obligation.id) if evidence is not None else []
    recorded_dates = sorted({e.occurred_on for e in events})
    unfiled = sum(1 for e in events if e.filed_on is None)
    last_recorded = recorded_dates[-1] if recorded_dates else None

    common = {
        "obligation_id": obligation.id,
        "clause_id": obligation.source.clause_id,
        "requirement": f"{obligation.action.verb} {obligation.action.object}".strip(),
        "occasions_recorded": len(recorded_dates),
        "unfiled": unfiled,
        "last_recorded_on": last_recorded,
        "process": getattr(binding, "process", "") or "",
        "function": getattr(binding, "function", "") or "",
    }

    deadline = obligation.deadline
    recurring = (
        deadline is not None
        and deadline.kind is DeadlineKind.END_OF_PERIOD
        and bool(deadline.period)
    )
    if not recurring:
        # An event-driven duty has no date until the event happens, and this
        # module will not invent one. Unfiled records still say something.
        return RuleHealth(
            signal=Signal.UNFILED if unfiled else Signal.NO_SCHEDULE, **common
        )

    due = expected_occasions(obligation, since, today)
    last_due = due[-1] if due else None
    health = dict(common, occasions_due=len(due), last_due_on=last_due, period=deadline.period)

    if not due:
        # Certified, recurring, but its period has not closed inside the
        # window. Nothing is late because nothing has fallen due.
        return RuleHealth(signal=Signal.CURRENT, **health)
    if not recorded_dates:
        return RuleHealth(signal=Signal.NEVER_RECORDED, **health)
    if last_due is not None and last_recorded is not None and last_recorded < last_due:
        # An occasion closed after the most recent record. Whether the firm
        # discharged it is unknown, which is precisely the point.
        return RuleHealth(signal=Signal.GONE_QUIET, **health)
    if unfiled:
        return RuleHealth(signal=Signal.UNFILED, **health)
    return RuleHealth(signal=Signal.CURRENT, **health)

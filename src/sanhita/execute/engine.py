"""The rule engine.

The deterministic core the whole product is built to protect. It takes certified
rules and an evidence store and produces a gap report. It makes no network call,
loads no model, and consults nothing outside its arguments. Run it twice on the
same inputs and the two reports are identical, field for field.

Four rules hold here and each has a test:

  **Only certified rules execute.** A proposed rule is an extractor's opinion.
  It has not been read by a person and it must never be able to tell a firm it
  is in breach. Rejected and superseded rules are likewise inert.

  **No rule is silently skipped.** Anything the engine will not evaluate is
  returned as an ``Unevaluable`` with a reason. Skipping quietly would let the
  compliance rate climb every time the extractor got weaker, which is the same
  failure the coverage denominator was designed to prevent.

  **No convention is inherited.** A deadline whose day count is unspecified
  raises rather than defaulting. Certification already blocks that case, so
  reaching it means something upstream broke, and guessing would bake an
  interpretation into a breach notice.

  **Every finding cites its clause.** The citation is built from the signed
  artifact, not re-derived, so what the report quotes is exactly what the
  officer put their name to.
"""

from __future__ import annotations

import datetime as _dt

from sanhita.execute.calendar import (
    WEEKENDS_ONLY,
    DayCountUnresolved,
    TradingCalendar,
)
from sanhita.execute.evidence import ComplianceEvent, EvidenceStore
from sanhita.execute.report import Finding, GapReport, Outcome, Unevaluable
from sanhita.ir.enums import DayCount, DeadlineKind, Modality, RuleStatus
from sanhita.ir.schema import Deadline, Obligation

__all__ = ["RuleEngine", "due_date"]


#: Modalities that can be breached by failing to do something. A MAY confers a
#: permission, so there is nothing to test; a MUST_NOT is a prohibition, which
#: needs evidence of the forbidden act rather than evidence of an artifact, and
#: this engine checks artifacts.
_ENFORCEABLE = {Modality.MUST, Modality.SHOULD}


def due_date(
    deadline: Deadline, occurred_on: _dt.date, calendar: TradingCalendar
) -> _dt.date:
    """When the artifact was due, given the day the clock started.

    Raises ``DayCountUnresolved`` rather than assuming a convention.
    """
    if deadline.kind is DeadlineKind.ABSOLUTE:
        if deadline.absolute_date is None:
            raise ValueError("an absolute deadline carries no date")
        return deadline.absolute_date

    if deadline.kind is DeadlineKind.END_OF_PERIOD:
        end = calendar.end_of_period(occurred_on, deadline.period or "DAY")
        if deadline.offset_days:
            return _offset(end, deadline, calendar)
        return end

    if deadline.kind is DeadlineKind.RELATIVE:
        return _offset(occurred_on, deadline, calendar)

    raise ValueError(f"{deadline.kind.value} deadlines have no computable due date")


def _offset(
    start: _dt.date, deadline: Deadline, calendar: TradingCalendar
) -> _dt.date:
    if deadline.offset_months is not None:
        return calendar.add_months(start, deadline.offset_months)

    if deadline.offset_hours is not None:
        # An hours-valued deadline lands on the same day or the next one. We
        # hold dates, not times, so the honest reading is the day the clock
        # started for anything under 24 hours.
        return calendar.add_calendar_days(start, deadline.offset_hours // 24)

    days = deadline.offset_days or 0
    if days == 0:
        return start

    if deadline.business_days is DayCount.UNSPECIFIED:
        raise DayCountUnresolved(
            "this deadline never had its working-versus-calendar question "
            "answered, and the engine will not pick one"
        )
    if deadline.business_days is DayCount.BUSINESS:
        return calendar.add_working_days(start, days)
    return calendar.add_calendar_days(start, days)


class RuleEngine:
    """Runs certified rules against an evidence store."""

    def __init__(self, calendar: TradingCalendar | None = None) -> None:
        self.calendar = calendar or WEEKENDS_ONLY

    # ------------------------------------------------------------------- run

    def run(
        self,
        obligations: list[Obligation],
        evidence: EvidenceStore,
        *,
        as_of: _dt.date | None = None,
        window_days: int = 365,
    ) -> GapReport:
        """Evaluate every certified rule, including the ones with no evidence.

        ``as_of`` is the date the report is run for. An artifact that is not yet
        due is not a breach, so events whose due date is in the future are not
        counted either way.

        ``window_days`` is how far back applicability is assessed for a rule
        with no evidence at all. It has to be bounded: a monthly duty certified
        today has not been breached every month since the circular was issued,
        and reporting it that way would bury real findings under arithmetic.
        """
        today = as_of or _dt.date.today()
        window_start = today - _dt.timedelta(days=window_days)
        report = GapReport(
            evidence_label=evidence.label,
            calendar_name=self.calendar.name,
            run_at=_dt.datetime.now(_dt.timezone.utc),
        )

        certified = [o for o in obligations if o.status is RuleStatus.CERTIFIED]
        report.certified_rules = len(certified)

        for obligation in sorted(certified, key=lambda o: o.id):
            events = evidence.for_obligation(obligation.id)
            if not events:
                # This used to be a bare `continue`, on the reasoning that no
                # evidence is not a breach. That is true of a rule which never
                # fell due and false of one which fell due twelve times and
                # produced nothing, and the two were indistinguishable.
                #
                # So ask the question the skip was dodging: was anything owed
                # here? Only the applicability layer can answer it, and only
                # from the rule's own trigger and deadline. Where it says yes,
                # silence is a finding. Where it cannot tell, that is recorded
                # as undetermined and left for a person rather than counted
                # either way.
                self._no_evidence(obligation, report, window_start, today)
                continue

            problem = self._why_not(obligation)
            if problem is not None:
                report.unevaluable.append(
                    Unevaluable(
                        obligation_id=obligation.id,
                        clause_id=obligation.source.clause_id,
                        page=obligation.source.page,
                        reason=problem,
                    )
                )
                continue

            report.rules_evaluated += 1
            for event in events:
                outcome = self._check(obligation, event, today)
                if outcome is None:
                    continue  # not yet due
                report.events_checked += 1
                if isinstance(outcome, Finding):
                    report.findings.append(outcome)
                else:
                    report.satisfied += 1

        return report

    # ------------------------------------------------------------- decisions

    def _no_evidence(
        self,
        obligation: Obligation,
        report: GapReport,
        window_start: _dt.date,
        today: _dt.date,
    ) -> None:
        """Handle a certified rule the evidence store says nothing about."""
        from sanhita.execute.applicability import (
            Verdict,
            assess_applicability,
            expected_occasions,
        )

        verdict = assess_applicability(obligation, start=window_start, end=today)
        report.applicability.append(verdict)

        if verdict.verdict is Verdict.NOT_APPLICABLE:
            report.not_applicable += 1
            return

        if verdict.verdict is Verdict.UNDETERMINED:
            # Never a pass. A rule whose applicability cannot be decided is
            # reported as needing a person, and is excluded from the
            # compliance rate rather than quietly improving it.
            report.undetermined.append(
                Unevaluable(
                    obligation_id=obligation.id,
                    clause_id=obligation.source.clause_id,
                    page=obligation.source.page,
                    reason=verdict.reason,
                )
            )
            return

        # Owed, and nothing on file. One finding for the obligation rather than
        # one per occasion: twelve identical lines for the same untouched duty
        # is noise, and the occasion count carries the scale.
        problem = self._why_not(obligation)
        if problem is not None:
            report.unevaluable.append(
                Unevaluable(
                    obligation_id=obligation.id,
                    clause_id=obligation.source.clause_id,
                    page=obligation.source.page,
                    reason=problem,
                )
            )
            return

        report.rules_evaluated += 1
        # Counted apart from the occasions that had a record.
        #
        # These occasions were never evidenced, so the engine adjudicated none
        # of them one by one; it concluded once about the rule. Adding them to
        # "occasions checked" put five thousand unevidenced occasions into the
        # denominator of the compliance rate and one finding into the
        # numerator, so a firm with nothing failing still read 0% compliant.
        report.occasions_unevidenced += verdict.occasions
        citation = self._cite(obligation)
        occasions = expected_occasions(obligation, window_start, today)
        last_due = occasions[-1] if occasions else today
        report.findings.append(
            Finding(
                outcome=Outcome.NO_EVIDENCE,
                event_id=f"none:{obligation.id}",
                entity=report.evidence_label or "this firm",
                occurred_on=occasions[0] if occasions else window_start,
                due_on=last_due,
                filed_on=None,
                days_late=None,
                due_date_is_approximate=False,
                **citation,
            )
        )

    def _why_not(self, obligation: Obligation) -> str | None:
        """The reason this certified rule cannot be checked, or None."""
        if obligation.modality not in _ENFORCEABLE:
            return (
                f"modality is {obligation.modality.value}. This engine checks "
                "whether a required artifact was produced on time, and a "
                f"{obligation.modality.value} does not require one."
            )
        if obligation.deadline is None:
            return (
                "the clause sets no deadline, so there is no date against which "
                "a filing could be early or late"
            )
        if obligation.deadline.kind is DeadlineKind.ON_DEMAND:
            return (
                "the deadline runs from a demand by the regulator or the client. "
                "Until that demand is itself recorded as an event there is no "
                "clock to measure against."
            )
        if obligation.deadline.business_days is DayCount.UNSPECIFIED and (
            obligation.deadline.offset_days
        ):
            return (
                "the working-versus-calendar day question was never answered for "
                "this deadline, so no due date can be computed without inventing "
                "an interpretation"
            )
        if not obligation.evidence:
            return (
                "the clause names no artifact to retain, so there is nothing for "
                "the engine to look for in the evidence store"
            )
        return None

    def _check(
        self, obligation: Obligation, event: ComplianceEvent, today: _dt.date
    ) -> Finding | bool | None:
        """Compare one event against the rule. None means not yet due."""
        assert obligation.deadline is not None  # guarded by _why_not

        try:
            due = due_date(obligation.deadline, event.occurred_on, self.calendar)
        except (DayCountUnresolved, ValueError):
            # _why_not should have caught this. If it did not, refuse rather
            # than guess.
            return None

        if due > today and event.filed_on is None:
            return None  # still has time

        approximate = (
            obligation.deadline.business_days is DayCount.BUSINESS
            and not self.calendar.covers_date(due)
        )
        citation = self._cite(obligation)

        if event.filed_on is None:
            return Finding(
                outcome=Outcome.MISSING,
                event_id=event.id,
                entity=event.entity,
                occurred_on=event.occurred_on,
                due_on=due,
                filed_on=None,
                days_late=None,
                due_date_is_approximate=approximate,
                **citation,
            )

        if event.filed_on > due:
            return Finding(
                outcome=Outcome.LATE,
                event_id=event.id,
                entity=event.entity,
                occurred_on=event.occurred_on,
                due_on=due,
                filed_on=event.filed_on,
                days_late=(event.filed_on - due).days,
                due_date_is_approximate=approximate,
                **citation,
            )

        return True

    @staticmethod
    def _cite(obligation: Obligation) -> dict:
        """The citation, taken from the signed artifact rather than re-derived."""
        certification = obligation.certification
        if certification is None:  # pragma: no cover - guarded by status check
            raise ValueError(f"{obligation.id} is certified but carries no certificate")

        action = obligation.action
        requirement = f"{obligation.modality.value.lower()} {action.verb} {action.object}"
        if action.recipient:
            requirement += f" to {action.recipient}"

        return {
            "obligation_id": obligation.id,
            "clause_id": obligation.source.clause_id,
            "section": obligation.source.section,
            "page": obligation.source.page,
            "verbatim": obligation.source.verbatim_text,
            "requirement": requirement,
            "certified_by": certification.certified_by,
            "certified_at": certification.certified_at,
            "signature": certification.signature,
        }

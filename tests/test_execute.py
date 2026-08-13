"""The rule engine.

These tests guard the four promises the engine makes. Each of them is a way the
product could quietly become dishonest, and each has bitten a real compliance
tool at some point.

  Only certified rules execute.
  Nothing is silently skipped.
  No day-count convention is inherited.
  Every finding cites its clause.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

import pytest

from sanhita.execute import (
    ComplianceEvent,
    DayCountUnresolved,
    EvidenceStore,
    Outcome,
    RuleEngine,
    TradingCalendar,
    due_date,
)
from sanhita.execute.synthetic import generate
from sanhita.ir.enums import (
    Actor,
    DayCount,
    DeadlineKind,
    Modality,
    RuleStatus,
    TriggerKind,
)
from sanhita.ir.schema import (
    Action,
    Certification,
    Deadline,
    EvidenceReq,
    Obligation,
    SourceAnchor,
    Trigger,
)

JAN = _dt.date(2026, 1, 1)
VERBATIM = "The stock broker shall report the short-collection."
SHA = hashlib.sha256(VERBATIM.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ fixtures


def _relative(
    days: int = 5, count: DayCount = DayCount.BUSINESS
) -> Deadline:
    """A RELATIVE deadline. The IR requires an anchor event, correctly."""
    return Deadline(
        kind=DeadlineKind.RELATIVE,
        offset_days=days,
        business_days=count,
        anchor_event="trade.date",
    )


def _obligation(
    clause_id: str = "40.1.8",
    *,
    status: RuleStatus = RuleStatus.CERTIFIED,
    modality: Modality = Modality.MUST,
    deadline: Deadline | None = None,
    evidence: bool = True,
    certified: bool = True,
) -> Obligation:
    """The IR requires the obligation id to agree with its clause, so it is
    derived here rather than passed in separately."""
    return Obligation(
        id=f"SB-{clause_id}-a",
        actor=Actor.STOCK_BROKER,
        modality=modality,
        action=Action(verb="report", object="short-collection of margin"),
        trigger=Trigger(kind=TriggerKind.EVENT, expression="short collection observed"),
        deadline=deadline if deadline is not None else _relative(),
        evidence=[EvidenceReq(artifact_type="report")] if evidence else [],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=137,
            char_span=(0, len(VERBATIM)),
            verbatim_text=VERBATIM,
            sha256=SHA,
        ),
        confidence=0.9,
        status=status,
        certification=Certification(
            certified_by="R Yerramsetty, Compliance Officer",
            certified_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc),
            signature="a" * 64,
        )
        if certified and status is RuleStatus.CERTIFIED
        else None,
    )


def _event(
    event_id: str = "EV-1",
    *,
    obligation_id: str = "SB-40.1.8-a",
    occurred: _dt.date = JAN,
    filed: _dt.date | None = None,
) -> ComplianceEvent:
    return ComplianceEvent(
        id=event_id,
        obligation_id=obligation_id,
        entity="Demo Broking Pvt Ltd",
        occurred_on=occurred,
        artifact_type="report",
        filed_on=filed,
    )


def _store(*events: ComplianceEvent) -> EvidenceStore:
    return EvidenceStore(label="test events", events=list(events))


# --------------------------------------------------- only certified rules run


@pytest.mark.parametrize(
    "status", [RuleStatus.PROPOSED, RuleStatus.REJECTED, RuleStatus.SUPERSEDED]
)
def test_an_uncertified_rule_never_executes(status):
    """An extractor's opinion must not be able to tell a firm it is in breach."""
    rule = _obligation(status=status)
    report = RuleEngine().run(
        [rule], _store(_event(occurred=JAN)), as_of=_dt.date(2026, 3, 1)
    )

    assert report.certified_rules == 0
    assert report.findings == []
    assert report.events_checked == 0


def test_a_certified_rule_does_execute():
    report = RuleEngine().run(
        [_obligation()], _store(_event(occurred=JAN)), as_of=_dt.date(2026, 3, 1)
    )
    assert report.certified_rules == 1
    assert report.events_checked == 1


# ------------------------------------------------------- nothing is skipped


def test_a_rule_that_cannot_be_checked_is_reported_not_dropped():
    """The alternative is a compliance rate that rises as the tool gets worse."""
    rule = _obligation(deadline=Deadline(kind=DeadlineKind.ON_DEMAND))
    report = RuleEngine().run([rule], _store(_event()), as_of=_dt.date(2026, 3, 1))

    assert report.findings == []
    assert len(report.unevaluable) == 1
    assert report.unevaluable[0].clause_id == "40.1.8"
    assert "demand" in report.unevaluable[0].reason


def test_every_unevaluable_reason_is_a_sentence_a_person_can_act_on():
    cases = [
        _obligation("40.1.1", modality=Modality.MAY),
        _obligation("40.1.2", deadline=Deadline(kind=DeadlineKind.ON_DEMAND)),
        # The IR already requires a MUST to name an artifact, so a rule with no
        # evidence requirement can only be a SHOULD.
        _obligation("40.1.3", modality=Modality.SHOULD, evidence=False),
    ]
    events = _store(*[_event(f"EV-{i}", obligation_id=o.id) for i, o in enumerate(cases)])
    report = RuleEngine().run(cases, events, as_of=_dt.date(2026, 3, 1))

    assert len(report.unevaluable) == 3
    for item in report.unevaluable:
        assert len(item.reason) > 40, f"{item.obligation_id} explains nothing"


def test_an_unevaluable_rule_is_not_counted_as_satisfied():
    rule = _obligation(deadline=Deadline(kind=DeadlineKind.ON_DEMAND))
    report = RuleEngine().run([rule], _store(_event()), as_of=_dt.date(2026, 3, 1))
    assert report.satisfied == 0
    assert report.compliance_rate is None


def test_an_empty_run_reports_no_rate_rather_than_full_compliance():
    """100% because nothing was checked is the most dangerous number here."""
    report = RuleEngine().run([], _store(), as_of=JAN)
    assert report.compliance_rate is None
    assert report.events_checked == 0


# --------------------------------------------- no convention is inherited


def test_an_unspecified_day_count_refuses_rather_than_guessing():
    deadline = _relative(count=DayCount.UNSPECIFIED)
    with pytest.raises(DayCountUnresolved):
        due_date(deadline, JAN, TradingCalendar(name="test"))


def test_a_rule_with_an_unresolved_day_count_is_declared_unevaluable():
    rule = _obligation(deadline=_relative(count=DayCount.UNSPECIFIED))
    report = RuleEngine().run([rule], _store(_event()), as_of=_dt.date(2026, 3, 1))
    assert len(report.unevaluable) == 1
    assert "working-versus-calendar" in report.unevaluable[0].reason


def test_working_and_calendar_days_give_different_due_dates():
    """The whole reason the question has to be answered by a person."""
    calendar = TradingCalendar(name="test")
    thursday = _dt.date(2026, 1, 1)  # a Thursday

    business = due_date(_relative(5, DayCount.BUSINESS), thursday, calendar)
    calendar_days = due_date(_relative(5, DayCount.CALENDAR), thursday, calendar)
    assert business != calendar_days
    assert business == _dt.date(2026, 1, 8)
    assert calendar_days == _dt.date(2026, 1, 6)


def test_a_holiday_pushes_the_due_date_out():
    holiday = _dt.date(2026, 1, 2)
    calendar = TradingCalendar(name="with one holiday", holidays=frozenset({holiday}))
    deadline = _relative(1, DayCount.BUSINESS)
    assert due_date(deadline, _dt.date(2026, 1, 1), calendar) == _dt.date(2026, 1, 5)


def test_a_due_date_outside_the_holiday_list_is_marked_approximate():
    """We say when we are guessing, rather than averaging the doubt away."""
    calendar = TradingCalendar(
        name="jan only", holidays=frozenset({_dt.date(2026, 1, 2)})
    )
    rule = _obligation()
    report = RuleEngine(calendar).run(
        [rule], _store(_event(occurred=_dt.date(2026, 6, 1))), as_of=_dt.date(2026, 9, 1)
    )
    assert report.findings and report.findings[0].due_date_is_approximate
    assert any("holiday list covers" in note for note in report.caveats())


def test_months_clamp_to_the_end_of_the_target_month():
    calendar = TradingCalendar(name="test")
    assert calendar.add_months(_dt.date(2025, 12, 31), 2) == _dt.date(2026, 2, 28)


# -------------------------------------------------- every finding cites its clause


def test_a_missing_filing_is_a_finding_that_cites_the_clause():
    report = RuleEngine().run(
        [_obligation()], _store(_event(filed=None)), as_of=_dt.date(2026, 3, 1)
    )

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.outcome is Outcome.MISSING
    assert finding.clause_id == "40.1.8"
    assert finding.page == 137
    assert finding.certified_by == "R Yerramsetty, Compliance Officer"
    assert finding.signature == "a" * 64
    assert "short-collection" in finding.verbatim


def test_a_late_filing_counts_the_days():
    due = due_date(_obligation().deadline, JAN, TradingCalendar(name="t"))
    report = RuleEngine().run(
        [_obligation()],
        _store(_event(filed=due + _dt.timedelta(days=3))),
        as_of=_dt.date(2026, 3, 1),
    )
    assert report.findings[0].outcome is Outcome.LATE
    assert report.findings[0].days_late == 3


def test_a_filing_on_the_due_date_is_not_a_breach():
    due = due_date(_obligation().deadline, JAN, TradingCalendar(name="t"))
    report = RuleEngine().run(
        [_obligation()], _store(_event(filed=due)), as_of=_dt.date(2026, 3, 1)
    )
    assert report.findings == []
    assert report.satisfied == 1


def test_something_not_yet_due_is_not_a_breach():
    report = RuleEngine().run(
        [_obligation()], _store(_event(occurred=_dt.date(2026, 2, 28))),
        as_of=_dt.date(2026, 3, 1),
    )
    assert report.findings == []
    assert report.events_checked == 0


def test_no_finding_can_exist_without_its_citation():
    """The claim the product rests on, asserted rather than assumed."""
    rules = [_obligation(f"40.1.{i}") for i in range(4)]
    events = _store(*[_event(f"EV-{i}", obligation_id=r.id) for i, r in enumerate(rules)])
    report = RuleEngine().run(rules, events, as_of=_dt.date(2026, 3, 1))

    assert report.findings
    for finding in report.findings:
        assert finding.clause_id and finding.page > 0
        assert finding.verbatim.strip()
        assert finding.certified_by.strip()
        assert len(finding.signature) == 64


# ------------------------------------------------------------- determinism


def test_two_runs_over_the_same_inputs_agree_exactly():
    rules = [_obligation()]
    events = _store(_event(), _event("EV-2", occurred=_dt.date(2026, 1, 8)))

    first = RuleEngine().run(rules, events, as_of=_dt.date(2026, 3, 1)).to_json()
    second = RuleEngine().run(rules, events, as_of=_dt.date(2026, 3, 1)).to_json()

    first.pop("run_at")
    second.pop("run_at")
    assert first == second


def test_generated_evidence_is_reproducible_from_its_seed():
    rules = [_obligation(f"40.1.{i}") for i in range(3)]
    kwargs = dict(
        calendar=TradingCalendar(name="t"),
        start=JAN,
        end=_dt.date(2026, 6, 30),
        seed="fixed",
    )
    a = generate(rules, **kwargs)
    b = generate(rules, **kwargs)
    assert [e.to_json() for e in a.events] == [e.to_json() for e in b.events]


def test_generated_evidence_says_it_is_generated():
    """Nobody must be able to mistake a demo run for a firm's real books."""
    store = generate(
        [_obligation()],
        calendar=TradingCalendar(name="t"),
        start=JAN,
        end=_dt.date(2026, 6, 30),
    )
    assert "generated" in store.label
    assert "not real books" in store.label

    report = RuleEngine().run([_obligation()], store, as_of=_dt.date(2026, 12, 1))
    assert any("generated" in note for note in report.caveats())


def test_the_generator_only_makes_events_for_certified_rules():
    proposed = _obligation("40.1.1", status=RuleStatus.PROPOSED)
    certified = _obligation("40.1.2")
    store = generate(
        [proposed, certified],
        calendar=TradingCalendar(name="t"),
        start=JAN,
        end=_dt.date(2026, 6, 30),
    )
    assert store.obligation_ids == {"SB-40.1.2-a"}

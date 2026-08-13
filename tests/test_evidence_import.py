"""Importing a firm's own filing records.

The gap report is only as good as what it runs against. Two rules hold:

  A row that cannot be read is reported with its line number, never skipped.
  An importer that quietly drops malformed rows will one day drop the row that
  mattered, and the report will look cleaner for it.

  Evidence pointing at a rule that does not exist is refused, because it cannot
  be checked against anything and counting it would inflate the compliance rate.
"""

from __future__ import annotations

import datetime as _dt

from sanhita.execute.importer import TEMPLATE_CSV, read_csv

KNOWN = {"SB-19.5.2.8-a", "SB-40.1.8-a"}

GOOD = (
    "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
    "SB-40.1.8-a,Demo Broking Pvt Ltd,2026-03-16,2026-03-20,report,REF-1\n"
    "SB-40.1.8-a,Demo Broking Pvt Ltd,2026-04-16,,report,\n"
)


def test_a_clean_file_imports():
    result = read_csv(GOOD, known_obligations=KNOWN)
    assert result.ok
    assert result.accepted == 2
    assert result.store is not None
    assert len(result.store) == 2


def test_an_unfiled_row_is_kept_as_never_filed():
    """Empty is not the same as late, and the engine treats them differently."""
    result = read_csv(GOOD, known_obligations=KNOWN)
    events = result.store.for_obligation("SB-40.1.8-a")
    assert events[0].filed_on == _dt.date(2026, 3, 20)
    assert events[1].filed_on is None


def test_the_shipped_template_imports_cleanly():
    result = read_csv(TEMPLATE_CSV, known_obligations={"SB-19.5.2.8-a"})
    assert result.ok, [e.problem for e in result.errors]


def test_a_bad_date_names_its_line_and_is_not_imported():
    text = GOOD + "SB-40.1.8-a,Demo Broking Pvt Ltd,16/03/2026,,report,\n"
    result = read_csv(text, known_obligations=KNOWN)

    assert result.accepted == 2, "the bad row must not be imported"
    assert len(result.errors) == 1
    assert result.errors[0].line == 4
    assert "YYYY-MM-DD" in result.errors[0].problem


def test_evidence_for_an_unknown_rule_is_refused():
    text = GOOD + "SB-99.9.9-z,Demo Broking Pvt Ltd,2026-03-16,,report,\n"
    result = read_csv(text, known_obligations=KNOWN)

    assert result.accepted == 2
    assert "SB-99.9.9-z" in result.unknown_obligations
    assert "does not exist" in result.errors[0].problem


def test_a_filing_before_the_event_is_refused():
    text = GOOD + "SB-40.1.8-a,Demo Broking Pvt Ltd,2026-03-16,2026-03-01,report,\n"
    result = read_csv(text, known_obligations=KNOWN)
    assert "cannot be filed before" in result.errors[0].problem


def test_a_missing_entity_is_refused():
    text = GOOD + "SB-40.1.8-a,,2026-03-16,,report,\n"
    result = read_csv(text, known_obligations=KNOWN)
    assert "entity is empty" in result.errors[0].problem


def test_a_missing_required_column_is_explained_not_crashed():
    result = read_csv("entity,occurred_on\nDemo,2026-03-16\n", known_obligations=KNOWN)
    assert result.store is None
    assert "obligation_id" in result.errors[0].problem


def test_an_empty_file_is_explained():
    result = read_csv("", known_obligations=KNOWN)
    assert result.store is None
    assert result.errors


def test_every_rejection_says_what_was_wrong():
    text = (
        "obligation_id,entity,occurred_on,filed_on\n"
        "SB-40.1.8-a,Demo,not-a-date,\n"
        ",Demo,2026-03-16,\n"
        "SB-40.1.8-a,,2026-03-16,\n"
    )
    result = read_csv(text, known_obligations=KNOWN)
    assert len(result.errors) == 3
    for error in result.errors:
        assert error.line >= 2
        assert len(error.problem) > 20, error.problem


def test_imported_evidence_drives_the_engine():
    """End to end: a CSV in, findings out, with no generated events involved."""
    import hashlib

    from sanhita.execute import RuleEngine, TradingCalendar
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

    text = "The stock broker shall report the short-collection."
    rule = Obligation(
        id="SB-40.1.8-a",
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(verb="report", object="short collection"),
        trigger=Trigger(kind=TriggerKind.EVENT, expression="short collection"),
        deadline=Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=5,
            business_days=DayCount.CALENDAR,
            anchor_event="trade.date",
        ),
        evidence=[EvidenceReq(artifact_type="report")],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section="40",
            clause_id="40.1.8",
            page=95,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=0.9,
        status=RuleStatus.CERTIFIED,
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc),
            signature="c" * 64,
        ),
    )

    result = read_csv(GOOD, known_obligations={"SB-40.1.8-a"})
    report = RuleEngine(TradingCalendar(name="test")).run(
        [rule], result.store, as_of=_dt.date(2026, 6, 1)
    )

    assert report.events_checked == 2
    assert report.breaches == 1, "the unfiled row is the breach"
    assert report.findings[0].clause_id == "40.1.8"
    assert "imported" in report.caveats()[0]

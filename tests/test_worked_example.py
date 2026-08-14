"""The README's worked example, compiled and verified.

Clause 40.1.8 of the Master Circular for Stock Brokers:

    "As like in derivatives segments, the TMs/CMs shall report to the
     Stock Exchange on T+5 day the actual short-collection/ non-
     collection of all margins from clients."

Its neighbour 40.1.6 carries the exemption, and the section heading carries the
footnote that supplies the regulatory lineage. Together they exercise every
field of the IR against text that is actually in the document.

This is a test rather than a snippet in a markdown file so the README cannot
drift away from a schema that no longer accepts it.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from sanhita.ir import (
    Action,
    Actor,
    Certification,
    Commencement,
    CommencementKind,
    Condition,
    ConditionKind,
    DayCount,
    Deadline,
    DeadlineKind,
    EvidenceReq,
    Modality,
    Obligation,
    RuleStatus,
    SourceAnchor,
    Trigger,
    TriggerKind,
    obligation_id,
    sha256_hex,
)
from tests.conftest import requires_corpus

CIRCULAR = "SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/90"
KEY = "worked-example-key"

VERBATIM = (
    "40.1.8. As like in derivatives segments, the TMs/CMs shall report to the\n"
    "Stock Exchange on T+5 day the actual short-collection/ non-\n"
    "collection of all margins from clients."
)


def build() -> Obligation:
    """Clause 40.1.8 compiled by hand, every field populated."""
    return Obligation(
        id=obligation_id("SB", "40.1.8", 0),
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(
            verb="report",
            object="actual short-collection or non-collection of margins",
            qualifiers=["for all margins", "in respect of clients"],
            recipient="Stock Exchange",
            medium="exchange reporting portal",
        ),
        trigger=Trigger(
            kind=TriggerKind.SCHEDULE,
            expression="settlement.cycle.completed",
            recurrence="FREQ=DAILY",
        ),
        deadline=Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=5,
            business_days=DayCount.BUSINESS,
            anchor_event="trade.date",
            commencement=Commencement(
                kind=CommencementKind.FIXED_DATE,
                fixed_date=_dt.date(2020, 9, 1),
            ),
        ),
        conditions=[
            Condition(
                kind=ConditionKind.EXEMPTION,
                expression="client.is_institutional_investor",
                parameters={"source_clause": "40.1.6"},
            ),
            Condition(
                kind=ConditionKind.EXEMPTION,
                expression="securities.early_payin_made",
                parameters={"source_clause": "40.1.6"},
            ),
            Condition(
                kind=ConditionKind.THRESHOLD,
                expression="upfront_margin_collected_pct >= min_upfront_pct",
                parameters={"min_upfront_pct": 20},
            ),
        ],
        evidence=[
            EvidenceReq(
                artifact_type="REPORT_FILING",
                retention_period_days=1825,
                producible_on_demand=True,
                description="T+5 margin short-collection report filed with the exchange",
            ),
            EvidenceReq(
                artifact_type="REGISTER",
                retention_period_days=1825,
                producible_on_demand=True,
                description="Client-wise margin collection register backing the filing",
            ),
        ],
        penalty_ref="40.3 (exchange disciplinary action per CIR/DNPD/7/2011)",
        source=SourceAnchor(
            circular_id=CIRCULAR,
            section="40",
            clause_id="40.1.8",
            page=95,
            char_span=(184271, 184443),
            verbatim_text=VERBATIM,
            sha256=sha256_hex(VERBATIM),
            source_circulars=[
                "CIR/HO/MIRSD/DOP/CIR/P/2019/139",
                "SEBI/HO/MIRSD/DOP/CIR/P/2020/146",
                "SEBI/HO/MIRSD/DOP/CIR/P/2020/173",
            ],
            earliest_source_date=_dt.date(2019, 11, 19),
        ),
        confidence=0.88,
        status=RuleStatus.PROPOSED,
        version="1.0.0",
    )


def test_the_worked_example_validates():
    rule = build()
    assert rule.id == "SB-40.1.8-a"
    assert rule.modality is Modality.MUST
    assert rule.evidence, "a MUST must carry evidence"
    assert rule.deadline.offset_days == 5
    assert rule.deadline.business_days is DayCount.BUSINESS
    assert rule.source.earliest_source_date == _dt.date(2019, 11, 19)


def test_the_worked_example_certifies_and_locks():
    certified = build().certify(
        certified_by="compliance.officer@firm.example",
        key=KEY,
        at=_dt.datetime(2026, 8, 4, 9, 30, tzinfo=_dt.timezone.utc),
        note="Verified against p95 of the master circular.",
    )
    assert certified.status is RuleStatus.CERTIFIED
    assert certified.verify_signature(KEY)
    assert isinstance(certified.certification, Certification)

    from sanhita.ir import CertifiedImmutableError

    with pytest.raises(CertifiedImmutableError):
        certified.deadline = None


def test_the_worked_example_signature_is_reproducible():
    """Same content, same key, same moment -> same signature, on any machine."""
    moment = _dt.datetime(2026, 8, 4, 9, 30, tzinfo=_dt.timezone.utc)
    a = build().certify(certified_by="officer", key=KEY, at=moment)
    b = build().certify(certified_by="officer", key=KEY, at=moment)
    assert a.certification.signature == b.certification.signature
    assert a.canonical_json() == b.canonical_json()


@requires_corpus
def test_the_worked_example_matches_the_real_document(parsed, footnote_report):
    """The hand-compiled anchor must agree with what the parser actually found."""
    node = parsed.get("40.1.8")
    assert node is not None
    rule = build()

    assert node.page == rule.source.page
    assert node.char_span == rule.source.char_span
    assert node.text == rule.source.verbatim_text
    assert node.sha256 == rule.source.sha256

    # Lineage comes from footnote 54, which is attached to the section heading.
    lineage = footnote_report.by_clause().get("40")
    assert lineage, "section 40 should carry a footnote"
    refs = set(lineage[0].all_refs)
    assert set(rule.source.source_circulars) <= refs
    assert lineage[0].dated == rule.source.earliest_source_date

    # And the exemption really is where the condition says it is.
    exemption = parsed.get("40.1.6")
    assert "exempted from" in exemption.text
    assert "institutional investors" in exemption.text

"""The Obligation IR: validators, deadline encoding, and certification locking."""

from __future__ import annotations

import datetime as _dt
import hashlib

import pytest
from pydantic import ValidationError

from sanhita.ir import (
    Action,
    Actor,
    CertifiedImmutableError,
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
    ObligationSet,
    RuleStatus,
    SourceAnchor,
    Trigger,
    TriggerKind,
    obligation_id,
    suffix_for_index,
)

KEY = "test-signing-key"
VERBATIM = (
    "The stock broker shall issue a daily margin statement to the client "
    "by end of the next trading day."
)


def anchor(clause_id: str = "40.1.8", text: str = VERBATIM) -> SourceAnchor:
    return SourceAnchor(
        circular_id="SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/90",
        section=clause_id.split(".")[0],
        clause_id=clause_id,
        page=248,
        char_span=(0, len(text)),
        verbatim_text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def obligation(**overrides) -> Obligation:
    base = dict(
        id=obligation_id("SB", "40.1.8", 0),
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(verb="issue", object="daily margin statement", recipient="client"),
        trigger=Trigger(kind=TriggerKind.SCHEDULE, expression="trading.day.close", recurrence="FREQ=DAILY"),
        deadline=Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=1,
            business_days=DayCount.BUSINESS,
            anchor_event="trade.date",
        ),
        evidence=[EvidenceReq(artifact_type="DISPATCH_LOG", retention_period_days=1825)],
        source=anchor(),
        confidence=0.91,
    )
    base.update(overrides)
    return Obligation(**base)


# ---------------------------------------------------------------- validators


def test_must_requires_evidence():
    """A MUST with no evidence cannot be executed against an evidence store."""
    with pytest.raises(ValidationError, match="EvidenceReq"):
        obligation(evidence=[])


def test_may_does_not_require_evidence():
    rule = obligation(modality=Modality.MAY, evidence=[])
    assert rule.modality is Modality.MAY


def test_schedule_trigger_requires_recurrence():
    with pytest.raises(ValidationError, match="recurrence"):
        Trigger(kind=TriggerKind.SCHEDULE, expression="daily.close")


def test_recurrence_on_a_non_schedule_trigger_is_refused():
    with pytest.raises(ValidationError, match="meaningless"):
        Trigger(kind=TriggerKind.EVENT, expression="trade.executed", recurrence="FREQ=DAILY")


def test_recurrence_must_be_rrule_shaped():
    with pytest.raises(ValidationError, match="FREQ="):
        Trigger(kind=TriggerKind.SCHEDULE, expression="x", recurrence="every day")


@pytest.mark.parametrize("value", [-0.01, 1.01, 2.0])
def test_confidence_must_be_within_zero_and_one(value):
    with pytest.raises(ValidationError):
        obligation(confidence=value)


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_confidence_bounds_are_inclusive(value):
    assert obligation(confidence=value).confidence == value


def test_source_hash_must_match_the_verbatim_text():
    with pytest.raises(ValidationError, match="provenance"):
        SourceAnchor(
            circular_id="X",
            section="114",
            clause_id="40.1.8",
            page=1,
            char_span=(0, 5),
            verbatim_text="hello",
            sha256="0" * 64,
        )


def test_id_clause_segment_must_match_the_source_clause():
    with pytest.raises(ValidationError, match="does not match"):
        obligation(id="SB-999.9-a")


def test_threshold_condition_requires_parameters():
    with pytest.raises(ValidationError, match="parameter"):
        Condition(kind=ConditionKind.THRESHOLD, expression="value > threshold")

    ok = Condition(
        kind=ConditionKind.THRESHOLD,
        expression="value > threshold_inr",
        parameters={"threshold_inr": 1000000},
    )
    assert ok.parameters["threshold_inr"] == 1000000


def test_evidence_artifact_type_is_normalised():
    assert EvidenceReq(artifact_type="dispatch log").artifact_type == "DISPATCH_LOG"
    assert EvidenceReq(artifact_type="client-ack").artifact_type == "CLIENT_ACK"
    assert EvidenceReq(artifact_type="DISPATCH_LOG").is_known_type
    assert not EvidenceReq(artifact_type="NOVEL_THING").is_known_type


# ----------------------------------------------------------------- deadlines


def test_relative_deadline_encodes_t_plus_one_working_day():
    deadline = Deadline(
        kind=DeadlineKind.RELATIVE,
        offset_days=1,
        business_days=DayCount.BUSINESS,
        anchor_event="trade.date",
    )
    assert (deadline.offset_days, deadline.business_days) == (1, DayCount.BUSINESS)


def test_relative_deadline_encodes_within_seven_days_of_an_event():
    deadline = Deadline(
        kind=DeadlineKind.RELATIVE,
        offset_days=7,
        business_days=DayCount.CALENDAR,
        anchor_event="client.complaint.received",
    )
    assert deadline.anchor_event == "client.complaint.received"


def test_end_of_period_encodes_end_of_day_t_and_quarterly():
    end_of_day = Deadline(kind=DeadlineKind.END_OF_PERIOD, period="DAY", offset_days=0)
    quarterly = Deadline(kind=DeadlineKind.END_OF_PERIOD, period="quarter")
    assert end_of_day.period == "DAY"
    assert quarterly.period == "QUARTER"


def test_absolute_deadline_encodes_a_fixed_date():
    deadline = Deadline(kind=DeadlineKind.ABSOLUTE, absolute_date=_dt.date(2026, 3, 31))
    assert deadline.absolute_date == _dt.date(2026, 3, 31)


def test_on_demand_deadline_carries_no_horizon():
    assert Deadline(kind=DeadlineKind.ON_DEMAND).offset_days is None


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"kind": DeadlineKind.RELATIVE, "offset_days": 1}, "anchor_event"),
        ({"kind": DeadlineKind.RELATIVE, "anchor_event": "x"}, "offset_days"),
        ({"kind": DeadlineKind.ABSOLUTE}, "absolute_date"),
        ({"kind": DeadlineKind.END_OF_PERIOD}, "period"),
        ({"kind": DeadlineKind.ON_DEMAND, "offset_days": 3}, "no offset"),
        ({"kind": DeadlineKind.END_OF_PERIOD, "period": "FORTNIGHT"}, "period must be"),
    ],
)
def test_malformed_deadlines_are_refused(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        Deadline(**kwargs)


def test_phased_conditional_commencement_is_representable():
    """The headline amendment case: paras 46.1-46.11 commence on an external event."""
    conditional = Deadline(
        kind=DeadlineKind.RELATIVE,
        offset_days=1,
        business_days=DayCount.BUSINESS,
        anchor_event="payin.deadline",
        commencement=Commencement(
            kind=CommencementKind.CONDITIONAL_ON_EVENT,
            offset_days=90,
            condition="exchanges issue operational guidelines",
        ),
    )
    assert conditional.commencement.condition == "exchanges issue operational guidelines"
    assert conditional.commencement.offset_days == 90

    # ... while paras 46.12-46.14 commence a fixed period after issuance.
    from_issuance = Commencement(kind=CommencementKind.RELATIVE_TO_ISSUANCE, offset_days=180)
    assert from_issuance.offset_days == 180


def test_commencement_shapes_are_validated():
    with pytest.raises(ValidationError, match="fixed_date"):
        Commencement(kind=CommencementKind.FIXED_DATE)
    with pytest.raises(ValidationError, match="offset_days"):
        Commencement(kind=CommencementKind.RELATIVE_TO_ISSUANCE)
    with pytest.raises(ValidationError, match="condition"):
        Commencement(kind=CommencementKind.CONDITIONAL_ON_EVENT)


# ------------------------------------------------------------ certification


def test_certification_produces_a_new_object_and_leaves_the_original_alone():
    proposed = obligation()
    certified = proposed.certify(certified_by="A. Mehta", key=KEY)

    assert proposed.status is RuleStatus.PROPOSED
    assert proposed.certification is None
    assert certified.status is RuleStatus.CERTIFIED
    assert certified.certification.certified_by == "A. Mehta"
    assert certified.certification.locked is True


def test_certified_obligation_raises_on_mutation():
    certified = obligation().certify(certified_by="A. Mehta", key=KEY)

    with pytest.raises(CertifiedImmutableError):
        certified.modality = Modality.MAY
    with pytest.raises(CertifiedImmutableError):
        certified.confidence = 0.1
    with pytest.raises(CertifiedImmutableError):
        del certified.penalty_ref

    assert certified.modality is Modality.MUST


def test_proposed_obligation_remains_mutable():
    rule = obligation()
    rule.penalty_ref = "SEBI Act s.15HB"
    assert rule.penalty_ref == "SEBI Act s.15HB"


def test_value_objects_are_frozen():
    rule = obligation()
    with pytest.raises(ValidationError):
        rule.action.verb = "withhold"
    with pytest.raises(ValidationError):
        rule.source.page = 1


def test_signature_verifies_and_detects_tampering():
    certified = obligation().certify(certified_by="A. Mehta", key=KEY)
    assert certified.verify_signature(KEY)
    assert not certified.verify_signature("wrong-key")

    # Field assignment is blocked, but an in-place list mutation is not; the
    # signature is what catches it.
    certified.conditions.append(
        Condition(kind=ConditionKind.EXEMPTION, expression="client.is_institutional")
    )
    assert not certified.verify_signature(KEY)


def test_certifying_twice_is_refused():
    certified = obligation().certify(certified_by="A. Mehta", key=KEY)
    with pytest.raises(CertifiedImmutableError):
        certified.certify(certified_by="B. Rao", key=KEY)


def test_signature_excludes_confidence_but_covers_the_source_hash():
    """Confidence is extractor telemetry, not normative content."""
    rule = obligation()
    payload = rule.signing_payload()
    assert "confidence" not in payload
    assert "certification" not in payload
    assert payload["source"]["sha256"] == rule.source.sha256


def test_certified_status_requires_a_certification_block():
    with pytest.raises(ValidationError, match="requires a Certification"):
        obligation(status=RuleStatus.CERTIFIED)


# -------------------------------------------------------- ids and multiplicity


@pytest.mark.parametrize(
    "index, expected",
    [(0, "a"), (1, "b"), (25, "z"), (26, "aa"), (27, "ab"), (51, "az"), (52, "ba")],
)
def test_suffixes_are_bijective_base_26(index, expected):
    assert suffix_for_index(index) == expected


def test_one_clause_can_yield_several_obligations_with_stable_ids():
    """"...shall issue a statement and shall retain proof of dispatch" is two duties."""
    drafts = [
        dict(
            actor=Actor.STOCK_BROKER,
            modality=Modality.MUST,
            action=Action(verb="issue", object="daily margin statement"),
            trigger=Trigger(
                kind=TriggerKind.SCHEDULE, expression="trading.day.close", recurrence="FREQ=DAILY"
            ),
            evidence=[EvidenceReq(artifact_type="DISPATCH_LOG")],
            source=anchor(),
            confidence=0.9,
        ),
        dict(
            actor=Actor.STOCK_BROKER,
            modality=Modality.MUST,
            action=Action(verb="retain", object="proof of dispatch"),
            trigger=Trigger(kind=TriggerKind.CONTINUOUS, expression="always"),
            evidence=[EvidenceReq(artifact_type="DISPATCH_LOG", retention_period_days=1825)],
            source=anchor(),
            confidence=0.85,
        ),
    ]
    built = ObligationSet.build("40.1.8", drafts)
    assert [o.id for o in built.obligations] == ["SB-40.1.8-a", "SB-40.1.8-b"]

    # Rebuilding from the same ordered drafts must reproduce the same ids.
    again = ObligationSet.build("40.1.8", drafts)
    assert again.canonical_json() == built.canonical_json()


def test_obligation_set_rejects_a_member_from_another_clause():
    with pytest.raises(ValidationError, match="anchors to clause"):
        ObligationSet(clause_id="114.3", obligations=[obligation()])


def test_canonical_json_is_stable_across_equivalent_construction_orders():
    """Two obligations built with fields supplied in different orders must agree."""
    first = obligation(
        conditions=[
            Condition(kind=ConditionKind.PRECONDITION, expression="client.is_active"),
            Condition(
                kind=ConditionKind.THRESHOLD,
                expression="margin > threshold_inr",
                parameters={"threshold_inr": 1000000, "currency": "INR"},
            ),
        ]
    )
    second = Obligation(
        source=anchor(),
        confidence=0.91,
        id=obligation_id("SB", "40.1.8", 0),
        evidence=[EvidenceReq(retention_period_days=1825, artifact_type="DISPATCH_LOG")],
        conditions=[
            Condition(kind=ConditionKind.PRECONDITION, expression="client.is_active"),
            Condition(
                expression="margin > threshold_inr",
                parameters={"currency": "INR", "threshold_inr": 1000000},
                kind=ConditionKind.THRESHOLD,
            ),
        ],
        deadline=Deadline(
            anchor_event="trade.date",
            business_days=DayCount.BUSINESS,
            offset_days=1,
            kind=DeadlineKind.RELATIVE,
        ),
        trigger=Trigger(
            recurrence="FREQ=DAILY", expression="trading.day.close", kind=TriggerKind.SCHEDULE
        ),
        action=Action(recipient="client", object="daily margin statement", verb="issue"),
        modality=Modality.MUST,
        actor=Actor.STOCK_BROKER,
    )

    assert first.canonical_json() == second.canonical_json()
    assert first.canonical_bytes() == second.canonical_bytes()
    # And therefore they certify to the same signature.
    moment = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    a = first.certify(certified_by="A", key=KEY, at=moment)
    b = second.certify(certified_by="A", key=KEY, at=moment)
    assert a.certification.signature == b.certification.signature


def test_qualifiers_are_order_independent():
    a = Action(verb="issue", object="statement", qualifiers=["in electronic form", "daily"])
    b = Action(verb="issue", object="statement", qualifiers=["daily", "in electronic form"])
    assert a.qualifiers == b.qualifiers

# --------------------------------------------------------------- conditions


def test_the_condition_field_does_not_claim_to_hold_predicates():
    """It holds prose. It said "machine-evaluable predicate" for a while, which
    is the kind of contract a reader would build on and then find unmet."""
    from sanhita.ir.schema import Condition

    described = Condition.model_fields["expression"].description
    assert "predicate" not in described.lower() or "not a predicate" in described.lower()
    assert "own words" in described.lower()


def test_the_condition_docstring_states_what_cannot_be_built_on_it():
    """The limit is the useful part: prose is enough to explain applicability to
    a person, and not enough to prove anything."""
    from sanhita.ir.schema import Condition

    doc = Condition.__doc__.lower()
    assert "prose" in doc
    assert "constraint solver" in doc or "proof" in doc


def test_a_threshold_still_has_to_carry_its_number():
    """Prose or not, a threshold with no number is not a threshold."""
    with pytest.raises(ValidationError):
        Condition(kind=ConditionKind.THRESHOLD, expression="not less than 15%")

    ok = Condition(
        kind=ConditionKind.THRESHOLD,
        expression="not less than 15%",
        parameters={"threshold_pct": 15},
    )
    assert ok.parameters["threshold_pct"] == 15
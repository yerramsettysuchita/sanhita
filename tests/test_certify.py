"""Certification lifecycle: propose, certify, amend, reject, and the audit trail."""

from __future__ import annotations

import datetime as _dt

import pytest

from sanhita.certify import RuleRegistry
from sanhita.certify.ledger import Transition, diff_obligations
from sanhita.certify.lifecycle import CertificationError, bump_version
from sanhita.ir.enums import DayCount, Modality, RuleStatus
from sanhita.ir.schema import CertifiedImmutableError, Deadline, DeadlineKind, EvidenceReq

from tests.test_worked_example import build as build_worked_example

KEY = "lifecycle-test-key"


@pytest.fixture()
def registry() -> RuleRegistry:
    reg = RuleRegistry()
    reg.propose(build_worked_example())
    return reg


def test_propose_registers_and_logs(registry: RuleRegistry):
    assert len(registry) == 1
    current = registry.current("SB-40.1.8-a")
    assert current.status is RuleStatus.PROPOSED
    assert len(registry.ledger) == 1
    assert list(registry.ledger)[0].transition is Transition.PROPOSED


def test_certify_locks_signs_and_is_immutable(registry: RuleRegistry):
    certified = registry.certify("SB-40.1.8-a", by="s.yerramsetty", key=KEY)

    assert certified.status is RuleStatus.CERTIFIED
    assert certified.certification.certified_by == "s.yerramsetty"
    assert certified.certification.locked is True
    assert certified.verify_signature(KEY)

    with pytest.raises(CertifiedImmutableError):
        certified.deadline = None


def test_the_proposed_version_survives_certification(registry: RuleRegistry):
    """The pre-certification state must stay recoverable for audit."""
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    history = registry.history("SB-40.1.8-a")
    assert [o.status for o in history] == [RuleStatus.PROPOSED, RuleStatus.CERTIFIED]


def test_certifying_twice_is_refused(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    with pytest.raises(CertifiedImmutableError):
        registry.certify("SB-40.1.8-a", by="someone.else", key=KEY)


def test_amend_creates_a_new_version_and_supersedes_the_old(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)

    amended = registry.amend(
        "SB-40.1.8-a",
        {"deadline": Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=2,
            business_days=DayCount.BUSINESS,
            anchor_event="trade.date",
        )},
        by="officer",
        note="T+5 shortened to T+2",
    )

    assert amended.version == "1.1.0"
    assert amended.status is RuleStatus.PROPOSED
    assert amended.certification is None
    assert amended.deadline.offset_days == 2

    statuses = [o.status for o in registry.history("SB-40.1.8-a")]
    assert statuses == [
        RuleStatus.PROPOSED,
        RuleStatus.CERTIFIED,
        RuleStatus.SUPERSEDED,
        RuleStatus.PROPOSED,
    ]

    # The superseded version keeps its signature so a historical run replays.
    superseded = registry.history("SB-40.1.8-a")[2]
    assert superseded.certification is not None
    assert superseded.certification.signature


def test_amend_records_a_field_level_diff(registry: RuleRegistry):
    registry.amend(
        "SB-40.1.8-a",
        {"deadline": Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=2,
            business_days=DayCount.BUSINESS,
            anchor_event="trade.date",
        )},
        by="officer",
    )
    entry = [e for e in registry.ledger if e.transition is Transition.AMENDED][-1]
    assert "deadline.offset_days" in entry.changes
    assert entry.changes["deadline.offset_days"] == (5, 2)


def test_amend_revalidates_the_ir(registry: RuleRegistry):
    """An edit that breaks an invariant must fail loudly, not produce a bad rule."""
    with pytest.raises(Exception):
        registry.amend("SB-40.1.8-a", {"evidence": []}, by="officer")


def test_amend_cannot_forge_status_or_certification(registry: RuleRegistry):
    with pytest.raises(CertificationError):
        registry.amend("SB-40.1.8-a", {"status": RuleStatus.CERTIFIED}, by="x")
    with pytest.raises(CertificationError):
        registry.amend("SB-40.1.8-a", {"id": "SB-1.1-a"}, by="x")


def test_amend_requires_at_least_one_edit(registry: RuleRegistry):
    with pytest.raises(CertificationError):
        registry.amend("SB-40.1.8-a", {}, by="officer")


def test_reject_retains_the_reason(registry: RuleRegistry):
    rejected = registry.reject("SB-40.1.8-a", by="officer", reason="Actor is wrong: this binds the exchange.")
    assert rejected.status is RuleStatus.REJECTED
    entry = list(registry.ledger)[-1]
    assert entry.transition is Transition.REJECTED
    assert "Actor is wrong" in entry.note


def test_reject_requires_a_reason(registry: RuleRegistry):
    with pytest.raises(CertificationError):
        registry.reject("SB-40.1.8-a", by="officer", reason="   ")


def test_a_certified_rule_cannot_be_rejected(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    with pytest.raises(CertifiedImmutableError):
        registry.reject("SB-40.1.8-a", by="officer", reason="changed my mind")


def test_a_rejected_rule_cannot_be_certified(registry: RuleRegistry):
    registry.reject("SB-40.1.8-a", by="officer", reason="not a real duty")
    with pytest.raises(CertificationError):
        registry.certify("SB-40.1.8-a", by="officer", key=KEY)


# --------------------------------------------------------------------- ledger


def test_the_ledger_is_hash_chained_and_verifies(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    registry.amend("SB-40.1.8-a", {"penalty_ref": "40.3"}, by="officer")
    assert registry.ledger.verify_chain() == []


def test_editing_a_ledger_entry_breaks_the_chain(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    from dataclasses import replace

    entries = list(registry.ledger)
    tampered = replace(entries[0], actor="someone.else")
    from sanhita.certify.ledger import AuditLedger

    broken = AuditLedger([tampered, *entries[1:]])
    problems = broken.verify_chain()
    assert problems
    assert any("content altered" in p for p in problems)


def test_removing_a_ledger_entry_breaks_the_chain(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    registry.amend("SB-40.1.8-a", {"penalty_ref": "40.3"}, by="officer")
    from sanhita.certify.ledger import AuditLedger

    entries = list(registry.ledger)
    broken = AuditLedger([entries[0], *entries[2:]])
    assert broken.verify_chain()


def test_every_transition_is_recorded(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    registry.amend("SB-40.1.8-a", {"penalty_ref": "40.3"}, by="officer")
    kinds = [e.transition for e in registry.ledger]
    assert kinds == [
        Transition.PROPOSED,
        Transition.CERTIFIED,
        Transition.SUPERSEDED,
        Transition.AMENDED,
    ]
    assert all(e.actor for e in registry.ledger)
    assert all(e.at.tzinfo is not None for e in registry.ledger)


# ----------------------------------------------------------------- signatures


def test_verify_signatures_passes_on_untampered_rules(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    report = registry.verify_signatures(KEY)
    assert report.checked == 1
    assert report.valid == 1
    assert report.ok


def test_verify_signatures_detects_in_place_tampering(registry: RuleRegistry):
    certified = registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    # Field assignment is blocked, but a list is still mutable in place.
    certified.evidence.append(EvidenceReq(artifact_type="FORGED_DOC"))
    report = registry.verify_signatures(KEY)
    assert not report.ok
    assert "SB-40.1.8-a" in report.tampered


def test_verify_signatures_fails_on_the_wrong_key(registry: RuleRegistry):
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    assert not registry.verify_signatures("a-different-key").ok


# ------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "start, level, expected",
    [("1.0.0", "minor", "1.1.0"), ("1.4.2", "major", "2.0.0"), ("2.1.0", "patch", "2.1.1")],
)
def test_version_bumping(start, level, expected):
    assert bump_version(start, level=level) == expected


def test_diff_ignores_audit_metadata():
    """Re-extracting a rule unchanged must not read as an amendment."""
    first = build_worked_example()
    second = build_worked_example()
    object.__setattr__(second, "confidence", 0.5) if False else None
    changed = second.model_copy(update={"confidence": 0.11}, deep=True)
    assert diff_obligations(first, changed) == {}


def test_certified_and_superseded_rules_are_both_signed(registry: RuleRegistry):
    """A superseded rule must remain verifiable, or history cannot be replayed."""
    registry.certify("SB-40.1.8-a", by="officer", key=KEY)
    registry.amend("SB-40.1.8-a", {"penalty_ref": "40.3"}, by="officer")
    superseded = [
        o for o in registry.history("SB-40.1.8-a") if o.status is RuleStatus.SUPERSEDED
    ][0]
    assert superseded.verify_signature(KEY)


def test_certification_timestamps_are_utc(registry: RuleRegistry):
    moment = _dt.datetime(2026, 8, 4, 12, 0)
    certified = registry.certify("SB-40.1.8-a", by="officer", key=KEY, at=moment)
    assert certified.certification.certified_at.tzinfo is _dt.timezone.utc

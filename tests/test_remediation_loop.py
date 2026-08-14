"""Gap to closed, on a real certified rule from the real circular.

Problem Statement 2 asks for gaps to be identified *and remediated*. The product
identified them and stopped, so a compliance officer read the gap screen and
wrote the gap into a spreadsheet.

These tests exercise the loop the PS actually describes:

    certified rule -> evidence -> deterministic check -> gap
    -> task -> owner -> deadline -> corrected evidence -> re-check -> closed

The property worth defending is in ``test_a_task_cannot_be_closed_by_asserting``:
closure is not something a person can declare. It is what the deterministic
engine returns when the same certified rule is run again.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

import pytest

from sanhita.execute.applicability import Verdict, assess_applicability
from sanhita.execute.engine import RuleEngine
from sanhita.execute.evidence import ComplianceEvent, EvidenceStore
from sanhita.execute.report import Outcome
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
from sanhita.remediate import Priority, RemediationStore, TaskStatus
from sanhita.remediate.service import recheck_task, suggested_due_date
from sanhita.remediate.tasks import RemediationError
from tests.conftest import requires_corpus

UTC = _dt.timezone.utc
TODAY = _dt.date(2026, 8, 11)


def _monthly_rule(clause_id: str = "40.1.8") -> Obligation:
    """A certified monthly filing duty, the shape most of the corpus is."""
    text = f"Clause {clause_id}: the broker shall file the monthly return."
    return Obligation(
        id=f"SB-{clause_id}-a",
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(verb="file", object="the monthly return"),
        trigger=Trigger(
            kind=TriggerKind.SCHEDULE, expression="monthly", recurrence="FREQ=MONTHLY"
        ),
        deadline=Deadline(kind=DeadlineKind.END_OF_PERIOD, period="MONTH"),
        evidence=[EvidenceReq(artifact_type="return")],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=95,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=0.9,
        status=RuleStatus.CERTIFIED,
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=_dt.datetime(2026, 1, 1, tzinfo=UTC),
            signature="f" * 64,
        ),
    )


# ═══════════════════════════════════════════════════ applicability ══


def test_a_duty_owed_and_never_filed_is_now_a_finding():
    """The defect this whole layer exists to fix.

    Before applicability, a rule with no events was skipped, so a firm that
    ignored a monthly duty for a year was indistinguishable from one that never
    owed it.
    """
    report = RuleEngine().run(
        [_monthly_rule()], EvidenceStore(label="empty"), as_of=TODAY
    )

    # A finding, and deliberately not a breach. The duty fell due and there is
    # no record of it either way, which is very often one discharged on paper
    # nobody uploaded. `breaches` counts what the records prove.
    assert len(report.findings) == 1
    assert report.breaches == 0
    assert report.unverified == 1
    finding = report.findings[0]
    assert finding.outcome is Outcome.NO_EVIDENCE
    assert finding.severity == "high"
    # It still cites the clause. A finding without a citation is an opinion.
    assert finding.clause_id == "40.1.8"
    assert finding.certified_by == "A Named Officer"


def test_a_prohibition_with_no_evidence_is_not_a_breach():
    """A clause forbidding something has no artifact whose absence is a gap."""
    rule = _monthly_rule().model_copy(update={"modality": Modality.MUST_NOT})
    report = RuleEngine().run([rule], EvidenceStore(label="empty"), as_of=TODAY)

    assert report.breaches == 0
    assert report.not_applicable == 1


def test_an_event_driven_duty_is_undetermined_not_passed():
    """How often a trade happened is a fact about the firm, not the regulation.

    The important half is the second assertion: undetermined must never be
    counted as a pass, or a tool gets safer-looking the less it understands.
    """
    rule = _monthly_rule().model_copy(
        update={
            "deadline": Deadline(
                kind=DeadlineKind.RELATIVE,
                offset_days=5,
                business_days=DayCount.BUSINESS,
                anchor_event="trade.date",
            ),
            "trigger": Trigger(kind=TriggerKind.EVENT, expression="a trade"),
        }
    )
    report = RuleEngine().run([rule], EvidenceStore(label="empty"), as_of=TODAY)

    assert report.undetermined, "an event-driven duty must be flagged for a person"
    assert report.breaches == 0
    assert report.satisfied == 0, "undetermined must not become a pass"


def test_applicability_states_its_grounds():
    """A verdict a compliance officer cannot argue with is not auditable."""
    verdict = assess_applicability(
        _monthly_rule(), start=_dt.date(2026, 1, 1), end=TODAY
    )

    assert verdict.verdict is Verdict.EXPECTED
    assert verdict.occasions == 7  # Jan through Jul closed by 11 Aug
    assert "recurs every month" in verdict.reason


# ═════════════════════════════════════════════ the remediation loop ══


def _store(tmp_path) -> RemediationStore:
    return RemediationStore.load(tmp_path / "remediation.json")


def test_a_gap_becomes_a_task_with_an_owner_and_a_deadline(tmp_path):
    rule = _monthly_rule()
    report = RuleEngine().run([rule], EvidenceStore(label="empty"), as_of=TODAY)
    gap = report.findings[0]

    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id=gap.event_id,
        obligation_id=gap.obligation_id,
        clause_id=gap.clause_id,
        company="Demo Stockbroker Ltd",
        title="Monthly return never filed",
        by="A Named Officer",
        owner="Compliance Operations",
        priority=Priority.HIGH,
        due_date=suggested_due_date("HIGH", today=TODAY),
    )

    assert task.task_id.startswith("REM-40.1.8-")
    assert task.status is TaskStatus.OPEN
    assert task.owner == "Compliance Operations"
    assert task.due_date == TODAY + _dt.timedelta(days=7)
    assert store.log.verify() == (True, "")


def test_raising_the_same_gap_twice_does_not_fork_the_work(tmp_path):
    """Two tasks for one breach means two people fixing it and neither sure."""
    store = _store(tmp_path)
    first = store.open_for_gap(
        gap_id="g1", obligation_id="SB-40.1.8-a", clause_id="40.1.8",
        company="X", title="t", by="officer",
    )
    second = store.open_for_gap(
        gap_id="g1", obligation_id="SB-40.1.8-a", clause_id="40.1.8",
        company="X", title="t", by="officer",
    )

    assert first.task_id == second.task_id
    assert len(store.tasks) == 1


def test_a_task_cannot_be_closed_by_asserting(tmp_path):
    """The property the whole design turns on.

    If a person could mark their own remediation verified, closure would mean
    nothing to an inspector.
    """
    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id="g1", obligation_id="SB-40.1.8-a", clause_id="40.1.8",
        company="X", title="t", by="officer",
    )

    for status in (TaskStatus.VERIFIED, TaskStatus.CLOSED):
        with pytest.raises(RemediationError, match="cannot be set by hand"):
            store.set_status(task.task_id, status, by="someone in a hurry")

    assert store.get(task.task_id).status is TaskStatus.OPEN


def test_the_full_loop_reaches_closed(tmp_path):
    """Gap, task, owner, evidence, re-check, closed. The whole PS sentence."""
    rule = _monthly_rule()
    empty = EvidenceStore(label="Demo Stockbroker Ltd, synthetic demo evidence")

    # 1. The deterministic check finds a gap.
    #
    # An unverifiable one: the firm has filed nothing at all, so the duty fell
    # due with no record either way. The remediation loop runs on it exactly as
    # it runs on a proven breach, which is the point of testing it here.
    before = RuleEngine().run([rule], empty, as_of=TODAY)
    assert len(before.findings) == 1
    assert before.unverified == 1
    gap = before.findings[0]

    # 2. A task is raised, owned, with a deadline.
    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id=gap.event_id,
        obligation_id=rule.id,
        clause_id=rule.source.clause_id,
        company="Demo Stockbroker Ltd",
        title="Monthly return never filed",
        by="A Named Officer",
        priority=Priority.HIGH,
        due_date=suggested_due_date("HIGH", today=TODAY),
        source_rule_version=rule.version,
    )
    store.assign(task.task_id, owner="R. Sharma", by="A Named Officer", team="Operations")
    assert store.get(task.task_id).status is TaskStatus.IN_PROGRESS

    # 3. The firm files the returns it owed. Synthetic company evidence.
    fixed = EvidenceStore(label="Demo Stockbroker Ltd, synthetic demo evidence")
    for month in range(1, 8):
        occurred = _dt.date(2026, month, 28)
        fixed.add(
            ComplianceEvent(
                id=f"EV-{month:03d}",
                obligation_id=rule.id,
                entity="Demo Stockbroker Ltd",
                occurred_on=occurred,
                artifact_type="return",
                filed_on=occurred,
                reference=f"RET-{month:03d}",
            )
        )
    store.attach_evidence(
        task.task_id, [f"EV-{m:03d}" for m in range(1, 8)], by="R. Sharma"
    )
    assert store.get(task.task_id).status is TaskStatus.READY_FOR_RECHECK

    # 4. The same certified rule runs again. This is what closes it.
    result = recheck_task(
        store, task.task_id, [rule], fixed, by="A Named Officer", as_of=TODAY
    )

    assert result.evaluated
    assert not result.still_failing
    assert result.closed
    closed = store.get(task.task_id)
    assert closed.status is TaskStatus.CLOSED
    assert closed.verified_at is not None
    assert closed.closed_at is not None

    # 5. And the whole life of it is on an intact chain.
    intact, problem = store.log.verify()
    assert intact, problem
    kinds = [e.transition.value for e in store.log.for_task(task.task_id)]
    assert kinds == [
        "CREATED", "ASSIGNED", "EVIDENCE_ATTACHED", "RECHECKED", "VERIFIED", "CLOSED",
    ]


def test_a_re_check_that_still_fails_reopens_rather_than_closes(tmp_path):
    rule = _monthly_rule()
    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id="g1", obligation_id=rule.id, clause_id=rule.source.clause_id,
        company="X", title="t", by="officer",
    )
    store.attach_evidence(task.task_id, ["EV-001"], by="owner")

    # Evidence exists but the return was never actually filed.
    partial = EvidenceStore(label="synthetic")
    partial.add(
        ComplianceEvent(
            id="EV-001",
            obligation_id=rule.id,
            entity="X",
            occurred_on=_dt.date(2026, 3, 31),
            artifact_type="return",
            filed_on=None,
        )
    )

    result = recheck_task(store, task.task_id, [rule], partial, by="officer", as_of=TODAY)

    assert result.still_failing
    assert store.get(task.task_id).status is TaskStatus.REOPENED
    assert store.log.verify()[0]


def test_an_unevaluable_rule_does_not_close_a_task(tmp_path):
    """Closure must not be obtainable by making the rule uncheckable."""
    rule = _monthly_rule().model_copy(update={"evidence": []})
    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id="g1", obligation_id=rule.id, clause_id=rule.source.clause_id,
        company="X", title="t", by="officer",
    )

    result = recheck_task(
        store, task.task_id, [rule], EvidenceStore(label="e"), by="officer", as_of=TODAY
    )

    assert not result.evaluated
    assert not result.closed
    assert store.get(task.task_id).status is not TaskStatus.CLOSED


def test_the_task_log_survives_a_round_trip(tmp_path):
    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id="g1", obligation_id="SB-1-a", clause_id="1", company="X",
        title="t", by="officer",
    )
    store.assign(task.task_id, owner="R. Sharma", by="officer")
    store.save()

    reloaded = RemediationStore.load(tmp_path / "remediation.json")

    assert reloaded.get(task.task_id).owner == "R. Sharma"
    assert len(reloaded.log) == len(store.log)
    assert reloaded.log.verify() == (True, "")


def test_a_tampered_log_does_not_verify(tmp_path):
    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id="g1", obligation_id="SB-1-a", clause_id="1", company="X",
        title="t", by="officer",
    )
    store.assign(task.task_id, owner="R. Sharma", by="officer")

    import dataclasses

    store.log.entries[0] = dataclasses.replace(
        store.log.entries[0], actor="somebody else"
    )

    intact, problem = store.log.verify()
    assert not intact
    assert "altered" in problem


def test_overdue_is_derived_not_stored(tmp_path):
    """A stored OVERDUE is wrong the moment the clock passes midnight."""
    store = _store(tmp_path)
    task = store.open_for_gap(
        gap_id="g1", obligation_id="SB-1-a", clause_id="1", company="X",
        title="t", by="officer", due_date=_dt.date(2026, 8, 1),
    )

    assert task.is_overdue(_dt.date(2026, 8, 11))
    assert not task.is_overdue(_dt.date(2026, 7, 1))


# ══════════════════════════════════════════════ against the real corpus ══


@requires_corpus
def test_the_real_rulebook_now_reports_duties_nobody_evidenced():
    """On the actual store, with a real empty evidence set."""
    from sanhita.cli_compile import _load_registry

    rules = _load_registry().all_current()
    report = RuleEngine().run(rules, EvidenceStore(label="empty"), as_of=TODAY)

    never = report.never_evidenced
    assert never, "certified recurring duties exist, so silence must be reported"
    assert report.not_applicable > 0, "most rules are not owed in any given window"
    assert report.undetermined, "event-driven duties must be surfaced, not passed"
    # Every finding still carries its citation.
    for finding in never:
        assert finding.clause_id
        assert finding.certified_by
        assert finding.signature

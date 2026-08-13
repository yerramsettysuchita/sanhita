"""Filing the corrected record must actually clear the breach.

Found by walking the journey in a browser rather than by a test, which is the
point of walking it. A firm uploaded its register, one occasion showed as never
filed, a task was raised, the firm filed the return and uploaded the corrected
register, and the re-check still failed. The store had kept both statements
about the same occasion, so the engine read one duty as never filed and also
filed, and the breach could never clear no matter what the firm did.

One occasion happens once. A later record about it supersedes the earlier one.
Nothing is lost, because every assessment stores the hash of the records it ran
against, so the earlier position keeps its own hash and stays reproducible.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus, sign_in


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    # Compliance actions record who did them, so the journey these
    # tests walk needs an authenticated officer behind it.
    sign_in(client)
    return client


# ------------------------------------------------------------- the store rule


def _event(eid: str, *, filed: _dt.date | None, occurred=_dt.date(2026, 1, 31)):
    from sanhita.execute.evidence import ComplianceEvent

    return ComplianceEvent(
        id=eid,
        obligation_id="OB-1",
        entity="ABC Securities",
        occurred_on=occurred,
        artifact_type="report",
        filed_on=filed,
    )


def test_a_later_record_of_one_occasion_replaces_the_earlier():
    from sanhita.execute import EvidenceStore

    store = EvidenceStore(label="ABC")
    store.supersede(_event("EV-1", filed=None))
    displaced = store.supersede(_event("EV-2", filed=_dt.date(2026, 2, 4)))

    assert len(store) == 1, "one occasion produced two records"
    assert store.events[0].id == "EV-2"
    assert store.events[0].filed_on == _dt.date(2026, 2, 4)
    assert displaced is not None and displaced.id == "EV-1"


def test_a_different_occasion_is_kept_alongside():
    from sanhita.execute import EvidenceStore

    store = EvidenceStore(label="ABC")
    store.supersede(_event("EV-1", filed=None))
    displaced = store.supersede(
        _event("EV-2", filed=None, occurred=_dt.date(2026, 2, 28))
    )

    assert len(store) == 2
    assert displaced is None


def test_the_wrong_artifact_then_the_right_one_is_still_one_occasion():
    """Otherwise correcting the document type would invent a second duty."""
    from dataclasses import replace

    from sanhita.execute import EvidenceStore

    store = EvidenceStore(label="ABC")
    store.supersede(_event("EV-1", filed=_dt.date(2026, 2, 4)))
    store.supersede(
        replace(_event("EV-2", filed=_dt.date(2026, 2, 4)), artifact_type="certificate")
    )

    assert len(store) == 1
    assert store.events[0].artifact_type == "certificate"


# ------------------------------------------------------ the journey it unblocks


def _recurring_rule(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if (
            rule.status is RuleStatus.CERTIFIED
            and rule.deadline is not None
            and rule.deadline.kind is DeadlineKind.END_OF_PERIOD
            and rule.evidence
        ):
            return rule
    raise AssertionError("the store carries no certified recurring rule")


def _upload(client, rule, filed: str, name: str):
    return client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": name},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-01-31,{filed},"
            f"{rule.evidence[0].artifact_type},REC-001\n"
        ).encode(),
    )


@requires_corpus
def test_the_corrected_register_supersedes_and_says_so(client, tmp_path):
    from sanhita.execute import EvidenceStore

    rule = _recurring_rule(tmp_path)
    _upload(client, rule, "", "register.csv")
    body = _upload(client, rule, "2026-01-31", "corrected-register.csv").json()

    assert body["ok"]
    assert body["superseded"] == 1, "a correction must not be silent"

    events = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(rule.id)
    assert len(events) == 1, "the firm's register reports one occasion twice"
    assert events[0].filed_on == _dt.date(2026, 1, 31)


@requires_corpus
def test_filing_what_was_owed_closes_the_task(client, tmp_path):
    """The whole loop, and the assertion the browser walk failed on."""
    from sanhita.remediate import RemediationStore, TaskStatus

    rule = _recurring_rule(tmp_path)
    _upload(client, rule, "", "register.csv")

    client.post("/w/demo/assess", follow_redirects=True)
    gaps = client.get("/w/demo/gaps").text
    form = re.search(
        rf'name="obligation_id" value="{re.escape(rule.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    assert form, "the January occasion produced no gap to raise"

    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": form.group(1),
            "clause_id": form.group(2),
            "title": "January filing outstanding",
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=True,
    )
    task_id = next(iter(RemediationStore.load(tmp_path / "remediation.json").tasks))

    # A re-check has to point at the record it is relying on. Attach the
    # occasion as it stands, unfiled, and the engine will still say no.
    from sanhita.execute import EvidenceStore

    unfiled = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(rule.id)[0]
    client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"evidence_id": unfiled.id, "by": "R. Sharma"},
        follow_redirects=True,
    )
    client.post(
        f"/w/demo/remediation/{task_id}/recheck",
        data={"by": "R. Sharma"},
        follow_redirects=True,
    )
    task = RemediationStore.load(tmp_path / "remediation.json").get(task_id)
    assert task.status is TaskStatus.REOPENED
    assert task.verified_at is None

    # The firm files it and uploads the corrected register.
    _upload(client, rule, "2026-01-31", "corrected-register.csv")
    client.post(
        f"/w/demo/remediation/{task_id}/recheck",
        data={"by": "R. Sharma"},
        follow_redirects=True,
    )

    store = RemediationStore.load(tmp_path / "remediation.json")
    task = store.get(task_id)
    assert task.status is TaskStatus.CLOSED, (
        f"the corrected filing did not clear the breach: {task.last_recheck_result}"
    )
    assert task.verified_at is not None
    assert task.recheck_count == 2
    intact, problem = store.log.verify()
    assert intact, problem


@requires_corpus
def test_an_attached_record_survives_the_corrected_upload(client, tmp_path):
    """Event ids used to be the row number, so a re-upload renumbered them all.

    A task that had attached EV-00001 was then pointing at a record that no
    longer existed, and the evidence panel on the remediation screen went blank
    at exactly the moment the firm did the right thing.
    """
    from sanhita.execute import EvidenceStore
    from sanhita.remediate import RemediationStore

    rule = _recurring_rule(tmp_path)
    _upload(client, rule, "", "register.csv")
    before = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(rule.id)[0]

    client.post("/w/demo/assess", follow_redirects=True)
    gaps = client.get("/w/demo/gaps").text
    form = re.search(
        rf'name="obligation_id" value="{re.escape(rule.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": form.group(1),
            "clause_id": form.group(2),
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=True,
    )
    task_id = next(iter(RemediationStore.load(tmp_path / "remediation.json").tasks))
    client.post(
        f"/w/demo/remediation/{task_id}/attach",
        data={"evidence_id": before.id, "by": "R. Sharma"},
        follow_redirects=True,
    )

    _upload(client, rule, "2026-01-31", "corrected-register.csv")

    after = EvidenceStore.load(tmp_path / "evidence.json").for_obligation(rule.id)[0]
    assert after.id == before.id, "the corrected upload renamed the occasion"
    assert after.filed_on is not None

    task = RemediationStore.load(tmp_path / "remediation.json").get(task_id)
    assert task.evidence_ids == [after.id]
    assert after.id in client.get("/w/demo/remediation").text, (
        "the attached record vanished from the screen after the correction"
    )


@requires_corpus
def test_zero_breaches_never_reads_as_zero_percent_compliant(client, tmp_path):
    """The contradiction the browser walk put on screen.

    Occasions under a rule the firm filed nothing at all for were counted in
    the denominator while producing one finding in the numerator, so a firm with
    nothing failing read 0.0% compliant.
    """
    from sanhita.execute import WEEKENDS_ONLY, EvidenceStore, RuleEngine

    rule = _recurring_rule(tmp_path)
    _upload(client, rule, "2026-01-31", "register.csv")

    from sanhita.cli_compile import _load_registry

    report = RuleEngine(WEEKENDS_ONLY).run(
        _load_registry(tmp_path / "rules.json").all_current(),
        EvidenceStore.load(tmp_path / "evidence.json"),
    )

    assert report.satisfied == 1
    assert report.events_checked == 1, "only one occasion had a record"
    assert report.occasions_unevidenced > 0, "the rest are still counted somewhere"
    assert report.compliance_rate == 1.0

    page = client.get("/w/demo/gaps").text
    assert "Not verifiable" in page
    assert ">\n      100.0%" in page or "100.0%" in page
    assert not re.search(r"(?<!\d)0\.0%", page), "the old contradiction is back"


@requires_corpus
def test_the_earlier_assessment_keeps_its_own_hash(client, tmp_path):
    """A firm may state a new position. It may not rewrite the old one."""
    from sanhita.assess import AssessmentLog

    rule = _recurring_rule(tmp_path)
    _upload(client, rule, "", "register.csv")
    client.post("/w/demo/assess", follow_redirects=True)
    _upload(client, rule, "2026-01-31", "corrected-register.csv")
    client.post("/w/demo/assess", follow_redirects=True)

    log = AssessmentLog.load(tmp_path / "assessments.json")
    assert len(log) == 2
    breached, corrected = log.runs
    assert breached.evidence_sha256 != corrected.evidence_sha256
    assert breached.breaches > corrected.breaches, (
        "the correction should show as an improvement, not erase the finding"
    )
    assert log.movement() == (corrected.breaches, breached.breaches)

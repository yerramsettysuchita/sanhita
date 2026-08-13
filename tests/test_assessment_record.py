"""An assessment is an event, so it has to be written down and reproducible.

Before this, a firm's compliance position was recomputed on every page view and
stored nowhere, so nobody could answer "when were we last assessed" or "has this
got better since last quarter". These tests hold the record to two properties:

* the same inputs do not manufacture a second assessment
* a changed input does, and the record says which input changed
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.conftest import requires_corpus, sign_in


# ------------------------------------------------------------- the log itself


def _event(eid: str, obligation: str, *, filed: bool = True):
    from sanhita.execute.evidence import ComplianceEvent

    return ComplianceEvent(
        id=eid,
        obligation_id=obligation,
        entity="ABC Securities",
        occurred_on=_dt.date(2026, 1, 31),
        artifact_type="report",
        filed_on=_dt.date(2026, 2, 3) if filed else None,
    )


def test_the_same_records_hash_the_same_whatever_order_they_arrive_in():
    """A re-import of the same facts is not a new assessment."""
    from sanhita.assess import evidence_fingerprint
    from sanhita.execute import EvidenceStore

    first = EvidenceStore(label="ABC")
    first.add(_event("EV-1", "OB-1"))
    first.add(_event("EV-2", "OB-2"))

    second = EvidenceStore(label="ABC")
    second.add(_event("EV-2", "OB-2"))
    second.add(_event("EV-1", "OB-1"))

    assert evidence_fingerprint(first) == evidence_fingerprint(second)


def test_a_changed_fact_changes_the_fingerprint():
    from sanhita.assess import evidence_fingerprint
    from sanhita.execute import EvidenceStore

    filed = EvidenceStore(label="ABC")
    filed.add(_event("EV-1", "OB-1", filed=True))
    unfiled = EvidenceStore(label="ABC")
    unfiled.add(_event("EV-1", "OB-1", filed=False))

    assert evidence_fingerprint(filed) != evidence_fingerprint(unfiled)


def test_no_evidence_still_has_a_stable_fingerprint():
    from sanhita.assess import evidence_fingerprint
    from sanhita.execute import EvidenceStore

    assert evidence_fingerprint(None) == evidence_fingerprint(EvidenceStore(label="x"))


def test_a_log_with_one_run_reports_no_movement(tmp_path):
    """A first assessment has no trend, and inventing one would be a fabrication."""
    from sanhita.assess import AssessmentLog, AssessmentRun

    log = AssessmentLog(path=tmp_path / "assessments.json")
    log.runs.append(_run(breaches=4))

    assert log.movement() is None
    assert isinstance(log.latest, AssessmentRun)


def test_a_second_run_reports_the_direction_of_travel(tmp_path):
    from sanhita.assess import AssessmentLog

    log = AssessmentLog(path=tmp_path / "assessments.json")
    log.runs.append(_run(breaches=9))
    log.runs.append(_run(breaches=4))

    assert log.movement() == (4, 9)


def test_a_log_survives_a_round_trip(tmp_path):
    from sanhita.assess import AssessmentLog

    path = tmp_path / "assessments.json"
    log = AssessmentLog(path=path)
    log.runs.append(_run(breaches=3))
    log.save()

    again = AssessmentLog.load(path)
    assert len(again) == 1
    assert again.latest.breaches == 3
    assert again.latest.ran_at == log.latest.ran_at


def _run(*, breaches: int):
    from sanhita.assess import AssessmentRun

    return AssessmentRun(
        run_id="AR-000000000001",
        ran_at=_dt.datetime(2026, 8, 12, 9, 0, tzinfo=_dt.timezone.utc),
        ran_by="A Named Officer",
        document="master-circular.pdf",
        document_sha256="a" * 64,
        rulebook_sha256="b" * 64,
        evidence_label="ABC",
        evidence_sha256="c" * 64,
        events_checked=20,
        rules_certified=126,
        rules_evaluated=12,
        satisfied=20 - breaches,
        breaches=breaches,
        missing=breaches,
        late=0,
        no_evidence=0,
        undetermined=0,
        unevaluable=0,
    )


# ------------------------------------------------------- through the web app


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


def _a_certified_rule(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if rule.status is RuleStatus.CERTIFIED:
            return rule
    raise AssertionError("the store carries no certified rule")


def _set_up_a_firm(client, name: str = "ABC Securities"):
    """Both setup steps, because the dashboard is gated behind them.

    A firm with no declared framework has not said which rulebook governs it,
    so the overview stays on step two rather than showing a position.
    """
    client.post(
        "/w/demo/company/save",
        data={"name": name, "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)


def _import_a_register(client, tmp_path, *, filed: str = "2026-04-02"):
    rule = _a_certified_rule(tmp_path)
    csv_text = (
        "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
        f"{rule.id},ABC Securities,2026-03-31,{filed},report,RET-001\n"
    )
    return client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=csv_text.encode(),
    )


@requires_corpus
def test_nothing_is_recorded_before_the_firm_provides_records(client, tmp_path):
    # The route refuses outright rather than recording an empty assessment.
    refused = client.post("/w/demo/assess", follow_redirects=False)
    assert refused.status_code == 400
    assert not (tmp_path / "assessments.json").is_file(), (
        "an assessment was recorded for a firm that supplied nothing"
    )


@requires_corpus
def test_the_first_real_assessment_is_recorded(client, tmp_path):
    from sanhita.assess import AssessmentLog

    _import_a_register(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    log = AssessmentLog.load(tmp_path / "assessments.json")
    assert len(log) == 1
    run = log.latest
    assert run.rules_certified > 0
    assert len(run.rulebook_sha256) == 64
    assert len(run.evidence_sha256) == 64


@requires_corpus
def test_assessing_the_same_records_twice_is_one_assessment(client, tmp_path):
    """Pressing the button again on unchanged inputs is not a new assessment."""
    from sanhita.assess import AssessmentLog

    _import_a_register(client, tmp_path)
    for _ in range(4):
        client.post("/w/demo/assess", follow_redirects=True)

    assert len(AssessmentLog.load(tmp_path / "assessments.json")) == 1


@requires_corpus
def test_changed_records_produce_a_second_assessment(client, tmp_path):
    from sanhita.assess import AssessmentLog

    _import_a_register(client, tmp_path, filed="2026-04-02")
    client.post("/w/demo/assess", follow_redirects=True)
    _import_a_register(client, tmp_path, filed="")
    client.post("/w/demo/assess", follow_redirects=True)

    log = AssessmentLog.load(tmp_path / "assessments.json")
    assert len(log) == 2, "a different filing history is a different assessment"
    assert log.runs[0].evidence_sha256 != log.runs[1].evidence_sha256
    assert log.runs[0].rulebook_sha256 == log.runs[1].rulebook_sha256


@requires_corpus
def test_the_company_page_names_the_framework_and_the_moment(client, tmp_path):
    import re

    def plain(html: str) -> str:
        body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))

    _set_up_a_firm(client)

    before = plain(client.get("/w/demo/company").text)
    assert "Assessed against" in before
    assert "Never" in before, "an unassessed firm must not imply it was assessed"

    _import_a_register(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)
    after = plain(client.get("/w/demo/company").text)

    assert "Assessment record" in after
    assert "reproducible" in after
    assert "Never" not in after.split("Rules in force")[0]


# --------------------------------------------- what the run actually said


@requires_corpus
def test_a_run_keeps_the_findings_it_produced(client, tmp_path):
    """Counts prove a run happened. They do not say what it told the firm."""
    from sanhita.assess import AssessmentLog

    _import_a_register(client, tmp_path, filed="")
    client.post("/w/demo/assess", follow_redirects=True)

    run = AssessmentLog.load(tmp_path / "assessments.json").latest
    assert run.has_findings, "the run recorded counts and threw the findings away"
    # Every finding is kept. `breaches` is the subset the records prove, so
    # the two agree only once the unknowns are added back.
    assert len(run.findings) == run.breaches + run.no_evidence

    worst = run.findings[0]
    assert worst.clause_id and worst.obligation_id
    assert worst.certified_by, "a finding without its signer is not a citation"
    assert len(worst.signature) == 64
    assert worst.requirement


def _recurring_certified_rule(tmp_path):
    """A rule where whether you filed actually changes the finding.

    An arbitrary certified rule will not do: many are event driven, so a single
    record neither satisfies nor breaches them and both runs come out identical.
    """
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


@requires_corpus
def test_an_old_finding_is_not_rewritten_by_a_later_correction(client, tmp_path):
    """The property the whole thing exists for.

    Recomputing today cannot answer "what were we told in March", because the
    evidence has since been corrected. The March run has to still say what it
    said.
    """
    from sanhita.assess import AssessmentLog

    rule = _recurring_certified_rule(tmp_path)
    artifact = rule.evidence[0].artifact_type

    def upload(filed: str):
        client.post(
            "/w/demo/evidence",
            headers={"x-sanhita-filename": "register.csv"},
            content=(
                "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
                f"{rule.id},ABC Securities,2026-01-31,{filed},{artifact},REC-001\n"
            ).encode(),
        )

    upload("")                 # never filed
    client.post("/w/demo/assess", follow_redirects=True)
    upload("2026-01-31")       # filed, and on time
    client.post("/w/demo/assess", follow_redirects=True)

    log = AssessmentLog.load(tmp_path / "assessments.json")
    assert len(log.runs) == 2
    before, after = log.runs
    assert before.has_findings

    was_breached = [f for f in before.findings if f.obligation_id == rule.id]
    assert was_breached, "the earlier run never recorded the breach it reported"
    assert was_breached[0].filed_on is None
    assert was_breached[0].outcome in ("MISSING", "NO_EVIDENCE")

    # The correction cleared it, and the earlier run is unchanged by that.
    still_breached = [f for f in after.findings if f.obligation_id == rule.id]
    assert not still_breached, "the correction did not clear the later run"
    assert [f.obligation_id for f in before.findings] != [
        f.obligation_id for f in after.findings
    ], "the two runs recorded identical findings"


@requires_corpus
def test_the_stored_findings_survive_a_round_trip(client, tmp_path):
    from sanhita.assess import AssessmentLog

    _import_a_register(client, tmp_path, filed="")
    client.post("/w/demo/assess", follow_redirects=True)

    reloaded = AssessmentLog.load(tmp_path / "assessments.json").latest
    assert reloaded.has_findings
    assert isinstance(reloaded.findings[0].clause_id, str)


@requires_corpus
def test_the_company_page_shows_what_the_run_said(client, tmp_path):
    import re

    _set_up_a_firm(client)
    _import_a_register(client, tmp_path, filed="")
    client.post("/w/demo/assess", follow_redirects=True)
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", client.get("/w/demo/company").text))

    assert "What this run said" in page
    assert "as they stood" in page
    assert "cannot rewrite what this firm was told at the time" in page


def test_a_run_recorded_before_findings_were_kept_says_so():
    """Older logs must load, and must not read as a run that found nothing."""
    from sanhita.assess import AssessmentRun

    run = AssessmentRun.from_dict(
        {
            "run_id": "AR-old",
            "ran_at": "2026-03-01T09:00:00+00:00",
            "ran_by": "A Named Officer",
            "document": "circular.pdf",
            "document_sha256": "a" * 64,
            "rulebook_sha256": "b" * 64,
            "evidence_label": "ABC",
            "evidence_sha256": "c" * 64,
            "events_checked": 10,
            "rules_certified": 100,
            "rules_evaluated": 10,
            "satisfied": 7,
            "breaches": 3,
            "missing": 3,
            "late": 0,
            "no_evidence": 0,
            "undetermined": 0,
            "unevaluable": 0,
        }
    )

    assert run.breaches == 3
    assert run.has_findings is False, "an old run must not claim to have kept findings"

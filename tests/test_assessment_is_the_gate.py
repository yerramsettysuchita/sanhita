"""Being able to compute a number is not the same event as recording one.

Three defects, all the same mistake in different places: the product treated
"evidence exists" as though it meant "an assessment happened".

* The overview ran the engine the moment any record existed and printed the
  result beside the firm's name. So one screen could say "33% compliant with
  this framework" while the history on the same screen said the firm had never
  been assessed.
* The gaps screen correctly called an unrecorded result a preview, and then
  offered to open a remediation task against it. An audit chain would have
  started from something nobody recorded.
* Uploading a file completed onboarding, so step three vanished under the user
  mid-action.
"""

from __future__ import annotations

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


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _recurring(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    return next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )


def _set_up(client):
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)


def _upload(client, tmp_path, filed=""):
    rule = _recurring(tmp_path)
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-01-31,{filed},"
            f"{rule.evidence[0].artifact_type},REC-001\n"
        ).encode(),
    )
    return rule


# ------------------------------------- the overview states a position or none


@requires_corpus
def test_evidence_alone_puts_no_percentage_beside_the_firms_name(client, tmp_path):
    """The defect, in one assertion."""
    _set_up(client)
    _upload(client, tmp_path)

    page = _plain(client.get("/w/demo/company").text)

    assert not re.search(r"\d+% Of what could be determined", page), (
        "a compliance percentage appeared without an assessment behind it"
    )
    assert "Evidence ready for assessment" in page
    assert "Run compliance assessment" in page


@requires_corpus
def test_evidence_counts_are_shown_but_labelled_as_evidence(client, tmp_path):
    """Counting records is useful. Calling those counts a verdict is not."""
    _set_up(client)
    _upload(client, tmp_path)

    page = _plain(client.get("/w/demo/company").text)

    assert "Records read" in page
    assert "Mapped to a requirement" in page
    for verdict in ("Of what could be determined", "Applicable duties", "Failing"):
        assert verdict not in page, f"{verdict!r} is shown before any assessment"


@requires_corpus
def test_running_the_assessment_is_what_produces_the_position(client, tmp_path):
    _set_up(client)
    _upload(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)

    assert re.search(r"\d+% Of what could be determined", page)
    assert "Of what could be determined" in page
    assert "Assessment record" in page


@requires_corpus
def test_changing_the_records_withdraws_the_position(client, tmp_path):
    """An assessment is of the records it ran against, not of the firm forever."""
    _set_up(client)
    _upload(client, tmp_path, filed="2026-01-31")
    client.post("/w/demo/assess", follow_redirects=True)
    assert re.search(r"\d+% Of what could be determined", _plain(client.get("/w/demo/company").text))

    _upload(client, tmp_path, filed="")
    page = _plain(client.get("/w/demo/company").text)

    assert not re.search(r"\d+% Of what could be determined", page), (
        "the old percentage is still presented as the current position"
    )
    assert "Assessment needs to be re-run" in page
    assert "history rather than this firm's current position" in page


@requires_corpus
def test_the_earlier_run_is_still_in_the_history(client, tmp_path):
    """Withdrawn as the current position, not deleted."""
    from sanhita.assess import AssessmentLog

    _set_up(client)
    _upload(client, tmp_path, filed="2026-01-31")
    client.post("/w/demo/assess", follow_redirects=True)
    _upload(client, tmp_path, filed="")

    log = AssessmentLog.load(tmp_path / "assessments.json")
    assert len(log) == 1
    assert log.latest.has_findings


# ------------------------------------------ a preview cannot become a task


@requires_corpus
def test_a_preview_does_not_offer_to_open_a_task(client, tmp_path):
    _set_up(client)
    _upload(client, tmp_path)

    page = _plain(client.get("/w/demo/gaps").text)

    assert "Not yet assessed on these records" in page
    assert "Fix this gap" not in page, "a preview offers remediation"
    assert "Assessment required" in page


@requires_corpus
def test_the_route_refuses_a_task_raised_from_a_preview(client, tmp_path):
    """Hiding the control is not enough. The route has to refuse."""
    from sanhita.remediate import RemediationStore

    _set_up(client)
    rule = _upload(client, tmp_path)

    response = client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": "EV-anything",
            "clause_id": rule.source.clause_id,
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Run the compliance assessment" in response.text
    assert not RemediationStore.load(tmp_path / "remediation.json").tasks


@requires_corpus
def test_a_gap_the_assessment_never_reported_cannot_become_a_task(client, tmp_path):
    from sanhita.remediate import RemediationStore

    _set_up(client)
    rule = _upload(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    response = client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": "EV-never-happened",
            "clause_id": rule.source.clause_id,
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "reports no finding" in response.text
    assert not RemediationStore.load(tmp_path / "remediation.json").tasks


@requires_corpus
def test_an_uncertified_rule_cannot_have_produced_a_finding(client, tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus
    from sanhita.remediate import RemediationStore

    _set_up(client)
    _upload(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    proposed = next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is not RuleStatus.CERTIFIED
    )
    response = client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": proposed.id,
            "gap_id": "EV-anything",
            "clause_id": proposed.source.clause_id,
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "not certified" in response.text
    assert not RemediationStore.load(tmp_path / "remediation.json").tasks


@requires_corpus
def test_a_recorded_finding_can_become_a_task(client, tmp_path):
    """The path that must still work, end to end through the routes."""
    from sanhita.remediate import RemediationStore

    _set_up(client)
    rule = _upload(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    gaps = client.get("/w/demo/gaps").text
    assert "Fix this gap" in _plain(gaps)
    form = re.search(
        rf'name="obligation_id" value="{re.escape(rule.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    assert form, "the recorded assessment offers no finding to remediate"

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
    assert RemediationStore.load(tmp_path / "remediation.json").tasks


@requires_corpus
def test_a_stored_finding_names_the_occasion_it_is_about(client, tmp_path):
    """Without it a run could not be asked whether it reported a given gap."""
    from sanhita.assess import AssessmentLog

    _set_up(client)
    _upload(client, tmp_path)
    client.post("/w/demo/assess", follow_redirects=True)

    run = AssessmentLog.load(tmp_path / "assessments.json").latest
    assert run.findings
    assert all(f.event_id for f in run.findings)


# ------------------------------------------ uploading is not finishing setup


@requires_corpus
def test_uploading_at_step_three_leaves_you_on_step_three(client, tmp_path):
    """The step used to vanish under the user the moment a file landed."""
    from sanhita.company import Company

    client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=True
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    _upload(client, tmp_path)

    page = _plain(client.get("/w/demo/company").text)
    assert "Step 3 of 3, setting up" in page
    assert "Stage 1 of 5" not in page
    assert Company.load(tmp_path / "company.json").setup_completed_at is None

    client.post("/w/demo/setup/complete", follow_redirects=True)
    assert "Stage 1 of 5" in _plain(client.get("/w/demo/company").text)

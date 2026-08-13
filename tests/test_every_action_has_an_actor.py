"""Every recorded compliance action names the person who took it.

Certification was fixed first, because it is the one the whole product rests
on. That left a worse-shaped problem: a trail in which the rule was signed by
an authenticated officer, and the assessment beneath it was run by
"unattributed", the evidence mapped by "unattributed", and the task closed by
"unattributed". A hole in exactly the place an inspector looks.

The rule is the same everywhere now. Reading is open: a visitor can walk the
entire product and see all of it. Writing a record somebody may later be asked
to answer for needs an account, and the account is what gets recorded. The form
still carries a `by` field for old bookmarks, and it is ignored.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus, sign_in


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    """Deliberately anonymous. These tests are about the refusal."""
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


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


def _ready(client, tmp_path):
    """A firm with records, so the refusals below are about the actor and not
    about a precondition the route would have failed on anyway."""
    rule = _recurring(tmp_path)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2026-01-31,,{rule.evidence[0].artifact_type},R1\n"
        ).encode(),
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    return rule


# --------------------------------------------- what an anonymous caller cannot do


@requires_corpus
def test_an_anonymous_visitor_cannot_run_an_assessment(client, tmp_path):
    """An assessment is a firm's stated position on a date. Somebody states it."""
    from sanhita.assess import AssessmentLog

    _ready(client, tmp_path)

    response = client.post("/w/demo/assess", data={"by": "S. Iyer"}, follow_redirects=False)

    assert response.status_code == 401
    assert "official compliance action" in response.text
    assert not AssessmentLog.load(tmp_path / "assessments.json").runs


@requires_corpus
def test_an_anonymous_visitor_cannot_bind_a_duty_to_a_team(client, tmp_path):
    """A gap report cites the binding by name. Somebody stands behind it."""
    from sanhita.controls import ControlStore

    rule = _recurring(tmp_path)

    response = client.post(
        f"/w/demo/clause/{rule.source.clause_id}/bind",
        data={"obligation_id": rule.id, "function": "Operations", "by": "S. Iyer"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert not ControlStore.load(tmp_path / "controls.json").bindings


@requires_corpus
def test_an_anonymous_visitor_cannot_raise_a_remediation_task(client, tmp_path):
    from sanhita.remediate import RemediationStore

    rule = _ready(client, tmp_path)

    response = client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": "EV-anything",
            "clause_id": rule.source.clause_id,
            "priority": "HIGH",
            "by": "S. Iyer",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert not RemediationStore.load(tmp_path / "remediation.json").tasks


@requires_corpus
def test_an_anonymous_visitor_cannot_raise_a_task_from_an_amendment(client, tmp_path):
    _ready(client, tmp_path)

    response = client.post(
        "/w/demo/change/open",
        data={"against": "demo", "kind": "RECERTIFY", "clause_id": "15.1"},
        follow_redirects=False,
    )

    assert response.status_code == 401


@requires_corpus
def test_an_anonymous_visitor_cannot_map_or_dismiss_a_candidate(client, tmp_path):
    """Mapping a document to a duty is the judgement the whole review exists for."""
    rule = _recurring(tmp_path)
    _ready(client, tmp_path)

    for path, data in (
        ("/w/demo/review/anything/map", {"obligation_id": rule.id, "by": "S. Iyer"}),
        ("/w/demo/review/anything/dismiss", {"reason": "not relevant", "by": "S. Iyer"}),
    ):
        response = client.post(f"/w/demo{path[7:]}", data=data, follow_redirects=False)
        assert response.status_code == 401, f"{path} accepted an anonymous caller"


@requires_corpus
def test_an_anonymous_visitor_cannot_assign_attach_or_recheck(client, tmp_path):
    _ready(client, tmp_path)

    for path, data in (
        ("/w/demo/remediation/REM-1/assign", {"owner": "R. Nair", "by": "S. Iyer"}),
        ("/w/demo/remediation/REM-1/attach", {"evidence_id": "EV-1", "by": "S. Iyer"}),
        ("/w/demo/remediation/REM-1/recheck", {"by": "S. Iyer"}),
    ):
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 401, f"{path} accepted an anonymous caller"


@requires_corpus
def test_the_refusal_says_what_to_do_about_it(client, tmp_path):
    _ready(client, tmp_path)
    response = client.post("/w/demo/assess", follow_redirects=False)

    assert "needs an account" in response.text
    assert "a name typed into a box is not a record of anybody" in response.text


# --------------------------------------------------- and what it can still do


@requires_corpus
def test_reading_is_never_gated(client, tmp_path):
    """A visitor has to be able to walk the whole product and see all of it."""
    _ready(client, tmp_path)

    for path in (
        "/",
        "/supervisor",
        "/facts",
        "/w/demo",
        "/w/demo/company",
        "/w/demo/review",
        "/w/demo/gaps",
        "/w/demo/remediation",
        "/w/demo/audit",
        "/w/demo/diff",
        "/w/demo/processes",
        "/w/demo/coverage",
    ):
        assert client.get(path).status_code == 200, f"{path} is gated for reading"


@requires_corpus
def test_onboarding_and_upload_stay_open(client, tmp_path):
    """Nothing is recorded against a person yet, and a demo has to be walkable."""
    rule = _recurring(tmp_path)

    assert client.post(
        "/w/demo/company/save", data={"name": "ABC Securities"}, follow_redirects=False
    ).status_code in (200, 303)
    assert client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC,2026-01-31,,{rule.evidence[0].artifact_type},R1\n"
        ).encode(),
    ).status_code == 200


# -------------------------------------------- the account is what gets recorded


@requires_corpus
def test_the_assessment_records_the_account_not_the_form(client, tmp_path):
    from sanhita.assess import AssessmentLog

    _ready(client, tmp_path)
    sign_in(client, name="R. Nair")

    client.post("/w/demo/assess", data={"by": "Somebody Else"}, follow_redirects=True)

    run = AssessmentLog.load(tmp_path / "assessments.json").latest
    assert run is not None
    assert run.ran_by == "R. Nair"


@requires_corpus
def test_the_remediation_log_records_the_account_not_the_form(client, tmp_path):
    from sanhita.remediate import RemediationStore

    rule = _ready(client, tmp_path)
    sign_in(client, name="R. Nair")
    client.post("/w/demo/assess", follow_redirects=True)

    gaps = client.get("/w/demo/gaps").text
    form = re.search(
        rf'name="obligation_id" value="{re.escape(rule.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    assert form, "no finding of record to raise a task against"
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": form.group(1),
            "clause_id": form.group(2),
            "priority": "HIGH",
            "by": "Somebody Else",
        },
        follow_redirects=True,
    )

    log = RemediationStore.load(tmp_path / "remediation.json").log
    assert log.entries
    assert all(e.actor == "R. Nair" for e in log.entries)
    assert not any(e.actor == "Somebody Else" for e in log.entries)


@requires_corpus
def test_no_compliance_record_is_ever_written_by_unattributed(client, tmp_path):
    """The fallback that used to exist, asserted gone at the source."""
    import inspect

    from sanhita.web import app as app_module

    source = inspect.getsource(app_module)

    # The literal expression, not the word: the docstring explaining why this
    # rule exists mentions "unattributed" and should keep doing so.
    assert 'else "unattributed"' not in source, (
        "the free-text actor fallback is back"
    )
    assert 'or "unknown"' not in source, "an anonymous ledger actor is reachable"

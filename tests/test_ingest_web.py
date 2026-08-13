"""Uploading evidence in each supported format, through the real route.

The module tests prove each reader works. These prove the upload path chooses
the right one and, more importantly, that a document naming no rule is refused
with an explanation rather than quietly becoming evidence.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import requires_corpus


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _a_certified_rule(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if rule.status is RuleStatus.CERTIFIED:
            return rule
    raise AssertionError("the store carries no certified rule")


def _upload(client, payload: bytes, filename: str):
    return client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": filename},
        content=payload,
    )


@requires_corpus
def test_csv_still_imports(client, tmp_path):
    rule = _a_certified_rule(tmp_path)
    csv_text = (
        "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
        f"{rule.id},Demo Broking,2026-03-31,2026-04-02,report,RET-001\n"
    )

    response = _upload(client, csv_text.encode(), "register.csv")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["accepted"] == 1
    assert body["format"] == "csv"


@requires_corpus
def test_json_imports(client, tmp_path):
    rule = _a_certified_rule(tmp_path)
    payload = json.dumps(
        [
            {
                "obligation_id": rule.id,
                "entity": "Demo Broking",
                "occurred_on": "2026-03-31",
                "filed_on": "2026-04-02",
                "artifact_type": "report",
                "reference": "RET-001",
            }
        ]
    )

    response = _upload(client, payload.encode(), "export.json")

    assert response.status_code == 200, response.text
    assert response.json()["format"] == "json"
    assert response.json()["accepted"] == 1


@requires_corpus
def test_a_pdf_that_names_no_rule_goes_to_review_rather_than_being_refused(client):
    """The property that matters, and it changed for the better.

    This used to assert a 400. Refusing was wrong: the document was read
    correctly, and a company PDF naming no rule id is the normal case rather
    than an error. Everything found is now held for a person to rule on, which
    is the step that was missing between reading a document and using it.
    """
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 80), "2026-03-31   RET-001   dispatched", fontsize=11)
    data = document.tobytes()
    document.close()

    response = _upload(client, data, "margin-report.pdf")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"], "reading a document that names no rule is not a failure"
    assert body["candidates"] >= 1, "it did read the document"
    assert body["awaiting"] >= 1, "and it kept what it found"
    assert body["accepted"] == 0, "but none of it is evidence yet"
    assert body["url"].endswith("/review")


@requires_corpus
def test_an_unsupported_format_names_what_is_supported(client):
    response = _upload(client, b"anything at all", "notes.docx")

    assert response.status_code == 400
    assert "CSV, JSON, XLSX and PDF" in json.dumps(response.json())


@requires_corpus
def test_the_upload_control_offers_every_format(client):
    """It lives on the evidence screen now, which is where evidence goes.

    It used to be on the gaps screen, which produced a loop: the company page
    sent you to Evidence, Evidence sent you to Gaps to upload, Gaps sent you
    back.
    """
    body = client.get("/w/demo/review").text

    for extension in (".csv", ".json", ".xlsx", ".pdf"):
        assert extension in body, f"{extension} is not offered on the upload control"

    assert "evfile" not in client.get("/w/demo/gaps").text, (
        "the gaps screen should no longer carry an uploader"
    )

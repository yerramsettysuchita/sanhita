"""Upload a real document, map it by hand, and watch the engine pick it up.

This is the bridge the audit called missing. Before it, a PDF was read, its
candidates computed, and everything that did not name a rule was discarded, so
a genuine margin report could achieve nothing at all.

The journey here is the one a compliance officer actually has.

    upload a PDF -> candidates appear -> a person maps one -> the rule runs
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


def _certified_id(tmp_path) -> str:
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if rule.status is RuleStatus.CERTIFIED:
            return rule.id
    raise AssertionError("no certified rule in the store")


def _margin_report() -> bytes:
    """A company PDF, of the kind that names no rule anywhere in it."""
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (60, 80),
        "ABC SECURITIES\nDaily Margin Statement Register\n"
        "2026-03-31   RET-001   dispatched\n"
        "2026-04-30   RET-002   dispatched",
        fontsize=11,
    )
    data = document.tobytes()
    document.close()
    return data


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@requires_corpus
def test_the_review_screen_carries_the_uploader_itself(client):
    """No detour. Evidence is uploaded on the evidence screen."""
    body = client.get("/w/demo/review").text
    page = _plain(body)

    assert "Nothing has been uploaded yet" in page
    assert "Drop your compliance records here" in page
    assert "PDF, CSV, XLSX and JSON are all read" in page
    assert 'id="evfile"' in body, "the file input has to be on this page"


@requires_corpus
def test_uploading_a_company_pdf_fills_the_review_queue(client):
    response = client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_margin_march.pdf"},
        content=_margin_report(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"], "a document naming no rule is not an error"
    assert body["awaiting"] >= 2
    assert body["url"].endswith("/review"), "it should send you to the review screen"

    page = _plain(client.get("/w/demo/review").text)
    assert "ABC_margin_march.pdf" in page
    assert "RET-001" in page, "the reviewer must see the words that produced this"


@requires_corpus
def test_nothing_reaches_the_engine_before_a_person_maps_it(client, tmp_path):
    from sanhita.execute.evidence import EvidenceStore

    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_margin_march.pdf"},
        content=_margin_report(),
    )

    store = EvidenceStore.load(tmp_path / "evidence.json")
    assert len(store) == 0, "unreviewed candidates must not become evidence"


@requires_corpus
def test_mapping_a_candidate_makes_it_evidence_with_its_provenance(client, tmp_path):
    from sanhita.company import ReviewQueue
    from sanhita.execute.evidence import EvidenceStore

    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_margin_march.pdf"},
        content=_margin_report(),
    )
    queue = ReviewQueue.load(tmp_path / "review.json")
    item = queue.awaiting()[0]
    rule_id = _certified_id(tmp_path)

    response = client.post(
        f"/w/demo/review/{item.item_id}/map",
        data={"obligation_id": rule_id, "by": "A Named Officer"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    store = EvidenceStore.load(tmp_path / "evidence.json")
    assert len(store) == 1, "the mapped candidate should now be evidence"

    event = store.events[0]
    assert event.obligation_id == rule_id
    assert event.source_document == "ABC_margin_march.pdf"
    assert event.source_page == 1
    assert event.source_excerpt
    assert event.mapped_by == "A Named Officer"


@requires_corpus
def test_a_dismissed_candidate_stays_visible_and_out_of_the_engine(client, tmp_path):
    from sanhita.company import ReviewQueue
    from sanhita.execute.evidence import EvidenceStore

    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_margin_march.pdf"},
        content=_margin_report(),
    )
    item = ReviewQueue.load(tmp_path / "review.json").awaiting()[0]

    client.post(
        f"/w/demo/review/{item.item_id}/dismiss",
        data={"reason": "a column header", "by": "A Named Officer"},
        follow_redirects=True,
    )

    assert len(EvidenceStore.load(tmp_path / "evidence.json")) == 0
    page = _plain(client.get("/w/demo/review").text)
    assert "a column header" in page, "a dismissal must remain answerable"


@requires_corpus
def test_a_csv_naming_its_rule_needs_no_review(client, tmp_path):
    """The easy case must not make somebody retype what the file already said."""
    rule_id = _certified_id(tmp_path)
    csv_text = (
        "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
        f"{rule_id},ABC Securities,2026-03-31,2026-04-02,report,RET-001\n"
    )

    body = client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=csv_text.encode(),
    ).json()

    assert body["ok"]
    assert body["awaiting"] == 0
    assert body["url"].endswith("/gaps"), "nothing to review, so go straight on"


@requires_corpus
def test_the_evidence_tab_is_in_the_navigation(client):
    assert "/w/demo/review" in client.get("/w/demo/gaps").text

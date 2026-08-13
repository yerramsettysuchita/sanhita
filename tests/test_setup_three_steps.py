"""Setting up is three answers, so it is three screens.

The product claimed a three step setup and implemented two. Saving a framework
dropped somebody straight onto the dashboard, where "upload compliance
evidence" was one call to action among fourteen other numbers, and step three
never existed as a screen. At the same time the five stage lifecycle rendered
underneath "Step 1 of 3", so a new visitor was shown two numbered journeys at
once and could not say which of them they were on.

Onboarding and the ongoing lifecycle are two modes. Only one is ever on screen.
"""

from __future__ import annotations

import re

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


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _bar(html: str) -> str:
    """Whichever navigation row is rendered. Only one ever is."""
    strip = html[html.index('class="stagebar"') :]
    return strip[: strip.index("</nav>")]


def _step_one(client):
    return client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=False,
    )


def _step_two(client):
    return client.post(
        "/w/demo/company/frameworks",
        data={"framework": "demo"},
        follow_redirects=False,
    )


# ------------------------------------------------------------- the sequence


@requires_corpus
def test_a_new_firm_starts_at_step_one(client):
    page = _plain(client.get("/w/demo/company").text)

    assert "Step 1 of 3, setting up" in page
    assert "Whose compliance is this" in page


@requires_corpus
def test_saving_the_company_moves_to_step_two(client):
    response = _step_one(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/w/demo/company"
    page = _plain(client.get("/w/demo/company").text)
    assert "Step 2 of 3, setting up" in page


@requires_corpus
def test_step_two_does_not_ask_for_the_whole_profile_again(client):
    """Being on the framework question is not a reason to re-edit the firm."""
    _step_one(client)
    body = client.get("/w/demo/company").text
    page = _plain(body)

    assert "Which SEBI rulebooks apply to ABC Securities Pvt Ltd" in page
    assert 'action="/w/demo/company/save"' not in body, (
        "step two still carries the company edit form"
    )
    for field in ("Business processes", "Systems of record", "Business facts"):
        assert field not in page, f"step two still asks for {field!r}"


@requires_corpus
def test_saving_the_framework_moves_to_step_three(client):
    _step_one(client)
    response = _step_two(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/w/demo/company"
    page = _plain(client.get("/w/demo/company").text)
    assert "Step 3 of 3, setting up" in page


@requires_corpus
def test_step_three_is_a_real_rendered_state(client):
    _step_one(client)
    _step_two(client)
    page = _plain(client.get("/w/demo/company").text)

    assert "Step 3 of 3, setting up" in page
    assert "Bring your compliance evidence" in page
    # And it is not the dashboard wearing a caption.
    assert "Stage 1 of 5" not in page
    assert "Applicable duties" not in page
    assert "Falling due in 30 days" not in page


@requires_corpus
def test_step_three_carries_the_company_evidence_uploader(client):
    _step_one(client)
    _step_two(client)
    body = client.get("/w/demo/company").text

    assert 'id="evfile"' in body, "the uploader is not on the page"
    for extension in (".csv", ".json", ".xlsx", ".pdf"):
        assert extension in body, f"{extension} is not offered"


@requires_corpus
def test_step_three_asks_for_the_firms_records_not_a_sebi_circular(client):
    _step_one(client)
    _step_two(client)
    page = _plain(client.get("/w/demo/company").text)

    assert "Drop your compliance records here" in page
    assert "Drop a SEBI circular here" not in page, (
        "setup asks the firm for the regulator's document"
    )
    assert "These are your own documents, not SEBI's" in page


@requires_corpus
def test_uploading_at_step_three_uses_the_existing_parser(client, tmp_path):
    """One ingestion path. Setup reuses it rather than growing a second."""
    from sanhita.company import ReviewQueue

    _step_one(client)
    _step_two(client)

    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 80), "2026-01-31   REC-001   dispatched", fontsize=11)
    data = document.tobytes()
    document.close()

    response = client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "ABC_register.pdf"},
        content=data,
    )

    assert response.status_code == 200, response.text
    assert response.json()["candidates"] >= 1
    assert ReviewQueue.load(tmp_path / "review.json").summary()["total"] >= 1


@requires_corpus
def test_finishing_setup_continues_to_evidence_review(client):
    _step_one(client)
    _step_two(client)

    response = client.post("/w/demo/setup/complete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/w/demo/review"


@requires_corpus
def test_a_firm_may_finish_without_uploading_anything(client, tmp_path):
    """It is then told there is nothing to assess, not scored on nothing."""
    from sanhita.company import Company

    _step_one(client)
    _step_two(client)
    page = _plain(client.get("/w/demo/company").text)
    assert "Finish without evidence for now" in page

    client.post("/w/demo/setup/complete", follow_redirects=True)

    assert Company.load(tmp_path / "company.json").setup_completed_at is not None
    assert "Nothing has been uploaded yet" in _plain(client.get("/w/demo/review").text)


@requires_corpus
def test_after_setup_the_overview_is_stage_one_of_five(client):
    _step_one(client)
    _step_two(client)
    client.post("/w/demo/setup/complete", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)
    assert "Stage 1 of 5" in page
    assert "ABC Securities Pvt Ltd" in page
    assert "setting up" not in page


# --------------------------------------------- the two navigations, apart


@requires_corpus
def test_during_setup_only_the_setup_navigation_is_shown(client):
    for stage in (1, 2, 3):
        if stage == 2:
            _step_one(client)
        if stage == 3:
            _step_two(client)
        bar = _plain(_bar(client.get("/w/demo/company").text))

        assert "Setting up" in bar, f"step {stage} shows no setup navigation"
        assert "Company" in bar and "Framework" in bar and "Evidence" in bar
        for lifecycle_only in ("Overview", "Assessment", "Remediation", "Audit"):
            assert lifecycle_only not in bar, (
                f"step {stage} also shows the lifecycle: {lifecycle_only}"
            )


@requires_corpus
def test_after_setup_only_the_lifecycle_navigation_is_shown(client):
    _step_one(client)
    _step_two(client)
    client.post("/w/demo/setup/complete", follow_redirects=True)
    bar = _plain(_bar(client.get("/w/demo/company").text))

    assert "Setting up" not in bar, "the setup navigation outlived setup"
    for stage in ("Overview", "Evidence", "Assessment", "Remediation", "Audit"):
        assert stage in bar, f"the lifecycle is missing {stage}"


@requires_corpus
def test_the_two_navigations_never_appear_together(client):
    """The defect in one assertion: two numbered journeys on one screen."""
    for finish in (False, True):
        if finish:
            _step_one(client)
            _step_two(client)
            client.post("/w/demo/setup/complete", follow_redirects=True)
        body = client.get("/w/demo/company").text

        assert body.count('class="stagebar"') == 1, (
            "two navigation rows are rendered at once"
        )


@requires_corpus
def test_regulatory_change_stays_out_of_both(client):
    _step_one(client)
    _step_two(client)
    during = _plain(_bar(client.get("/w/demo/company").text))
    assert "Regulatory changes" not in during, "setup offers the other journey"

    client.post("/w/demo/setup/complete", follow_redirects=True)
    after = _bar(client.get("/w/demo/company").text)
    numbered = after[: after.index("stagebar-apart")]
    assert "Regulatory changes" not in numbered
    assert "Separately" in after


@requires_corpus
def test_advanced_and_the_rulebook_screens_are_untouched(client):
    _step_one(client)
    _step_two(client)
    client.post("/w/demo/setup/complete", follow_redirects=True)

    assert client.get("/documents").status_code == 200
    assert "SEBI rulebooks" in _plain(client.get("/documents").text)
    for path in ("/w/demo/queue", "/w/demo/coverage", "/w/demo"):
        assert client.get(path).status_code == 200, path

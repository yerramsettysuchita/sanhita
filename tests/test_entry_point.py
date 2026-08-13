"""Where signing in puts you, and why it is not the rulebook screen.

Sanhita's primary user is a compliance team at a regulated intermediary asking
whether their own firm is complying. Signing in used to land them on
`/documents`, which opens with "Drop a SEBI circular here". That is the
regulatory authoring workflow, somebody bringing a rulebook in to be compiled
and certified, and being asked for it first tells a compliance officer they are
in the wrong product.

The rulebook workflow is not removed. It is the other persona's, and it lives
under Advanced.
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


def _sign_up(client, email="officer@example.com"):
    return client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": email,
            "password": "a-long-enough-password",
        },
        follow_redirects=False,
    )


# ------------------------------------------------------- where you land


@requires_corpus
def test_signing_up_lands_on_the_firm_not_the_rulebook(client):
    response = _sign_up(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/w/demo/company", (
        "a new account was sent to the regulatory authoring screen"
    )


@requires_corpus
def test_signing_in_lands_on_the_firm_not_the_rulebook(client):
    _sign_up(client)
    client.post("/signout", follow_redirects=False)

    response = client.post(
        "/signin",
        data={"email": "officer@example.com", "password": "a-long-enough-password"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/w/demo/company"


@requires_corpus
def test_a_new_account_is_asked_who_the_firm_is(client):
    """Step one, and nothing else. No SEBI PDF is requested."""
    response = _sign_up(client)
    page = _plain(client.get(response.headers["location"]).text)

    assert "Step 1 of 3, setting up" in page
    assert "Whose compliance is this" in page
    assert "Firm name" in page
    assert "Drop a SEBI circular here" not in page, (
        "the first screen still asks for a regulation"
    )


@requires_corpus
def test_a_returning_account_lands_on_its_own_overview(client):
    """Somebody who has already set up does not repeat the setup."""
    _sign_up(client)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    client.post("/signout", follow_redirects=False)

    landed = client.post(
        "/signin",
        data={"email": "officer@example.com", "password": "a-long-enough-password"},
        follow_redirects=True,
    )
    page = _plain(landed.text)

    assert "ABC Securities Pvt Ltd" in page
    assert "Stage 1 of 5" in page
    assert "setting up" not in page


@requires_corpus
def test_an_explicit_next_is_still_honoured(client):
    """Following a link into a screen, then signing in, returns you to it."""
    _sign_up(client)
    client.post("/signout", follow_redirects=False)

    response = client.post(
        "/signin",
        data={
            "email": "officer@example.com",
            "password": "a-long-enough-password",
            "next": "/w/demo/audit",
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/w/demo/audit"


# -------------------------------------- the other persona keeps its workflow


@requires_corpus
def test_the_rulebook_screen_still_exists_and_still_takes_a_circular(client, corpus_pdf):
    """Not removed, not moved. Only no longer the first thing a firm meets."""
    _sign_up(client)

    page = client.get("/documents")
    assert page.status_code == 200
    assert "Drop a SEBI circular here" in _plain(page.text)

    uploaded = client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes(),
        headers={"x-sanhita-filename": "another-circular.pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["id"]


@requires_corpus
def test_the_rulebook_screen_is_reachable_from_advanced(client):
    body = client.get("/w/demo/company").text
    menu = body[body.index("navgroup-menu-wide") :]
    menu = menu[: menu.index("</details>")]

    assert 'href="/documents"' in menu
    assert "SEBI rulebooks" in menu, "the menu entry no longer names what it opens"


@requires_corpus
def test_the_word_documents_no_longer_labels_the_regulation(client):
    """It read as "my compliance documents" to the person it was shown to."""
    page = _plain(client.get("/documents").text)

    assert "SEBI rulebooks" in page
    assert "Your documents" not in page, (
        "the regulation is still labelled as the firm's own documents"
    )


@requires_corpus
def test_company_evidence_and_regulatory_pdfs_are_different_doors(client):
    """Two uploads, two meanings, and neither should be mistaken for the other."""
    _sign_up(client)

    rulebook = _plain(client.get("/documents").text)
    assert "Drop a SEBI circular here" in rulebook

    evidence = _plain(client.get("/w/demo/review").text)
    assert "Drop your compliance records here" in evidence
    assert "Drop a SEBI circular here" not in evidence

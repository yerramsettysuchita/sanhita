"""The firm is the root object, and the rulebook is a property of the firm.

The product was built the other way round. A workspace was a circular, the
company profile was a file inside it, and switching document silently switched
firm. That shape cannot express the ordinary case of one stock broker held to
two SEBI frameworks, and it made the regulation the thing the user navigates
rather than their own compliance.

Two properties are held here.

* The profile lives above every rulebook, so it is the same firm whichever
  circular you arrived through.
* Which frameworks apply is declared by a named person, never inferred from
  the intermediary category. That is a legal judgement with consequences.
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


def _name_the_firm(client, name: str = "ABC Securities Pvt Ltd"):
    return client.post(
        "/w/demo/company/save",
        data={"name": name, "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )


# ------------------------------------------------------- the firm is the root


@requires_corpus
def test_the_profile_lives_above_the_rulebook(client, tmp_path):
    """Beside the store root, not inside a workspace folder."""
    _name_the_firm(client)

    assert (tmp_path / "company.json").is_file(), (
        "the firm was written inside a rulebook rather than above it"
    )


@requires_corpus
def test_the_same_firm_is_seen_through_any_rulebook(client, tmp_path, corpus_pdf):
    """Switching document must not switch firm.

    This is the defect in one sentence. A second rulebook used to come with a
    second, empty company, so the product forgot who you were when you changed
    circular.
    """
    _name_the_firm(client)

    # Sign up and bring a second rulebook, which gets its own workspace.
    client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    uploaded = client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes(),
        headers={"x-sanhita-filename": "second-rulebook.pdf"},
    )
    if uploaded.status_code != 200:  # pragma: no cover - upload guard changed
        pytest.skip(f"could not add a second rulebook: {uploaded.text}")
    second = uploaded.json()["id"]

    page = _plain(client.get(f"/w/{second}/company").text)
    assert "ABC Securities Pvt Ltd" in page, (
        "the firm was forgotten when the rulebook changed"
    )


# ------------------------------------------------------ declaring frameworks


@requires_corpus
def test_no_framework_is_assumed_from_the_intermediary_category(client):
    """Being a stock broker does not by itself declare which circular applies.

    A firm that has not declared one is held on setup step two rather than
    shown a compliance position against whichever rulebook it happens to be
    viewing.
    """
    _name_the_firm(client)

    page = _plain(client.get("/w/demo/company").text)
    assert "Step 2 of 3, setting up" in page
    assert "Which SEBI rulebooks apply to" in page
    assert "Sanhita will not decide this for you" in page

    # And nothing further is offered until the question is answered.
    assert "Upload compliance evidence" not in page
    assert "Compliance assessment not yet run" not in page


@requires_corpus
def test_declaring_a_framework_persists_it(client, tmp_path):
    from sanhita.company import Company

    _name_the_firm(client)
    response = client.post(
        "/w/demo/company/frameworks",
        data={"framework": "demo"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    firm = Company.load(tmp_path / "company.json")
    assert firm.frameworks == ["demo"]

    client.post("/w/demo/setup/complete", follow_redirects=True)
    page = _plain(client.get("/w/demo/company").text)
    assert "Step 2 of 3" not in page, "the setup gate did not lift"
    # The scope is stated, never implied. One framework declared, and this
    # screen is that one.
    assert "1 declared, 1 assessed here" in page


@requires_corpus
def test_an_unknown_rulebook_cannot_be_declared(client):
    """Otherwise a firm could claim to be governed by something that is not here."""
    _name_the_firm(client)

    response = client.post(
        "/w/demo/company/frameworks",
        data={"framework": "no-such-rulebook"},
        follow_redirects=False,
    )

    assert response.status_code == 404


@requires_corpus
def test_frameworks_cannot_be_declared_before_the_firm_is_named(client):
    response = client.post(
        "/w/demo/company/frameworks",
        data={"framework": "demo"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Name the firm" in response.text


@requires_corpus
def test_saving_the_profile_does_not_drop_the_declared_frameworks(client, tmp_path):
    """Two forms on one screen, and neither may quietly undo the other."""
    from sanhita.company import Company

    _name_the_firm(client)
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )

    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd", "processes": "Margin reporting"},
        follow_redirects=True,
    )

    firm = Company.load(tmp_path / "company.json")
    assert firm.frameworks == ["demo"], "saving the profile erased the declaration"
    assert firm.processes == ["Margin reporting"]


@requires_corpus
def test_a_rulebook_with_nothing_certified_says_so_beside_the_box(client, tmp_path):
    """Selecting a rulebook that can assess nothing should not be a surprise."""
    page = _plain(client.get("/w/demo/company").text)
    _name_the_firm(client)
    page = _plain(client.get("/w/demo/company").text)

    assert "certified of" in page, "the tick box does not say what the rulebook holds"


@requires_corpus
def test_clearing_every_box_is_a_real_declaration(client, tmp_path):
    """Unticking everything means none apply, not "leave it as it was"."""
    from sanhita.company import Company

    _name_the_firm(client)
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/company/frameworks", data={}, follow_redirects=True)

    assert Company.load(tmp_path / "company.json").frameworks == []

"""The routes must hold the same order the screens walk a visitor through.

Two holes are closed here, both found by driving the running application rather
than by reading it.

**Setting up was a suggestion.** The screens ask for the company, then the
frameworks that govern it, then its records, in that order. The routes did not.
``_require_declared`` opened with ``if firm is None: return``, so the guard was
skipped in exactly the case it most needed to cover: a POST to ``/assess`` with
no company profile recorded an assessment against nothing, and every lifecycle
screen then opened on it.

**Authoring the rulebook was open.** Certifying a rule required an account from
the start. Compiling one did not, which left the odd position that a stranger
could not sign a rule but could start the job that draws a thousand of them and
rewrites the store underneath the person reviewing it. Deleting a circular
somebody else uploaded was open too, and so was the one route that reaches
outside this machine.

Reading is still never gated. A visitor can walk the whole product and see
everything. Writing a record somebody may later be asked to answer for, or
starting a job that rewrites shared state, is what needs a name.
"""

from __future__ import annotations

import re
import shutil

import pytest

from tests.conftest import requires_corpus

# ----------------------------------------------------------------- fixtures


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def sign_in(client, name="A Named Officer", email="officer@example.com"):
    return client.post(
        "/signup",
        data={"name": name, "email": email, "password": "a-long-enough-password"},
        follow_redirects=True,
    )


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# ------------------------------------------- the lifecycle, held by the routes


@requires_corpus
def test_assessing_with_no_company_at_all_is_refused(client):
    """The defect, in one assertion.

    ``_require_declared(None, ...)`` used to return, so this POST succeeded and
    redirected to the gaps screen with an assessment recorded against no firm.
    """
    sign_in(client)
    response = client.post("/w/demo/assess", follow_redirects=False)

    assert response.status_code == 400, (
        "an assessment was accepted for an installation with no company profile"
    )
    assert "No company profile exists" in response.text


@requires_corpus
def test_assessing_before_setup_is_finished_is_refused(client):
    """A company and a framework are two of the three answers, not all three."""
    sign_in(client)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    response = client.post("/w/demo/assess", follow_redirects=False)

    assert response.status_code == 400
    assert "has not finished setting up" in response.text


@requires_corpus
def test_the_refusal_says_what_is_still_missing(client):
    """A 400 that does not say what to do next is a dead end."""
    sign_in(client)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    body = client.post("/w/demo/assess", follow_redirects=False).text

    assert "three answers" in body
    assert "company screen" in body


@requires_corpus
def test_raising_a_task_before_setup_is_refused(client):
    """The same guard covers every firm-scoped write, not only assessment."""
    sign_in(client)
    response = client.post(
        "/w/demo/remediation/open",
        data={"obligation_id": "SB-40.1.8-a"},
        follow_redirects=False,
    )
    assert response.status_code in (400, 404, 422), response.status_code


# ------------------------------------------ authoring the rulebook needs a name


@requires_corpus
@pytest.mark.parametrize(
    "path, data",
    [
        ("/w/demo/compile", {"engine": "rules"}),
        ("/w/demo/cancel", None),
        ("/w/demo/delete", None),
        ("/w/demo/discover", None),
    ],
)
def test_a_regulatory_write_needs_an_account(client, path, data):
    """Anonymous visitors read. They do not rewrite the shared store."""
    response = client.post(path, data=data or {}, follow_redirects=False)

    assert response.status_code == 401, (
        f"{path} accepted an anonymous write and returned {response.status_code}"
    )
    assert "need an account" in response.text


@requires_corpus
def test_compiling_is_allowed_once_signed_in(client):
    """The gate is on the account, not on the action.

    A 401 here would mean the fix had broken the feature rather than protected
    it. Anything other than 401 is a pass: the job may start, or be refused for
    a reason of its own, and both prove the account was accepted.
    """
    sign_in(client)
    response = client.post(
        "/w/demo/compile", data={"engine": "rules"}, follow_redirects=False
    )
    assert response.status_code != 401


# --------------------------------------------------- reading is still not gated


@requires_corpus
@pytest.mark.parametrize(
    "path",
    ["/", "/queue", "/coverage", "/audit", "/gaps", "/clause/40.1.8", "/documents"],
)
def test_reading_never_needs_an_account(client, path):
    """The product is walkable by a stranger, and that is deliberate."""
    assert client.get(path).status_code == 200, path


# ------------------------------------- editing the profile is not un-onboarding


@requires_corpus
def test_editing_the_profile_does_not_undo_setup(client):
    """Correcting a registration number must not send a firm back to step three.

    ``/company/save`` rebuilds the ``Company`` from the form and carries over
    the fields the form does not carry: the declared frameworks, the creation
    date, whether the profile is synthetic. ``setup_completed_at`` was missing
    from that list, so saving the profile silently un-finished onboarding.

    Nothing caught it while the routes let an un-set-up firm through anyway.
    The moment they stopped, a firm that edited its own details could no longer
    record a position, which is a worse bug than the one being fixed.
    """
    from sanhita.company import Company

    sign_in(client)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)

    # A month later, somebody corrects the registration number.
    client.post(
        "/w/demo/company/save",
        data={
            "name": "ABC Securities",
            "intermediary": "STOCK_BROKER",
            "registration": "INZ000123456",
        },
        follow_redirects=True,
    )

    page = _plain(client.get("/w/demo/company").text)
    assert "Step 3 of 3" not in page, "editing the profile sent the firm back to setup"
    assert Company is not None  # the import is the contract being relied on


@requires_corpus
def test_a_set_up_firm_can_still_be_assessed_after_an_edit(client):
    """The same defect, at the route rather than the screen."""
    sign_in(client)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )

    response = client.post("/w/demo/assess", follow_redirects=False)

    assert "has not finished setting up" not in response.text, (
        "editing the profile un-finished onboarding"
    )


# --------------------------- the seed is read from, never inherited from


@pytest.fixture()
def shared(corpus_pdf, tmp_path, monkeypatch):
    """A shared deployment carrying a seeded demonstration firm.

    Built here rather than copied off this machine. The first version took
    `.sanhita/company.json` from the working directory behind an `if it
    exists` guard. That file is deliberately not in the repository, so on CI
    the guard skipped, no demonstration firm existed, and the two tests below
    passed without exercising anything: with nothing to inherit from, a new
    visitor's firm cannot inherit wrongly.

    A test that passes because the situation it describes never arose is worse
    than one that fails, so the situation is constructed and asserted.
    """
    import datetime as _dt
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.company import Company, IntermediaryType
    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    monkeypatch.setenv("SANHITA_SHARED", "1")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)

    stamp = _dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc)
    Company(
        name="ABC Securities Pvt Ltd",
        intermediary=IntermediaryType.STOCK_BROKER,
        frameworks=["demo"],
        setup_completed_at=stamp,
        created_at=stamp,
        synthetic=True,
    ).save(tmp_path / "company.json")
    assert (tmp_path / "company.json").is_file(), "the demonstration firm was not seeded"

    return TestClient(create_app(corpus_pdf, store=store))


@requires_corpus
def test_a_new_visitors_firm_is_not_marked_synthetic(shared):
    """The worst version of inheriting from the seed.

    Reads fall through to the demonstration state, so ``_company`` returns the
    seeded firm to anybody who has not saved one yet. ``/company/save`` carried
    that firm's fields forward, including ``synthetic=True``, so a stranger who
    typed their own firm's name got "this is demonstration data" printed across
    a real profile. A screen that labels real data synthetic is worse than one
    that labels nothing.
    """
    sign_in(shared, name="New Visitor", email="new@example.com")
    shared.post(
        "/w/demo/company/save",
        data={"name": "Zeta Broking Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    page = _plain(shared.get("/w/demo/company").text)

    assert "Zeta Broking Pvt Ltd" in page
    assert "synthetic" not in page.lower(), (
        "a real visitor's firm inherited the demonstration firm's synthetic flag"
    )


@requires_corpus
def test_a_new_visitor_still_has_to_walk_the_setup(shared):
    """Inheriting ``setup_completed_at`` skipped onboarding on the one
    deployment where strangers actually arrive."""
    sign_in(shared, name="New Visitor", email="new@example.com")
    shared.post(
        "/w/demo/company/save",
        data={"name": "Zeta Broking Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )

    page = _plain(shared.get("/w/demo/company").text)
    assert "Step 2 of 3" in page, "the visitor skipped straight past onboarding"

    refused = shared.post("/w/demo/assess", follow_redirects=False)
    assert refused.status_code == 400
    assert "has not finished setting up" in refused.text

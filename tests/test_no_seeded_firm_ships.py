"""The deployment ships the regulation. It ships nobody's firm.

The image used to run `demo-seed` during its build, so a visitor opened the
site on a synthetic company with a filing register, a recorded assessment and
an account to sign in as. Labelling it as a demonstration helped, and did not
fix it. A jury judging a compliance product should meet the product. Every
figure on that first screen belonged to a company that does not exist, and the
one thing the product is for, assessing a real firm, was the one thing not
being shown.

So the split is now drawn at ownership rather than at usefulness:

    SEBI's published circulars ship, because a compiler with no source file is
    a blank screen, and because the amendment comparison needs both sides of a
    real reissue to open.

    No account, no company, no filing register and no assessment ship. Whoever
    opens the site records their own firm.

`sanhita shelve-circulars` is what the image runs. `demo-seed` still exists for
recording a walkthrough on a laptop, where a synthetic firm is the point.
"""

from __future__ import annotations

import re
import shutil

import pytest

from tests.conftest import requires_corpus


@pytest.fixture()
def shipped(corpus_pdf, tmp_path, monkeypatch):
    """A store built the way the image builds one: circulars, nothing else."""
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    monkeypatch.setenv("SANHITA_SHARED", "1")
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", tmp_path / "rules.json")

    from sanhita.demo_seed import _register_editions

    _register_editions(tmp_path, corpus_pdf.parent)
    return TestClient(create_app(corpus_pdf, store=tmp_path / "rules.json"))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


# ------------------------------------------- nobody's firm is in the image


@requires_corpus
def test_a_visitor_meets_their_own_setup(shipped):
    """The defect, in one assertion.

    Not a labelled demonstration. Not somebody else's 94%. The first question
    the product asks, which is whose compliance this is.
    """
    page = _plain(shipped.get("/w/demo/company").text)

    assert "Step 1 of 3" in page
    assert "Whose compliance is this" in page


@requires_corpus
def test_no_synthetic_firm_is_shipped(shipped):
    page = _plain(shipped.get("/w/demo/company").text)

    assert "ABC Securities" not in page
    assert "Demonstration workspace" not in page
    assert "synthetic" not in page.lower()


@requires_corpus
def test_no_account_is_shipped(shipped, tmp_path):
    """A published credential in an image is a credential on the internet."""
    assert not (tmp_path / "users.json").exists(), "an account shipped with the store"

    # A refused sign-in redirects too, carrying the reason, so the status alone
    # says nothing. Where it sends you is the answer.
    refused = shipped.post(
        "/signin",
        data={
            "email": "demo.officer@sanhita.invalid",
            "password": "demo-compliance-officer",
        },
        follow_redirects=False,
    )
    assert "/signin?error=" in refused.headers.get("location", ""), (
        "the old demonstration login still works"
    )
    from urllib.parse import parse_qs, unquote, urlparse

    reason = parse_qs(urlparse(refused.headers["location"]).query).get("error", [""])[0]
    assert "do not match an account" in unquote(reason)


@requires_corpus
def test_no_records_or_assessments_are_shipped(shipped, tmp_path):
    for name in ("company.json", "evidence.json", "assessments.json", "remediation.json"):
        assert not (tmp_path / name).exists(), f"{name} shipped with the store"


# ------------------------------------------------ the regulation does ship


@requires_corpus
def test_the_rulebook_ships(shipped):
    """A compiler with no source file is a blank screen."""
    payload = shipped.get("/healthz").json()

    assert payload["ok"] is True
    assert payload["rules"] == 1377
    assert payload["ledger"] == 1560


@requires_corpus
def test_both_amendment_editions_ship(shipped):
    """The comparison needs both sides of a real reissue."""
    payload = shipped.get("/healthz").json()
    assert payload["documents"] == 3, "the Investment Adviser editions are missing"


@requires_corpus
def test_an_anonymous_visitor_can_reach_the_editions(shipped):
    """The bug this split exists to prevent.

    The shipped circulars used to render inside a block headed "your
    rulebooks", behind a signed-in check. Nobody noticed while the image also
    shipped an account to sign in as. The moment it stopped, an anonymous
    visitor could no longer reach the two editions the strongest screen in the
    product is built on.
    """
    page = _plain(shipped.get("/documents").text)

    assert page.count("Investment Advisers") >= 2, (
        "a visitor with no account cannot see the shipped circulars"
    )
    assert "Also on this installation" in page


@requires_corpus
def test_the_shipped_circulars_are_not_called_the_visitors_own(shipped):
    """They belong to nobody. Saying otherwise claims something untrue."""
    page = _plain(shipped.get("/documents").text)
    heading = page.find("Also on this installation")
    listing = page.find("Investment Advisers")

    assert heading != -1 and heading < listing, (
        "the editions are listed without the note saying whose they are"
    )
    assert "belong to nobody" in page


@requires_corpus
def test_the_amendment_comparison_has_something_to_compare(shipped):
    page = _plain(shipped.get("/w/demo/diff").text)

    assert "Investment Advisers" in page
    assert "nothing to compare" not in page.lower()


# --------------------------------------------- the walkthrough still works


@requires_corpus
def test_demo_seed_still_exists_for_a_laptop(tmp_path, corpus_pdf):
    """Removing it from the image is not removing it.

    Recording a walkthrough wants a synthetic firm with a history. That is a
    reasonable thing to want on one machine, and an unreasonable thing to ship
    to everybody who opens a URL.
    """
    import shutil as _shutil

    from sanhita.demo_seed import seed_demo_state

    _shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", tmp_path / "rules.json")
    result = seed_demo_state(tmp_path, corpus=corpus_pdf.parent, backup=False)

    assert result.certified == 183
    assert (tmp_path / "company.json").is_file()
    assert (tmp_path / "users.json").is_file()

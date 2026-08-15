"""The seeded example must never read as the visitor's own firm.

On the shared deployment, reads fall through to the demonstration state so a
first-time visitor meets a working assessment instead of an empty form. That is
worth keeping: a judge who opens the URL should see the product, not a setup
wizard.

What was wrong is what the page then said about it. The tab read **My Firm**,
the heading read **Stage 1 of 5**, and ABC Securities sat under them at 94%.
Every figure was labelled synthetic, so nothing on the page was false, but the
page as a whole told a stranger that somebody else's compliance position was
theirs. The landing page button that got them there said "Check my company's
compliance", which made the claim explicit.

The fix is not authentication. Anonymous onboarding is deliberate: a visitor
creates their firm, declares frameworks and uploads evidence with no account,
and ``web/adopt.py`` carries that work across when they sign up later. Gating
these screens would mean a visitor creating a firm and then being unable to see
it, which is a worse defect than the one being fixed.

So the distinction is made in the data, not with a stylesheet. The screen is
told whether the firm it holds is this visitor's own or the seeded fallback,
and says so plainly when it is the latter.

Three states, and the tests below hold all three:

    1. No profile of their own. The demonstration, labelled as such, with the
       way out of it as the primary action.
    2. Their own firm, created anonymously. Their firm, no banner, no synthetic
       label, and no trace of ABC Securities.
    3. Signed in. Their own firm, never silently replaced by the seed.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil

import pytest

from tests.conftest import requires_corpus

BANNER = "Demonstration workspace"
DISCLAIMER = "This is not your firm's data"
CTA = "Start my company's compliance"


@pytest.fixture()
def shared(corpus_pdf, tmp_path, monkeypatch):
    """A shared deployment carrying a seeded demonstration firm.

    The firm is **built here**, not copied off this machine.

    The first version of this fixture copied `.sanhita/company.json` from the
    working directory, guarded by `if it exists`. On a laptop that has run
    `sanhita demo-seed` it exists, so the tests passed. It is not in the
    repository and never will be, because a firm's profile does not belong in
    version control, so on CI the guard silently skipped and every assertion
    about the banner then failed against a screen that had no firm on it at
    all.

    Tests that pass locally for a reason that does not hold anywhere else are
    worse than tests that fail, so this builds exactly the state it needs and
    asserts it landed.
    """
    from fastapi.testclient import TestClient

    from sanhita.company import Company, IntermediaryType
    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    monkeypatch.setenv("SANHITA_SHARED", "1")

    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)

    # The unscoped profile: what a visitor with no copy of their own reads.
    seeded = Company(
        name="ABC Securities Pvt Ltd",
        intermediary=IntermediaryType.STOCK_BROKER,
        frameworks=["demo"],
        setup_completed_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc),
        created_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc),
        synthetic=True,
    )
    seeded.save(tmp_path / "company.json")
    assert (tmp_path / "company.json").is_file(), "the demonstration firm was not seeded"

    return TestClient(create_app(corpus_pdf, store=store))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _own_firm(client, name="XYZ Securities Pvt Ltd"):
    return client.post(
        "/w/demo/company/save",
        data={"name": name, "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )


def _sign_up(client, email="new@example.invalid"):
    return client.post(
        "/signup",
        data={"name": "A Visitor", "email": email, "password": "a-long-enough-password"},
        follow_redirects=True,
    )


# ------------------------- state one: no profile of their own


@requires_corpus
def test_the_demonstration_says_it_is_a_demonstration(shared):
    """The defect, in one assertion."""
    page = _plain(shared.get("/w/demo/company").text)

    assert BANNER in page
    assert DISCLAIMER in page


@requires_corpus
def test_it_is_not_called_my_firm(shared):
    """The tab title made the claim before the page had said anything."""
    html = shared.get("/w/demo/company").text

    assert "Sanhita - Demonstration workspace" in html
    assert "Sanhita - My Firm" not in html


@requires_corpus
def test_the_way_out_is_the_primary_action(shared):
    """A label alone leaves somebody stuck reading a firm that is not theirs."""
    html = shared.get("/w/demo/company").text

    assert CTA in _plain(html)
    assert "/company?start=1" in html, "the call to action leads nowhere"


@requires_corpus
def test_the_demonstration_is_still_readable(shared):
    """Labelling it must not hide it. A judge should still see the product."""
    page = _plain(shared.get("/w/demo/company").text)

    assert "ABC Securities" in page, "the demonstration was hidden rather than labelled"


@requires_corpus
def test_starting_your_own_opens_step_one(shared):
    """Pressing the call to action reaches the first onboarding step."""
    page = _plain(shared.get("/w/demo/company?start=1").text)

    assert "Step 1 of 3" in page
    assert "Whose compliance is this" in page
    assert BANNER not in page, "the banner followed them into their own setup"


# ------------------- state two: their own firm, created anonymously


@requires_corpus
def test_anonymous_onboarding_still_works(shared):
    """The feature the obvious fix would have broken.

    No account anywhere in this test. A visitor must be able to try the product
    before committing to it.
    """
    _own_firm(shared)
    page = _plain(shared.get("/w/demo/company").text)

    assert "XYZ Securities Pvt Ltd" in page


@requires_corpus
def test_their_own_firm_carries_no_demonstration_banner(shared):
    _own_firm(shared)
    page = _plain(shared.get("/w/demo/company").text)

    assert BANNER not in page
    assert DISCLAIMER not in page


@requires_corpus
def test_the_seeded_firm_is_gone_once_they_have_their_own(shared):
    """Two firms on one screen would be worse than the original defect."""
    _own_firm(shared)
    page = _plain(shared.get("/w/demo/company").text)

    assert "ABC Securities" not in page
    assert "synthetic" not in page.lower(), "their real firm inherited the demo's label"


@requires_corpus
def test_their_firm_survives_a_reload(shared):
    _own_firm(shared)
    shared.get("/w/demo/company")
    page = _plain(shared.get("/w/demo/company").text)

    assert "XYZ Securities Pvt Ltd" in page


@requires_corpus
def test_they_can_continue_through_onboarding_without_an_account(shared):
    """Company, then framework, then the evidence step. No sign-in."""
    _own_firm(shared)
    shared.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    page = _plain(shared.get("/w/demo/company").text)

    assert "Step 3 of 3" in page
    assert "Bring your compliance evidence" in page


# ------------------------------- state three: signed in


@requires_corpus
def test_signing_up_keeps_the_firm_they_made_anonymously(shared):
    """``web/adopt.py`` carries the work across. This is the regression guard."""
    _own_firm(shared)
    _sign_up(shared)
    page = _plain(shared.get("/w/demo/company").text)

    assert "XYZ Securities Pvt Ltd" in page, "signing up lost the firm"
    assert BANNER not in page


@requires_corpus
def test_signing_up_stops_the_fallback_entirely(shared):
    """The complaint that produced this rule, in one assertion.

    A compliance officer creates an account, signs in, and is shown ABC
    Securities at 94% under a heading about their compliance. Labelling it
    helped and did not fix it: the reader came to see their own firm and was
    answered with somebody else's.

    An account is not a firm, but it is a commitment. Reads fall through to the
    demonstration for people who have committed to nothing, and that stops the
    moment somebody signs up. What they get instead is their own empty state,
    which is step one of setting up, and that is the honest answer to "what is
    my firm's position" when no firm has been recorded.
    """
    _sign_up(shared, email="empty@example.invalid")
    page = _plain(shared.get("/w/demo/company").text)

    assert "ABC Securities" not in page, (
        "a signed-in officer was shown another firm's compliance position"
    )
    assert BANNER not in page
    assert "Step 1 of 3" in page, "they were not offered their own setup"
    assert "Whose compliance is this" in page


@requires_corpus
def test_the_anonymous_visitor_still_gets_the_demonstration(shared):
    """The fallback must survive for the person it was written for."""
    page = _plain(shared.get("/w/demo/company").text)

    assert "ABC Securities" in page
    assert BANNER in page


@requires_corpus
def test_a_signed_in_officer_keeps_their_own_empty_state_across_screens(shared):
    """Not only the company screen. Every sidecar reads through the same rule."""
    _sign_up(shared, email="empty2@example.invalid")

    for path in ("/w/demo/company", "/w/demo/gaps", "/w/demo/review"):
        page = _plain(shared.get(path).text)
        assert "ABC Securities" not in page, f"{path} fell through to the demonstration"


@requires_corpus
def test_an_authenticated_firm_is_never_replaced_by_the_seed(shared):
    """The worst possible version: signing in and being shown ABC Securities."""
    _sign_up(shared)
    _own_firm(shared, name="Meridian Advisers LLP")
    page = _plain(shared.get("/w/demo/company").text)

    assert "Meridian Advisers LLP" in page
    assert "ABC Securities" not in page
    assert BANNER not in page


# ------------------------------ the laptop case, unchanged


@requires_corpus
def test_without_a_seed_there_is_nothing_to_mistake(corpus_pdf, tmp_path, monkeypatch):
    """On a clean install the button was always honest. Keep it that way."""
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))

    page = _plain(client.get("/w/demo/company").text)
    assert "Step 1 of 3" in page
    assert BANNER not in page, "a banner appeared where there is no demonstration"


# ---------------- an anonymous workspace, and the offer to keep it


@requires_corpus
def test_an_anonymous_firm_is_told_it_is_not_saved(shared):
    """Where authentication becomes visibly worth having.

    The workspace is real and it is theirs, but it is keyed to a cookie. Say so
    on the screen rather than letting somebody find out by clearing it.
    """
    _own_firm(shared)
    page = _plain(shared.get("/w/demo/company").text)

    assert "This workspace is not saved to an account" in page
    assert "yours on this browser only" in page
    assert "Save this workspace" in page


@requires_corpus
def test_it_explains_what_an_account_is_actually_for(shared):
    """Not a wall. The reason the product asks who you are."""
    _own_firm(shared)
    page = _plain(shared.get("/w/demo/company").text)

    assert "run an assessment" in page
    assert "named person against the act" in page
    assert "Nothing you have entered is lost" in page


@requires_corpus
def test_the_offer_disappears_once_they_have_an_account(shared):
    """Signing up answers it, so the prompt must stop asking."""
    _own_firm(shared)
    _sign_up(shared)
    page = _plain(shared.get("/w/demo/company").text)

    assert "This workspace is not saved to an account" not in page
    assert "XYZ Securities Pvt Ltd" in page, "and the firm came with them"


@requires_corpus
def test_the_demonstration_is_never_offered_for_saving(shared):
    """It is not theirs to keep. The two banners must not both appear."""
    page = _plain(shared.get("/w/demo/company").text)

    assert BANNER in page
    assert "This workspace is not saved to an account" not in page


# --------------- the demonstration officer owns the demonstration


@requires_corpus
def test_demo_seed_gives_the_officer_their_own_copy(tmp_path, corpus_pdf):
    """Otherwise the fix would make the demonstration account useless.

    Stopping the fallback at sign-in is right, and taken alone it would leave
    the demonstration officer looking at an empty setup form, because the
    seeded files are unscoped and match nobody. So `demo-seed` copies them to
    that officer's scope: the demonstration firm gets an owner, which is also
    the more honest data model.
    """
    import shutil

    from sanhita.demo_seed import seed_demo_state

    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", tmp_path / "rules.json")
    result = seed_demo_state(tmp_path, corpus=corpus_pdf.parent, backup=False)

    assert result.owned_by_officer, "the officer was left with nothing of their own"
    assert "company.json" in result.owned_by_officer

    scoped = list(tmp_path.glob("company.u*.json"))
    assert len(scoped) == 1, f"expected one scoped profile, found {scoped}"
    assert (tmp_path / "company.json").is_file(), (
        "the unscoped copy was moved rather than copied, so an anonymous "
        "visitor now sees nothing"
    )

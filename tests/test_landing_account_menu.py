"""Signing out has to be possible from the page a visitor lands on.

The workbench has carried an account menu since sign-in arrived: an initial in
a circle, and behind it the name, the address and a sign-out button. The
landing page had an initial in a circle and nothing behind it, because the
avatar was a bare ``<span>`` with a ``title`` attribute.

A tooltip is not a control. So the one page a signed-in visitor sees first was
the one page they could not sign out from, and the only way out was to guess
that the workbench had a menu the landing page did not.

The two pages do not share a stylesheet. The landing header is dark and the
workbench header is not, and dropping the workbench's light popup onto a purple
bar looks like a rendering fault rather than a menu, so the styles are written
separately on purpose.
"""

from __future__ import annotations

import shutil

import pytest

from tests.conftest import requires_corpus


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def sign_in(client, name="R Suchita, Compliance Officer", email="suchita@example.com"):
    return client.post(
        "/signup",
        data={"name": name, "email": email, "password": "a-long-enough-password"},
        follow_redirects=True,
    )


# ------------------------------------------------------- signed out


@requires_corpus
def test_a_stranger_is_offered_a_way_in(client):
    html = client.get("/").text

    assert "Sign in" in html
    assert "lp-account-menu" not in html, "a menu was rendered for nobody"


# -------------------------------------------------------- signed in


@requires_corpus
def test_the_avatar_is_a_control_not_a_tooltip(client):
    """The defect, in one assertion."""
    sign_in(client)
    html = client.get("/").text

    assert '<details class="lp-account">' in html, (
        "the avatar is still not something a visitor can open"
    )
    assert "lp-account-menu" in html


@requires_corpus
def test_the_menu_says_who_is_signed_in(client):
    sign_in(client)
    html = client.get("/").text

    assert "R Suchita, Compliance Officer" in html
    assert "suchita@example.com" in html


@requires_corpus
def test_the_menu_offers_sign_out(client):
    sign_in(client)
    html = client.get("/").text

    assert "Sign out" in html
    assert 'action="/signout"' in html, "the button posts nowhere"


@requires_corpus
def test_signing_out_from_the_landing_page_works(client):
    """The whole point. A button that renders and does nothing is worse."""
    sign_in(client)
    response = client.post("/signout", follow_redirects=False)

    assert response.status_code == 303
    after = client.get("/").text
    assert "Sign in" in after
    assert "lp-account-menu" not in after, "still signed in after signing out"


@requires_corpus
def test_the_menu_can_be_dismissed(client):
    """A <details> stays open until its summary is clicked again, which reads
    as a stuck panel. The landing script closes it on an outside click and on
    Escape, like every other menu on the web."""
    from pathlib import Path

    script = Path("src/sanhita/web/static/landing.js").read_text(encoding="utf-8")

    assert ".lp-account" in script, "nothing on the page closes the menu"
    assert "Escape" in script
    assert "account.contains(event.target)" in script


@requires_corpus
def test_the_menu_is_styled_for_a_dark_header(client):
    """Written separately from the workbench menu, deliberately."""
    from pathlib import Path

    css = Path("src/sanhita/web/static/landing.css").read_text(encoding="utf-8")

    assert ".lp-account-menu" in css
    assert ".lp-account-out" in css, "the sign-out button has no styling"

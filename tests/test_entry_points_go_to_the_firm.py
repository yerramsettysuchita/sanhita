"""Every way in has to land on the firm's own screen.

A compliance officer arrives to find out whether their firm is complying.
"Open the workbench" pointed at `/documents`, which opens with "Drop a SEBI
circular here": the regulatory authoring workflow, and a screen that tells the
primary user they are in the wrong product.

It is an easy defect to reintroduce, because `/documents` is a perfectly good
page and the phrase "the workbench" once meant it. So the destinations are
asserted rather than trusted, and the Advanced menu is asserted to be the only
place the rulebook workflow is offered from.
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

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _hero_links(html: str) -> list[tuple[str, str]]:
    """Every button on the landing page, as (href, label)."""
    return [
        (m.group(1), re.sub(r"\s+", " ", m.group(2)).strip())
        for m in re.finditer(
            r'<a[^>]*class="[^"]*lp-btn[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html,
            re.S,
        )
    ]


@requires_corpus
def test_every_landing_button_lands_on_the_firm_or_the_change_journey(client):
    """The defect, in one assertion.

    A landing page button may go to the company journey, the regulatory change
    journey, or an account screen. It may not go to the rulebook upload page,
    because nobody presses "Check my compliance" hoping to be asked for a PDF.
    """
    links = _hero_links(client.get("/").text)
    assert links, "the landing page has no buttons"

    allowed = ("/w/demo/company", "/w/demo/diff", "/signup", "/signin")
    for href, label in links:
        assert href.startswith(allowed), (
            f"{label!r} points at {href!r}, which is not the firm's journey"
        )


@requires_corpus
def test_open_the_workbench_opens_the_firms_screen(client):
    """Shown only to a signed-in visitor, which is who it misdirected."""
    sign_in(client)
    links = dict((label, href) for href, label in _hero_links(client.get("/").text))

    assert links.get("Open the workbench") == "/w/demo/company", (
        "the workbench button sent a compliance officer to the rulebook upload page"
    )


@requires_corpus
def test_the_two_headline_buttons_are_the_two_journeys(client):
    links = dict((label, href) for href, label in _hero_links(client.get("/").text))

    assert links.get("Check my company's compliance") == "/w/demo/company"
    assert links.get("See what changed in SEBI") == "/w/demo/diff"


@requires_corpus
def test_signing_in_lands_on_the_firm(client):
    landed = client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=False,
    )

    assert landed.status_code == 303
    assert landed.headers["location"].startswith("/w/demo/company")


@requires_corpus
def test_signing_back_in_lands_on_the_firm(client):
    sign_in(client)
    client.post("/signout", follow_redirects=True)

    landed = client.post(
        "/signin",
        data={"email": "officer@example.com", "password": "a-long-enough-password"},
        follow_redirects=False,
    )

    assert landed.status_code == 303
    assert landed.headers["location"].startswith("/w/demo/company")


@requires_corpus
def test_the_rulebook_workflow_is_reachable_only_from_advanced(client):
    """Not removed. Moved to where the analyst works rather than the firm."""
    body = client.get("/w/demo/company").text

    menu = body[body.index("navgroup-menu") : body.index("mast-end")]
    assert 'href="/documents"' in menu, "the rulebook workflow became unreachable"

    # And not in the firm's own stage bar.
    stages = body[body.index('class="stagebar"') : body.index("navgroup-menu")]
    assert 'href="/documents"' not in stages


@requires_corpus
def test_advanced_is_grouped_the_way_the_product_is_organised(client):
    body = client.get("/w/demo/company").text
    menu = body[body.index("navgroup-menu") : body.index("mast-end")]
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", menu))

    for heading in ("Regulation", "Operational analysis", "Supervision", "Trust"):
        assert heading in page, f"the Advanced menu has no {heading!r} group"

    # And the order is the order the product is read in.
    positions = [page.index(h) for h in ("Regulation", "Operational analysis", "Supervision", "Trust")]
    assert positions == sorted(positions)


@requires_corpus
def test_every_advanced_destination_still_answers(client):
    """A navigation change must not quietly drop a capability."""
    body = client.get("/w/demo/company").text
    menu = body[body.index("navgroup-menu") : body.index("mast-end")]
    hrefs = sorted(set(re.findall(r'href="([^"]+)"', menu)))

    assert len(hrefs) >= 10, f"Advanced lost capabilities: {hrefs}"
    for href in hrefs:
        assert client.get(href).status_code == 200, f"{href} is broken"

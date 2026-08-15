"""Three small things a person notices and a codebase does not.

**The framework switcher asked two questions with one control.** On a firm's
own screen it listed every rulebook on the installation and linked each to
`/w/<id>`, the regulatory workspace. So a compliance officer changing framework
on their assessment screen was dropped into a circular nobody had said governed
them, out of the firm's context entirely. It now offers what the firm declared
and stays where it was. On a regulatory screen it is unchanged, because there
the question really is which document to author against.

**The password field could not be read back.** A passphrase of a few words is
stronger than a short jumble and is also the thing people mistype, so hiding it
unconditionally makes the stronger choice the harder one.

**The primary call to action.** Nothing to fix: the deployment stopped shipping
a seeded firm, so "Check my company's compliance" already lands on step one.
The test is here so that stays true if anything ever seeds one again.
"""

from __future__ import annotations

import re
import shutil

import pytest

from tests.conftest import requires_corpus


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    monkeypatch.setenv("SANHITA_SHARED", "1")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)

    from sanhita.demo_seed import _register_editions

    _register_editions(tmp_path, corpus_pdf.parent)
    client = TestClient(create_app(corpus_pdf, store=store))
    client.post(
        "/signup",
        data={
            "name": "R Suchita",
            "email": "officer@example.invalid",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _set_up(client, name="Meridian Capital Services Pvt Ltd", frameworks=("demo",)):
    client.post(
        "/w/demo/company/save",
        data={"name": name, "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    for framework in frameworks:
        client.post(
            "/w/demo/company/frameworks",
            data={"framework": list(frameworks)},
            follow_redirects=True,
        )
        break
    client.post("/w/demo/setup/complete", follow_redirects=True)


# ------------------------------------------ the first click goes to the firm


@requires_corpus
def test_the_primary_call_to_action_opens_step_one(client):
    """No seeded firm ships, so the button keeps its promise on its own."""
    html = client.get("/").text
    assert "/w/demo/company" in html, "the landing page no longer offers it"

    page = _plain(client.get("/w/demo/company").text)
    assert "Step 1 of 3" in page
    assert "Whose compliance is this" in page


# ----------------------------------------------- the switcher, on a firm's screen


@requires_corpus
def test_a_firms_switcher_offers_only_what_it_declared(client):
    """The defect, in one assertion.

    Three rulebooks are on this installation. The firm declared one. The
    switcher used to offer all three.
    """
    _set_up(client)
    html = client.get("/w/demo/company").text
    menu = html[html.index('class="docpick-menu"') : html.index("</details>")]

    assert "Investment Advisers" not in menu, (
        "a rulebook the firm never declared is offered as one of its frameworks"
    )
    assert "declared by Meridian Capital Services Pvt Ltd" in menu


@requires_corpus
def test_switching_framework_stays_inside_the_firm(client):
    """Where the entries lead, which is the half that actually broke."""
    _set_up(client)
    html = client.get("/w/demo/company").text
    menu = html[html.index('class="docpick-menu"') : html.index("</details>")]

    targets = re.findall(r'class="docpick-item[^"]*" href="([^"]+)"', menu)
    assert targets, "the firm's switcher offers nothing at all"
    for target in targets:
        assert target.endswith("/company"), (
            f"{target} leaves the firm's context for the regulatory workspace"
        )


@requires_corpus
def test_a_firm_with_no_framework_is_told_so(client):
    """An empty menu reads as broken rather than as empty."""
    client.post(
        "/w/demo/company/save",
        data={"name": "Meridian Capital Services Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    page = _plain(client.get("/w/demo/company").text)
    assert "has not declared a rulebook yet" in page


# ------------------------------------- and unchanged on a regulatory screen


@requires_corpus
def test_the_regulatory_switcher_still_offers_every_rulebook(client):
    """The analyst's question is a different question."""
    _set_up(client)
    html = client.get("/w/demo/queue").text
    menu = html[html.index('class="docpick-menu"') : html.index("</details>")]

    assert "Investment Advisers" in menu, "the analyst lost access to the editions"
    assert "Bring your own circular" in menu


@requires_corpus
def test_regulatory_entries_still_lead_to_the_workspace(client):
    _set_up(client)
    html = client.get("/w/demo/queue").text
    menu = html[html.index('class="docpick-menu"') : html.index("</details>")]

    targets = re.findall(r'class="docpick-item[^"]*" href="([^"]+)"', menu)
    assert targets
    for target in targets:
        assert not target.endswith("/company"), (
            f"{target} sends the analyst into a firm's screens"
        )


# ------------------------------------------------- reading your own password


@requires_corpus
@pytest.mark.parametrize("path", ["/signin", "/signup"])
def test_the_password_can_be_revealed(client, path):
    html = client.get(path).text

    assert 'id="pweye"' in html, "no way to check what was typed"
    assert 'type="button"' in html, "the control would submit the form"
    assert 'aria-label="Show password"' in html
    assert 'aria-pressed="false"' in html
    assert 'aria-controls="pw"' in html


@requires_corpus
def test_the_toggle_is_ours_rather_than_a_dependency(client):
    """No CDN anywhere in this product, and a reveal is not worth the first."""
    html = client.get("/signin").text

    assert "<svg" in html, "the icon came from somewhere else"
    for host in ("googleapis", "gstatic", "unpkg", "jsdelivr", "cdnjs", "fontawesome"):
        assert host not in html, f"the sign-in page reaches {host}"


@requires_corpus
def test_the_field_still_starts_hidden(client):
    """The point is that it can be read back, not that it is on show."""
    html = client.get("/signin").text
    assert 'type="password" name="password" id="pw"' in html


@requires_corpus
@pytest.mark.parametrize(
    "expected", ["current-password", "new-password"]
)
def test_autocomplete_survives_the_change(client, expected):
    """A password manager keys off this, and losing it is a real regression."""
    path = "/signin" if expected == "current-password" else "/signup"
    assert f'autocomplete="{expected}"' in client.get(path).text

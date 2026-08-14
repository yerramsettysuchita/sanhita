"""Whose screen is this? The masthead has to answer that correctly.

The product serves two people. A compliance officer at an intermediary walks
the five company stages asking whether their own firm is complying. A
regulatory analyst works on the rulebook itself. The masthead said

    DOCUMENT
    Stock Brokers Master Circular

on both, so the firm's own compliance screen introduced itself as a circular,
and the whole company journey read as something happening inside a document.

The rulebook is still named on the firm's screens, because an assessment is
against a specific framework and hiding that would be worse. It is named as
the framework, second, after the firm.
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


def _masthead(html: str) -> str:
    return html[html.index('class="masthead"') : html.index("</header>")]


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


COMPANY_PAGES = [
    "/w/demo/company",
    "/w/demo/review",
    "/w/demo/gaps",
    "/w/demo/remediation",
    "/w/demo/audit",
]


def _set_up(client, name="ABC Securities Pvt Ltd"):
    client.post(
        "/w/demo/company/save",
        data={"name": name, "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    # Step three. Without it the firm exists but has not finished onboarding,
    # and every route that records something for it now refuses.
    client.post("/w/demo/setup/complete", follow_redirects=True)


# ------------------------------------------------- the firm's own screens


@requires_corpus
def test_every_company_screen_leads_with_the_firm(client):
    _set_up(client)

    for path in COMPANY_PAGES:
        head = _plain(_masthead(client.get(path).text))
        assert "Firm ABC Securities Pvt Ltd" in head, f"{path} does not name the firm"


@requires_corpus
def test_no_company_screen_calls_the_rulebook_the_document(client):
    _set_up(client)

    for path in COMPANY_PAGES:
        head = _plain(_masthead(client.get(path).text))
        assert "Document" not in head, (
            f"{path} still presents the regulation as this screen's subject"
        )


@requires_corpus
def test_the_framework_is_still_named_on_every_company_screen(client):
    """An assessment is against one framework. Hiding that would be worse."""
    _set_up(client)

    for path in COMPANY_PAGES:
        head = _plain(_masthead(client.get(path).text))
        assert "Framework Stock Brokers Master Circular" in head, path


@requires_corpus
def test_the_chain_view_is_a_company_screen_too(client, tmp_path):
    """It renders under remediation, so it must inherit the same context."""
    from sanhita.remediate import RemediationStore

    _set_up(client)
    # A task needs a finding of record, which needs records and an assessment.
    from sanhita.cli_compile import _load_registry as _lr
    from sanhita.ir.enums import DeadlineKind as _DK
    from sanhita.ir.enums import RuleStatus as _RS

    recurring = next(
        r
        for r in _lr(tmp_path / "rules.json").all_current()
        if r.status is _RS.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is _DK.END_OF_PERIOD
        and r.evidence
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{recurring.id},ABC,2026-01-31,,{recurring.evidence[0].artifact_type},\n"
        ).encode(),
    )
    client.post("/w/demo/assess", follow_redirects=True)
    import re as _re

    gaps = client.get("/w/demo/gaps").text
    form = _re.search(
        rf'name="obligation_id" value="{_re.escape(recurring.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    assert form, "no finding of record to raise a task against"
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": recurring.id,
            "gap_id": form.group(1),
            "clause_id": form.group(2),
            "priority": "HIGH",
            "by": "S. Officer",
        },
        follow_redirects=True,
    )
    task_id = next(iter(RemediationStore.load(tmp_path / "remediation.json").tasks))

    head = _plain(_masthead(client.get(f"/w/demo/chain/{task_id}").text))
    assert "Firm ABC Securities Pvt Ltd" in head
    assert "Document" not in head


@requires_corpus
def test_the_firm_name_comes_from_the_saved_profile(client):
    """Not hardcoded anywhere. Rename the firm and the masthead follows."""
    _set_up(client, name="Zeta Broking Services")

    head = _plain(_masthead(client.get("/w/demo/gaps").text))
    assert "Zeta Broking Services" in head
    assert "ABC Securities" not in head


@requires_corpus
def test_before_a_firm_exists_nothing_pretends_one_does(client):
    """During setup there is no firm to name, so the masthead does not invent one."""
    head = _masthead(client.get("/w/demo/company").text)

    assert "firmpick" not in head
    assert "Framework" in _plain(head), (
        "the rulebook should still be named while setting up"
    )


# ------------------------------------------------ the regulatory screens


@requires_corpus
def test_the_rulebook_screens_still_speak_about_a_document(client):
    """Those pages really are about a regulatory document, so they say so."""
    _set_up(client)

    for path in ("/w/demo", "/w/demo/queue", "/w/demo/coverage", "/w/demo/conflicts"):
        head = _plain(_masthead(client.get(path).text))
        assert "Document Stock Brokers Master Circular" in head, path
        assert "Firm ABC" not in head, f"{path} claims to be about the firm"


@requires_corpus
def test_the_rulebook_list_still_works(client):
    assert client.get("/documents").status_code == 200
    assert "SEBI rulebooks" in _plain(client.get("/documents").text)


@requires_corpus
def test_switching_rulebooks_still_works_from_a_company_screen(client):
    """The switcher was relabelled, not removed."""
    _set_up(client)
    body = client.get("/w/demo/company").text
    head = _masthead(body)

    assert 'class="docpick-menu"' in head, "the switcher is gone"
    assert 'href="/w/demo"' in head
    assert 'href="/documents"' in head, "no way left to reach another rulebook"

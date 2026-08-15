"""One person, several firms, and never a record under the wrong name.

A visitor could record one firm and only one. Every sidecar a firm owns is
keyed by the visitor and nothing else, so a compliance officer who advises two
brokers had to sign out and use a different browser to look at the second, and
there was no screen anywhere listing what they had recorded.

The fix threads a company slot through the scope every sidecar already reads,
which is a small change in one place and a large change in what it means. It is
also the most dangerous change this product can make, because seven files move
at once:

    company.json  evidence.json  assessments.json  remediation.json
    controls.json  review.json  plans.json

Getting it wrong does not raise an error. It shows one broker's filing register
under another broker's name, on a screen headed with a compliance position, and
nothing about the page looks wrong. That is the failure these tests exist to
make impossible, so they are written before the change rather than after it.

**The first company keeps the unscoped name.** A visitor's existing files are
company one, untouched, and later companies take a suffix. Nobody has to
migrate anything, and a deployment that already holds a firm keeps it.
"""

from __future__ import annotations

import re
import shutil

import pytest

from tests.conftest import requires_corpus

FIRST = "Meridian Capital Services Pvt Ltd"
SECOND = "Ashwini Broking Services Pvt Ltd"
THIRD = "Trident Stock Broking Pvt Ltd"


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    """A shared deployment, which is where scoping happens at all."""
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    monkeypatch.setenv("SANHITA_SHARED", "1")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    client.post(
        "/signup",
        data={
            "name": "R Suchita, Compliance Officer",
            "email": "officer@example.invalid",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    return client


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def add_company(client, name: str):
    """Record a firm and declare the rulebook that governs it."""
    client.post("/w/demo/companies/new", follow_redirects=True)
    client.post(
        "/w/demo/company/save",
        data={"name": name, "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    return client.post("/w/demo/setup/complete", follow_redirects=True)


def file_a_record(client, entity: str, rule_id: str, due: str, filed: str = ""):
    return client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule_id},{entity},{due},{filed},AUDIT_REPORT,REF-1\n"
        ).encode(),
    )


# ------------------------------------------------- recording more than one


@requires_corpus
def test_a_second_company_can_be_recorded(client):
    """The defect, in one assertion."""
    add_company(client, FIRST)
    add_company(client, SECOND)

    page = _plain(client.get("/w/demo/companies").text)
    assert FIRST in page
    assert SECOND in page


@requires_corpus
def test_the_companies_screen_offers_to_add_one(client):
    add_company(client, FIRST)
    html = client.get("/w/demo/companies").text

    assert "Add a new company" in _plain(html)
    assert "/companies/new" in html, "the call to action leads nowhere"


@requires_corpus
def test_adding_one_opens_step_one_rather_than_the_last_firm(client):
    add_company(client, FIRST)
    page = _plain(client.post("/w/demo/companies/new", follow_redirects=True).text)

    assert "Step 1 of 3" in page
    assert FIRST not in page, "the new company opened on the previous one's profile"


# ------------------------------------------------------- switching between


@requires_corpus
def test_switching_shows_the_other_firm(client):
    add_company(client, FIRST)
    add_company(client, SECOND)

    # The second is current, having just been added.
    assert SECOND in _plain(client.get("/w/demo/company").text)

    first_id = _company_ids(client)[0]
    client.post(f"/w/demo/companies/{first_id}/open", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)
    assert FIRST in page
    assert SECOND not in page


def _company_ids(client) -> list[str]:
    html = client.get("/w/demo/companies").text
    return re.findall(r"/companies/([A-Za-z0-9_-]+)/open", html)


# ------------------------------------- the failure that must be impossible


@requires_corpus
def test_one_firms_records_never_appear_under_another(client, tmp_path):
    """The whole reason these tests were written before the change.

    Two firms, one record each, filed against the same duty on different dates.
    If the scope is threaded wrongly, the second firm's screen shows the first
    firm's filing and nothing looks broken.
    """
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    rule = next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )

    add_company(client, FIRST)
    file_a_record(client, FIRST, rule.id, "2026-03-31", "2026-03-31")

    add_company(client, SECOND)
    file_a_record(client, SECOND, rule.id, "2026-06-30", "2026-06-30")

    second = _plain(client.get("/w/demo/review").text)
    assert "2026-06-30" in second or SECOND in second
    assert "2026-03-31" not in second, (
        "the second firm's screen is showing the first firm's filing record"
    )


@requires_corpus
def test_each_company_keeps_its_own_files_on_disk(client, tmp_path):
    """Belt and braces, at the filesystem rather than the screen."""
    add_company(client, FIRST)
    add_company(client, SECOND)
    add_company(client, THIRD)

    profiles = sorted(p.name for p in tmp_path.glob("company*.json"))
    assert len(profiles) == 3, f"expected three profiles, found {profiles}"


@requires_corpus
def test_an_assessment_belongs_to_the_company_it_was_run_for(client, tmp_path):
    """An assessment history that leaks is a compliance record against the
    wrong firm, which is worse than having no history."""
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    rule = next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )

    add_company(client, FIRST)
    file_a_record(client, FIRST, rule.id, "2026-03-31", "2026-03-31")
    client.post("/w/demo/assess", follow_redirects=True)

    histories = sorted(p.name for p in tmp_path.glob("assessments*.json"))
    assert len(histories) == 1, f"expected one history, found {histories}"

    add_company(client, SECOND)
    assert SECOND in _plain(client.get("/w/demo/company").text)

    # Asserted on disk rather than by reading the screen for a phrase. The
    # question is whether the second firm has a history at all, and the file
    # either exists or it does not.
    after = sorted(p.name for p in tmp_path.glob("assessments*.json"))
    assert after == histories, (
        f"a newly added firm gained an assessment history: {after}"
    )

    # And the run that did happen still belongs to the firm it was run for.
    client.post("/w/demo/companies/first/open", follow_redirects=True)
    assert FIRST in _plain(client.get("/w/demo/company").text)


# ----------------------------------------- nothing that worked stops working


@requires_corpus
def test_a_visitor_with_one_company_sees_no_change(client):
    """The first company keeps the unscoped filename, so a deployment that
    already holds a firm keeps it without migrating anything."""
    add_company(client, FIRST)
    page = _plain(client.get("/w/demo/company").text)

    assert FIRST in page
    assert "Step 1 of 3" not in page


@requires_corpus
def test_the_first_company_uses_the_original_filename(client, tmp_path):
    """The migration that is not needed, asserted rather than assumed."""
    add_company(client, FIRST)

    unsuffixed = [
        p.name
        for p in tmp_path.glob("company.*.json")
        if p.name.count(".") == 2  # company.<visitor>.json
    ]
    assert unsuffixed, (
        "the first company took a suffixed name, so existing deployments would "
        "lose the firm they already hold"
    )

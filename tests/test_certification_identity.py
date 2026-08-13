"""Who certified this rule, and what does the signature actually prove?

The product's whole claim is that a named human certified an interpretation
once, and that everything afterwards runs deterministically against what they
signed. The name was a text box. Anybody could type "S. Iyer, Chief Compliance
Officer" and the ledger would carry it forever with nothing behind it.

Worse, the signature never covered the name. A signature cannot cover the bytes
containing it, so ``certified_by`` sits outside the signed payload by design.
What the signature proves is that the rule's content has not changed since it
was signed. It proves nothing about who signed it.

Two fixes, and they are different in kind. The acting officer now comes from
the session, so the recorded name is one somebody authenticated as. And the
screen says what the signature does and does not cover, because a hex string
beside a person's name invites the reading that their key produced it.

What this does **not** claim is a per-officer key. That is a real change to the
trust model and the product must not imply it has one.
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

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _sign_in(client, name="S. Iyer"):
    return client.post(
        "/signup",
        data={
            "name": name,
            "email": f"{name.lower().replace(' ', '.').replace('.', '')}@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )


def _proposed(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    return next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.PROPOSED and not r.blocking_issues()
    )


# --------------------------------------------- the acting officer is the account


@requires_corpus
def test_an_anonymous_visitor_cannot_certify_anything(client, tmp_path):
    """The defect, in one assertion. A typed name is a record of nobody."""
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    rule = _proposed(tmp_path)

    response = client.post(
        f"/w/demo/clause/{rule.source.clause_id}/certify",
        data={"obligation_id": rule.id, "by": "S. Iyer, Chief Compliance Officer"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "need an account" in response.text
    after = _load_registry(tmp_path / "rules.json").current(rule.id)
    assert after.status is not RuleStatus.CERTIFIED


@requires_corpus
def test_the_recorded_officer_is_the_account_not_the_form(client, tmp_path):
    """Posting somebody else's name records the account that was signed in."""
    from sanhita.cli_compile import _load_registry

    _sign_in(client, name="R. Nair")
    rule = _proposed(tmp_path)

    response = client.post(
        f"/w/demo/clause/{rule.source.clause_id}/certify",
        data={"obligation_id": rule.id, "by": "Somebody Else Entirely"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    certified = _load_registry(tmp_path / "rules.json").current(rule.id)
    assert certified.certification.certified_by == "R. Nair"
    assert "Somebody Else" not in certified.certification.certified_by


@requires_corpus
def test_rejecting_and_amending_carry_the_same_rule(client, tmp_path):
    """They are ledger entries too, and a ledger of typed names proves nothing."""
    rule = _proposed(tmp_path)
    clause = rule.source.clause_id

    for path, data in (
        (f"/w/demo/clause/{clause}/reject", {"obligation_id": rule.id, "reason": "no duty"}),
        (
            f"/w/demo/clause/{clause}/edit",
            {
                "obligation_id": rule.id,
                "verb": rule.action.verb,
                "object": rule.action.object + " amended",
            },
        ),
    ):
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 401, f"{path} accepted an anonymous act"


@requires_corpus
def test_a_signed_in_officer_can_still_do_all_of_it(client, tmp_path):
    """The gate has to let the real path through."""
    from sanhita.cli_compile import _load_registry

    _sign_in(client, name="R. Nair")
    rule = _proposed(tmp_path)

    amended = client.post(
        f"/w/demo/clause/{rule.source.clause_id}/edit",
        data={
            "obligation_id": rule.id,
            "verb": rule.action.verb,
            "object": rule.action.object + " and the annexure",
        },
        follow_redirects=False,
    )
    assert amended.status_code == 303, amended.text

    ledger = _load_registry(tmp_path / "rules.json").ledger
    amendments = [e for e in ledger if e.obligation_id == rule.id]
    assert amendments and amendments[-1].actor == "R. Nair"


@requires_corpus
def test_the_ledger_never_records_an_unattributed_lifecycle_act(client, tmp_path):
    """"unknown" used to be the fallback, and it was reachable from a browser."""
    from sanhita.cli_compile import _load_registry

    _sign_in(client, name="R. Nair")
    rule = _proposed(tmp_path)
    client.post(
        f"/w/demo/clause/{rule.source.clause_id}/certify",
        data={"obligation_id": rule.id, "by": ""},
        follow_redirects=True,
    )

    actors = {e.actor for e in _load_registry(tmp_path / "rules.json").ledger}
    assert "unknown" not in actors
    assert "unattributed" not in actors


# ------------------------------------------------- and the screen says so


@requires_corpus
def test_the_screen_names_who_the_certification_will_be_recorded_against(
    client, tmp_path
):
    _sign_in(client, name="R. Nair")
    rule = _proposed(tmp_path)

    page = _plain(client.get(f"/w/demo/clause/{rule.source.clause_id}").text)

    assert "Recorded against R. Nair" in page


@requires_corpus
def test_an_anonymous_visitor_is_told_why_the_button_is_off(client, tmp_path):
    rule = _proposed(tmp_path)
    body = client.get(f"/w/demo/clause/{rule.source.clause_id}").text

    assert "acts by a named person, so they need an account" in _plain(body)
    assert 'href="/signin"' in body
    # And the control is actually disabled rather than merely explained.
    form = body[body.index("act-certify") :]
    assert "disabled" in form[: form.index("</form>")]


@requires_corpus
def test_the_screen_does_not_let_the_signature_imply_a_personal_key(
    client, tmp_path
):
    """One key belongs to this deployment, not to the officer. Say so."""
    _sign_in(client, name="R. Nair")
    rule = _proposed(tmp_path)
    client.post(
        f"/w/demo/clause/{rule.source.clause_id}/certify",
        data={"obligation_id": rule.id},
        follow_redirects=True,
    )

    page = _plain(client.get(f"/w/demo/clause/{rule.source.clause_id}").text)

    assert "under this deployment's key" in page
    assert "It does not cover the name above" in page


@requires_corpus
def test_the_free_text_officer_box_is_gone_from_every_lifecycle_form(client, tmp_path):
    """Leaving the box would teach people the name is theirs to choose."""
    _sign_in(client, name="R. Nair")
    rule = _proposed(tmp_path)
    body = client.get(f"/w/demo/clause/{rule.source.clause_id}").text

    for placeholder in ('placeholder="certifying officer"', 'placeholder="your name"'):
        assert placeholder not in body, f"{placeholder} is still on the workbench"

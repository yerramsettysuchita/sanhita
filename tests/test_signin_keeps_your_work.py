"""Signing in must not lose the work you did before you signed in.

On a shared deployment a firm's own data is kept per visitor, so one person's
filing register is never served to the next. Anonymous visitors are keyed to an
opaque cookie; signed-in ones to their account.

That was correct and it had a hole in the middle of it. A visitor could walk the
whole journey anonymously, create a firm, upload a register, run an assessment
and raise a task, then sign up, and the scope would change from the cookie token
to the account with nothing moving the files.

Nobody lost data on disk, which is exactly why it survived: anybody inspecting
afterwards finds the anonymous files intact and concludes it worked. The user,
who cannot see the disk, watched their company disappear at the moment they
committed to the product.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import requires_corpus


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    """A shared deployment, which is the only place scoping happens at all."""
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    monkeypatch.setenv("SANHITA_SHARED", "1")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _recurring(tmp_path):
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import DeadlineKind, RuleStatus

    return next(
        r
        for r in _load_registry(tmp_path / "rules.json").all_current()
        if r.status is RuleStatus.CERTIFIED
        and r.deadline is not None
        and r.deadline.kind is DeadlineKind.END_OF_PERIOD
        and r.evidence
    )


def _walk_anonymously(client, tmp_path, firm="ABC Securities Pvt Ltd"):
    """Everything a visitor can do without an account.

    Which is now onboarding and document intake, and stops there. Running an
    assessment, mapping a record to a requirement and raising a task are
    official compliance actions: they record who did them and refuse an
    anonymous caller. So this is exactly the work that would be lost if signing
    up did not carry it across, which is what makes it worth testing.
    """
    rule = _recurring(tmp_path)
    client.post(
        "/w/demo/company/save",
        data={"name": firm, "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},{firm},2026-01-31,,{rule.evidence[0].artifact_type},REC-001\n"
        ).encode(),
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    return rule


def _sign_up(client, email="officer@example.com", name="A Named Officer"):
    return client.post(
        "/signup",
        data={"name": name, "email": email, "password": "a-long-enough-password"},
        follow_redirects=True,
    )


# --------------------------------------------------- the work comes with you


@requires_corpus
def test_the_firm_survives_signing_up(client, tmp_path):
    """The defect, in one assertion."""
    _walk_anonymously(client, tmp_path)
    assert "ABC Securities Pvt Ltd" in _plain(client.get("/w/demo/company").text)

    _sign_up(client)

    page = _plain(client.get("/w/demo/company").text)
    assert "ABC Securities Pvt Ltd" in page, (
        "the firm vanished at the moment the visitor committed to the product"
    )


@requires_corpus
def test_the_records_survive_signing_up(client, tmp_path):
    _walk_anonymously(client, tmp_path)
    _sign_up(client)

    page = _plain(client.get("/w/demo/review").text)
    assert "Are the records still arriving?" in page
    assert "Records read" in page or "Documents read" in page


@requires_corpus
def test_the_carried_records_are_what_the_first_assessment_runs_against(client, tmp_path):
    """The anonymous upload has to still be there, or the assessment is of nothing.

    An assessment cannot be run anonymously, so what is carried across is the
    evidence, and the proof it arrived intact is that the first assessment the
    new account runs produces a position at all.
    """
    _walk_anonymously(client, tmp_path)
    _sign_up(client)
    client.post("/w/demo/assess", follow_redirects=True)

    page = _plain(client.get("/w/demo/company").text)
    assert "Assessment record" in page
    assert re.search(r"\d+% Of what could be determined", page), "the carried evidence was not there"


@requires_corpus
def test_the_assessment_history_survives_signing_out_and_back_in(client, tmp_path):
    """An assessment is the firm's own record of where it stood."""
    _walk_anonymously(client, tmp_path)
    _sign_up(client)
    client.post("/w/demo/assess", follow_redirects=True)
    client.post("/signout", follow_redirects=True)
    client.post(
        "/signin",
        data={"email": "officer@example.com", "password": "a-long-enough-password"},
        follow_redirects=True,
    )

    page = _plain(client.get("/w/demo/company").text)
    assert "Assessment record" in page
    assert re.search(r"\d+% Of what could be determined", page), "the recorded position was lost"


@requires_corpus
def test_remediation_survives_signing_out_and_back_in(client, tmp_path):
    from sanhita.remediate import RemediationStore

    rule = _walk_anonymously(client, tmp_path)
    _sign_up(client)
    client.post("/w/demo/assess", follow_redirects=True)
    gaps = client.get("/w/demo/gaps").text
    form = re.search(
        rf'name="obligation_id" value="{re.escape(rule.id)}">\s*'
        r'<input type="hidden" name="gap_id" value="([^"]+)">\s*'
        r'<input type="hidden" name="clause_id" value="([^"]+)"',
        gaps,
    )
    assert form, "no finding of record to raise a task against"
    client.post(
        "/w/demo/remediation/open",
        data={
            "obligation_id": rule.id,
            "gap_id": form.group(1),
            "clause_id": form.group(2),
            "priority": "HIGH",
        },
        follow_redirects=True,
    )
    client.post("/signout", follow_redirects=True)
    client.post(
        "/signin",
        data={"email": "officer@example.com", "password": "a-long-enough-password"},
        follow_redirects=True,
    )

    # The task itself, not merely a screen that renders.
    scoped = sorted(tmp_path.glob("remediation.u*.json"))
    assert scoped, "the tasks are not under the account"
    assert RemediationStore.load(scoped[0]).tasks


@requires_corpus
def test_the_anonymous_files_are_moved_rather_than_copied(client, tmp_path):
    """Two copies of one firm's history is two firms as far as a reader knows.

    A visitor scope is bare hex and an account scope is ``u`` followed by an
    id, so "exactly one scoped copy, and its scope begins with u" is the whole
    assertion. Globbing for a ``visitor`` prefix would pass whatever happened.
    """
    _walk_anonymously(client, tmp_path)
    _sign_up(client)

    for name in ("company", "evidence", "review"):
        scoped = sorted(p.name for p in tmp_path.glob(f"{name}.*.json"))
        assert len(scoped) == 1, f"{name} was copied rather than moved: {scoped}"
        assert scoped[0].split(".")[1].startswith("u"), (
            f"{scoped[0]} is still under the anonymous scope"
        )


# ------------------------------------------------- and it stays yours alone


@requires_corpus
def test_another_visitor_still_cannot_see_it(client, corpus_pdf, tmp_path):
    """The whole reason scoping exists must survive the fix."""
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    _walk_anonymously(client, tmp_path)
    _sign_up(client)

    stranger = TestClient(create_app(corpus_pdf, store=tmp_path / "rules.json"))
    page = _plain(stranger.get("/w/demo/company").text)

    assert "ABC Securities Pvt Ltd" not in page


@requires_corpus
def test_signing_out_and_back_in_keeps_it(client, tmp_path):
    _walk_anonymously(client, tmp_path)
    _sign_up(client)
    client.post("/signout", follow_redirects=True)
    client.post(
        "/signin",
        data={"email": "officer@example.com", "password": "a-long-enough-password"},
        follow_redirects=True,
    )

    assert "ABC Securities Pvt Ltd" in _plain(client.get("/w/demo/company").text)


# ------------------------------------------- two histories are never merged


@requires_corpus
def test_an_account_that_already_has_a_firm_keeps_it(client, corpus_pdf, tmp_path):
    """Interleaving two compliance histories makes every earlier assessment a
    statement about a set of records that never existed."""
    from fastapi.testclient import TestClient

    from sanhita.company import Company
    from sanhita.web.app import create_app

    # An account with its own firm.
    _sign_up(client, email="owner@example.com", name="The Owner")
    client.post(
        "/w/demo/company/save",
        data={"name": "Zeta Broking Services", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post("/signout", follow_redirects=True)

    # A different browser walks anonymously, then signs in to that same account.
    other = TestClient(create_app(corpus_pdf, store=tmp_path / "rules.json"))
    _walk_anonymously(other, tmp_path, firm="Impostor Securities")
    other.post(
        "/signin",
        data={"email": "owner@example.com", "password": "a-long-enough-password"},
        follow_redirects=True,
    )

    page = _plain(other.get("/w/demo/company").text)
    assert "Zeta Broking Services" in page
    assert "Impostor Securities" not in page, "an anonymous firm overwrote an account's"

    scoped = sorted(tmp_path.glob("company.u*.json"))
    assert len(scoped) == 1
    assert Company.load(scoped[0]).name == "Zeta Broking Services"


@requires_corpus
def test_a_collision_is_said_out_loud_rather_than_swallowed(client, corpus_pdf, tmp_path):
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    _sign_up(client, email="owner@example.com", name="The Owner")
    client.post(
        "/w/demo/company/save",
        data={"name": "Zeta Broking Services"},
        follow_redirects=True,
    )
    client.post("/signout", follow_redirects=True)

    other = TestClient(create_app(corpus_pdf, store=tmp_path / "rules.json"))
    _walk_anonymously(other, tmp_path, firm="Impostor Securities")
    landed = other.post(
        "/signin",
        data={"email": "owner@example.com", "password": "a-long-enough-password"},
        follow_redirects=False,
    )

    assert landed.status_code == 303
    assert "carried=" in landed.headers["location"], (
        "the visitor was not told their anonymous work stayed behind"
    )


# ------------------------------------------------------- the plain machinery


def test_the_migration_refuses_to_run_without_two_different_scopes(tmp_path):
    from sanhita.web.adopt import adopt_visitor_data

    for visitor, user in (("", "u1"), ("v1", ""), ("u1", "u1")):
        result = adopt_visitor_data(
            roots=[tmp_path], visitor_scope=visitor, user_scope=user
        )
        assert not result.moved
        assert not result.kept_back


def test_every_sidecar_the_app_scopes_is_on_the_migration_list():
    """A sidecar added to the app and forgotten here is data lost on sign-in."""
    import inspect
    import re as _re

    from sanhita.web import app as app_module
    from sanhita.web.adopt import SIDECARS

    source = inspect.getsource(app_module)
    used = set(_re.findall(r'_sidecar\(state, "([a-z_]+\.json)"\)', source))
    used.add("company.json")  # built by _company_path, not by _sidecar

    missing = used - set(SIDECARS)
    assert not missing, f"these sidecars would be lost when a visitor signs in: {missing}"

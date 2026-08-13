"""On a public URL, one visitor's filing register must not reach the next one.

The worked example is a single workspace that everybody lands on, and the
journey it demonstrates ends with a firm uploading its own compliance records.
On a laptop that is fine. Deployed, without scoping, the second visitor would
open the gaps screen and read the first visitor's evidence, with their firm's
name on it. That is a privacy breach dressed as a demo.

`SANHITA_SHARED=1` turns on per visitor scoping of the firm's own data. The
rulebook is never scoped: it is the regulator's text and is the same document
for everybody.
"""

from __future__ import annotations

import pytest

from tests.conftest import requires_corpus


@pytest.fixture()
def app_factory(corpus_pdf, tmp_path, monkeypatch):
    """Two independent browsers against one shared instance."""
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    app = create_app(corpus_pdf, store=store)

    def browser():
        return TestClient(app)

    return browser


def _certified_id(tmp_path) -> str:
    from sanhita.cli_compile import _load_registry
    from sanhita.ir.enums import RuleStatus

    for rule in _load_registry(tmp_path / "rules.json").all_current():
        if rule.status is RuleStatus.CERTIFIED:
            return rule.id
    raise AssertionError("no certified rule in the store")


def _register(rule_id: str, entity: str) -> bytes:
    return (
        "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
        f"{rule_id},{entity},2026-03-31,2026-04-02,report,RET-001\n"
    ).encode()


@requires_corpus
def test_two_visitors_do_not_see_each_others_evidence(
    app_factory, tmp_path, monkeypatch
):
    monkeypatch.setenv("SANHITA_SHARED", "1")
    rule_id = _certified_id(tmp_path)

    first = app_factory()
    first.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=_register(rule_id, "Confidential Broking Pvt Ltd"),
    )
    assert "No assessment has been run" not in first.get("/w/demo/gaps").text

    second = app_factory()

    assert "No assessment has been run" in second.get("/w/demo/gaps").text, (
        "one visitor's filing register was assessed for another"
    )
    # And on disk it is not even in a file the second visitor could name.
    assert not (tmp_path / "evidence.json").exists()
    assert list(tmp_path.glob("evidence.*.json")), "nothing was written anywhere"


@requires_corpus
def test_a_visitor_keeps_their_own_evidence_across_requests(
    app_factory, tmp_path, monkeypatch
):
    """Scoping must isolate people, not lose their work."""
    monkeypatch.setenv("SANHITA_SHARED", "1")
    rule_id = _certified_id(tmp_path)

    browser = app_factory()
    browser.get("/w/demo/company")  # picks up the handle
    browser.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=_register(rule_id, "ABC Securities"),
    )

    for _ in range(3):
        assert "No assessment has been run" not in browser.get("/w/demo/gaps").text
    # One handle, one file. A new one on every request would isolate a visitor
    # from themselves.
    assert len(list(tmp_path.glob("evidence.*.json"))) == 1


@requires_corpus
def test_the_handle_is_opaque_and_carries_nothing(app_factory, monkeypatch):
    from sanhita.web.app import VISITOR_COOKIE

    monkeypatch.setenv("SANHITA_SHARED", "1")
    browser = app_factory()
    browser.get("/w/demo/company")

    token = browser.cookies.get(VISITOR_COOKIE)
    assert token, "no handle was issued on a shared deployment"
    assert len(token) == 16 and all(c in "0123456789abcdef" for c in token)


@requires_corpus
def test_the_rulebook_is_shared_because_it_is_the_regulators(
    app_factory, monkeypatch
):
    """Scoping the firm's data must not fork the regulation itself."""
    monkeypatch.setenv("SANHITA_SHARED", "1")

    first = app_factory().get("/w/demo/coverage").text
    second = app_factory().get("/w/demo/coverage").text

    assert "certified" in first.lower()
    assert first == second, "two visitors saw different rulebooks"


@requires_corpus
def test_nothing_is_scoped_on_a_single_user_install(app_factory, tmp_path, monkeypatch):
    """A laptop should have plain filenames and no cookie at all."""
    from sanhita.web.app import VISITOR_COOKIE

    monkeypatch.delenv("SANHITA_SHARED", raising=False)
    rule_id = _certified_id(tmp_path)

    browser = app_factory()
    browser.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=_register(rule_id, "ABC Securities"),
    )

    assert browser.cookies.get(VISITOR_COOKIE) is None
    assert (tmp_path / "evidence.json").is_file()
    assert not list(tmp_path.glob("evidence.*.json"))
    # And a second window is the same person, because it is the same machine.
    assert "No assessment has been run" not in app_factory().get("/w/demo/gaps").text

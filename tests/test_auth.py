"""Accounts and sessions.

Compliance work is confidential, so this is a real boundary rather than a
decoration. The things that must hold:

  A password is never stored, only a salted scrypt hash.
  A cookie proves nothing unless it verifies, and there is no partial trust.
  Session cookies are not signed with the certification key.
  Signing in reveals nothing about which addresses have accounts.
"""

from __future__ import annotations

import os

import pytest

from sanhita.auth import AuthError, UserStore
from sanhita.auth import session as sess
from tests.conftest import requires_corpus


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.json")


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-signing-key")
    return "a-test-signing-key"


# ------------------------------------------------------------------ accounts


def test_a_password_is_never_stored(store, tmp_path):
    store.create(email="a@example.com", name="A Person", password="correct horse battery")
    on_disk = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert "correct horse battery" not in on_disk
    assert "password_hash" in on_disk


def test_the_same_password_hashes_differently_for_two_people(store):
    a = store.create(email="a@example.com", name="A", password="correct horse battery")
    b = store.create(email="b@example.com", name="B", password="correct horse battery")
    assert a.password_hash != b.password_hash, "salts are not per user"
    assert a.salt != b.salt


def test_the_right_password_authenticates(store):
    store.create(email="a@example.com", name="A Person", password="correct horse battery")
    user = store.authenticate("a@example.com", "correct horse battery")
    assert user.name == "A Person"


def test_the_wrong_password_does_not(store):
    store.create(email="a@example.com", name="A", password="correct horse battery")
    with pytest.raises(AuthError):
        store.authenticate("a@example.com", "correct horse batteries")


def test_a_failed_sign_in_does_not_reveal_whether_the_account_exists(store):
    """Otherwise the form becomes a way to enumerate a firm's staff."""
    store.create(email="a@example.com", name="A", password="correct horse battery")

    with pytest.raises(AuthError) as known:
        store.authenticate("a@example.com", "wrong password here")
    with pytest.raises(AuthError) as unknown:
        store.authenticate("nobody@example.com", "wrong password here")

    assert str(known.value) == str(unknown.value)


def test_email_is_matched_case_insensitively(store):
    store.create(email="A.Person@Example.COM", name="A", password="correct horse battery")
    assert store.authenticate("a.person@example.com", "correct horse battery")


def test_a_duplicate_account_is_refused(store):
    store.create(email="a@example.com", name="A", password="correct horse battery")
    with pytest.raises(AuthError) as exc:
        store.create(email="a@example.com", name="A again", password="another passphrase")
    assert "already" in str(exc.value)


def test_a_short_password_is_refused_with_a_reason(store):
    with pytest.raises(AuthError) as exc:
        store.create(email="a@example.com", name="A", password="short")
    assert "10" in str(exc.value)


def test_a_name_is_required_because_it_appears_on_certifications(store):
    with pytest.raises(AuthError) as exc:
        store.create(email="a@example.com", name="   ", password="correct horse battery")
    assert "certify" in str(exc.value)


def test_accounts_survive_a_restart(store, tmp_path):
    store.create(email="a@example.com", name="A", password="correct horse battery")
    reopened = UserStore(tmp_path / "users.json")
    assert reopened.authenticate("a@example.com", "correct horse battery")


# ------------------------------------------------------------------ sessions


def test_a_cookie_round_trips(key):
    cookie = sess.issue("user-123")
    assert sess.read(cookie) == "user-123"


def test_a_tampered_cookie_proves_nothing(key):
    cookie = sess.issue("user-123")
    forged = cookie.replace("user-123", "user-999")
    assert sess.read(forged) is None


def test_a_cookie_signed_with_another_key_is_rejected(monkeypatch):
    monkeypatch.setenv("SANHITA_SIGNING_KEY", "key-one")
    cookie = sess.issue("user-123")
    monkeypatch.setenv("SANHITA_SIGNING_KEY", "key-two")
    assert sess.read(cookie) is None


def test_an_expired_cookie_is_rejected(key):
    cookie = sess.issue("user-123", now=0)
    assert sess.read(cookie, now=sess.MAX_AGE_SECONDS + 1) is None
    assert sess.read(cookie, now=sess.MAX_AGE_SECONDS - 1) == "user-123"


def test_a_cookie_from_the_future_is_rejected(key):
    """Clock skew is tolerated, a forged future issue time is not."""
    cookie = sess.issue("user-123", now=10_000)
    assert sess.read(cookie, now=0) is None


@pytest.mark.parametrize("junk", ["", "nonsense", "a.b", "a.b.c.d", "..", "x.y.z"])
def test_malformed_cookies_prove_nothing(key, junk):
    assert sess.read(junk) is None


def test_the_session_key_is_not_the_certification_key(key):
    """A weakness in one must not become forged signatures on the other."""
    assert sess.session_key() != key.encode("utf-8")
    assert sess.session_key() is not None


def test_without_a_signing_key_no_session_can_be_issued(monkeypatch):
    monkeypatch.delenv("SANHITA_SIGNING_KEY", raising=False)
    assert sess.session_key() is None
    assert sess.issue("user-123") is None
    assert sess.read("anything.at.all") is None


# -------------------------------------------------------------------- routes


@pytest.fixture(scope="module")
def client(corpus_pdf):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sanhita.web.app import create_app

    os.environ.setdefault("SANHITA_SIGNING_KEY", "test-workbench-key")
    return fastapi_testclient.TestClient(create_app(corpus_pdf))


@requires_corpus
def test_the_sign_in_screen_renders(client):
    body = client.get("/signin").text
    assert 'type="password"' in body
    assert "Create one" in body


@requires_corpus
def test_the_workbench_is_open_until_someone_creates_an_account(client):
    """A first run must not demand a sign-up before anything can be seen."""
    assert client.get("/queue").status_code == 200
    assert client.get("/documents").status_code == 200


@requires_corpus
def test_signing_in_with_a_bad_password_returns_to_the_form(client):
    response = client.post(
        "/signin",
        data={"email": "nobody@example.com", "password": "wrong password here"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/signin?error=" in response.headers["location"]


@requires_corpus
def test_the_sign_in_screen_loads_nothing_from_the_network(client):
    import re

    body = client.get("/signin").text
    for attr in re.findall(r'(?:src|href)="([^"]+)"', body):
        assert not attr.startswith(("http://", "https://", "//"))

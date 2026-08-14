"""The guarantees added when this stopped being a laptop-only prototype.

Each of these was a real defect, not a hypothetical:

  A cache that never evicts grows with every document anyone opens.
  Two writers that both read and both write lose one officer's signature.
  A page that can be framed can have its certify button clicked by someone else.
  An unthrottled sign-in is a password guesser's front door.
  A raw JSON error is a machine talking to a person.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from tests.conftest import requires_corpus

# ------------------------------------------------------------- store locking


def test_two_writers_do_not_lose_each_others_work(tmp_path):
    """The failure that reports nothing: last write wins, silently."""
    from sanhita.cli_compile import store_lock

    target = tmp_path / "rules.json"
    target.write_text("[]", encoding="utf-8")
    order: list[str] = []
    started = threading.Event()

    def slow_writer():
        with store_lock(target):
            order.append("a-in")
            started.set()
            # Hold it long enough that the other thread must wait.
            threading.Event().wait(0.25)
            order.append("a-out")

    def fast_writer():
        started.wait(2)
        with store_lock(target):
            order.append("b-in")
            order.append("b-out")

    threads = [threading.Thread(target=slow_writer), threading.Thread(target=fast_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    # b never gets in while a holds the lock.
    assert order == ["a-in", "a-out", "b-in", "b-out"], order


def test_a_held_lock_times_out_with_an_instruction(tmp_path):
    from sanhita.cli_compile import StoreBusyError, store_lock

    target = tmp_path / "rules.json"
    with store_lock(target):
        with pytest.raises(StoreBusyError) as exc:
            with store_lock(target, timeout=0.1):
                pass
    message = str(exc.value)
    assert "delete" in message.lower(), "a stuck lock must say how to clear it"
    assert ".lock" in message


def test_the_lock_is_released_even_when_the_write_raises(tmp_path):
    from sanhita.cli_compile import store_lock

    target = tmp_path / "rules.json"
    lock = target.with_name("rules.json.lock")

    with pytest.raises(ValueError):
        with store_lock(target):
            raise ValueError("write blew up")

    assert not lock.exists(), "a failed write must not leave the store locked forever"
    with store_lock(target):  # still usable
        pass


@requires_corpus
def test_the_real_save_path_takes_the_lock(tmp_path):
    from sanhita.certify.lifecycle import RuleRegistry
    from sanhita.cli_compile import _save_registry

    store = tmp_path / "rules.json"
    _save_registry(RuleRegistry(), circular_id="T", fingerprint="f" * 64, path=store)
    assert store.is_file()
    assert not store.with_name("rules.json.lock").exists()
    assert json.loads(store.read_text(encoding="utf-8"))["circular_id"] == "T"


# ------------------------------------------------------------------- routes


@pytest.fixture(scope="module")
def client(corpus_pdf):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sanhita.web.app import create_app

    os.environ.setdefault("SANHITA_SIGNING_KEY", "test-workbench-key")
    return fastapi_testclient.TestClient(create_app(corpus_pdf))


@requires_corpus
def test_the_app_cannot_be_framed(client):
    """The certify button is one click and irreversible."""
    headers = client.get("/w/demo").headers
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


@requires_corpus
def test_security_headers_are_present_on_every_page(client):
    for path in ("/", "/documents", "/facts", "/w/demo/queue", "/signin"):
        headers = client.get(path).headers
        assert headers.get("x-content-type-options") == "nosniff", path
        assert headers.get("referrer-policy"), path
        assert headers.get("content-security-policy"), path


@requires_corpus
def test_the_policy_allows_nothing_off_this_machine(client):
    """The offline claim, enforced by the browser rather than only promised."""
    policy = client.get("/w/demo").headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "base-uri 'none'" in policy


@requires_corpus
def test_sign_in_is_throttled(corpus_pdf, tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sanhita.web.app import create_app

    os.environ.setdefault("SANHITA_SIGNING_KEY", "test-workbench-key")
    fresh = fastapi_testclient.TestClient(
        create_app(corpus_pdf, store=tmp_path / "rules.json")
    )

    seen_throttle = False
    for _ in range(12):
        response = fresh.post(
            "/signin",
            data={"email": "nobody@example.com", "password": "guessing"},
            follow_redirects=False,
        )
        if "Too+many" in response.headers.get("location", "") or "Too%20many" in response.headers.get(
            "location", ""
        ):
            seen_throttle = True
            break
    assert seen_throttle, "a password guesser is never slowed down"


# -------------------------------------------------------------- error pages


@requires_corpus
def test_a_missing_page_stays_inside_the_design(client):
    response = client.get("/w/nosuchdocument")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "There is nothing at that address" in response.text
    assert "masthead" in response.text, "the error page should look like the product"


@requires_corpus
def test_a_json_caller_still_gets_json(client):
    response = client.get("/w/nosuchdocument", headers={"accept": "application/json"})
    assert response.status_code == 404
    assert response.json()["detail"]


# ------------------------------------------------------------ accessibility


@requires_corpus
def test_every_page_offers_a_skip_link(client):
    for path in ("/w/demo/queue", "/documents", "/facts"):
        body = client.get(path).text
        assert 'class="skip"' in body, path
        assert 'id="main"' in body, path


@requires_corpus
def test_the_current_page_is_announced(client):
    body = client.get("/w/demo/coverage").text
    assert "aria-current=page" in body


@requires_corpus
def test_asynchronous_updates_are_announced(client):
    """These change without a page load, so a screen reader needs telling.

    The uploader used to sit on the gaps screen and its status region with it.
    Both now live on the evidence screen, which is where evidence goes, so the
    live region is asserted where the control actually is.
    """
    for path in ("/w/demo/audit", "/w/demo/review"):
        assert 'aria-live="polite"' in client.get(path).text, path


# --------------------------------------------------------------- pagination


@requires_corpus
def test_the_queue_is_paged_not_dumped(client):
    body = client.get("/w/demo/queue").text
    cards = body.count('class="rulecard row')
    assert 0 < cards <= 50, f"rendered {cards} cards in one page"
    assert 'aria-label="Queue pages"' in body


@requires_corpus
def test_paging_moves_to_different_rules(client):
    import re

    def ids(page):
        html = client.get(f"/w/demo/queue?page={page}").text
        return set(re.findall(r'data-clause="([^"]+)"', html))

    first, second = ids(1), ids(2)
    assert first and second
    assert not (first & second), "page 2 repeats page 1"


@requires_corpus
def test_a_page_number_past_the_end_clamps(client):
    """A hand-typed URL must not produce an empty screen with no explanation."""
    response = client.get("/w/demo/queue?page=99999")
    assert response.status_code == 200
    assert 'class="rulecard row' in response.text


@requires_corpus
def test_the_journey_is_not_crammed_into_the_masthead(client):
    """The crowding that printed one label through another, held off.

    Ten items on one line is what produced "Regulatory changes" overlapping
    "Advanced" and "Audit" overlapping "Sign in". The masthead carries the
    brand, the document switcher, the Advanced menu and the account; the
    journey has a row of its own.
    """
    body = client.get("/w/demo/company").text
    masthead = body[body.index('class="masthead"') : body.index("</header>")]

    assert 'class="stagebar"' in body, "the journey row is gone"
    assert 'class="lifecycle"' not in masthead, "the journey is back in the masthead"
    # One dropdown in the bar, not three. Counting the container, not the
    # navgroup-item links inside its menu.
    assert masthead.count("<details class=\"navgroup") == 1, (
        "the masthead is filling up with menus again"
    )


@requires_corpus
def test_the_journey_row_scrolls_rather_than_wraps(client):
    """A ragged second line of stages reads as two lists rather than one path."""
    css = client.get("/static/app.css").text

    # Anchored to the line start, so a rule inside a media query is not mistaken
    # for the base rule.
    stagebar = css[css.index("\n.stagebar {") :][:400]
    assert "overflow-x: auto" in stagebar, "the row will wrap on a narrow window"

    lifecycle = css[css.index("\n.lifecycle {") :][:300]
    assert "min-width: max-content" in lifecycle, "the stages will be squashed"

    # And nothing still styles the stages as though they were in the masthead.
    assert ".masthead .lifestep" not in css
    assert ".masthead .lifecycle" not in css

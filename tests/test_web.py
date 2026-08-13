"""Route smoke tests and the provenance-highlighting core.

The highlighting tests do not need the corpus and always run â€” they are the
guarantee that the workbench's central interaction cannot silently drift from
the spans that were actually signed.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import requires_corpus

from sanhita.web.highlight import segment_text


# --------------------------------------------------------------- highlighting


def test_segments_reproduce_the_text_exactly():
    """The clause is rendered verbatim; markup goes around it, never inside."""
    text = "The stock broker shall report the short-collection on T+5 day."
    spans = {"modality": (17, 22), "deadline": (51, 61)}
    segments = segment_text(text, spans)
    assert "".join(s.text for s in segments) == text


def test_each_segment_carries_the_fields_covering_it():
    text = "The stock broker shall report to the Stock Exchange."
    spans = {"action.object": (24, 51), "action.recipient": (37, 51)}
    segments = segment_text(text, spans)

    by_field: dict[str, str] = {}
    for segment in segments:
        for field in segment.fields:
            by_field[field] = by_field.get(field, "") + segment.text

    assert by_field["action.object"] == text[24:51]
    assert by_field["action.recipient"] == text[37:51]


def test_overlapping_spans_both_survive():
    """recipient nests inside object â€” a naive wrap would lose one of them."""
    text = "shall report to the Stock Exchange the short-collection"
    spans = {"outer": (6, 34), "inner": (20, 34)}
    segments = segment_text(text, spans)
    nested = [s for s in segments if set(s.fields) == {"outer", "inner"}]
    assert nested, "the overlap produced no doubly-tagged run"
    assert "".join(s.text for s in nested) == text[20:34]


def test_a_span_outside_the_text_is_ignored_not_crashed():
    text = "short clause"
    segments = segment_text(text, {"bogus": (5, 900), "ok": (0, 5)})
    assert "".join(s.text for s in segments) == text
    assert any("ok" in s.fields for s in segments)
    assert not any("bogus" in s.fields for s in segments)


def test_no_spans_yields_one_plain_segment():
    segments = segment_text("nothing highlighted here", {})
    assert len(segments) == 1
    assert not segments[0].is_highlighted


def test_empty_text_yields_nothing():
    assert segment_text("", {"a": (0, 1)}) == []


# ---------------------------------------------------------------- the routes


@pytest.fixture(scope="module")
def client(corpus_pdf):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sanhita.web.app import create_app

    os.environ.setdefault("SANHITA_SIGNING_KEY", "test-workbench-key")
    return fastapi_testclient.TestClient(create_app(corpus_pdf))


@requires_corpus
def test_healthz_reports_the_real_fingerprint(client):
    payload = client.get("/healthz").json()
    assert payload["ok"] is True
    assert len(payload["fingerprint"]) == 64


@requires_corpus
@pytest.mark.parametrize("path", ["/", "/queue", "/coverage", "/audit"])
def test_core_screens_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@requires_corpus
def test_no_external_requests_anywhere(client):
    """The app must run offline.

    What breaks offline is a *loaded resource* — a stylesheet, script, font or
    image fetched from another host. A plain anchor to github.com is a link the
    user may click, not a request the page makes, so this checks the loading
    attributes rather than every occurrence of "https://".
    """
    import re

    loaders = re.compile(
        r'<(?:link|script|img|source|iframe|video|audio)\b[^>]*\b(?:href|src)\s*=\s*'
        r'["\'](?P<url>[^"\']+)["\']',
        re.I,
    )
    for path in ("/", "/queue", "/coverage", "/audit", "/clause/40.1.8"):
        body = client.get(path).text
        for match in loaders.finditer(body):
            url = match.group("url")
            assert not re.match(r"(?:https?:)?//", url), (
                f"{path} loads an external resource: {url}"
            )
        # Belt and braces: no known CDN host anywhere on the page at all.
        for host in ("googleapis", "gstatic", "unpkg", "jsdelivr", "cdnjs", "bootstrapcdn"):
            assert host not in body, f"{path} references {host}"


@requires_corpus
def test_there_is_no_search_or_question_box(client):
    """The product thesis forbids an ask-the-regulation surface.

    Checked against input *affordances*, not against substrings â€” the corpus
    itself contains clause titles like "Frequently Asked Questions", and a
    naive text search flags those as a chatbot.
    """
    import re

    forbidden_names = re.compile(
        r'<(?:input|textarea)[^>]*\bname="(q|query|question|search|ask|prompt|message|chat)"',
        re.I,
    )
    for path in ("/", "/queue", "/coverage", "/audit", "/clause/40.1.8"):
        body = client.get(path).text
        assert 'type="search"' not in body.lower(), path
        assert not forbidden_names.search(body), f"{path} exposes a question input"
        # Every form must be either a lifecycle action or a list filter.
        # Nothing may accept free text and answer it.
        for form in re.finditer(r"<form\b[^>]*>.*?</form>", body, re.I | re.S):
            markup = form.group(0)
            action = re.search(r'action="([^"]*)"', markup)
            target = action.group(1) if action else ""
            method = re.search(r'method="([^"]*)"', markup)
            verb = (method.group(1) if method else "get").lower()

            if verb == "get":
                # A filter form: GET, and every control is a select or checkbox.
                assert not re.search(r'<(?:textarea|input[^>]*type="text")', markup, re.I), (
                    f"{path} has a GET form accepting free text — that is a query box"
                )
            else:
                # ``bind`` records which team inside the firm owns a rule. It
                # takes free text and writes it down; it never reads text and
                # answers it, which is the distinction this test exists to
                # protect. Anything that returns a response derived from what
                # was typed does not belong on this list.
                assert re.search(r"/(resolve|certify|reject|edit|bind)$", target), (
                    f"{path} posts to {target!r}, which is not a lifecycle action"
                )


@requires_corpus
def test_the_workbench_renders_the_clause_verbatim(client):
    response = client.get("/clause/40.1.8")
    assert response.status_code == 200
    body = response.text
    assert "short-collection" in body
    assert "T+5 day" in body


@requires_corpus
def test_unknown_clause_is_a_clean_404(client):
    assert client.get("/clause/999.999").status_code == 404


@requires_corpus
def test_certify_without_a_named_officer_is_refused(client):
    """The officer is the account now, not the form.

    This used to check that a blank name was rejected, which was the best a
    text box could do: anybody could type any name and it was accepted. The
    rule it was reaching for is enforced properly now, so the refusal is a 401
    on the act rather than a 400 on the field. See
    ``tests/test_certification_identity.py``.
    """
    response = client.post(
        "/clause/40.1.8/certify",
        data={"obligation_id": "SB-40.1.8-a", "by": "   ", "note": ""},
    )
    assert response.status_code == 401
    assert "need an account" in response.text


@requires_corpus
def test_reject_without_a_reason_is_refused(client):
    """Signed in, so what is under test is the missing reason and nothing else."""
    client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    response = client.post(
        "/clause/40.1.8/reject",
        data={"obligation_id": "SB-40.1.8-a", "reason": "  "},
    )
    assert response.status_code == 400


@requires_corpus
def test_verify_endpoint_reports_structurally(client):
    payload = client.post("/audit/verify").json()
    assert set(payload) >= {"ok", "checked", "valid", "tampered", "ledger_problems"}

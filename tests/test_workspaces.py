"""Bring your own document.

The promise this file protects: a person can arrive with their own SEBI PDF and
run the whole pipeline on it, and Sanhita will tell them the truth about what it
could read. Two things must never happen.

  A document the parser cannot read must not quietly produce a thin rulebook.
  It must refuse, and say why.

  One person's document must not touch another's. Separate rules, separate
  certifications, separate audit chain.
"""

from __future__ import annotations

import os

import pytest

from sanhita.parse.quality import Verdict, assess
from sanhita.workspace import UploadRejected, WorkspaceStore, check_pdf
from tests.conftest import requires_corpus

# --------------------------------------------------------------- upload guard


def test_a_file_that_is_not_a_pdf_is_refused():
    with pytest.raises(UploadRejected) as exc:
        check_pdf(b"clause 1.1 the broker shall do the thing")
    assert "PDF" in str(exc.value)


def test_an_empty_file_is_refused():
    with pytest.raises(UploadRejected):
        check_pdf(b"")


def test_a_file_past_the_size_limit_is_refused():
    from sanhita.workspace import MAX_UPLOAD_BYTES

    with pytest.raises(UploadRejected) as exc:
        check_pdf(b"%PDF-1.7" + b"\0" * MAX_UPLOAD_BYTES)
    assert "MB" in str(exc.value)


def test_uploads_are_rate_limited_per_caller():
    """An open upload endpoint is a way to take the machine down."""
    from sanhita.workspace import RateLimited, RateLimiter

    limiter = RateLimiter(limit=3, window_seconds=300)
    for _ in range(3):
        limiter.check("1.2.3.4")

    with pytest.raises(RateLimited) as exc:
        limiter.check("1.2.3.4")
    assert "limit" in str(exc.value)

    # A different caller is unaffected.
    limiter.check("5.6.7.8")


def test_the_rate_limit_window_expires():
    from sanhita.workspace import RateLimiter

    limiter = RateLimiter(limit=2, window_seconds=60)
    limiter.check("1.2.3.4", now=0.0)
    limiter.check("1.2.3.4", now=1.0)
    # Past the window, the allowance is back.
    limiter.check("1.2.3.4", now=100.0)


@requires_corpus
def test_uploads_stop_before_they_fill_the_disk(tmp_path, corpus_pdf, monkeypatch):
    from sanhita import workspace as ws_module

    monkeypatch.setattr(ws_module, "MAX_TOTAL_BYTES", 1024)
    store = _store(tmp_path, corpus_pdf)
    with pytest.raises(UploadRejected) as exc:
        store.create(corpus_pdf.read_bytes(), filename="a.pdf")
    assert "Delete a document" in str(exc.value)


def test_the_refusal_is_a_sentence_not_a_stack_trace():
    """Whatever we refuse, the person is told in words they can act on."""
    with pytest.raises(UploadRejected) as exc:
        check_pdf(b"not a pdf at all")
    message = str(exc.value)
    assert message[0].isupper() and message.rstrip().endswith(".")
    assert "Traceback" not in message and "Error" not in message


# ------------------------------------------------------------- parse verdict


def _blank_pdf(tmp_path, text: str, name: str = "doc.pdf"):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, fontsize=11)
    path = tmp_path / name
    doc.save(str(path))
    return path


def test_a_document_with_no_numbered_clauses_is_unreadable(tmp_path):
    """A press release is not a regulation, and must not be treated as one."""
    from sanhita.parse.clause_tree import parse_clause_tree

    pdf = _blank_pdf(tmp_path, "SEBI today announced measures for participants.")
    quality = assess(parse_clause_tree(pdf))

    assert quality.verdict is Verdict.UNREADABLE
    assert quality.can_compile is False
    assert quality.blockers, "an unreadable document must state at least one blocker"


def test_every_concern_explains_itself(tmp_path):
    """No bare error codes. Each concern says what happened and what it means."""
    from sanhita.parse.clause_tree import parse_clause_tree

    pdf = _blank_pdf(tmp_path, "A press release with no clause numbering at all.")
    quality = assess(parse_clause_tree(pdf))

    for concern in quality.concerns:
        assert concern.level in {"blocker", "warning", "note"}
        assert len(concern.detail) > 60, f"{concern.title!r} explains nothing"


@requires_corpus
def test_the_real_circular_reads_cleanly(parsed):
    quality = assess(parsed)
    assert quality.can_compile
    assert quality.clauses > 500
    assert quality.sections > 50


# ------------------------------------------------------------- the store


def _store(tmp_path, corpus_pdf):
    return WorkspaceStore(
        root=tmp_path / "workspaces",
        builtin_pdf=corpus_pdf,
        builtin_store=tmp_path / "rules.json",
    )


@requires_corpus
def test_the_same_bytes_land_in_the_same_workspace(tmp_path, corpus_pdf):
    """Uploading a document twice must not fork its audit trail in two."""
    store = _store(tmp_path, corpus_pdf)
    data = corpus_pdf.read_bytes()

    first = store.create(data, filename="circular.pdf")
    second = store.create(data, filename="circular-copy.pdf")

    assert first.id == second.id
    assert len(store.uploaded()) == 1


@requires_corpus
def test_different_documents_get_separate_stores(tmp_path, corpus_pdf):
    store = _store(tmp_path, corpus_pdf)
    a = store.create(corpus_pdf.read_bytes(), filename="a.pdf")
    b = store.create(corpus_pdf.read_bytes() + b"\n% different", filename="b.pdf")

    assert a.id != b.id
    assert a.store_path != b.store_path
    assert a.pdf_path.read_bytes() != b.pdf_path.read_bytes()


@requires_corpus
def test_the_worked_example_cannot_be_deleted(tmp_path, corpus_pdf):
    """It is the thing a first-time visitor looks at. It is not theirs to remove."""
    store = _store(tmp_path, corpus_pdf)
    assert store.delete("demo") is False


@requires_corpus
def test_deleting_a_workspace_removes_everything_it_produced(tmp_path, corpus_pdf):
    store = _store(tmp_path, corpus_pdf)
    workspace = store.create(corpus_pdf.read_bytes(), filename="a.pdf")
    root = workspace.root
    assert root.is_dir()

    assert store.delete(workspace.id) is True
    assert not root.exists()
    assert store.get(workspace.id) is None


@requires_corpus
def test_an_id_cannot_escape_the_workspaces_directory(tmp_path, corpus_pdf):
    """Ids come off the URL, so they must not be able to point anywhere else."""
    store = _store(tmp_path, corpus_pdf)
    for hostile in ("../..", "../../etc", "a/../../b"):
        assert store.get(hostile) is None
        assert store.delete(hostile) is False


@requires_corpus
def test_a_filename_cannot_carry_a_path(tmp_path, corpus_pdf):
    store = _store(tmp_path, corpus_pdf)
    workspace = store.create(
        corpus_pdf.read_bytes(), filename="../../../../etc/passwd.pdf"
    )
    assert "/" not in workspace.source_name and "\\" not in workspace.source_name


# ------------------------------------------------------------------- routes


@pytest.fixture(scope="module")
def client(corpus_pdf):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sanhita.web.app import create_app

    os.environ.setdefault("SANHITA_SIGNING_KEY", "test-workbench-key")
    return fastapi_testclient.TestClient(create_app(corpus_pdf))


@pytest.fixture
def clean_client(corpus_pdf, tmp_path):
    """The real certified rulebook, but a workspace with no firm data in it.

    The plain ``client`` fixture runs against the checked-in store, so anything
    a developer uploads while looking at the running app becomes a fixture for
    every test that reads the gaps screen. A test about the empty state has to
    own its emptiness.
    """
    import shutil

    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sanhita.web.app import create_app

    os.environ.setdefault("SANHITA_SIGNING_KEY", "test-workbench-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return fastapi_testclient.TestClient(create_app(corpus_pdf, store=store))


@pytest.fixture
def private_client(corpus_pdf, tmp_path):
    """An app with its own store, so signing up cannot touch the real one."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from sanhita.web.app import create_app

    os.environ.setdefault("SANHITA_SIGNING_KEY", "test-workbench-key")
    return fastapi_testclient.TestClient(
        create_app(corpus_pdf, store=tmp_path / "rules.json")
    )


def _sign_up(client, email="officer@example.com"):
    response = client.post(
        "/signup",
        data={"email": email, "name": "A Named Officer", "password": "correct horse battery"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


@requires_corpus
def test_signed_out_the_documents_page_asks_you_to_sign_in(client):
    """Say it before somebody drags a file onto something that will refuse it."""
    body = client.get("/documents").text
    assert "Sign in to bring your own circular" in body
    assert 'id="drop"' not in body


@requires_corpus
def test_signed_in_the_drop_target_appears(private_client):
    _sign_up(private_client)
    body = private_client.get("/documents").text
    assert "Drop a SEBI circular here" in body
    assert 'id="drop"' in body


@requires_corpus
def test_the_worked_example_is_listed(client):
    assert "Worked example" in client.get("/documents").text


@requires_corpus
def test_the_document_page_states_a_verdict(client):
    body = client.get("/w/demo").text
    assert "Can this be compiled?" in body
    assert "Parse tree fingerprint" in body


@requires_corpus
def test_the_legacy_paths_still_resolve_to_the_worked_example(client):
    """The CLI prints these, and earlier bookmarks point at them."""
    for path in ("/queue", "/coverage", "/audit"):
        assert client.get(path).status_code == 200


@requires_corpus
def test_an_unknown_document_is_a_404_not_a_crash(client):
    assert client.get("/w/nosuchdocument").status_code == 404


@requires_corpus
def test_uploading_without_an_account_is_refused(client):
    """Upload is the one route that spends real CPU for a stranger."""
    response = client.post(
        "/documents/upload",
        content=b"not a pdf",
        headers={"x-sanhita-filename": "notes.txt"},
    )
    assert response.status_code == 401
    assert response.json()["needs_account"] is True
    assert "confidential" in response.json()["error"]


@requires_corpus
def test_uploading_a_non_pdf_is_refused_once_signed_in(private_client):
    _sign_up(private_client)
    response = private_client.post(
        "/documents/upload",
        content=b"not a pdf",
        headers={"x-sanhita-filename": "notes.txt"},
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "PDF" in response.json()["error"]


@requires_corpus
def test_one_persons_document_is_not_visible_to_another(private_client, corpus_pdf):
    """The confidentiality claim, asserted rather than assumed."""
    _sign_up(private_client, "first@example.com")
    uploaded = private_client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes(),
        headers={"x-sanhita-filename": "mine.pdf"},
    )
    assert uploaded.status_code == 200
    wid = uploaded.json()["id"]
    assert private_client.get(f"/w/{wid}").status_code == 200

    # A second person, signed in as themselves, must not see or open it.
    private_client.post("/signout", follow_redirects=False)
    _sign_up(private_client, "second@example.com")

    assert "mine.pdf" not in private_client.get("/documents").text
    # 404 rather than 403, so the answer does not confirm the id exists.
    assert private_client.get(f"/w/{wid}").status_code == 404
    assert private_client.get(f"/w/{wid}/export").status_code == 404


@requires_corpus
def test_the_worked_example_stays_open_to_everyone(private_client):
    """It is the thing a first-time visitor is meant to look at."""
    assert private_client.get("/w/demo").status_code == 200
    assert private_client.get("/w/demo/queue").status_code == 200


@requires_corpus
def test_the_gaps_screen_refuses_to_assess_a_firm_that_gave_it_nothing(clean_client):
    """It used to generate events and report breaches against a named firm.

    A caption admitting the events were generated did not undo the impression
    made by a page headed "where you are out of compliance" listing dozens of
    findings. There is no assessment now until the firm provides records.
    """
    body = clean_client.get("/w/demo/gaps").text

    assert "No assessment has been run" in body
    assert "Confirmed gaps" not in body
    assert "will not tell a firm it is in breach" in body
    assert "/review" in body, "it must say where to upload"


@requires_corpus
def test_the_demonstration_run_is_reachable_and_labelled(clean_client):
    """Showing what the engine does is fine. Doing it unasked is not."""
    body = clean_client.get("/w/demo/gaps?demo=1").text

    assert "Confirmed gaps" in body, "the demonstration should actually run"
    assert "This is a demonstration, not this firm's position" in body
    assert "No firm filed any of this" in body


@requires_corpus
def test_the_gaps_screen_carries_its_caveats(clean_client):
    """No number on that screen may travel without what qualifies it."""
    body = clean_client.get("/w/demo/gaps?demo=1").text
    assert "Before you quote any of that" in body


@requires_corpus
def test_the_gaps_screen_shows_a_signature_for_every_finding(clean_client):
    """A finding without its citation is an opinion, not a finding."""
    import re

    body = clean_client.get("/w/demo/gaps").text
    breaches = len(re.findall(r'class="gapcard gapcard-', body))
    if breaches:
        assert body.count("Certified by") >= breaches
        assert len(re.findall(r"signature [0-9a-f]{64}", body)) >= breaches


@requires_corpus
def test_there_is_still_no_search_or_question_box_anywhere(client):
    """The rule that has held since Phase 0 must survive the new screens."""
    import re

    for path in ("/documents", "/w/demo"):
        body = client.get(path).text
        for match in re.finditer(r"<input\b[^>]*>", body):
            tag = match.group(0)
            assert 'type="search"' not in tag, f"{path} has a search input"
            assert "ask" not in tag.lower(), f"{path} has an ask box"
        assert "chat" not in body.lower()


@requires_corpus
def test_the_new_screens_load_nothing_from_the_network(client):
    """Offline in a room with no network, still true for the upload screens."""
    import re

    for path in ("/documents", "/w/demo"):
        body = client.get(path).text
        for attr in re.findall(r'(?:src|href)="([^"]+)"', body):
            assert not attr.startswith(("http://", "https://", "//")), (
                f"{path} loads {attr} from off this machine"
            )

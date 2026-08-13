"""Has a newer edition arrived, and has anybody looked at it?

The product could compare two editions well, once a person thought to open the
comparison screen and pick the earlier one from a dropdown. That is not
monitoring. It is a tool that answers a question nobody remembers to ask, and
the whole point of a regulatory change is that it happens on SEBI's calendar
rather than on the compliance officer's.

The claim these tests hold the line on is the one it would be easiest to
overstate. Sanhita does not watch sebi.gov.in. Nothing polls a website or
subscribes to a feed, and a circular published this week is invisible until
somebody uploads it. Every screen carrying this has to say so, because a firm
that believed otherwise would read silence as cover.

What it does remove is the other failure, which is real: the newer circular has
been in the system since March, and no comparison was ever run.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import re

import pytest

from tests.conftest import requires_corpus

IN_USE = _dt.date(2025, 6, 17)


# ------------------------------------------------------------ the fixtures


class _Status:
    def __init__(self, is_open):
        self.is_open = is_open


class _Task:
    def __init__(self, amended_from="f" * 64, is_open=True):
        self.amended_from = amended_from
        self.status = _Status(is_open)


def _row(wid, name, issued, *, declared=True, certified=10, created=None):
    return {
        "id": wid,
        "name": name,
        "issued_on": issued,
        "certified": certified,
        "total": certified,
        "declared": declared,
        "builtin": False,
        "created_at": created,
    }


def _watch(candidates, tasks_by_id=None, **kw):
    from sanhita.monitor import watch_for_firm

    tasks_by_id = tasks_by_id or {}
    kw.setdefault("firm", "ABC Securities")
    kw.setdefault("in_use_id", "demo")
    kw.setdefault("in_use_name", "Stock Brokers Master Circular")
    kw.setdefault("in_use_issued_on", IN_USE)
    kw.setdefault("in_use_fingerprint", "f" * 64)
    kw.setdefault("certified_in_use", 183)
    return watch_for_firm(
        candidates=candidates, tasks_for=lambda wid: tasks_by_id.get(wid, []), **kw
    )


# ------------------------------------------------------ what counts as news


def test_a_later_edition_nobody_compared_is_the_headline():
    """The defect this exists for, in one assertion."""
    from sanhita.monitor import EditionState

    watch = _watch([_row("w2", "Stock Brokers, February 2026", _dt.date(2026, 2, 1))])

    assert watch.waiting == 1
    assert watch.editions[0].state is EditionState.NOT_COMPARED
    assert "never been compared" in watch.headline()
    assert "183 certified rules it touches" in watch.editions[0].describe()


def test_an_earlier_edition_is_not_news():
    """It is the thing you compare against, not something that arrived."""
    watch = _watch([_row("w0", "Stock Brokers, June 2024", _dt.date(2024, 6, 1))])

    assert not watch.editions
    assert watch.is_quiet


def test_the_edition_in_use_is_not_watching_itself():
    watch = _watch([_row("demo", "Stock Brokers Master Circular", _dt.date(2026, 2, 1))])

    assert not watch.editions


def test_a_rulebook_this_firm_never_declared_is_not_its_regulatory_change():
    """Somebody else's circular sitting on the same installation is not news."""
    watch = _watch(
        [_row("w2", "Mutual Funds, March 2026", _dt.date(2026, 3, 1), declared=False)]
    )

    assert not watch.editions


def test_an_undated_edition_is_not_guessed_at_but_is_named():
    """The upload date says nothing about when SEBI issued the circular.

    Naming it rather than dropping it matters: an edition nothing can order is
    the one most likely to be a later circular nobody has noticed, so silence
    would make this watch quietest exactly where it is needed.
    """
    watch = _watch([_row("w2", "Something, undated", None)])

    assert not watch.editions
    assert watch.undated == ["Something, undated"]
    assert not watch.is_quiet
    assert "no readable issue date" in watch.headline()


def test_an_uploaded_circular_carries_the_date_printed_on_it():
    """Without this the watch could never fire: uploads had no issue date at
    all, so no edition was ever orderable and the feature was dead."""
    import tempfile

    from tests.conftest import ROOT

    later = ROOT / "corpus" / "investment-advisers-2026-02.pdf"
    if not later.is_file():
        pytest.skip("corpus/ is gitignored")

    from sanhita.workspace import WorkspaceStore

    root = pathlib.Path(tempfile.mkdtemp())
    store = WorkspaceStore(
        root,
        builtin_pdf=ROOT / "corpus" / "stock-brokers-master-circular-2025-06-17.pdf",
        builtin_store=root / "rules.json",
    )
    space = store.create(later.read_bytes(), filename="ia-2026-02.pdf")

    assert space.issued_on == _dt.date(2026, 2, 6), (
        "the date printed on page one of the February 2026 edition"
    )
    # And it is the regulation's date, not this machine's. Conflating them
    # would let a 2024 circular uploaded today outrank a 2026 one.
    assert space.issued_on != space.created_at.date()


def test_the_issue_date_is_the_first_one_on_page_one():
    """A rule that searched further would pick up a consolidated circular's
    'dated August 09, 2024' and report a 2025 master circular as a 2024 one."""
    from sanhita.parse.footnotes import read_issue_date

    header = (
        "SEBI/HO/MIRSD/POD/CIR/2025/85\n"
        "June 17, 2025\n\n"
        "To,\nAll Stock Brokers\nSir/Madam,\n\n"
        "Sub: Master Circular for Stock Brokers\n\n"
        "This consolidates the circular dated August 09, 2024 and others."
    )
    found = read_issue_date(header)

    assert found is not None
    assert found[0] == _dt.date(2025, 6, 17)
    # And it hands back the characters, so the date can be checked.
    assert found[1] == "June 17, 2025"


def test_a_document_with_no_date_is_left_undated():
    from sanhita.parse.footnotes import read_issue_date

    assert read_issue_date("A circular with no date anywhere on it.") is None


def test_a_declared_rulebook_with_no_certified_rule_is_reported():
    """A firm that ticked a box believes it is covered."""
    watch = _watch(
        [_row("w2", "Depositories, December 2024", _dt.date(2024, 12, 1), certified=0)]
    )

    assert watch.declared_but_empty == ["Depositories, December 2024"]
    assert "would find nothing" in watch.headline()


# ------------------------------------------- and what has been done about it


def test_an_edition_with_open_tasks_is_being_worked():
    from sanhita.monitor import EditionState

    watch = _watch(
        [_row("w2", "February 2026", _dt.date(2026, 2, 1))],
        {"w2": [_Task(is_open=True), _Task(is_open=False)]},
    )

    assert watch.editions[0].state is EditionState.IN_HAND
    assert watch.editions[0].tasks_open == 1
    assert watch.editions[0].tasks_total == 2
    assert "1 of 2 action(s) raised from it are still open" in watch.editions[0].describe()


def test_an_edition_whose_work_all_closed_is_settled():
    from sanhita.monitor import EditionState

    watch = _watch(
        [_row("w2", "February 2026", _dt.date(2026, 2, 1))],
        {"w2": [_Task(is_open=False)]},
    )

    assert watch.editions[0].state is EditionState.SETTLED
    assert not watch.needing_attention


def test_work_raised_against_a_different_edition_does_not_count():
    """Comparing February against 2023 says nothing about the edition in use."""
    from sanhita.monitor import EditionState

    watch = _watch(
        [_row("w2", "February 2026", _dt.date(2026, 2, 1))],
        {"w2": [_Task(amended_from="e" * 64)]},
    )

    assert watch.editions[0].state is EditionState.NOT_COMPARED


def test_the_unexamined_editions_sort_above_the_settled_ones():
    from sanhita.monitor import EditionState

    watch = _watch(
        [
            _row("w2", "February 2026", _dt.date(2026, 2, 1)),
            _row("w3", "August 2026", _dt.date(2026, 8, 1)),
        ],
        {"w2": [_Task(is_open=False)]},
    )

    assert [e.state for e in watch.editions] == [
        EditionState.NOT_COMPARED,
        EditionState.SETTLED,
    ]


def test_how_long_an_edition_has_been_sitting_unread_is_said():
    uploaded = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=45)
    watch = _watch(
        [_row("w2", "February 2026", _dt.date(2026, 2, 1), created=uploaded)]
    )

    assert watch.editions[0].days_waiting == 45
    assert "on file for 45 day(s)" in watch.editions[0].describe()


def test_a_firm_with_no_later_edition_is_told_plainly():
    watch = _watch([])

    assert watch.is_quiet
    assert watch.headline() == "No later edition of this firm's rulebooks is on file."


# ------------------------------------------------------------ through the UI


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a-test-only-key")
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    return TestClient(create_app(corpus_pdf, store=store))


def _plain(html: str) -> str:
    body = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _set_up(client):
    client.post(
        "/signup",
        data={
            "name": "A Named Officer",
            "email": "officer@example.com",
            "password": "a-long-enough-password",
        },
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )
    client.post(
        "/w/demo/company/frameworks", data={"framework": "demo"}, follow_redirects=True
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)


@requires_corpus
def test_the_overview_says_nothing_when_no_later_edition_exists(client):
    """Silence here is correct, and it must not be a claim of coverage."""
    _set_up(client)

    page = _plain(client.get("/w/demo/company").text)

    assert "Has the regulation moved?" not in page


@requires_corpus
def test_a_firm_without_a_profile_is_not_watched(client):
    """There is nobody to watch on behalf of yet."""
    page = _plain(client.get("/w/demo/company").text)

    assert "Has the regulation moved?" not in page


@requires_corpus
def test_the_watch_never_implies_it_polls_sebi(client, corpus_pdf):
    """The most damaging overclaim available to this feature."""
    _set_up(client)
    uploaded = client.post(
        "/documents/upload",
        content=corpus_pdf.read_bytes() + b"\n% a later edition",
        headers={"x-sanhita-filename": "later-edition.pdf"},
    )
    assert uploaded.status_code == 200, uploaded.text
    client.post(
        "/w/demo/company/frameworks",
        data={"framework": ["demo", uploaded.json()["id"]]},
        follow_redirects=True,
    )

    page = _plain(client.get("/w/demo/company").text)

    if "Has the regulation moved?" in page:
        assert "It does not poll sebi.gov.in" in page
        assert "invisible here until somebody uploads it" in page

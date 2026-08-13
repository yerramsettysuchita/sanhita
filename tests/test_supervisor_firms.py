"""The supervisor screen counted documents and called them firms.

A broker declaring three rulebooks appeared as three firms. A circular nobody
had attached a company to appeared as a firm with no name. Every number on the
screen was a fact about documents wearing a label that said firms, which is the
kind of error that survives a demo and fails an audit.

Two things these tests hold. A firm is a company profile somebody recorded, and
one firm across three frameworks is one firm. And a position shown beside a
firm's name is the one that firm's own recorded assessment produced, never
recomputed here, never shown at all when the records have moved since. Two
screens disagreeing about whether a firm is compliant is the exact failure the
assessment record was introduced to end.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus


# ------------------------------------------------------------ the fixtures


class _Intermediary:
    def __init__(self, value):
        self.value = value


class _Company:
    def __init__(self, name, intermediary="STOCK_BROKER"):
        self.name = name
        self.intermediary = _Intermediary(intermediary)


class _Run:
    def __init__(self, satisfied=8, breaches=2, evaluated=12, ran_at=None):
        self.satisfied = satisfied
        self.breaches = breaches
        self.rules_evaluated = evaluated
        self.ran_at = ran_at or _dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc)


def _entry(name="ABC Securities", framework="Stock Brokers", **kw):
    entry = {
        "company": _Company(name) if name else None,
        "framework_id": kw.pop("framework_id", "demo"),
        "framework_name": framework,
        "certified": kw.pop("certified", 183),
        "recorded": kw.pop("recorded", None),
        "latest_run": kw.pop("latest_run", None),
        "open_tasks": kw.pop("open_tasks", 0),
        "records": kw.pop("records", 0),
        "days_since_record": kw.pop("days_since_record", None),
    }
    entry.update(kw)
    return entry


def _view(*entries):
    from sanhita.supervise import build_view

    return build_view(entries)


# ------------------------------------------------------- a firm is a firm


def test_one_firm_across_three_frameworks_is_one_firm():
    """The defect, in one assertion."""
    view = _view(
        _entry(framework="Stock Brokers", framework_id="w1"),
        _entry(framework="Depositories", framework_id="w2"),
        _entry(framework="Research Analysts", framework_id="w3"),
    )

    assert view.firms == 1
    assert len(view.rows) == 3, "the frameworks themselves should still be listed"
    assert view.is_a_single_firm
    assert "This is not a sector view" in view.headline()


def test_a_document_with_no_company_is_counted_rather_than_named_a_firm():
    view = _view(_entry(name=None), _entry(name="ABC Securities"))

    assert view.firms == 1
    assert view.documents_without_a_firm == 1


def test_two_firms_are_two_firms():
    view = _view(
        _entry(name="ABC Securities"), _entry(name="Zeta Broking", framework_id="w2")
    )

    assert view.firms == 2
    assert not view.is_a_single_firm
    assert "2 firms" in view.headline()


# ------------------------------------------------ what a position may say


def test_a_recorded_run_produces_the_position_and_its_date():
    view = _view(_entry(recorded=_Run(satisfied=9, breaches=1)))
    row = view.rows[0]

    assert row.has_position
    assert row.position == 90
    assert row.checked == 10
    assert row.assessed_at.date() == _dt.date(2026, 8, 1)
    assert "1 of 10 checkable duties had a finding" in row.describe()


def test_a_firm_never_assessed_shows_no_number_at_all():
    view = _view(_entry())
    row = view.rows[0]

    assert row.state == "NEVER_ASSESSED"
    assert row.position is None
    assert "No position exists to report" in row.describe()


def test_a_run_whose_records_have_moved_is_withdrawn_not_shown():
    """The stale-verdict problem, which this screen must not reintroduce."""
    view = _view(_entry(latest_run=_Run(), recorded=None))
    row = view.rows[0]

    assert row.stale
    assert row.state == "WITHDRAWN"
    assert row.position is None, "an old percentage was presented as current"
    assert "history rather than its current position" in row.describe()
    assert view.withdrawn == 1


def test_a_clean_assessment_reads_as_clean():
    view = _view(_entry(recorded=_Run(satisfied=12, breaches=0)))
    row = view.rows[0]

    assert row.state == "CLEAN"
    assert row.position == 100
    assert "with no findings across 12 checkable duties" in row.describe()


def test_a_run_that_checked_nothing_produces_no_percentage():
    """Zero over zero is not one hundred percent."""
    view = _view(_entry(recorded=_Run(satisfied=0, breaches=0, evaluated=0)))

    assert view.rows[0].position is None


def test_the_position_uses_the_runs_own_arithmetic():
    """The same ratio as AssessmentRun.compliance_rate, so no two screens
    can disagree about what the number means."""
    from sanhita.assess import AssessmentRun

    fields = AssessmentRun.__dataclass_fields__
    for name in ("satisfied", "breaches", "rules_evaluated", "ran_at"):
        assert name in fields, f"the supervisor reads {name} off a run"


# ------------------------------------------------------------- the ranking


def test_the_firms_a_supervisor_should_look_at_sort_first():
    view = _view(
        _entry(name="Clean Co", recorded=_Run(satisfied=5, breaches=0)),
        _entry(name="Never Co", framework_id="w2"),
        _entry(name="Failing Co", framework_id="w3", recorded=_Run(breaches=3)),
        _entry(name="Withdrawn Co", framework_id="w4", latest_run=_Run()),
    )

    assert [r.firm for r in view.rows] == [
        "Failing Co",
        "Withdrawn Co",
        "Never Co",
        "Clean Co",
    ]


def test_an_unknown_position_outranks_a_clean_one():
    """Not knowing is a supervisory problem. Being clean is not."""
    view = _view(
        _entry(name="Clean Co", recorded=_Run(satisfied=5, breaches=0)),
        _entry(name="Never Co", framework_id="w2"),
    )

    assert view.rows[0].firm == "Never Co"
    assert view.never_assessed == 1
    assert "1 never assessed" in view.headline()


def test_a_firms_frameworks_are_grouped_together():
    view = _view(
        _entry(name="ABC", framework="Brokers", recorded=_Run(breaches=1)),
        _entry(name="ABC", framework="Depositories", framework_id="w2"),
        _entry(name="Zeta", framework="Brokers", framework_id="w3",
               recorded=_Run(satisfied=4, breaches=0)),
    )
    grouped = view.by_firm()

    assert len(grouped["ABC"]) == 2
    # The firm with the worst row comes first.
    assert list(grouped)[0] == "ABC"


def test_open_tasks_are_summed_across_every_firm():
    view = _view(_entry(open_tasks=3), _entry(name="Zeta", framework_id="w2", open_tasks=4))

    assert view.open_tasks == 7


def test_an_installation_with_no_firms_says_so():
    view = _view()

    assert not view.rows
    assert view.headline() == "No company profile has been recorded on this installation."


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


@requires_corpus
def test_the_screen_lists_firms_before_documents(client):
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )

    page = _plain(client.get("/supervisor").text)

    assert "Every firm, and what is known about it" in page
    assert "ABC Securities Pvt Ltd" in page
    assert "And the documents behind them" in page
    assert page.index("Every firm") < page.index("And the documents behind them")


@requires_corpus
def test_a_firm_that_was_never_assessed_gets_no_percentage(client):
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd", "intermediary": "STOCK_BROKER"},
        follow_redirects=True,
    )

    page = _plain(client.get("/supervisor").text)

    assert "Never assessed" in page
    assert "No position exists to report" in page


@requires_corpus
def test_the_screen_does_not_claim_to_be_a_market(client):
    """The overclaim available here, and the reason it must not be made."""
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities Pvt Ltd"},
        follow_redirects=True,
    )

    page = _plain(client.get("/supervisor").text)

    assert "not SEBI's register of intermediaries" in page
    assert "no figure here is extrapolated" in page


@requires_corpus
def test_an_installation_with_no_firm_says_so_rather_than_drawing_one(client):
    page = _plain(client.get("/supervisor").text)

    assert "No company profile has been recorded here yet" in page

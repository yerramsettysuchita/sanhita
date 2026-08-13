"""A duty with no record is unknown, and the engine used to call it a breach.

Two parts of this product were built from the same run and disagreed about what
they were looking at. ``health.py`` says, in as many words, that a duty with no
record is very often one discharged perfectly on paper that nobody uploaded, so
it reports it as unknown. The engine emitted ``NO_EVIDENCE`` for the same
situation and counted it in ``report.breaches``.

The demonstration state made the contradiction concrete: one firm, one uploaded
register, and a screen reading **30 breaches** of which 29 were duties nobody
had given Sanhita any record of either way. On the next screen the same run was
described as 29 unknowns.

A breach is now a claim the records can defend: an occasion fell due, there is a
record of it, and that record shows the artifact was never filed or was filed
late. Everything else is unverified, counted separately, and never described as
a finding against the firm.

The line these tests hold is the one a judge would push on: **the product must
not say a firm failed a duty when what it means is that it cannot tell.**
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus, sign_in


def _finding(outcome, oid="SB-1.1-a", clause="1.1"):
    from sanhita.execute.report import Finding, Outcome

    return Finding(
        outcome=Outcome(outcome),
        event_id=f"EV-{oid}-{outcome}",
        entity="ABC Securities",
        occurred_on=_dt.date(2026, 1, 31),
        due_on=_dt.date(2026, 2, 7),
        filed_on=None if outcome != "LATE" else _dt.date(2026, 2, 20),
        days_late=13 if outcome == "LATE" else None,
        obligation_id=oid,
        clause_id=clause,
        section=clause.split(".")[0],
        page=1,
        verbatim="Every intermediary shall file the return.",
        requirement="file the return",
        certified_by="A Named Officer",
        certified_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        signature="a" * 64,
    )


def _report(*findings, satisfied=0, checked=0):
    from sanhita.execute.report import GapReport

    return GapReport(
        evidence_label="ABC Securities, filing register",
        calendar_name="weekends only",
        run_at=_dt.datetime(2026, 8, 13, tzinfo=_dt.timezone.utc),
        satisfied=satisfied,
        events_checked=checked,
        findings=list(findings),
    )


# ------------------------------------------------------- the two counters


def test_no_evidence_is_not_counted_as_a_breach():
    """The defect, in one assertion."""
    report = _report(
        _finding("NO_EVIDENCE", "SB-1.1-a", "1.1"),
        _finding("NO_EVIDENCE", "SB-2.1-a", "2.1"),
        _finding("MISSING", "SB-3.1-a", "3.1"),
    )

    assert report.breaches == 1, "a duty with no record was reported as a breach"
    assert report.unverified == 2
    assert len(report.findings) == 3, "nothing was dropped, only re-counted"


def test_a_missing_filing_is_a_breach_because_the_record_proves_it():
    report = _report(_finding("MISSING"))

    assert report.breaches == 1
    assert report.unverified == 0


def test_a_late_filing_is_a_breach_too():
    report = _report(_finding("LATE"))

    assert report.breaches == 1
    assert report.unverified == 0


def test_a_run_with_nothing_but_unknowns_reports_no_breaches():
    """The demonstration state's exact shape, and the sentence that has to
    be true when a judge asks about it."""
    report = _report(*[_finding("NO_EVIDENCE", f"SB-{n}.1-a", f"{n}.1") for n in range(1, 30)])

    assert report.breaches == 0
    assert report.unverified == 29


def test_both_numbers_survive_serialisation():
    """The assessment record has to preserve the distinction, or a run read
    back later loses the thing this whole change was about."""
    report = _report(_finding("MISSING"), _finding("NO_EVIDENCE", "SB-2.1-a", "2.1"))
    payload = report.to_json()

    assert payload["breaches"] == 1
    assert payload["unverified"] == 1
    assert len(payload["findings"]) == 2


def test_the_compliance_rate_is_over_what_could_be_verified():
    """Unknowns are not in the denominator either. A firm that filed three of
    four occasions is at 75%, not at 3 over 33."""
    report = _report(
        _finding("MISSING"),
        *[_finding("NO_EVIDENCE", f"SB-{n}.1-a", f"{n}.1") for n in range(5, 34)],
        satisfied=3,
        checked=4,
    )

    assert report.compliance_rate == pytest.approx(0.75)


# ------------------------------------------------------------ on the screens


@pytest.fixture()
def client(corpus_pdf, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    monkeypatch.setenv("SANHITA_SIGNING_KEY", "a" * 64)
    store = tmp_path / "rules.json"
    shutil.copy(corpus_pdf.parent.parent / ".sanhita" / "rules.json", store)
    client = TestClient(create_app(corpus_pdf, store=store))
    sign_in(client, name="R. Nair")
    return client


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


def _assessed(client, tmp_path):
    """The demonstration's exact shape: one register, one unfiled occasion."""
    rule = _recurring(tmp_path)
    client.post(
        "/w/demo/company/save",
        data={"name": "ABC Securities", "intermediary": "STOCK_BROKER"},
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
            f"{rule.id},ABC Securities,2026-01-31,,{rule.evidence[0].artifact_type},R1\n"
        ).encode(),
    )
    client.post("/w/demo/setup/complete", follow_redirects=True)
    client.post("/w/demo/assess", follow_redirects=True)
    return rule


@requires_corpus
def test_the_gaps_screen_keeps_the_two_apart(client, tmp_path):
    _assessed(client, tmp_path)
    page = _plain(client.get("/w/demo/gaps").text)

    assert "Confirmed gaps" in page
    assert "Not verifiable" in page
    assert "The records prove these" in page
    assert "nothing here knows either way" in page
    assert "Breaches found" not in page, "the merged number is still on screen"


@requires_corpus
def test_the_overview_keeps_the_two_apart(client, tmp_path):
    _assessed(client, tmp_path)
    page = _plain(client.get("/w/demo/company").text)

    assert "Confirmed gaps" in page
    assert "Not verifiable" in page
    assert "No record either way, so not a finding" in page


@requires_corpus
def test_an_unverifiable_row_is_not_labelled_as_a_breach(client, tmp_path):
    """A row reading NO_EVIDENCE beside "high severity" reads as guilt."""
    _assessed(client, tmp_path)
    page = _plain(client.get("/w/demo/gaps").text)

    assert "UNVERIFIED" in page
    assert "not a proven breach" in page


@requires_corpus
def test_an_unverifiable_row_asks_for_evidence_rather_than_a_fix(client, tmp_path):
    """The control has to name what is actually wanted."""
    _assessed(client, tmp_path)
    page = _plain(client.get("/w/demo/gaps").text)

    assert "Ask for the evidence" in page
    assert "which is not the same as finding that the firm failed it" in page


@requires_corpus
def test_a_confirmed_gap_still_offers_to_be_fixed(client, tmp_path):
    """The path the whole remediation loop runs on must survive the change."""
    _assessed(client, tmp_path)
    page = _plain(client.get("/w/demo/gaps").text)

    assert "Fix this gap" in page


@requires_corpus
def test_the_recorded_run_stores_the_narrow_number(client, tmp_path):
    """What is written down has to mean what the screen said."""
    from sanhita.assess import AssessmentLog

    _assessed(client, tmp_path)
    run = AssessmentLog.load(tmp_path / "assessments.json").latest

    assert run is not None
    assert run.no_evidence > 0, "the demonstration should have unknowns in it"
    assert run.breaches == run.missing + run.late, (
        "the stored breach count still includes duties with no record"
    )
    assert run.breaches < run.no_evidence + run.breaches


@requires_corpus
def test_the_two_screens_no_longer_contradict_each_other(client, tmp_path):
    """The whole point. Evidence health and the gap report are built from the
    same records and used to describe them differently."""
    _assessed(client, tmp_path)

    gaps = _plain(client.get("/w/demo/gaps").text)
    review = _plain(client.get("/w/demo/review").text)

    # One says it cannot tell.
    assert "None of this is a compliance finding" in review
    # The other no longer says it can.
    assert "Not verifiable" in gaps
    assert "not a finding" in gaps or "not a proven breach" in gaps


@requires_corpus
def test_the_headline_percentage_is_over_what_could_be_determined(client, tmp_path):
    """The same contradiction, one level up, and the one a jury sees first.

    The denominator used to be every applicable duty, so a firm that had
    uploaded a single register read 33% compliant and the missing 67% was
    mostly "we have not been shown". A percentage that falls because nobody
    uploaded a file is a percentage about us, not about the firm.
    """
    _assessed(client, tmp_path)
    page = _plain(client.get("/w/demo/company").text)

    assert "Of what could be determined" in page
    assert "could not be determined either way and are not in this figure" in page
    assert "Compliant with this framework" not in page, (
        "the figure still claims to be the firm's compliance with the framework"
    )

    shown = int(re.search(r"(\d+)% Of what could be determined", page).group(1))
    met = int(re.search(r"(\d+) met of (\d+) the records settle", page).group(1))
    settled = int(re.search(r"(\d+) met of (\d+) the records settle", page).group(2))
    assert shown == round(met / settled * 100)
    assert settled < 45, "the unknowns are still in the denominator"


def test_a_proven_failure_is_ranked_above_an_unknown():
    """It used to be the other way round, which buried the one gap a firm
    could act on under twenty-nine it could only ask about."""
    from sanhita.execute.report import Outcome

    report = _report(
        *[_finding("NO_EVIDENCE", f"SB-{n}.1-a", f"{n}.1") for n in range(1, 30)],
        _finding("MISSING", "SB-99.1-a", "99.1"),
    )
    order = [f.outcome for f in report.ranked()]

    assert order[0] is Outcome.MISSING
    assert order[1] is Outcome.NO_EVIDENCE


@requires_corpus
def test_the_one_remediable_gap_is_on_the_screen(client, tmp_path):
    """A demonstration whose actionable finding is below the fold is one the
    video cannot show."""
    _assessed(client, tmp_path)
    page = _plain(client.get("/w/demo/gaps").text)

    assert "Fix this gap" in page, "the remediable finding is not on the screen"
    assert page.index("Fix this gap") < page.index("Ask for the evidence"), (
        "the unknowns are ranked above the gap the firm can act on"
    )

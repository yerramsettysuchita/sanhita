"""Is this firm's evidence still arriving, or did it stop in March?

An assessment is a photograph. It says where a firm stood against the records
it had on the day somebody ran it, and nothing about the months since. That
silence is the failure mode compliance software actually has: a register
uploaded once during onboarding looks exactly like a register kept up to date,
and the product either says nothing or keeps showing a green number.

The line these tests defend hardest is what a missing record means. It is not a
breach. A duty with no record is very often one discharged perfectly on paper
that nobody uploaded, and calling that a finding would be the fabrication the
whole product exists to avoid. What can be said is that nothing here knows
either way, and that is what the screens have to say.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from tests.conftest import requires_corpus

TODAY = _dt.date(2026, 8, 13)


# ------------------------------------------------------------ the fixtures


def _rule(clause="15.1", period="MONTH", evidence=True, certified=True):
    """A certified recurring obligation, built through the real schema."""
    import hashlib

    from sanhita.ir.enums import (
        DayCount,
        DeadlineKind,
        Modality,
        RuleStatus,
        TriggerKind,
    )
    from sanhita.ir.schema import (
        Action,
        Deadline,
        EvidenceReq,
        Obligation,
        SourceAnchor,
        Trigger,
    )

    text = f"Clause {clause}. Every intermediary shall file the periodic return."
    # The id has to carry the clause, and the schema enforces it.
    oid = f"SB-{clause}-a"
    proposed = Obligation(
        id=oid,
        version="1.0.0",
        status=RuleStatus.PROPOSED,
        actor="STOCK_BROKER",
        modality=Modality.MUST if evidence else Modality.SHOULD,
        action=Action(verb="file", object="the periodic return"),
        trigger=(
            Trigger(
                kind=TriggerKind.SCHEDULE,
                expression="period.end",
                recurrence=f"FREQ={period}LY" if period else None,
            )
            if period
            else Trigger(kind=TriggerKind.EVENT, expression="client.onboarded")
        ),
        deadline=(
            Deadline(
                kind=DeadlineKind.END_OF_PERIOD,
                period=period,
                offset_days=0,
                # Settled, because an unresolved day-count blocks certification
                # and these fixtures need to be signed rules.
                business_days=DayCount.CALENDAR,
            )
            if period
            else None
        ),
        evidence=[EvidenceReq(artifact_type="PERIODIC_RETURN")] if evidence else [],
        source=SourceAnchor(
            circular_id="test",
            section=clause.split(".")[0],
            clause_id=clause,
            page=1,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=0.9,
    )
    if not certified:
        return proposed
    # Signed for real rather than by setting a field, so a rule in these tests
    # is the same artifact the rest of the product handles.
    return proposed.certify(certified_by="A Named Officer", key="a" * 64)


def _event(oid, occurred_on, filed_on=None, entity="ABC Securities"):
    from sanhita.execute import ComplianceEvent

    return ComplianceEvent(
        id=f"EV-{oid}-{occurred_on.isoformat()}",
        obligation_id=oid,
        entity=entity,
        occurred_on=occurred_on,
        artifact_type="PERIODIC_RETURN",
        filed_on=filed_on,
    )


def _store(*events, label="ABC Securities' own records"):
    from sanhita.execute import EvidenceStore

    return EvidenceStore(label=label, events=list(events))


def _month_ends(count, up_to=TODAY):
    """The last `count` month ends on or before `up_to`, most recent last."""
    import calendar as _cal

    out, year, month = [], up_to.year, up_to.month
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append(_dt.date(year, month, _cal.monthrange(year, month)[1]))
    return sorted(out)


def _health(obligations, evidence, **kw):
    from sanhita.health import assess_evidence_health

    kw.setdefault("as_of", TODAY)
    return assess_evidence_health(obligations, evidence, **kw)


# --------------------------------------------------- what the signals mean


def test_a_duty_with_records_against_every_occasion_is_current():
    from sanhita.health import Signal

    ends = _month_ends(6)
    report = _health(
        [_rule()], _store(*[_event("SB-15.1-a", d, filed_on=d) for d in ends])
    )

    assert report.watched == 1
    assert report.rules[0].signal is Signal.CURRENT
    assert not report.needing_attention


def test_records_that_stop_are_reported_as_stopped():
    """The defect this module exists for, in one assertion."""
    from sanhita.health import Signal

    ends = _month_ends(12)
    # Filed diligently for six months, then nothing.
    report = _health(
        [_rule()], _store(*[_event("SB-15.1-a", d, filed_on=d) for d in ends[:6]])
    )
    rule = report.rules[0]

    assert rule.signal is Signal.GONE_QUIET
    assert rule.last_recorded_on == ends[5]
    assert rule.last_due_on == ends[-1]
    assert rule.days_quiet and rule.days_quiet > 150
    assert "has fallen due again since" in rule.describe()


def test_a_duty_with_no_records_at_all_is_not_called_a_breach():
    """The line this module must never cross.

    A duty with no record is very often one discharged perfectly on paper that
    nobody uploaded. Saying "breach" here would fabricate a finding.
    """
    from sanhita.health import Signal

    report = _health([_rule()], _store())
    rule = report.rules[0]

    assert rule.signal is Signal.NEVER_RECORDED
    said = rule.describe().lower()
    assert "nothing here knows whether it was done" in said
    for forbidden in ("breach", "non-compliant", "violation", "failed to"):
        assert forbidden not in said, f"the wording implies a finding: {forbidden!r}"


def test_an_event_driven_duty_is_reported_rather_than_hidden():
    """Silently omitting it would look like a clean bill of health."""
    from sanhita.health import Signal

    report = _health([_rule(period=None)], _store())

    assert report.rules[0].signal is Signal.NO_SCHEDULE
    assert "no schedule to judge" in report.rules[0].describe()


def test_records_that_name_no_filing_date_are_counted():
    from sanhita.health import Signal

    ends = _month_ends(4)
    report = _health(
        [_rule()],
        _store(
            *[_event("SB-15.1-a", d, filed_on=d) for d in ends[:-1]],
            _event("SB-15.1-a", ends[-1], filed_on=None),
        ),
    )

    assert report.rules[0].signal is Signal.UNFILED
    assert report.rules[0].unfiled == 1


def test_an_uncertified_rule_places_no_duty_on_anybody():
    """Nobody signed it, so its records cannot be missing."""
    report = _health([_rule(certified=False)], _store())

    assert report.watched == 0


def test_a_rule_requiring_no_evidence_is_not_watched():
    """Nothing was ever asked of the filing cabinet here."""
    report = _health([_rule(evidence=False)], _store())

    assert report.watched == 0


def test_the_worst_signal_sorts_first():
    """A list that buries the stopped ones under the healthy ones is a list
    nobody reads to the end."""
    from sanhita.health import Signal

    ends = _month_ends(8)
    report = _health(
        [
            _rule("1.1"),
            _rule("2.1"),
            _rule("3.1"),
        ],
        _store(
            *[_event("SB-1.1-a", d, filed_on=d) for d in ends],
            *[_event("SB-2.1-a", d, filed_on=d) for d in ends[:2]],
        ),
    )

    assert [r.signal for r in report.rules] == [
        Signal.GONE_QUIET,
        Signal.NEVER_RECORDED,
        Signal.CURRENT,
    ]


# ------------------------------------------------------- the store as a whole


def test_a_store_that_has_gone_silent_says_so_first():
    ends = _month_ends(12)
    report = _health([_rule()], _store(*[_event("SB-15.1-a", d, filed_on=d) for d in ends[:3]]))

    assert report.is_quiet
    assert report.days_since_last_record > 200
    assert "Nothing has been recorded for" in report.headline()


def test_a_firm_with_no_records_is_told_that_and_nothing_else():
    report = _health([_rule()], _store())

    assert report.headline() == "No compliance records have been uploaded for this firm."


def test_the_report_carries_the_label_of_the_records_it_read():
    """A report built on generated events must never look like one built on
    a firm's books."""
    report = _health([_rule()], _store(label="generated for demonstration"))

    assert report.source == "generated for demonstration"


def test_the_work_is_grouped_by_the_team_that_has_to_chase_it():
    class _Binding:
        def __init__(self, function):
            self.function, self.process, self.system, self.control_ref = function, "", "", ""

    class _Controls:
        def get(self, oid):
            return _Binding("Operations") if oid == "SB-1.1-a" else None

    report = _health(
        [_rule("1.1"), _rule("2.1")], _store(), controls=_Controls()
    )
    grouped = report.by_function()

    assert "Operations" in grouped
    assert "Not yet mapped" in grouped
    assert list(grouped)[-1] == "Not yet mapped"


def test_only_the_duties_needing_attention_are_grouped_for_chasing():
    """A chase list with healthy rules in it is a chase list nobody uses."""
    ends = _month_ends(6)
    report = _health(
        [_rule("1.1"), _rule("2.1")],
        _store(*[_event("SB-1.1-a", d, filed_on=d) for d in ends]),
    )
    grouped = report.by_function()

    assert sum(len(v) for v in grouped.values()) == 1
    assert grouped["Not yet mapped"][0].obligation_id == "SB-2.1-a"


def test_a_stale_assessment_is_carried_through_rather_than_recomputed():
    """The overview already decides this. Two screens must not disagree."""
    ran = _dt.datetime(2026, 3, 1, tzinfo=_dt.timezone.utc)
    report = _health([_rule()], _store(), assessed_on=ran, assessment_is_stale=True)

    assert report.assessed_on == ran
    assert report.assessment_is_stale


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


@requires_corpus
def test_the_evidence_screen_says_whether_records_are_still_arriving(client, tmp_path):
    rule = _recurring(tmp_path)
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2025-01-31,2025-01-31,"
            f"{rule.evidence[0].artifact_type},REC-001\n"
        ).encode(),
    )

    page = _plain(client.get("/w/demo/review").text)

    assert "Are the records still arriving?" in page
    assert "Evidence health" in page
    assert "Duties watched" in page


@requires_corpus
def test_the_screen_refuses_to_let_a_missing_record_read_as_a_finding(client, tmp_path):
    rule = _recurring(tmp_path)
    client.post(
        "/w/demo/evidence",
        headers={"x-sanhita-filename": "register.csv"},
        content=(
            "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
            f"{rule.id},ABC Securities,2025-01-31,2025-01-31,"
            f"{rule.evidence[0].artifact_type},REC-001\n"
        ).encode(),
    )

    page = _plain(client.get("/w/demo/review").text)

    assert "None of this is a compliance finding" in page
    assert "nobody uploaded" in page


@requires_corpus
def test_a_firm_with_no_records_gets_no_health_section(client):
    """There is nothing to be healthy or unhealthy about yet."""
    page = _plain(client.get("/w/demo/review").text)

    assert "Are the records still arriving?" not in page

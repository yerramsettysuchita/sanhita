"""What the regulation costs, where it will be misread, and what a change does.

These modules make larger claims than anything else in the product: that a
number can stand for regulatory burden, that a ranking can anticipate where
firms will disagree, that an amendment's cost can be computed before it is
published. The tests are mostly about the limits of those claims, because a
feature that overstates what it did is worse than one that does less and says
so.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json

import pytest

from tests.conftest import requires_corpus

from sanhita.analyse.conflicts import find_conflicts
from sanhita.analyse.divergence import assess_divergence
from sanhita.analyse.forecast import Outlook, build_forecast
from sanhita.analyse.impact_assessment import Change, ChangeKind, assess_amendment
from sanhita.analyse.latency import humanise, measure_latency
from sanhita.controls import ControlStore
from sanhita.ir.enums import (
    Actor,
    DayCount,
    DeadlineKind,
    Modality,
    RuleStatus,
    TriggerKind,
)
from sanhita.ir.schema import (
    Action,
    Certification,
    Deadline,
    EvidenceReq,
    ExtractionMeta,
    Obligation,
    SourceAnchor,
    Trigger,
)

UTC = _dt.timezone.utc


def _rule(
    clause_id: str = "40.1.8",
    *,
    suffix: str = "a",
    period: str | None = "MONTH",
    days: int | None = None,
    count: DayCount = DayCount.CALENDAR,
    modality: Modality = Modality.MUST,
    status: RuleStatus = RuleStatus.CERTIFIED,
    actor: Actor = Actor.STOCK_BROKER,
    verb: str = "report",
    obj: str = "the short collection",
    confidence: float = 0.9,
    extracted_at: _dt.datetime | None = None,
    certified_at: _dt.datetime | None = None,
    engine: str = "rules",
) -> Obligation:
    text = f"Clause {clause_id}: the broker shall {verb} {obj}."
    if days is not None:
        deadline = Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=days,
            business_days=count,
            anchor_event="trade.date",
        )
        trigger = Trigger(kind=TriggerKind.EVENT, expression="a trade")
    else:
        deadline = Deadline(kind=DeadlineKind.END_OF_PERIOD, period=period)
        trigger = Trigger(
            kind=TriggerKind.SCHEDULE,
            expression=str(period).lower(),
            recurrence=f"FREQ={period}LY",
        )

    return Obligation(
        id=f"SB-{clause_id}-{suffix}",
        actor=actor,
        modality=modality,
        action=Action(verb=verb, object=obj),
        trigger=trigger,
        deadline=deadline,
        evidence=[EvidenceReq(artifact_type="report")]
        if modality is Modality.MUST
        else [],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=95,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=confidence,
        status=status,
        extraction=ExtractionMeta(
            engine=engine,
            extracted_at=extracted_at or _dt.datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        ),
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=certified_at or _dt.datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            signature="f" * 64,
        )
        if status is RuleStatus.CERTIFIED
        else None,
    )


# ═══════════════════════════════════════════════════════════════ latency ══


def test_the_shelf_time_is_kept_out_of_every_headline():
    """Issuance to first read measures when we ran it, not how fast we are.

    Reporting it as a latency figure would be dishonest in the flattering
    direction, so it has its own property and is never folded into the others.
    """
    rules = [
        _rule(
            extracted_at=_dt.datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            certified_at=_dt.datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        ),
    ]
    report = measure_latency(rules, issued_on=_dt.date(2025, 6, 17))

    assert report.shelf_time.days > 300, "the document sat for about a year"
    # The measured windows must not have absorbed it.
    assert report.compile_window == _dt.timedelta(0)
    assert report.time_to_first_certified == _dt.timedelta(hours=4)

    issued = [m for m in report.milestones if m.key == "issued"]
    assert issued and issued[0].external, "issuance is a fact about the regulation"


def test_a_sub_second_compile_is_not_reported_as_zero():
    """The deterministic extractor finishes a circular inside a second.

    Rounding to integer seconds would print "0 seconds" for the single most
    striking measurement the product makes.
    """
    assert humanise(_dt.timedelta(seconds=0.7)) == "0.7 seconds"
    assert humanise(_dt.timedelta(seconds=0)) == "0.0 seconds"
    assert humanise(None) == "not recorded"
    assert "minute" in humanise(_dt.timedelta(minutes=5))


def test_latency_records_which_engine_produced_the_timings():
    """A deterministic and a model-drafted extraction carry different warranties."""
    rules = [
        _rule(suffix="a", engine="rules"),
        _rule(suffix="b", engine="llm"),
    ]
    report = measure_latency(rules)

    assert report.engines == {"rules": 1, "llm": 1}
    assert "rules" in report.engine_summary
    assert any("Extraction engine" in note for note in report.caveats())


# ════════════════════════════════════════════════════════════ divergence ══


def test_divergence_is_a_ranking_and_says_so():
    rules = [_rule(confidence=0.4)]
    report = assess_divergence(rules)

    assert any("not a probability" in note for note in report.caveats())


def test_the_low_confidence_threshold_comes_from_the_document():
    """A fixed cut flags everything or nothing, because confidence clusters.

    With too few rules for a percentile to mean anything it falls back, and
    either way the number used is reported so a reader knows what "low" meant.
    """
    tight = [
        _rule(clause_id=f"1.{n}", confidence=0.70 + (n % 5) / 100)
        for n in range(40)
    ]
    report = assess_divergence(tight)

    assert 0.70 <= report.low_confidence_at <= 0.74
    assert any(
        f"{report.low_confidence_at:.0%}" in note for note in report.caveats()
    )


def test_a_reviewer_overruling_the_machine_outranks_every_textual_signal():
    """The only backward-looking signal is a record, not a prediction."""
    from sanhita.certify.ledger import AuditEntry, Transition

    contested = _rule(clause_id="9.1", confidence=0.95)
    entry = AuditEntry(
        sequence=1,
        obligation_id=contested.id,
        transition=Transition.AMENDED,
        actor="A Named Officer",
        at=_dt.datetime(2026, 1, 2, tzinfo=UTC),
        from_state="PROPOSED",
        to_state="PROPOSED",
        version="1.1",
    )
    report = assess_divergence([contested], ledger=[entry])

    risk = report.risks[0]
    assert risk.contested
    signal = next(s for s in risk.signals if s.kind == "contested")
    assert signal.weight >= 3.0


@requires_corpus
def test_the_flattened_summary_tables_are_kept_out_of_the_ranking(parsed):
    """Clause 98.3 would otherwise dominate every ranking in the product."""
    from sanhita.cli_compile import _load_registry

    report = assess_divergence(_load_registry().all_current())

    assert report.excluded_table_like > 0
    assert any("summary tables" in note for note in report.caveats())


# ═════════════════════════════════════════════════ impact assessment ══


def test_tightening_a_deadline_against_a_sibling_creates_a_contradiction():
    """The point of the whole feature: a draft can contradict the rulebook."""
    rules = [
        _rule(clause_id="27.4", suffix="a", days=5, count=DayCount.BUSINESS),
        _rule(clause_id="31.2", suffix="b", days=5, count=DayCount.BUSINESS),
    ]
    assessment = assess_amendment(
        rules,
        [
            Change(
                obligation_id="SB-27.4-a",
                kind=ChangeKind.DEADLINE_DAYS,
                days=1,
                day_count=DayCount.BUSINESS,
            )
        ],
    )

    assert len(assessment.directly_changed) == 1
    assert assessment.directly_changed[0]["was"] == "5 business days"
    assert assessment.directly_changed[0]["becomes"] == "1 business day"
    assert assessment.new_conflicts, "31.2 still says five days"
    assert assessment.is_material


def test_making_a_duty_recur_more_often_costs_occasions_a_year():
    rules = [_rule(clause_id="15.9.1", period="MONTH")]
    assessment = assess_amendment(
        rules,
        [
            Change(
                obligation_id="SB-15.9.1-a",
                kind=ChangeKind.DEADLINE_PERIOD,
                period="DAY",
            )
        ],
    )

    # Monthly is 12 a year, daily is 250 trading days.
    assert assessment.total_occasions_delta == 238
    assert assessment.occasions_delta["STOCK_BROKER"] == 238


def test_the_simulation_never_hands_back_the_amended_rules():
    """The copies carry stale signatures by construction.

    That is safe only because they never leave the module, so the report must
    not expose them. This test exists to fail loudly if somebody adds a
    convenient ``amended`` attribute later.
    """
    rules = [_rule(clause_id="27.4", days=5)]
    assessment = assess_amendment(
        rules, [Change(obligation_id="SB-27.4-a", kind=ChangeKind.DEADLINE_DAYS, days=1)]
    )

    for value in vars(assessment).values():
        if isinstance(value, list):
            assert not any(isinstance(item, Obligation) for item in value)


def test_the_original_rules_are_not_mutated_by_a_simulation():
    rules = [_rule(clause_id="27.4", days=5, count=DayCount.BUSINESS)]
    before = rules[0].deadline.offset_days

    assess_amendment(
        rules, [Change(obligation_id="SB-27.4-a", kind=ChangeKind.DEADLINE_DAYS, days=1)]
    )

    assert rules[0].deadline.offset_days == before == 5


def test_editing_a_rule_that_does_not_exist_is_reported_not_ignored():
    assessment = assess_amendment(
        [_rule()],
        [Change(obligation_id="SB-NOPE-z", kind=ChangeKind.DEADLINE_DAYS, days=1)],
    )

    assert assessment.directly_changed[0]["status"] == "NOT FOUND"


def test_without_a_reference_graph_the_reread_list_is_not_claimed():
    """Reporting zero as though it had looked would be a lie of omission."""
    assessment = assess_amendment(
        [_rule()],
        [Change(obligation_id="SB-40.1.8-a", kind=ChangeKind.DEADLINE_DAYS, days=1)],
    )

    assert not assessment.reference_graph_available
    assert any("not attempted" in note for note in assessment.caveats())


# ══════════════════════════════════════════════════════════════ forecast ══


class _Events:
    """A minimal stand-in for an evidence store."""

    label = "a test fixture"

    def __init__(self, rows):
        self.rows = rows

    def for_obligation(self, obligation_id):
        return [r for r in self.rows if r.obligation_id == obligation_id]


class _Event:
    def __init__(self, obligation_id, occurred_on, filed_on):
        self.obligation_id = obligation_id
        self.occurred_on = occurred_on
        self.filed_on = filed_on


def test_a_duty_never_once_evidenced_is_an_observation_not_a_guess():
    start = _dt.date(2026, 1, 1)
    rules = [_rule(clause_id="15.9.1", period="MONTH")]
    history = _Events(
        [
            _Event("SB-15.9.1-a", _dt.date(2025, 10, 31), None),
            _Event("SB-15.9.1-a", _dt.date(2025, 11, 30), None),
        ]
    )

    forecast = build_forecast(rules, history, start=start, horizon_days=60)

    duty = forecast.duties[0]
    assert duty.outlook is Outlook.NEVER_EVIDENCED
    assert duty.occasions_before == 2
    assert duty.filed_before == 0
    assert forecast.at_risk


def test_no_history_is_not_counted_as_a_good_record():
    rules = [_rule(clause_id="15.9.1", period="MONTH")]
    forecast = build_forecast(rules, _Events([]), start=_dt.date(2026, 1, 1))

    assert forecast.duties[0].outlook is Outlook.UNPROVEN
    assert not forecast.at_risk, "unproven is not the same as at risk"
    assert any("empty record" in note for note in forecast.caveats())


def test_a_proposed_rule_owes_nothing_yet():
    rules = [_rule(status=RuleStatus.PROPOSED, period="MONTH")]
    forecast = build_forecast(rules, None, start=_dt.date(2026, 1, 1))

    assert not forecast.duties


def test_one_row_per_duty_even_when_it_falls_due_repeatedly():
    """A monthly duty inside a 90 day window is still one track record."""
    rules = [_rule(clause_id="15.9.1", period="MONTH")]
    forecast = build_forecast(rules, None, start=_dt.date(2026, 1, 1), horizon_days=90)

    assert len(forecast.duties) == 1


# ══════════════════════════════════════════════════════ control bindings ══


def test_a_binding_never_enters_the_signed_payload(tmp_path):
    """The whole reason this is a sidecar.

    Adding a field to Obligation would change the signing payload and
    invalidate every signature already made by a named person.
    """
    rule = _rule()
    signature_before = rule.certification.signature
    canonical_before = rule.model_dump_json()

    store = ControlStore.load(tmp_path / "controls.json")
    store.bind(
        rule.id,
        function="Operations",
        system="margin engine",
        control_ref="SOP-12",
        bound_by="A Named Officer",
    )
    store.save()

    assert rule.certification.signature == signature_before
    assert rule.model_dump_json() == canonical_before
    assert "Operations" not in canonical_before


def test_a_binding_round_trips_through_disk(tmp_path):
    path = tmp_path / "controls.json"
    store = ControlStore.load(path)
    store.bind("SB-114.2-a", function="Operations", system="margin engine")
    store.save()

    reloaded = ControlStore.load(path)
    binding = reloaded.get("SB-114.2-a")

    assert binding is not None
    assert binding.function == "Operations"
    assert binding.describe() == "Operations, margin engine"


def test_a_corrupt_sidecar_does_not_take_the_rulebook_down(tmp_path):
    path = tmp_path / "controls.json"
    path.write_text("{ this is not json", encoding="utf-8")

    store = ControlStore.load(path)

    assert store.bindings == {}


def test_a_binding_needs_a_function_that_owns_it(tmp_path):
    store = ControlStore.load(tmp_path / "controls.json")

    with pytest.raises(ValueError):
        store.bind("SB-114.2-a", function="   ")


def test_control_coverage_counts_what_has_an_owner(tmp_path):
    """Bound and mapped are different questions and are counted separately.

    A rule bound to "Operations" and nothing else has an owner but does not
    tell anybody what to go and fix, so it counts toward ``bound`` and not
    toward ``mapped``.
    """
    store = ControlStore.load(tmp_path / "controls.json")
    store.bind("SB-1-a", function="Operations")

    coverage = store.coverage(["SB-1-a", "SB-2-a", "SB-3-a"])

    assert coverage == {
        "total": 3,
        "bound": 1,
        "mapped": 0,
        "unbound": 2,
        "ratio": pytest.approx(0.3333, abs=1e-4),
        "mapped_ratio": 0.0,
    }


# ═════════════════════════════════════════════════════════════ coverage ══


@requires_corpus
def test_the_ladder_reports_three_numbers_with_two_denominators(parsed):
    """One number cannot answer "how much is covered". Three questions, three answers."""
    from sanhita.cli_compile import _load_registry
    from sanhita.metrics.coverage import compute_coverage

    report = compute_coverage(parsed, _load_registry().all_current())
    rungs = report.ladder()

    assert [r["key"] for r in rungs] == ["compiled", "certified", "evidence"]
    # The first two share a denominator; the third has its own.
    assert rungs[0]["denominator"] == rungs[1]["denominator"]
    assert rungs[2]["denominator"] == report.certified_obligations
    # Compiled reach must exceed certified: signing is the slower half.
    assert report.compiled_coverage > report.clause_coverage
    assert report.clauses_with_any_rule == (
        report.clauses_with_certified + report.clauses_with_proposed_only
    )


def test_the_certified_rung_names_reviewer_hours_as_its_limit():
    """The middle number is the one that gets misread as compiler failure."""
    from sanhita.metrics.coverage import CoverageReport

    rungs = CoverageReport().ladder()
    certified = next(r for r in rungs if r["key"] == "certified")

    assert "reviewer hours" in certified["limited_by"]


# ════════════════════════════════════════════════════════════ conflicts ══


def test_duplications_are_counted_apart_from_contradictions():
    """Adding them together would overstate the finding six-fold on this corpus."""
    rules = [
        _rule(clause_id="27.4", suffix="a", days=5, count=DayCount.BUSINESS),
        _rule(clause_id="31.2", suffix="b", days=1, count=DayCount.BUSINESS),
    ]
    report = find_conflicts(rules)

    assert len(report.conflicts) == len(report.contradictions) + len(
        report.duplications
    )
    for finding in report.duplications:
        assert finding not in report.contradictions


@requires_corpus
def test_most_findings_in_the_real_corpus_are_duplications_not_contradictions():
    """Guards the headline. If this ever inverts, the framing has to change."""
    from sanhita.cli_compile import _load_registry

    report = find_conflicts(_load_registry().all_current())

    assert report.conflicts, "the corpus is known to contain findings"
    assert len(report.duplications) > len(report.contradictions)


# ═══════════════════════════════════════════════════════════════ screens ══


@requires_corpus
def test_every_new_screen_renders(corpus_pdf):
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    client = TestClient(create_app(corpus_pdf))
    for path in (
        "/w/demo/load",
        "/w/demo/divergence",
        "/w/demo/forecast",
        "/w/demo/simulate",
        "/supervisor",
    ):
        assert client.get(path).status_code == 200, path


@requires_corpus
def test_the_impact_screen_answers_a_real_draft(corpus_pdf):
    from fastapi.testclient import TestClient

    from sanhita.cli_compile import _load_registry
    from sanhita.web.app import create_app

    certified = [
        o
        for o in _load_registry().all_current()
        if o.status is RuleStatus.CERTIFIED
        and o.deadline
        and o.deadline.offset_days is not None
    ]
    if not certified:
        pytest.skip("no certified rule with a numeric deadline in the store")

    client = TestClient(create_app(corpus_pdf))
    response = client.get(
        "/w/demo/simulate",
        params={"rule": certified[0].id, "mode": "days", "days": 1},
    )

    assert response.status_code == 200
    assert "New contradictions" in response.text


@requires_corpus
def test_the_supervisor_screen_shows_the_worked_example_on_a_cold_start(corpus_pdf):
    """It must not open empty.

    The screen skips uploaded circulars that are not already parsed, because
    reading a 748 page PDF on demand made it take forty seconds and time out.
    That guard was first written to apply to every document, including the
    worked example, so on a freshly started server the page reported nothing at
    all. A supervisory view that opens blank is worse than one that takes a
    moment on its first load.
    """
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    app = create_app(corpus_pdf)
    assert not app.state.bench_cache, "this test is about a cold cache"

    body = TestClient(app).get("/supervisor").text

    assert "Stock Brokers Master Circular" in body
    assert "0</div>" not in body.split("Documents in view")[0][-40:], (
        "the worked example was skipped and the view is empty"
    )


@requires_corpus
def test_the_supervisor_screen_refuses_to_draw_a_market_from_one_firm(corpus_pdf):
    """Honesty about its own limits is the whole reason it is credible."""
    from fastapi.testclient import TestClient

    from sanhita.web.app import create_app

    client = TestClient(create_app(corpus_pdf))
    body = client.get("/supervisor").text

    assert "one working copy of each document" in body

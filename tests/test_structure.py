"""Two measurements of the regulation itself, rather than of a firm.

Both are only possible because the rules are typed. Both are narrow, and the
tests are largely about holding them to their narrowness: a structural measure
presented as a judgement about importance, or an occasion count presented as a
measure of effort, would be worse than not measuring at all.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

import pytest

from tests.conftest import requires_corpus

from sanhita.analyse.burden import measure_burden
from sanhita.analyse.fragility import assess_fragility
from sanhita.analyse.references import ReferenceGraph, build_graph
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
    Obligation,
    SourceAnchor,
    Trigger,
)


class _Node:
    def __init__(self, node_id, text, page=10):
        self.id = node_id
        self.text = text
        self.page = page
        self.section = node_id.split(".")[0]
        self.kind = "CLAUSE"


class _Tree:
    def __init__(self, *nodes):
        self.nodes = {n.id: n for n in nodes}

    def get(self, node_id):
        return self.nodes.get(node_id)


def _rule(
    clause_id: str,
    *,
    actor: Actor = Actor.STOCK_BROKER,
    modality: Modality = Modality.MUST,
    period: str | None = "MONTH",
    kind: DeadlineKind = DeadlineKind.END_OF_PERIOD,
    status: RuleStatus = RuleStatus.CERTIFIED,
    text: str | None = None,
    suffix: str = "a",
) -> Obligation:
    body = text or f"Clause {clause_id} requires something."
    deadline = None
    if kind is DeadlineKind.END_OF_PERIOD:
        deadline = Deadline(kind=kind, period=period)
    elif kind is DeadlineKind.RELATIVE:
        deadline = Deadline(
            kind=kind,
            offset_days=5,
            business_days=DayCount.CALENDAR,
            anchor_event="trade.date",
        )

    return Obligation(
        id=f"SB-{clause_id}-{suffix}",
        actor=actor,
        modality=modality,
        action=Action(verb="report", object="the thing"),
        trigger=Trigger(kind=TriggerKind.EVENT, expression="an event"),
        deadline=deadline,
        evidence=[EvidenceReq(artifact_type="report")]
        if modality in (Modality.MUST, Modality.SHOULD)
        else [],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=10,
            char_span=(0, len(body)),
            verbatim_text=body,
            sha256=hashlib.sha256(body.encode()).hexdigest(),
        ),
        confidence=0.9,
        status=status,
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc),
            signature="a" * 64,
        )
        if status is RuleStatus.CERTIFIED
        else None,
    )


# ═════════════════════════════════════════════════════════════ fragility ══


def test_a_cited_clause_carries_the_rules_that_depend_on_it():
    tree = _Tree(
        _Node("7.2.2", "The base procedure."),
        _Node("7.2.3", "Follow the procedure in para 7.2.2 above."),
    )
    graph = build_graph(tree)
    report = assess_fragility(tree, graph, [_rule("7.2.2"), _rule("7.2.3")])

    top = report.ranked[0]
    assert top.clause_id == "7.2.2"
    assert top.dependent_clauses == 1
    assert top.dependent_certified == 1
    assert top.blast_radius == 2, "its own rule plus the one that depends on it"


def test_a_clause_nothing_cites_has_no_dependents():
    tree = _Tree(_Node("15.1", "A standalone duty."))
    report = assess_fragility(tree, build_graph(tree), [_rule("15.1")])
    assert report.ranked[0].dependent_clauses == 0
    assert report.load_bearing == []


def test_the_ranking_leads_on_signatures_at_risk():
    """A proposed rule costs nobody a decision. A signed one does."""
    tree = _Tree(_Node("15.1", "Duty."), _Node("40.1", "Duty."))
    rules = [
        _rule("15.1", status=RuleStatus.PROPOSED),
        _rule("15.1", status=RuleStatus.PROPOSED, suffix="b"),
        _rule("15.1", status=RuleStatus.PROPOSED, suffix="c"),
        _rule("40.1", status=RuleStatus.CERTIFIED),
    ]
    report = assess_fragility(tree, build_graph(tree), rules)
    assert report.ranked[0].clause_id == "40.1"


def test_a_flattened_table_cannot_top_the_ranking():
    """Sixty-four rules from a mis-parsed table is a defect, not a fragile clause."""
    from sanhita.analyse.conflicts import TABLE_LIKE_CHARS

    long_text = "x" * (TABLE_LIKE_CHARS + 1)
    tree = _Tree(_Node("98.3", long_text), _Node("15.1", "A real duty."))
    rules = [_rule("98.3", text=long_text, suffix=chr(97 + i)) for i in range(9)]
    rules.append(_rule("15.1"))

    report = assess_fragility(tree, build_graph(tree), rules)
    assert report.ranked, "the real clause should still be ranked"
    assert all(c.clause_id != "98.3" for c in report.ranked)


def test_the_verdict_reports_coupling_rather_than_drama():
    tree = _Tree(_Node("15.1", "A duty."), _Node("15.2", "Another duty."))
    report = assess_fragility(tree, build_graph(tree), [_rule("15.1"), _rule("15.2")])
    assert "structurally flat" in report.verdict()
    assert report.coupling == 0.0


def test_it_says_it_measures_blast_radius_not_importance():
    tree = _Tree(_Node("15.1", "A duty."))
    report = assess_fragility(tree, build_graph(tree), [_rule("15.1")])
    joined = " ".join(report.caveats())
    assert "not importance" in joined
    assert "most consequential sentence" in joined


@requires_corpus
def test_the_real_circular_is_measurably_flat(parsed):
    from sanhita.cli_compile import _load_registry

    graph = build_graph(parsed)
    report = assess_fragility(parsed, graph, _load_registry().all_current())

    assert report.clauses_examined > 700
    assert report.coupling < 0.05, "this circular has very little internal coupling"
    assert "98.3" not in {c.clause_id for c in report.ranked}


# ════════════════════════════════════════════════════════════════ burden ══


def test_duties_are_counted_per_actor():
    rules = [
        _rule("15.1", actor=Actor.STOCK_BROKER),
        _rule("15.2", actor=Actor.STOCK_BROKER),
        _rule("40.1", actor=Actor.DEPOSITORY),
    ]
    report = measure_burden(rules)
    by_actor = {a.actor: a for a in report.actors}
    assert by_actor["STOCK_BROKER"].duties == 2
    assert by_actor["DEPOSITORY"].duties == 1
    assert report.heaviest.actor == "STOCK_BROKER"


def test_a_monthly_duty_is_twelve_occasions_a_year():
    report = measure_burden([_rule("15.1", period="MONTH")])
    assert report.actors[0].filings_per_year == 12


def test_a_daily_duty_counts_trading_days_not_calendar_days():
    report = measure_burden([_rule("15.1", period="DAY")])
    assert report.actors[0].filings_per_year == 250
    assert any("trading days" in c for c in report.caveats())


def test_an_event_driven_duty_is_left_out_of_the_yearly_figure():
    """How often it fires depends on the firm, not on the regulation."""
    report = measure_burden([_rule("15.1", kind=DeadlineKind.RELATIVE)])
    actor = report.actors[0]
    assert actor.event_driven == 1
    assert actor.filings_per_year == 0
    assert any("business volume" in c for c in report.caveats())


def test_a_prohibition_is_not_a_filing():
    report = measure_burden([_rule("15.1", modality=Modality.MUST_NOT)])
    actor = report.actors[0]
    assert actor.prohibitions == 1
    assert actor.duties == 0
    assert actor.filings_per_year == 0


def test_a_permission_is_not_a_duty():
    report = measure_burden([_rule("15.1", modality=Modality.MAY)])
    assert report.actors[0].permissions == 1
    assert report.actors[0].duties == 0


def test_certified_only_narrows_the_count():
    rules = [
        _rule("15.1", status=RuleStatus.CERTIFIED),
        _rule("15.2", status=RuleStatus.PROPOSED),
    ]
    assert measure_burden(rules).actors[0].duties == 2
    assert measure_burden(rules, certified_only=True).actors[0].duties == 1


def test_it_refuses_to_be_read_as_a_measure_of_effort():
    """A one-line confirmation and a cyber framework are not the same work."""
    report = measure_burden([_rule("15.1")])
    joined = " ".join(report.caveats())
    assert "not a measure of effort" in joined
    assert "cyber-resilience framework" in joined


def test_the_basis_of_the_count_is_always_stated():
    loose = " ".join(measure_burden([_rule("15.1")]).caveats())
    strict = " ".join(measure_burden([_rule("15.1")], certified_only=True).caveats())
    assert "not had checked" in loose
    assert "Only rules a person has certified" in strict


@requires_corpus
def test_the_real_corpus_produces_a_burden_figure():
    from sanhita.cli_compile import _load_registry

    report = measure_burden(_load_registry().all_current())
    broker = next(a for a in report.actors if a.actor == "STOCK_BROKER")

    assert broker.duties > 500
    assert broker.filings_per_year > 1000
    assert len(broker.clauses) > 400

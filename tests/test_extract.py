"""Extraction: what it finds, what it refuses to find, and what it cites."""

from __future__ import annotations

import pytest

from sanhita.compile.extract import ExtractionStatus, RuleExtractor
from sanhita.eval.gold import GOLD_SET
from sanhita.eval.harness import run_eval
from sanhita.ir.enums import DayCount, DeadlineKind, Modality
from sanhita.metrics.coverage import ClauseClass, classify_clause, compute_coverage
from tests.conftest import requires_corpus

CIRCULAR = "SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/90"


@pytest.fixture(scope="module")
def extractor() -> RuleExtractor:
    return RuleExtractor(circular_id=CIRCULAR)


# --------------------------------------------------------------- the contract


@requires_corpus
def test_zero_obligations_is_a_common_outcome(parsed, extractor):
    """An extractor that finds a duty in every clause is broken."""
    body = [
        n
        for n in parsed.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
    ]
    outcomes = [extractor.extract(n) for n in body]
    empty = sum(1 for o in outcomes if o.status is ExtractionStatus.NO_OBLIGATION)
    assert empty > 0.3 * len(body), "suspiciously few zero-obligation clauses"
    assert empty < 0.9 * len(body), "suspiciously many — extraction may be failing silently"


@requires_corpus
def test_no_clause_is_silently_dropped(parsed, extractor):
    """Every clause gets exactly one outcome, including failures."""
    body = [
        n
        for n in parsed.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
    ]
    for node in body:
        outcome = extractor.extract(node)
        assert outcome.clause_id == node.id
        assert outcome.status in set(ExtractionStatus)
        assert outcome.reason


@requires_corpus
def test_extraction_is_deterministic(parsed, extractor):
    node = parsed.get("40.1.8")
    first = extractor.extract(node)
    second = extractor.extract(node)
    assert [o.canonical_json() for o in first.obligations] == [
        # extraction timestamps differ, so compare the signed payload only
        o.canonical_json()
        for o in second.obligations
    ] or [o.signing_payload() for o in first.obligations] == [
        o.signing_payload() for o in second.obligations
    ]


@requires_corpus
def test_every_provenance_span_quotes_the_clause(parsed, extractor):
    """A citation that does not land on real characters is a fabricated one."""
    checked = 0
    for node in list(parsed.nodes.values())[:600]:
        for obligation in extractor.extract(node).obligations:
            for field_name, (start, end) in obligation.field_provenance.items():
                assert 0 <= start <= end <= len(node.text), f"{obligation.id}.{field_name}"
                assert obligation.quote(field_name) == node.text[start:end]
                checked += 1
    assert checked > 100


# ---------------------------------------------------------- the worked clause


@requires_corpus
def test_clause_40_1_8_compiles_as_specified(parsed, extractor):
    """The Phase 1 definition of done, asserted field by field."""
    outcome = extractor.extract(parsed.get("40.1.8"))
    assert outcome.status is ExtractionStatus.PROPOSED
    assert len(outcome.obligations) == 1

    rule = outcome.obligations[0]
    assert rule.actor.value == "STOCK_BROKER"
    assert rule.modality is Modality.MUST
    assert rule.deadline.kind is DeadlineKind.RELATIVE
    assert rule.deadline.offset_days == 5
    assert rule.deadline.business_days is DayCount.UNSPECIFIED
    assert rule.evidence, "a MUST must carry evidence"

    for field_name in ("modality", "actor", "action.verb", "action.object", "deadline"):
        assert field_name in rule.field_provenance, field_name
        assert rule.quote(field_name)

    assert rule.quote("modality") == "shall"
    assert rule.quote("deadline") == "T+5 day"
    assert rule.quote("actor") == "TMs/CMs"


# ------------------------------------------------------------ refusals to act


def _node(text: str, clause_id: str = "9.9"):
    from sanhita.parse.anchors import clause_sha256
    from sanhita.parse.clause_tree import ClauseNode

    return ClauseNode(
        id=clause_id,
        kind="CLAUSE",
        number=clause_id,
        title="",
        text=text,
        page=1,
        char_span=(0, len(text)),
        sha256=clause_sha256(text),
        depth=2,
        section=clause_id.split(".")[0],
    )


@pytest.mark.parametrize(
    "text",
    [
        "The 'margins' for this purpose shall mean VaR margin, extreme loss margin "
        "and mark to market margin as prescribed by the Exchange.",
        "Shifting of trades to the error account of the broker shall be deemed not to "
        "be a modification of the client code for this purpose.",
        "If the client fails to make pay-in by settlement day, the same shall result in "
        "levy of penalty as applicable to the stock broker.",
    ],
)
def test_definitions_and_consequences_are_not_obligations(text):
    """'shall mean' and 'shall result in' contain a modal but impose no duty."""
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(_node(text))
    assert outcome.status is ExtractionStatus.NO_OBLIGATION


def test_a_clause_with_no_modal_verb_yields_nothing():
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(
        _node("The framework for utilisation of pledged clients' securities is provided below.")
    )
    assert outcome.status is ExtractionStatus.NO_OBLIGATION
    assert outcome.reason == "no-deontic-duty"


def test_a_prohibition_carries_no_evidence():
    """There is no artifact proving a thing was not done."""
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(
        _node("The trading member shall not transfer the securities to any other pool account.")
    )
    rule = outcome.obligations[0]
    assert rule.modality is Modality.MUST_NOT
    assert rule.evidence == []


def test_a_permission_carries_no_evidence():
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(
        _node("A stock broker may appoint one or more authorised persons for a segment.")
    )
    rule = outcome.obligations[0]
    assert rule.modality is Modality.MAY
    assert rule.evidence == []


def test_one_clause_can_yield_several_obligations():
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(
        _node(
            "The stock broker shall issue a daily margin statement to the client and "
            "shall retain proof of dispatch for five years."
        )
    )
    assert len(outcome.obligations) == 2
    assert [o.id for o in outcome.obligations] == ["SB-9.9-a", "SB-9.9-b"]
    assert outcome.obligations[0].action.verb == "issue"
    assert outcome.obligations[1].action.verb == "retain"


def test_the_object_is_not_the_recipient():
    """'report to the Exchange the short-collection' — the duty is the reporting."""
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(
        _node(
            "The stock broker shall report to the Stock Exchange on T+5 day the actual "
            "short-collection of all margins from clients."
        )
    )
    rule = outcome.obligations[0]
    assert "short-collection" in rule.action.object
    assert not rule.action.object.startswith("to the")


def test_a_long_object_is_truncated_not_discarded():
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(
        _node(
            "The intermediary shall conduct appropriate due diligence in selecting the "
            "third party and in monitoring of its performance over time."
        )
    )
    assert outcome.status is ExtractionStatus.PROPOSED
    assert outcome.obligations[0].action.verb == "conduct"


# ----------------------------------------------------------------- classifier


@pytest.mark.parametrize(
    "text, expected",
    [
        ("The stock broker shall submit the report to the Stock Exchange every month.",
         ClauseClass.OBLIGATION_BEARING),
        ("'Margin' shall mean VaR margin and extreme loss margin for this purpose.",
         ClauseClass.DEFINITION),
        ("As per para 15.3 above, the tagging requirement continues to apply here.",
         ClauseClass.CROSS_REFERENCE),
        ("The framework for utilisation of pledged securities is provided in the table.",
         ClauseClass.RECITAL),
    ],
)
def test_clause_classification(text, expected):
    assert classify_clause(_node(text)) is expected


def test_the_classifier_does_not_consult_the_extractor():
    """The coverage denominator must not move when the extractor changes.

    A clause the extractor cannot handle (no actor in the closed vocabulary) is
    still obligation-bearing, and must stay in the denominator. Otherwise a
    weaker extractor would score *higher* coverage.
    """
    node = _node("Such dedicated team shall submit a quarterly report to the Board.")
    assert classify_clause(node) is ClauseClass.OBLIGATION_BEARING
    outcome = RuleExtractor(circular_id=CIRCULAR).extract(node)
    assert outcome.status is ExtractionStatus.NO_OBLIGATION


# ------------------------------------------------------------------- coverage


@requires_corpus
def test_coverage_denominator_is_stated_and_adds_up(parsed):
    report = compute_coverage(parsed, [])
    assert report.obligation_bearing_clauses + report.excluded_total == report.total_clauses
    assert report.clause_coverage == 0.0, "no certified rules yet"
    statement = report.denominator_statement()
    assert str(report.obligation_bearing_clauses) in statement
    assert "NOT YET VALIDATED" in statement


@requires_corpus
def test_coverage_reports_accuracy_when_it_has_been_measured(parsed):
    report = compute_coverage(parsed, [], classifier_accuracy=0.95)
    assert "95.0% accurate" in report.denominator_statement()


# ----------------------------------------------------------------------- eval


@requires_corpus
def test_every_gold_clause_exists_in_the_parsed_tree(parsed):
    missing = [g.clause_id for g in GOLD_SET if parsed.get(g.clause_id) is None]
    assert not missing, f"gold set references clauses the parser did not produce: {missing}"


@requires_corpus
def test_eval_runs_and_meets_the_floor_we_claim(parsed, extractor):
    """Guards the numbers quoted in the README against silent regression."""
    result = run_eval(parsed, extractor)
    detection = result.metrics["obligation detection"]
    assert detection.precision >= 0.85
    assert detection.recall >= 0.75
    assert detection.f1 >= 0.80
    assert result.metrics["modality"].accuracy >= 0.85
    assert result.metrics["deadline kind"].accuracy >= 0.90
    assert result.classifier_accuracy >= 0.90

"""Clauses that lean on other clauses.

The case this exists for: SEBI amends 7.2.2. Clause 7.2.3 says "follow the
procedure in para 7.2.2 above". Its own text never changes, its own hash never
changes, and a diff that compares text calls it untouched. But the rule compiled
from it now means something different from what the officer signed.

That is invisible to a reader and invisible to a text diff. It is visible here.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

import pytest

from tests.conftest import requires_corpus

from sanhita.analyse.references import ReferenceGraph, build_graph
from sanhita.diff import ChangeKind, Consequence, assess_impact
from sanhita.diff.tree_diff import ClauseChange, TreeDiff
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
    """The parts of a ClauseNode this module reads."""

    def __init__(self, node_id, text, section=None, kind="CLAUSE"):
        self.id = node_id
        self.text = text
        self.section = section if section is not None else node_id.split(".")[0]
        self.kind = kind


class _Tree:
    def __init__(self, *nodes):
        self.nodes = {n.id: n for n in nodes}


def _graph(*pairs) -> ReferenceGraph:
    return build_graph(_Tree(*[_Node(i, t) for i, t in pairs]))


# ------------------------------------------------------------------ reading


def test_a_plain_citation_is_found():
    g = _graph(
        ("7.2.2", "The entity shall register with the Clearing Corporation."),
        ("7.2.3", "The entity shall follow the procedure as prescribed in para 7.2.2 above."),
    )
    assert g.edges == 1
    assert g.citations[0].source == "7.2.3"
    assert g.citations[0].target == "7.2.2"


@pytest.mark.parametrize(
    "phrase",
    ["para 7.2.2", "paragraph 7.2.2", "Para 7.2.2", "clause 7.2.2", "section 7.2.2"],
)
def test_the_phrasings_sebi_actually_uses(phrase):
    g = _graph(("7.2.2", "A duty."), ("7.2.3", f"See {phrase} above for the procedure."))
    assert g.edges == 1


def test_a_list_of_citations_is_expanded():
    """'The data at Para 15.9.1.1, 15.9.1.2 and 15.9.1.3' is three pointers."""
    g = _graph(
        ("15.9.1.1", "First."),
        ("15.9.1.2", "Second."),
        ("15.9.1.3", "Third."),
        ("15.9.1.4", "The data at Para 15.9.1.1, 15.9.1.2 and 15.9.1.3 pertains to it."),
    )
    assert {c.target for c in g.citations} == {"15.9.1.1", "15.9.1.2", "15.9.1.3"}


def test_a_clause_citing_its_own_child_is_not_a_dependency():
    g = _graph(("15.1", "See para 15 for context."), ("15", "Parent."))
    assert g.edges == 0


def test_the_same_pointer_twice_is_one_edge():
    g = _graph(
        ("7.2.2", "A duty."),
        ("7.2.3", "Per para 7.2.2. As stated in para 7.2.2, the entity shall comply."),
    )
    assert g.edges == 1


def test_a_pointer_to_a_clause_that_does_not_exist_is_a_broken_reference():
    """A real defect: the text tells you to follow a pointer to nowhere."""
    g = _graph(("65.3.1", "A duty."), ("98.3", "Submit information stated in para 63.3.1 above."))
    assert g.edges == 0
    assert len(g.broken) == 1
    assert g.broken[0].target == "63.3.1"


def test_a_number_that_could_not_be_a_clause_is_noise_not_a_defect():
    """'para 230' after a section list is a page number, not a broken pointer."""
    g = _graph(("15.1", "A duty."), ("15.2", "As set out at para 230 of the annexure."))
    assert g.broken == []
    assert "230" in g.noise


def test_it_is_deterministic():
    nodes = [
        ("7.2.2", "A duty."),
        ("7.2.3", "Per para 7.2.2."),
        ("7.2.4", "Per para 7.2.3 and 7.2.2."),
    ]
    assert _graph(*nodes).to_json() == _graph(*nodes).to_json()


# ------------------------------------------------------------------ walking


def test_transitive_dependents_are_found_with_their_distance():
    g = _graph(
        ("1.1", "The base duty."),
        ("1.2", "Per para 1.1."),
        ("1.3", "Per para 1.2."),
        ("1.4", "Per para 1.3."),
    )
    deps = g.dependents_of("1.1")
    assert deps == {"1.2": 1, "1.3": 2, "1.4": 3}


def test_a_reference_cycle_terminates():
    g = _graph(("1.1", "Per para 1.2."), ("1.2", "Per para 1.1."))
    assert g.dependents_of("1.1") == {"1.2": 1}


def test_the_chain_is_not_followed_for_ever():
    """Four references away is beyond anything a person would call affected."""
    nodes = [("1.1", "Base.")]
    for i in range(2, 12):
        nodes.append((f"1.{i}", f"Per para 1.{i - 1}."))
    g = _graph(*nodes)
    assert max(g.dependents_of("1.1").values()) <= 4


# ------------------------------------------------------- second-order impact


def _rule(clause_id: str, text: str, status=RuleStatus.CERTIFIED) -> Obligation:
    return Obligation(
        id=f"SB-{clause_id}-a",
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(verb="follow", object="the prescribed procedure"),
        trigger=Trigger(kind=TriggerKind.EVENT, expression="registration"),
        deadline=Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=5,
            business_days=DayCount.CALENDAR,
            anchor_event="event",
        ),
        evidence=[EvidenceReq(artifact_type="record")],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=14,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=0.9,
        status=status,
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc),
            signature="e" * 64,
        )
        if status is RuleStatus.CERTIFIED
        else None,
    )


def _amended(clause_id: str) -> TreeDiff:
    return TreeDiff(
        before_label="before",
        after_label="after",
        before_fingerprint="a" * 64,
        after_fingerprint="b" * 64,
        changes=[ClauseChange(kind=ChangeKind.MODIFIED, clause_id=clause_id)],
    )


def test_a_clause_that_points_at_an_amended_clause_is_reported():
    """The whole reason this feature exists."""
    graph = _graph(
        ("7.2.2", "The base procedure."),
        ("7.2.3", "The entity shall follow the procedure as prescribed in para 7.2.2 above."),
    )
    rules = [_rule("7.2.2", "The base procedure."), _rule("7.2.3", "Per para 7.2.2.")]

    impact = assess_impact(_amended("7.2.2"), rules, references=graph)

    reread = impact.of(Consequence.REREAD)
    assert len(reread) == 1
    assert reread[0].clause_id == "7.2.3"
    assert reread[0].via == "7.2.2"
    assert reread[0].hops == 1


def test_a_reread_does_not_count_as_a_lost_signature():
    """Its own text is intact, so the signature still covers what was read."""
    graph = _graph(("7.2.2", "Base."), ("7.2.3", "Per para 7.2.2."))
    rules = [_rule("7.2.2", "Base."), _rule("7.2.3", "Per para 7.2.2.")]

    impact = assess_impact(_amended("7.2.2"), rules, references=graph)

    assert impact.signatures_lost == 1, "only the directly amended clause"
    assert len(impact.needing_reread) == 1
    assert "moved underneath them" in impact.headline()


def test_without_a_graph_the_second_order_case_is_invisible():
    """Which is exactly the gap this closes."""
    rules = [_rule("7.2.2", "Base."), _rule("7.2.3", "Per para 7.2.2.")]
    impact = assess_impact(_amended("7.2.2"), rules)
    assert impact.of(Consequence.REREAD) == []


def test_a_clause_that_changed_on_its_own_account_is_not_also_a_reread():
    graph = _graph(("7.2.2", "Base."), ("7.2.3", "Per para 7.2.2."))
    rules = [_rule("7.2.2", "Base."), _rule("7.2.3", "Per para 7.2.2.")]

    both = TreeDiff(
        before_label="a",
        after_label="b",
        before_fingerprint="a" * 64,
        after_fingerprint="b" * 64,
        changes=[
            ClauseChange(kind=ChangeKind.MODIFIED, clause_id="7.2.2"),
            ClauseChange(kind=ChangeKind.MODIFIED, clause_id="7.2.3"),
        ],
    )
    impact = assess_impact(both, rules, references=graph)
    assert impact.of(Consequence.REREAD) == []
    assert len(impact.of(Consequence.RECERTIFY)) == 2


# -------------------------------------------------------------- the corpus


@requires_corpus
def test_the_real_circular_has_a_reference_graph(parsed):
    graph = build_graph(parsed)
    assert graph.edges > 40
    assert len(graph.citing_clauses) > 30


@requires_corpus
def test_the_real_circular_contains_broken_cross_references(parsed):
    """SEBI's own text points at paras 63.3.1 to 63.3.3, which do not exist."""
    graph = build_graph(parsed)
    assert graph.broken, "expected at least one pointer that leads nowhere"
    assert any(c.target.startswith("63.3") for c in graph.broken)

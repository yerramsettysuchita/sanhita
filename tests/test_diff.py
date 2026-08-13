"""The diff engine, and what it does to signatures.

The claim being protected: a certification signs over the clause's own
characters, so when those characters change the certification stops covering
them. Not "mostly covers", not "97% similar". Stops.

The real-amendment validation against circulars issued after 17 June 2025 is
still outstanding and is stated as such in the README. These tests use a copy of
the real tree with known edits applied, which proves the mechanism without
claiming to have proven it against a real amendment.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib


from tests.conftest import requires_corpus

from sanhita.diff import ChangeKind, Consequence, assess_impact, diff_trees
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


def _rule(
    clause_id: str,
    text: str,
    *,
    status: RuleStatus = RuleStatus.CERTIFIED,
) -> Obligation:
    return Obligation(
        id=f"SB-{clause_id}-a",
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(verb="report", object="the short collection"),
        trigger=Trigger(kind=TriggerKind.EVENT, expression="short collection observed"),
        deadline=Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=5,
            business_days=DayCount.BUSINESS,
            anchor_event="trade.date",
        ),
        evidence=[EvidenceReq(artifact_type="report")],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=137,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ),
        confidence=0.9,
        status=status,
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=_dt.datetime(2026, 8, 1, tzinfo=_dt.timezone.utc),
            signature="b" * 64,
        )
        if status is RuleStatus.CERTIFIED
        else None,
    )


# ------------------------------------------------------------------ the diff


@requires_corpus
def test_a_tree_compared_with_itself_reports_no_change(parsed):
    diff = diff_trees(parsed, parsed)
    assert diff.identical
    assert diff.summary()["modified"] == 0
    assert diff.summary()["added"] == 0
    assert diff.summary()["removed"] == 0
    assert diff.unchanged > 500


@requires_corpus
def test_editing_one_clause_shows_up_as_exactly_one_modification(parsed):
    after = copy.deepcopy(parsed)
    target = next(
        n
        for n in after.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX" and len(n.text) > 80
    )
    # A real re-parse recomputes the hash with the text, so the fixture must too.
    target.text = target.text + " This sentence was inserted by an amendment."
    target.sha256 = hashlib.sha256(target.text.encode("utf-8")).hexdigest()

    diff = diff_trees(parsed, after, before_label="17 June 2025", after_label="amended")

    assert not diff.identical
    assert len(diff.modified) == 1
    assert diff.modified[0].clause_id == target.id
    assert diff.modified[0].before_sha != diff.modified[0].after_sha


@requires_corpus
def test_a_deleted_clause_is_reported_as_removed(parsed):
    after = copy.deepcopy(parsed)
    victim = next(
        n.id
        for n in after.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
    )
    del after.nodes[victim]

    diff = diff_trees(parsed, after)
    assert [c.clause_id for c in diff.removed] == [victim]


@requires_corpus
def test_the_same_words_under_a_new_number_are_a_renumbering_not_a_rewrite(parsed):
    """Otherwise every renumbering would read as a wholesale rewrite."""
    after = copy.deepcopy(parsed)
    old_id = next(
        n.id
        for n in after.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX" and len(n.text) > 80
    )
    node = after.nodes.pop(old_id)
    node.id = old_id + "9"
    after.nodes[node.id] = node

    diff = diff_trees(parsed, after)
    renumbered = diff.renumbered
    assert len(renumbered) == 1
    assert renumbered[0].clause_id == old_id
    assert renumbered[0].now_at == node.id
    # Not double counted as a removal plus an addition.
    assert not diff.removed
    assert not diff.added


@requires_corpus
def test_the_diff_is_deterministic(parsed):
    after = copy.deepcopy(parsed)
    first = diff_trees(parsed, after).to_json()
    second = diff_trees(parsed, after).to_json()
    assert first == second


# ---------------------------------------------------------------- the impact


def _diff_of(before_text: str, after_text: str, clause_id: str = "40.1.8"):
    """A one-clause diff built without needing the corpus."""
    from sanhita.diff.tree_diff import ChangeKind, ClauseChange, TreeDiff

    kind = ChangeKind.UNCHANGED if before_text == after_text else ChangeKind.MODIFIED
    return TreeDiff(
        before_label="before",
        after_label="after",
        before_fingerprint="a" * 64,
        after_fingerprint="b" * 64,
        changes=[
            ClauseChange(
                kind=kind,
                clause_id=clause_id,
                before_sha=hashlib.sha256(before_text.encode()).hexdigest(),
                after_sha=hashlib.sha256(after_text.encode()).hexdigest(),
            )
        ],
    )


def test_an_amended_clause_invalidates_the_signature_over_it():
    """The claim the whole diff stage exists to make."""
    text = "The stock broker shall report within five days."
    rule = _rule("40.1.8", text)
    diff = _diff_of(text, text + " Amended.")

    impact = assess_impact(diff, [rule])

    assert impact.signatures_lost == 1
    assert impact.certified_after == 0
    assert impact.of(Consequence.RECERTIFY)[0].obligation_id == rule.id
    assert impact.of(Consequence.RECERTIFY)[0].certified_by == "A Named Officer"


def test_an_unchanged_clause_keeps_its_signature():
    text = "The stock broker shall report within five days."
    impact = assess_impact(_diff_of(text, text), [_rule("40.1.8", text)])

    assert impact.affected == []
    assert impact.signatures_lost == 0
    assert "No compiled rule" in impact.headline()


def test_a_proposed_rule_loses_nothing_because_nobody_signed_it():
    text = "The stock broker shall report within five days."
    rule = _rule("40.1.8", text, status=RuleStatus.PROPOSED)
    impact = assess_impact(_diff_of(text, text + " Amended."), [rule])

    assert impact.signatures_lost == 0
    assert impact.of(Consequence.RECOMPILE)[0].obligation_id == rule.id


def test_a_rejected_rule_is_not_dragged_back_in():
    text = "The stock broker shall report within five days."
    rule = _rule("40.1.8", text, status=RuleStatus.REJECTED)
    impact = assess_impact(_diff_of(text, text + " Amended."), [rule])
    assert impact.affected == []


def test_a_deleted_clause_withdraws_its_rule():
    from sanhita.diff.tree_diff import ClauseChange, TreeDiff

    text = "The stock broker shall report within five days."
    diff = TreeDiff(
        before_label="a",
        after_label="b",
        before_fingerprint="a" * 64,
        after_fingerprint="b" * 64,
        changes=[ClauseChange(kind=ChangeKind.REMOVED, clause_id="40.1.8")],
    )
    impact = assess_impact(diff, [_rule("40.1.8", text)])
    assert impact.of(Consequence.WITHDRAW)
    assert impact.signatures_lost == 1


def test_a_renumbered_clause_needs_its_anchor_repointed():
    from sanhita.diff.tree_diff import ClauseChange, TreeDiff

    text = "The stock broker shall report within five days."
    diff = TreeDiff(
        before_label="a",
        after_label="b",
        before_fingerprint="a" * 64,
        after_fingerprint="b" * 64,
        changes=[
            ClauseChange(kind=ChangeKind.RENUMBERED, clause_id="40.1.8", now_at="41.1.8")
        ],
    )
    impact = assess_impact(diff, [_rule("40.1.8", text)])
    affected = impact.of(Consequence.REPOINT)
    assert affected and affected[0].now_at == "41.1.8"


def test_the_headline_says_how_many_signatures_are_gone():
    text = "The stock broker shall report within five days."
    rules = [_rule(f"40.1.{i}", text + str(i)) for i in range(3)]

    from sanhita.diff.tree_diff import ClauseChange, TreeDiff

    diff = TreeDiff(
        before_label="a",
        after_label="b",
        before_fingerprint="a" * 64,
        after_fingerprint="b" * 64,
        changes=[
            ClauseChange(kind=ChangeKind.MODIFIED, clause_id=f"40.1.{i}")
            for i in range(3)
        ],
    )
    impact = assess_impact(diff, rules)
    assert impact.signatures_lost == 3
    assert "3 certification(s)" in impact.headline()


def test_similarity_is_never_consulted():
    """A near-identical clause still loses its signature. One character is enough."""
    text = "The stock broker shall report within five days."
    impact = assess_impact(_diff_of(text, text.replace("five", "seven")), [_rule("40.1.8", text)])
    assert impact.signatures_lost == 1

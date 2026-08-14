"""Rules that disagree with each other.

The value of this feature is entirely in its precision. A screen that reports
thirty conflicts of which twenty are noise is worse than no screen, because a
reviewer learns to ignore it. So most of what is tested here is what the
detector must NOT report.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

from sanhita.analyse import ConflictKind, find_conflicts
from sanhita.analyse.conflicts import TABLE_LIKE_CHARS
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
from tests.conftest import requires_corpus


def _rule(
    clause_id: str,
    *,
    verb: str = "report",
    obj: str = "the short collection of margin",
    actor: Actor = Actor.STOCK_BROKER,
    modality: Modality = Modality.MUST,
    days: int | None = 5,
    count: DayCount = DayCount.CALENDAR,
    status: RuleStatus = RuleStatus.PROPOSED,
    text: str | None = None,
    suffix: str = "a",
) -> Obligation:
    body = text or f"Clause {clause_id} says the broker shall {verb} {obj}."
    deadline = (
        Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=days,
            business_days=count,
            anchor_event="trade.date",
        )
        if days is not None
        else None
    )
    return Obligation(
        id=f"SB-{clause_id}-{suffix}",
        actor=actor,
        modality=modality,
        action=Action(verb=verb, object=obj),
        trigger=Trigger(kind=TriggerKind.EVENT, expression="an event"),
        deadline=deadline,
        evidence=[EvidenceReq(artifact_type="report")],
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
            signature="d" * 64,
        )
        if status is RuleStatus.CERTIFIED
        else None,
    )


# ------------------------------------------------------------- what it finds


def test_the_same_duty_on_two_timelines_is_reported():
    report = find_conflicts([_rule("15.1", days=5), _rule("40.1", days=1)])
    found = report.of(ConflictKind.DEADLINE)
    assert len(found) == 1
    assert found[0].clauses == ("15.1", "40.1")
    assert "5" in found[0].question and "1" in found[0].question


def test_a_requirement_against_a_prohibition_is_reported():
    report = find_conflicts(
        [
            _rule("45.2.5", verb="transfer", obj="the securities"),
            _rule("45.2.6", verb="transfer", obj="the securities", modality=Modality.MUST_NOT),
        ]
    )
    assert len(report.of(ConflictKind.MODALITY)) == 1


def test_two_officers_choosing_different_day_counts_is_reported():
    """The one kind here that is our fault rather than the regulation's."""
    report = find_conflicts(
        [
            _rule("15.1", days=5, count=DayCount.BUSINESS),
            _rule("40.1", days=5, count=DayCount.CALENDAR),
        ]
    )
    found = report.of(ConflictKind.DAY_COUNT)
    assert len(found) == 1
    assert "a person chose" in found[0].question


def test_the_same_duty_stated_twice_is_reported_as_a_duplicate():
    report = find_conflicts([_rule("15.10.1.1"), _rule("48.1.1")])
    assert len(report.of(ConflictKind.DUPLICATE)) == 1


def test_a_conflict_between_signed_rules_is_marked():
    report = find_conflicts(
        [
            _rule("15.1", days=5, status=RuleStatus.CERTIFIED),
            _rule("40.1", days=1, status=RuleStatus.CERTIFIED),
        ]
    )
    assert report.between_certified
    assert report.ranked()[0].involves_certified


# ------------------------------------------------- what it must NOT report


def test_different_actors_are_not_compared():
    report = find_conflicts(
        [
            _rule("15.1", days=5, actor=Actor.STOCK_BROKER),
            _rule("40.1", days=1, actor=Actor.DEPOSITORY),
        ]
    )
    assert report.conflicts == []


def test_different_duties_are_not_compared():
    """Same verb, unrelated object. Reporting these would drown the real ones."""
    report = find_conflicts(
        [
            _rule("15.1", verb="report", obj="the short collection of margin", days=5),
            _rule("40.1", verb="report", obj="a cyber security incident", days=1),
        ]
    )
    assert report.conflicts == []


def test_two_rules_from_the_same_clause_are_not_a_conflict():
    """Those are alternatives the extractor drew from one sentence."""
    report = find_conflicts(
        [
            _rule("15.1", days=5, suffix="a"),
            _rule("15.1", days=1, suffix="b"),
        ]
    )
    assert report.conflicts == []


def test_rejected_and_superseded_rules_are_ignored():
    for dead in (RuleStatus.REJECTED, RuleStatus.SUPERSEDED):
        report = find_conflicts(
            [_rule("15.1", days=5), _rule("40.1", days=1, status=dead)]
        )
        assert report.conflicts == [], dead


def test_an_unresolved_day_count_is_not_a_disagreement():
    """Nobody has chosen yet, so there is nothing for two people to disagree on."""
    report = find_conflicts(
        [
            _rule("15.1", days=5, count=DayCount.UNSPECIFIED),
            _rule("40.1", days=5, count=DayCount.CALENDAR),
        ]
    )
    assert report.of(ConflictKind.DAY_COUNT) == []


def test_a_may_against_a_must_is_not_a_contradiction():
    """A permission and a duty can both stand."""
    report = find_conflicts(
        [
            _rule("15.1", verb="publish", obj="the notice", modality=Modality.MAY, days=None),
            _rule("40.1", verb="publish", obj="the notice", modality=Modality.MUST, days=None),
        ]
    )
    assert report.of(ConflictKind.MODALITY) == []


# -------------------------------------------------- the flattened-table guard


def test_rules_from_a_flattened_table_are_excluded_and_declared():
    """Clause 98.3 is the circular's own summary table. Comparing rules drawn
    from it against the clauses it summarises reports duties conflicting with
    themselves, which is how this feature would have become useless."""
    table_text = "x" * (TABLE_LIKE_CHARS + 1)
    report = find_conflicts(
        [
            _rule("15.1", days=5),
            _rule("98.3", days=1, text=table_text),
        ]
    )

    assert report.conflicts == []
    assert report.excluded_rules == 1
    assert report.excluded_clauses == ["98.3"]
    assert any("98.3" in note for note in report.caveats())


def test_the_exclusion_is_never_silent():
    """A number is worth what its statement of exclusions is worth."""
    report = find_conflicts([_rule("98.3", text="x" * (TABLE_LIKE_CHARS + 1))])
    assert report.excluded_rules == 1
    joined = " ".join(report.caveats())
    assert "left out" in joined and "98.3" in joined


# ------------------------------------------------------------ the report


def test_every_finding_is_phrased_as_a_question_for_a_person():
    """These are not defects in the regulation and must not read as if they are."""
    report = find_conflicts(
        [
            _rule("15.1", days=5),
            _rule("40.1", days=1),
            _rule("48.1", verb="settle", obj="the running account", days=30),
            _rule("60.1", verb="settle", obj="the running account", days=90),
        ]
    )
    assert report.conflicts
    for c in report.conflicts:
        assert len(c.question) > 60
        assert c.question.rstrip().endswith(("?", ".")), c.question


def test_the_caveats_always_state_what_was_compared():
    report = find_conflicts([_rule("15.1"), _rule("40.1", days=1)])
    joined = " ".join(report.caveats())
    assert "same actor" in joined
    assert "not a defect in the regulation" in joined
    assert "model" in joined


def test_it_is_deterministic():
    rules = [_rule("15.1", days=5), _rule("40.1", days=1), _rule("48.1", days=9)]
    assert find_conflicts(rules).to_json() == find_conflicts(rules).to_json()


def test_an_empty_rulebook_finds_nothing_and_does_not_crash():
    report = find_conflicts([])
    assert report.conflicts == []
    assert report.rules_examined == 0


# -------------------------------------------------------------- the corpus


@requires_corpus
def test_the_real_corpus_yields_few_and_credible_findings():
    """Precision matters more than recall. A noisy screen gets ignored."""
    from sanhita.cli_compile import _load_registry

    report = find_conflicts(_load_registry().all_current())

    assert report.conflicts, "the corpus is known to contain duplicates"
    # Before the flattened-table guard this was 37, most of them noise.
    assert len(report.conflicts) < 25, (
        f"{len(report.conflicts)} findings is too many to be credible; "
        "the table guard has probably regressed"
    )
    assert "98.3" in report.excluded_clauses


@requires_corpus
def test_the_known_duplicate_is_found():
    """19.5.5.14 and 62.62 both require a CERT-IN empanelled audit."""
    from sanhita.cli_compile import _load_registry

    report = find_conflicts(_load_registry().all_current())
    pairs = {c.clauses for c in report.of(ConflictKind.DUPLICATE)}
    assert ("19.5.5.14", "62.62") in pairs or ("62.62", "19.5.5.14") in pairs

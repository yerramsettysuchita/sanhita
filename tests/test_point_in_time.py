"""What did we believe our obligations were on the day it happened?

After an incident, that is the question. A system that stores only the current
state cannot answer it, and reconstructing the answer from memory is worth
nothing to an auditor.

Every version of every rule is kept and the ledger records the moment of each
transition, so the state at any past instant is a replay rather than a
recollection. These tests hold that replay to being exact.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

import pytest

from sanhita.certify.lifecycle import RuleRegistry
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
    Deadline,
    EvidenceReq,
    Obligation,
    SourceAnchor,
    Trigger,
)
from tests.conftest import requires_corpus

KEY = "a-test-signing-key"


def _proposal(clause_id: str, verb: str = "report") -> Obligation:
    text = f"Clause {clause_id}: the broker shall {verb} the thing."
    return Obligation(
        id=f"SB-{clause_id}-a",
        actor=Actor.STOCK_BROKER,
        modality=Modality.MUST,
        action=Action(verb=verb, object="the short collection"),
        trigger=Trigger(kind=TriggerKind.EVENT, expression="an event"),
        deadline=Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=5,
            business_days=DayCount.CALENDAR,
            anchor_event="trade.date",
        ),
        evidence=[EvidenceReq(artifact_type="report")],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=10,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=0.9,
    )


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def test_nothing_existed_before_the_first_proposal():
    registry = RuleRegistry()
    before = _now() - _dt.timedelta(days=1)
    registry.propose(_proposal("15.1"))
    assert registry.as_of(before) == []


def test_a_rule_proposed_later_does_not_appear_earlier():
    """Showing it would invent a duty nobody had at the time."""
    registry = RuleRegistry()
    registry.propose(_proposal("15.1"))
    checkpoint = _now()
    registry.propose(_proposal("40.1"))

    ids = {o.id for o in registry.as_of(checkpoint)}
    assert ids == {"SB-15.1-a"}
    assert {o.id for o in registry.as_of(_now())} == {"SB-15.1-a", "SB-40.1-a"}


def test_a_rule_reads_as_proposed_before_it_was_signed():
    registry = RuleRegistry()
    registry.propose(_proposal("15.1"))
    before_signing = _now()
    registry.certify("SB-15.1-a", by="A Named Officer", key=KEY)

    was = registry.as_of(before_signing)
    assert len(was) == 1
    assert was[0].status is RuleStatus.PROPOSED
    assert was[0].certification is None

    now = registry.as_of(_now())
    assert now[0].status is RuleStatus.CERTIFIED
    assert now[0].certification.certified_by == "A Named Officer"


def test_an_amendment_does_not_rewrite_the_past():
    """The version in force then is the version returned, not the newest one."""
    registry = RuleRegistry()
    registry.propose(_proposal("15.1"))
    registry.certify("SB-15.1-a", by="A Named Officer", key=KEY)

    original = registry.current("SB-15.1-a")
    checkpoint = _now()

    registry.amend(
        "SB-15.1-a",
        {"action": original.action.model_copy(update={"verb": "submit"})},
        by="A Named Officer",
        note="wording corrected",
    )

    was = registry.as_of(checkpoint)[0]
    assert was.action.verb == "report", "the past was rewritten"
    assert was.version == original.version
    assert registry.current("SB-15.1-a").action.verb == "submit"


def test_a_rejection_is_visible_only_after_it_happened():
    registry = RuleRegistry()
    registry.propose(_proposal("15.1"))
    before = _now()
    registry.reject("SB-15.1-a", by="A Named Officer", reason="not an obligation")

    assert registry.as_of(before)[0].status is RuleStatus.PROPOSED
    assert registry.as_of(_now())[0].status is RuleStatus.REJECTED


def test_a_naive_timestamp_is_read_as_utc():
    """A caller passing a bare datetime must not silently get a wrong answer."""
    registry = RuleRegistry()
    registry.propose(_proposal("15.1"))
    naive = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(minutes=1)
    assert len(registry.as_of(naive)) == 1


def test_the_far_future_matches_the_present():
    registry = RuleRegistry()
    registry.propose(_proposal("15.1"))
    registry.propose(_proposal("40.1"))
    registry.certify("SB-15.1-a", by="A Named Officer", key=KEY)

    later = _now() + _dt.timedelta(days=365)
    assert {o.id for o in registry.as_of(later)} == {
        o.id for o in registry.all_current()
    }


def test_the_replay_is_deterministic():
    registry = RuleRegistry()
    registry.propose(_proposal("15.1"))
    registry.propose(_proposal("40.1"))
    moment = _now()
    first = [o.id for o in registry.as_of(moment)]
    second = [o.id for o in registry.as_of(moment)]
    assert first == second == sorted(first)


# -------------------------------------------------------------- the corpus


@requires_corpus
def test_the_real_ledger_replays():
    """1,560 entries, every one with its own timestamp."""
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    entries = list(registry.ledger)
    assert entries

    first = min(e.at for e in entries)
    last = max(e.at for e in entries)

    before_everything = first - _dt.timedelta(seconds=1)
    assert registry.as_of(before_everything) == []

    at_the_end = registry.as_of(last)
    assert len(at_the_end) == len(registry.all_current())


@requires_corpus
def test_certifications_appear_only_after_they_were_made():
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    certified_now = [
        o for o in registry.all_current() if o.status is RuleStatus.CERTIFIED
    ]
    if not certified_now:
        pytest.skip("nothing has been certified in this store")

    earliest_signature = min(
        o.certification.certified_at for o in certified_now if o.certification
    )
    just_before = earliest_signature - _dt.timedelta(seconds=1)

    was = registry.as_of(just_before)
    assert not [o for o in was if o.status is RuleStatus.CERTIFIED], (
        "a certification is visible before it was made"
    )

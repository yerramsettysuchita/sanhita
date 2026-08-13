"""Company X as a first-class thing, and the review flow that was missing.

Ingestion produced candidates and discarded anything that did not name a rule,
so a real margin report could be read perfectly and never reach the engine. The
bridge is a person saying *this satisfies SB-40.1.8-a*, and these tests hold
both halves of that: the mapping works, and nothing crosses without it.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from sanhita.company import Company, IntermediaryType, ReviewQueue
from sanhita.execute.ingest import Candidate, Confidence


def _candidate(**kwargs) -> Candidate:
    base = dict(
        source_document="margin_july.pdf",
        page=7,
        excerpt="2026-03-31   RET-001   dispatched",
        occurred_on=_dt.date(2026, 3, 31),
        reference="RET-001",
        confidence=Confidence.PROBABLE,
        why="a date and a reference on one line",
    )
    base.update(kwargs)
    return Candidate(**base)


def _queue(tmp_path) -> ReviewQueue:
    return ReviewQueue.load(tmp_path / "review.json")


# ══════════════════════════════════════════════════════ the company ══


def test_a_company_is_more_than_a_name(tmp_path):
    firm = Company(name="ABC Securities")
    assert not firm.is_configured

    firm.processes = ["Daily margin reporting"]
    assert firm.is_configured


def test_a_company_round_trips_with_its_business_facts(tmp_path):
    firm = Company(
        name="ABC Securities",
        intermediary=IntermediaryType.STOCK_BROKER,
        registration="INZ000000000",
        business_facts={"Offers derivatives": True, "Holds client funds": False},
        processes=["Daily margin reporting", "Client onboarding"],
        systems=["Margin engine", "CRM"],
    )
    path = tmp_path / "company.json"
    firm.save(path)

    loaded = Company.load(path)

    assert loaded is not None
    assert loaded.name == "ABC Securities"
    assert loaded.business_facts["Offers derivatives"] is True
    assert loaded.processes == ["Daily margin reporting", "Client onboarding"]
    assert loaded.intermediary is IntermediaryType.STOCK_BROKER


def test_a_missing_or_corrupt_profile_returns_none_rather_than_raising(tmp_path):
    assert Company.load(tmp_path / "nothing.json") is None

    broken = tmp_path / "company.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert Company.load(broken) is None


def test_a_synthetic_profile_says_so(tmp_path):
    """A demo company must never be mistakable for a real firm."""
    firm = Company(name="Demo Stockbroker Ltd", synthetic=True)
    firm.save(tmp_path / "c.json")

    assert Company.load(tmp_path / "c.json").synthetic is True


# ═══════════════════════════════════════════════════ the review flow ══


def test_an_unreviewed_candidate_never_reaches_the_engine(tmp_path):
    """The boundary the whole module exists to hold."""
    queue = _queue(tmp_path)
    queue.add([_candidate()])

    store = queue.to_evidence("test")

    assert len(store) == 0, "nothing awaiting review may become evidence"
    assert queue.summary()["awaiting"] == 1


def test_mapping_a_candidate_turns_it_into_evidence(tmp_path):
    queue = _queue(tmp_path)
    item = queue.add([_candidate()])[0]

    queue.map_to(item.item_id, "SB-40.1.8-a", by="A Named Officer")
    store = queue.to_evidence("ABC Securities")

    assert len(store) == 1
    event = store.events[0]
    assert event.obligation_id == "SB-40.1.8-a"
    assert event.occurred_on == _dt.date(2026, 3, 31)


def test_provenance_survives_the_crossing(tmp_path):
    """A gap report cites the clause to the byte. The evidence must answer back.

    Before this, an event knew a date and a reference and nothing about which
    document, page or words produced it.
    """
    queue = _queue(tmp_path)
    item = queue.add([_candidate()])[0]
    queue.map_to(item.item_id, "SB-40.1.8-a", by="A Named Officer")

    event = queue.to_evidence("ABC Securities").events[0]

    assert event.source_document == "margin_july.pdf"
    assert event.source_page == 7
    assert "RET-001" in event.source_excerpt
    assert event.mapped_by == "A Named Officer"
    assert event.mapped_at is not None
    assert event.where() == "margin_july.pdf, page 7"


def test_a_document_that_names_the_rule_needs_no_confirmation(tmp_path):
    """A CSV stating its obligation should not make somebody retype it."""
    queue = _queue(tmp_path)
    queue.add(
        [_candidate(obligation_id="SB-40.1.8-a", confidence=Confidence.STATED)]
    )

    assert queue.summary()["awaiting"] == 0
    assert len(queue.to_evidence("x")) == 1
    assert queue.mapped()[0].mapped_by == "the document itself"


def test_a_dismissed_candidate_is_kept_and_excluded(tmp_path):
    """A queue that forgets its rejections cannot show who made them."""
    queue = _queue(tmp_path)
    item = queue.add([_candidate()])[0]

    queue.dismiss(item.item_id, by="A Named Officer", reason="a header row")

    assert len(queue.to_evidence("x")) == 0
    assert queue.summary()["dismissed"] == 1
    assert queue.dismissed()[0].dismissed_reason == "a header row"
    assert queue.dismissed()[0].mapped_by == "A Named Officer"


def test_a_candidate_with_no_date_cannot_be_mapped(tmp_path):
    """There is no occasion for a rule to be checked against."""
    queue = _queue(tmp_path)
    item = queue.add([_candidate(occurred_on=None)])[0]

    with pytest.raises(ValueError, match="no date"):
        queue.map_to(item.item_id, "SB-40.1.8-a", by="officer")


def test_mapping_to_nothing_is_refused(tmp_path):
    queue = _queue(tmp_path)
    item = queue.add([_candidate()])[0]

    with pytest.raises(ValueError, match="not mapping"):
        queue.map_to(item.item_id, "   ", by="officer")


def test_a_dismissal_can_be_reversed_by_mapping_it(tmp_path):
    queue = _queue(tmp_path)
    item = queue.add([_candidate()])[0]
    queue.dismiss(item.item_id, by="officer")

    queue.map_to(item.item_id, "SB-40.1.8-a", by="a second officer")

    assert not queue.items[item.item_id].dismissed
    assert len(queue.to_evidence("x")) == 1


def test_the_queue_survives_a_round_trip(tmp_path):
    queue = _queue(tmp_path)
    first, second = queue.add([_candidate(), _candidate(page=9)])
    queue.map_to(first.item_id, "SB-40.1.8-a", by="officer")
    queue.dismiss(second.item_id, by="officer", reason="duplicate")
    queue.save()

    reloaded = ReviewQueue.load(tmp_path / "review.json")

    assert reloaded.summary() == queue.summary()
    assert reloaded.items[first.item_id].mapped_obligation == "SB-40.1.8-a"
    assert reloaded.items[second.item_id].dismissed_reason == "duplicate"
    assert reloaded.items[first.item_id].candidate.page == 7


def test_the_queue_groups_by_document(tmp_path):
    queue = _queue(tmp_path)
    queue.add(
        [
            _candidate(source_document="march.pdf"),
            _candidate(source_document="march.pdf"),
            _candidate(source_document="april.xlsx"),
        ]
    )

    assert queue.documents() == {"april.xlsx": 1, "march.pdf": 2}


def test_probable_candidates_are_offered_first(tmp_path):
    """A reviewer's time goes furthest on the ones most likely to be real."""
    queue = _queue(tmp_path)
    queue.add(
        [
            _candidate(confidence=Confidence.UNRESOLVED),
            _candidate(confidence=Confidence.PROBABLE),
        ]
    )

    assert queue.awaiting()[0].candidate.confidence is Confidence.PROBABLE


def test_an_old_evidence_store_without_provenance_still_loads():
    """Stores written before provenance existed must not fail to read."""
    from sanhita.execute.evidence import ComplianceEvent

    event = ComplianceEvent.from_json(
        {
            "id": "EV-1",
            "obligation_id": "SB-1-a",
            "entity": "X",
            "occurred_on": "2026-03-31",
            "artifact_type": "report",
        }
    )

    assert event.source_document == ""
    assert event.where() == ""

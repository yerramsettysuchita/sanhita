"""Known unknowns, the calendar, the receipt and the policy export.

Each of these makes a claim, and each claim has a limit. The tests are mostly
about the limits, because a feature that overstates what it did is worse than
one that does less and says so.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json

import pytest

from tests.conftest import requires_corpus

from sanhita.analyse.calendar import build_schedule
from sanhita.analyse.receipt import build_receipt, verify_receipt
from sanhita.analyse.rego import to_rego
from sanhita.analyse.uncompiled import find_uncompiled
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

JAN = _dt.date(2026, 1, 1)


def _rule(
    clause_id: str = "40.1.8",
    *,
    kind: DeadlineKind = DeadlineKind.END_OF_PERIOD,
    period: str | None = "MONTH",
    days: int | None = None,
    count: DayCount = DayCount.CALENDAR,
    modality: Modality = Modality.MUST,
    status: RuleStatus = RuleStatus.CERTIFIED,
    evidence: bool = True,
) -> Obligation:
    text = f"Clause {clause_id}: the broker shall report the thing."
    deadline = None
    if kind is DeadlineKind.END_OF_PERIOD:
        deadline = Deadline(kind=kind, period=period)
    elif kind is DeadlineKind.RELATIVE:
        deadline = Deadline(
            kind=kind, offset_days=days, business_days=count, anchor_event="trade.date"
        )
    elif kind is DeadlineKind.ON_DEMAND:
        deadline = Deadline(kind=kind)

    return Obligation(
        id=f"SB-{clause_id}-a",
        actor=Actor.STOCK_BROKER,
        modality=modality,
        action=Action(verb="report", object="the short collection"),
        # The IR requires a SCHEDULE trigger to state its recurrence, correctly.
        trigger=Trigger(
            kind=TriggerKind.SCHEDULE,
            expression="monthly",
            recurrence="FREQ=MONTHLY",
        ),
        deadline=deadline,
        evidence=[EvidenceReq(artifact_type="report")] if evidence else [],
        source=SourceAnchor(
            circular_id="SB-2025-06-17",
            section=clause_id.split(".")[0],
            clause_id=clause_id,
            page=95,
            char_span=(0, len(text)),
            verbatim_text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
        confidence=0.9,
        status=status,
        certification=Certification(
            certified_by="A Named Officer",
            certified_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
            signature="f" * 64,
        )
        if status is RuleStatus.CERTIFIED
        else None,
    )


# ══════════════════════════════════════════════════════════ known unknowns ══


@requires_corpus
def test_the_missing_list_is_the_other_side_of_coverage(parsed):
    from sanhita.cli_compile import _load_registry

    report = find_uncompiled(parsed, _load_registry().all_current())

    assert report.duty_bearing > 1000
    assert report.missing, "the corpus is known to have uncompiled duties"
    assert report.with_a_rule + len(report.missing) + len(report.rejected_away) == (
        report.duty_bearing
    )


@requires_corpus
def test_every_missing_clause_can_be_looked_up(parsed):
    from sanhita.cli_compile import _load_registry

    report = find_uncompiled(parsed, _load_registry().all_current())
    for item in report.missing[:20]:
        assert parsed.get(item.clause_id) is not None
        assert item.page > 0
        assert item.excerpt.strip()


@requires_corpus
def test_the_caveats_admit_the_classifier_is_imperfect(parsed):
    """Otherwise this reads as a defect list rather than a reading list."""
    from sanhita.cli_compile import _load_registry

    report = find_uncompiled(parsed, _load_registry().all_current())
    joined = " ".join(report.caveats())
    assert "imperfect" in joined
    assert "never sees the extractor" in joined


# ═════════════════════════════════════════════════════════════════ calendar ══


def test_a_monthly_duty_lands_on_each_month_end():
    schedule = build_schedule([_rule(period="MONTH")], start=JAN, days=90)
    dates = sorted({d.on for d in schedule.due})
    assert dates == [
        _dt.date(2026, 1, 31),
        _dt.date(2026, 2, 28),
        _dt.date(2026, 3, 31),
    ]


def test_a_daily_duty_is_listed_once_not_on_every_date():
    """Twenty daily duties across ninety days is eighteen hundred rows of noise."""
    schedule = build_schedule([_rule(period="DAY")], start=JAN, days=90)
    assert schedule.due == []
    assert len(schedule.daily) == 1
    assert any("every single day" in c for c in schedule.caveats())


def test_rules_from_a_flattened_table_never_reach_the_calendar():
    """Clause 62.63 yields fragments like "password protected", not duties."""
    from sanhita.analyse.conflicts import TABLE_LIKE_CHARS

    fragment = _rule("62.63", period="MONTH")
    long_text = "x" * (TABLE_LIKE_CHARS + 1)
    fragment = fragment.model_copy(
        update={
            "source": fragment.source.model_copy(
                update={
                    "verbatim_text": long_text,
                    "sha256": hashlib.sha256(long_text.encode()).hexdigest(),
                    "char_span": (0, len(long_text)),
                }
            )
        }
    )
    schedule = build_schedule([fragment], start=JAN, days=90)
    assert schedule.due == []
    assert schedule.excluded_rules == 1
    assert "62.63" in schedule.excluded_clauses


def test_a_quarterly_duty_lands_only_on_quarter_ends():
    schedule = build_schedule([_rule(period="QUARTER")], start=JAN, days=200)
    assert sorted({d.on for d in schedule.due}) == [
        _dt.date(2026, 3, 31),
        _dt.date(2026, 6, 30),
    ]


def test_an_event_driven_duty_is_never_given_a_date():
    """Inventing one would be inventing a deadline."""
    schedule = build_schedule(
        [_rule(kind=DeadlineKind.RELATIVE, days=5)], start=JAN, days=90
    )
    assert schedule.due == []
    assert len(schedule.event_driven) == 1
    assert any("inventing a deadline" in c for c in schedule.caveats())


def test_a_proposed_rule_never_appears_on_the_calendar():
    schedule = build_schedule(
        [_rule(status=RuleStatus.PROPOSED)], start=JAN, days=90
    )
    assert schedule.due == []
    assert schedule.certified == 0


def test_a_permission_is_not_scheduled():
    schedule = build_schedule([_rule(modality=Modality.MAY)], start=JAN, days=90)
    assert schedule.due == []


def test_every_entry_cites_its_clause_and_its_officer():
    schedule = build_schedule([_rule(period="MONTH")], start=JAN, days=40)
    assert schedule.due
    for item in schedule.due:
        assert item.clause_id and item.page > 0
        assert item.certified_by == "A Named Officer"


def test_the_calendar_is_deterministic():
    rules = [_rule(period="MONTH"), _rule("15.1", period="QUARTER")]
    a = build_schedule(rules, start=JAN, days=120).to_json()
    b = build_schedule(rules, start=JAN, days=120).to_json()
    assert a == b


# ══════════════════════════════════════════════════════════════════ receipt ══


@requires_corpus
def test_a_receipt_records_the_inputs_and_the_outputs(corpus_pdf, parsed):
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    receipt = build_receipt(
        pdf=corpus_pdf,
        tree=parsed,
        obligations=registry.all_current(),
        ledger_head=registry.ledger.head,
        key="a-test-key",
    )

    assert receipt.source_sha256 == hashlib.sha256(corpus_pdf.read_bytes()).hexdigest()
    assert receipt.tree_fingerprint == parsed.fingerprint()
    assert receipt.rules_total == len(registry.all_current())
    assert len(receipt.rulebook_sha256) == 64
    assert receipt.signature


@requires_corpus
def test_a_receipt_signature_verifies(corpus_pdf, parsed):
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    receipt = build_receipt(
        pdf=corpus_pdf,
        tree=parsed,
        obligations=registry.all_current(),
        ledger_head=registry.ledger.head,
        key="a-test-key",
    )
    ok, why = verify_receipt(receipt.to_json(), "a-test-key")
    assert ok, why


@requires_corpus
def test_an_edited_receipt_fails_verification(corpus_pdf, parsed):
    """The whole point: it cannot be edited after the fact."""
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    receipt = build_receipt(
        pdf=corpus_pdf,
        tree=parsed,
        obligations=registry.all_current(),
        ledger_head=registry.ledger.head,
        key="a-test-key",
    )
    raw = json.loads(json.dumps(receipt.to_json()))
    raw["rules_certified"] = raw["rules_certified"] + 500

    ok, why = verify_receipt(raw, "a-test-key")
    assert not ok
    assert "edited" in why


@requires_corpus
def test_a_receipt_signed_with_another_key_fails(corpus_pdf, parsed):
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    receipt = build_receipt(
        pdf=corpus_pdf,
        tree=parsed,
        obligations=registry.all_current(),
        ledger_head=registry.ledger.head,
        key="one-key",
    )
    ok, _ = verify_receipt(receipt.to_json(), "another-key")
    assert not ok


@requires_corpus
def test_an_unsigned_receipt_says_so(corpus_pdf, parsed):
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    receipt = build_receipt(
        pdf=corpus_pdf,
        tree=parsed,
        obligations=registry.all_current(),
        ledger_head=registry.ledger.head,
    )
    assert receipt.signature is None
    ok, why = verify_receipt(receipt.to_json(), "a-key")
    assert not ok and "no signature" in why


@requires_corpus
def test_the_receipt_tells_a_sceptic_how_to_check_it(corpus_pdf, parsed):
    from sanhita.cli_compile import _load_registry

    registry = _load_registry()
    receipt = build_receipt(
        pdf=corpus_pdf,
        tree=parsed,
        obligations=registry.all_current(),
        ledger_head=registry.ledger.head,
    )
    steps = receipt.how_to_check()
    assert len(steps) >= 3
    assert any("sanhita ingest" in s for s in steps)


# ═════════════════════════════════════════════════════════════ rego export ══


def test_a_clean_rule_becomes_policy():
    export = to_rego([_rule(kind=DeadlineKind.RELATIVE, days=5)])
    assert export.translated == 1
    assert "package sanhita.obligations" in export.policy
    assert "SB-40.1.8-a" in export.policy
    assert "deny contains finding" in export.policy


def test_an_unresolved_day_count_is_refused_not_guessed():
    export = to_rego(
        [_rule(kind=DeadlineKind.RELATIVE, days=5, count=DayCount.UNSPECIFIED)]
    )
    assert export.translated == 0
    assert "will not choose" in export.refused[0].reason


def test_an_on_demand_rule_is_refused():
    export = to_rego([_rule(kind=DeadlineKind.ON_DEMAND)])
    assert export.translated == 0
    assert "demand" in export.refused[0].reason


def test_a_permission_is_refused():
    export = to_rego([_rule(kind=DeadlineKind.RELATIVE, days=5, modality=Modality.MAY)])
    assert export.translated == 0


def test_a_proposed_rule_never_becomes_policy():
    """A policy engine acting on an unsigned rule is the failure this product exists to stop."""
    export = to_rego(
        [_rule(kind=DeadlineKind.RELATIVE, days=5, status=RuleStatus.PROPOSED)]
    )
    assert export.translated == 0
    assert export.certified == 0
    assert "SB-40.1.8-a" not in export.policy


def test_what_was_refused_is_written_into_the_file():
    """Anyone deploying this must be able to see the gap without asking us."""
    export = to_rego(
        [
            _rule("15.1", kind=DeadlineKind.RELATIVE, days=5),
            _rule("40.1", kind=DeadlineKind.ON_DEMAND),
        ]
    )
    assert export.translated == 1
    assert "40.1" in export.policy
    assert "NOT enforced by this policy" in export.policy


def test_every_policy_rule_carries_its_clause_and_signature():
    export = to_rego([_rule(kind=DeadlineKind.RELATIVE, days=5)])
    assert '"clause":      "40.1.8"' in export.policy
    assert '"certified_by":"A Named Officer"' in export.policy
    assert "f" * 64 in export.policy


def test_quotes_in_a_requirement_cannot_break_the_policy():
    rule = _rule(kind=DeadlineKind.RELATIVE, days=5)
    hostile = rule.model_copy(
        update={"action": Action(verb="report", object='the "special" case')}
    )
    export = to_rego([hostile])
    assert '\\"special\\"' in export.policy


@requires_corpus
def test_the_real_rulebook_exports_a_stated_subset():
    from sanhita.cli_compile import _load_registry

    export = to_rego(_load_registry().all_current())
    assert export.certified > 0
    assert 0 < export.coverage <= 1
    # Whatever the number, the file must own it.
    assert f"of {export.certified} certified rules are here" in export.policy

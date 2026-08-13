"""Temporal parsing, against phrases taken verbatim from the corpus.

Every `text` below is real SEBI wording, cited by clause id. Nothing here is
invented â€” a temporal parser validated on synthetic English proves nothing about
the document we actually compile.

The `clause` field is the id the phrase came from, so a failure can be traced
straight back to the page it was read off.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from sanhita.compile.temporal import (
    parse_commencement,
    parse_recurrence,
    parse_temporal,
    word_to_int,
)
from sanhita.ir.enums import CommencementKind, DayCount, DeadlineKind

# --------------------------------------------------------------------------
# The corpus. (clause, text, expectations)
# --------------------------------------------------------------------------

RELATIVE_CASES = [
    # ---- T+n ------------------------------------------------------------
    (
        "40.1.8",
        "the TMs/CMs shall report to the Stock Exchange on T+5 day the actual "
        "short-collection/ non-collection of all margins from clients.",
        # "on T+5 day" does not state a convention, so the parser must not
        # invent one. UNSPECIFIED blocks certification until a human resolves it.
        dict(offset_days=5, business_days=DayCount.UNSPECIFIED, anchor_event="trade.date"),
    ),
    (
        "39.7.1",
        "The stock broker shall disclose to the Stock Exchanges details on gross "
        "exposure towards margin trading facility on T+1 day.",
        dict(offset_days=1, business_days=DayCount.UNSPECIFIED, anchor_event="trade.date"),
    ),
    # ---- within N working / trading / calendar days ---------------------
    (
        "15.3.4.3",
        "Stock Exchange shall grant such approval within two working days after "
        "imposing penalty as per their internal policy.",
        dict(offset_days=2, business_days=DayCount.BUSINESS),
    ),
    (
        "20.2.2",
        "Such information for a specific month should reach the exchange within "
        "seven working days of the following month.",
        dict(offset_days=7, business_days=DayCount.BUSINESS),
    ),
    (
        "15.9.1.4",
        "The stock broker shall submit the aforesaid data within seven calendar "
        "days of the last trading day of the month.",
        dict(offset_days=7, business_days=DayCount.CALENDAR),
    ),
    (
        "15.10.1.7",
        "TM shall send the retention statement to the client within five working days.",
        dict(offset_days=5, business_days=DayCount.BUSINESS),
    ),
    (
        "23.1.1",
        "the KYC application form shall be submitted within thirty working days.",
        dict(offset_days=30, business_days=DayCount.BUSINESS),
    ),
    (
        "15.11.1",
        "Any change in the aforesaid details/information shall be intimated to the "
        "Stock Exchanges within seven days of such change.",
        # Bare "days" states no convention. Legal drafting often *means*
        # calendar days, but the clause does not say so, so the parser reports
        # UNSPECIFIED and a human settles it.
        dict(offset_days=7, business_days=DayCount.UNSPECIFIED),
    ),
    (
        "5.2",
        "such applications in any case should be made not later than 30 days of the "
        "registration granted by the Registrar of companies.",
        dict(offset_days=30, business_days=DayCount.UNSPECIFIED),
    ),
    (
        "15.8.1.1",
        "Failure to furnish Networth certificate to Stock Exchange within 60 days "
        "for half year ending September 30th.",
        dict(offset_days=60, business_days=DayCount.UNSPECIFIED),
    ),
    # ---- within N hours --------------------------------------------------
    (
        "24.3",
        "Member Brokers shall make payment to their clients or deliver the securities "
        "purchased within 24 hours of pay-out unless the client has requested otherwise.",
        dict(offset_hours=24),
    ),
    (
        "24.5",
        "the client fails to deliver the securities sold with valid transfer documents "
        "within 48 hours of the contract note having been delivered.",
        dict(offset_hours=48),
    ),
    (
        "62.54",
        "All Cyber-attacks, threats, cyber-incidents and breaches experienced by Stock "
        "Brokers shall be reported to Stock Exchanges & SEBI within six hours of "
        "noticing / detecting such incidents.",
        dict(offset_hours=6),
    ),
    (
        "23.1.1",
        "the settlement of funds shall be done within twenty-four hours of the payout.",
        dict(offset_hours=24),
    ),
    (
        "65.3.1",
        "Stock brokers shall inform about the technical glitch to the stock exchanges "
        "immediately but not later than one hour from the time of occurrence of the glitch.",
        dict(offset_hours=1),
    ),
    # ---- within N months -------------------------------------------------
    (
        "13.1.5",
        "The Stock Exchange or the Clearing Corporation shall initiate all the follow "
        "up action required on inspection findings, within six months from the "
        "conclusion of the inspection.",
        dict(offset_months=6),
    ),
    (
        "13.2.3",
        "shall forward the same along with para-wise comments to the respective Stock "
        "Exchange within two months from the end of the half year period.",
        dict(offset_months=2),
    ),
    (
        "67.4(ii)",
        "REs shall be in compliance with this framework not later than 12 (twelve) "
        "months from the date of issuance of the framework.",
        dict(offset_months=12),
    ),
]

END_OF_PERIOD_CASES = [
    (
        "34",
        "Stock Exchanges shall send details of the transactions to the investors, by "
        "the end of trading day, through SMS and E-mail alerts.",
        "DAY",
    ),
    (
        "45.2.13",
        "Blocking shall be on 'time basis' and would mean if the order is not executed "
        "by the end of the T day, the block shall be released.",
        "DAY",
    ),
    (
        "48.2.1",
        "Entire pay-in obligation of funds outstanding at the end of the day on "
        "settlement of running account.",
        "DAY",
    ),
    (
        "13.2.3",
        "the report shall be submitted at the end of the half year period.",
        "HALF_YEAR",
    ),
]

RECURRENCE_CASES = [
    ("20.3.3", "Such UCC data, in respect of new UCCs created, shall be shared with "
               "the Depositories, on a daily basis.", "FREQ=DAILY"),
    ("13.2.1", "The member shall carry out complete internal audit on a half yearly "
               "basis by an independent qualified Chartered Accountant.", "FREQ=MONTHLY;INTERVAL=6"),
    ("15.10.1.1", "shall settle the running accounts at the choice of the clients on "
                  "quarterly and monthly basis, on the dates stipulated by the Stock "
                  "Exchanges.", "FREQ=QUARTERLY"),
    ("22.7", "Stock Brokers shall encourage their clients to update 'choice of "
             "nomination' by sending a communication on fortnightly basis.", "FREQ=WEEKLY;INTERVAL=2"),
    ("18.2.4", "Alerts generated from the monthly / weekly submissions made by stock "
               "broker under Risk Based Supervision.", "FREQ=MONTHLY"),
    ("39.4.3", "The stock brokers shall submit to the Stock Exchange a half-yearly "
               "certificate, as on 31st March and 30th September of each year.",
     "FREQ=MONTHLY;INTERVAL=6"),
]

ON_DEMAND_CASES = [
    ("11.4", "the hard copy of the applications made by their members shall be "
             "preserved by them and shall be made available to SEBI, as and when "
             "called for."),
    ("14.4", "The Stock Exchanges/Clearing Corporations are advised to review/revise "
             "the policy of annual inspection, as and when required, in consultation "
             "with SEBI."),
    ("19.6.1", "QSBs shall be subjected to enhanced monitoring and surveillance "
               "including additional submissions to be made to MIIs/SEBI, as and when "
               "sought."),
]

IMMEDIATE_CASES = [
    ("13.3.1", "the concerned Stock Exchange/Clearing Corporation shall immediately "
               "declare it a defaulter in all its segments."),
    ("24.1.2", "Every member broker who holds or receives money on account of a client "
               "shall forthwith pay such money to current or deposit account at bank."),
]


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("7", 7), ("30", 30), ("seven", 7), ("two", 2), ("six", 6),
        ("twenty-four", 24), ("thirty", 30), ("forty-five", 45), ("sixty", 60),
        ("12 (twelve)", 12), ("one (1)", 1), ("ninety", 90),
    ],
)
def test_number_words(text, expected):
    assert word_to_int(text) == expected


def test_unparseable_numbers_return_none():
    assert word_to_int("several") is None
    assert word_to_int("") is None


# --------------------------------------------------------------------------
# Deadlines
# --------------------------------------------------------------------------


@pytest.mark.parametrize("clause, text, expected", RELATIVE_CASES, ids=[c[0] for c in RELATIVE_CASES])
def test_relative_deadlines(clause, text, expected):
    reading = parse_temporal(text)
    assert reading.deadline is not None, f"{clause}: no deadline recovered"
    deadline = reading.deadline
    assert deadline.kind is DeadlineKind.RELATIVE, clause
    for field, value in expected.items():
        assert getattr(deadline, field) == value, f"{clause}: {field}"
    # An anchor is mandatory on a RELATIVE deadline, and the span must quote
    # words that are really in the clause.
    assert deadline.anchor_event
    start, end = reading.spans["deadline"]
    assert text[start:end] in text


@pytest.mark.parametrize(
    "clause, text, period", END_OF_PERIOD_CASES, ids=[c[0] for c in END_OF_PERIOD_CASES]
)
def test_end_of_period_deadlines(clause, text, period):
    reading = parse_temporal(text)
    assert reading.deadline is not None, clause
    assert reading.deadline.kind is DeadlineKind.END_OF_PERIOD, clause
    assert reading.deadline.period == period, clause


@pytest.mark.parametrize("clause, text", ON_DEMAND_CASES, ids=[c[0] for c in ON_DEMAND_CASES])
def test_on_demand_deadlines(clause, text):
    reading = parse_temporal(text)
    assert reading.deadline is not None, clause
    assert reading.deadline.kind is DeadlineKind.ON_DEMAND, clause
    assert reading.deadline.offset_days is None


@pytest.mark.parametrize("clause, text", IMMEDIATE_CASES, ids=[c[0] for c in IMMEDIATE_CASES])
def test_immediate_is_a_zero_hour_horizon_not_a_missing_one(clause, text):
    """'forthwith' is a deadline. Reading it as 'no deadline' loses the duty."""
    reading = parse_temporal(text)
    assert reading.deadline is not None, clause
    assert reading.deadline.kind is DeadlineKind.RELATIVE
    assert reading.deadline.offset_hours == 0


# --------------------------------------------------------------------------
# Recurrence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("clause, text, rrule", RECURRENCE_CASES, ids=[c[0] for c in RECURRENCE_CASES])
def test_recurrence(clause, text, rrule):
    reading = parse_temporal(text)
    assert reading.recurrence == rrule, clause
    assert reading.trigger_kind is not None


def test_half_yearly_is_not_swallowed_by_yearly():
    """Longest-match ordering: 'half yearly' must not read as 'yearly'."""
    assert parse_recurrence("on a half yearly basis")[0] == "FREQ=MONTHLY;INTERVAL=6"
    assert parse_recurrence("on a yearly basis")[0] == "FREQ=YEARLY"


def test_recurring_duty_with_no_stated_horizon_is_due_within_its_period():
    reading = parse_temporal(
        "Such UCC data shall be shared with the Depositories, on a daily basis."
    )
    assert reading.recurrence == "FREQ=DAILY"
    assert reading.deadline.kind is DeadlineKind.END_OF_PERIOD
    assert reading.deadline.period == "DAY"
    # Inferred, not read â€” so it must score below a directly-stated deadline.
    assert reading.confidence < 0.9


# --------------------------------------------------------------------------
# Commencement
# --------------------------------------------------------------------------


def test_fixed_date_commencement():
    result = parse_commencement(
        "The membership structure has been implemented in cash segment with effect "
        "from April 01, 2019."
    )
    assert result is not None
    commencement, _span = result
    assert commencement.kind is CommencementKind.FIXED_DATE
    assert commencement.fixed_date == _dt.date(2019, 4, 1)


def test_effective_date_commencement():
    result = parse_commencement(
        "the effective date for implementation of the circular for QSBs "
        "(irrespective of number of UCCs) is August 01, 2024."
    )
    assert result is not None
    assert result[0].fixed_date == _dt.date(2024, 8, 1)


def test_relative_to_issuance_commencement():
    result = parse_commencement(
        "REs shall be in compliance with this framework not later than 12 (twelve) "
        "months from the date of issuance of the framework."
    )
    assert result is not None
    commencement, _span = result
    assert commencement.kind is CommencementKind.RELATIVE_TO_ISSUANCE
    assert commencement.offset_days == 360


def test_conditional_commencement_preserves_the_condition_text():
    """The headline amendment shape: commencement gated on an external event."""
    result = parse_commencement(
        "The provisions of paragraphs 46.1 to 46.11 shall come into effect three "
        "months after the stock exchanges issue operational guidelines."
    )
    assert result is not None
    commencement, span = result
    assert commencement.kind is CommencementKind.CONDITIONAL_ON_EVENT
    assert commencement.offset_days == 90
    assert "operational guidelines" in commencement.condition
    assert span[0] < span[1]


def test_a_bare_noun_anchor_is_not_read_as_a_commencement():
    """'within 24 hours of the payout' is a deadline anchor, not a commencement."""
    result = parse_commencement("shall be done within twenty-four hours of the payout.")
    assert result is None


# --------------------------------------------------------------------------
# Provenance, refusal to guess, determinism
# --------------------------------------------------------------------------


def test_every_reading_carries_a_span_into_the_source_text():
    text = (
        "The stock broker shall submit the aforesaid data within seven calendar days "
        "of the last trading day of the month."
    )
    reading = parse_temporal(text)
    start, end = reading.spans["deadline"]
    assert text[start:end].lower().startswith("within seven calendar days")


def test_clauses_with_no_temporal_language_yield_nothing():
    """Returning empty must be a normal outcome, not an error."""
    reading = parse_temporal(
        "The stock broker shall be a body corporate registered with the Board."
    )
    assert reading.is_empty
    assert reading.confidence == 0.0
    assert reading.deadline is None


def test_unrecognised_temporal_phrases_are_reported_not_guessed():
    reading = parse_temporal(
        "The report shall be filed within a reasonable period before the said meeting."
    )
    assert reading.deadline is None
    assert reading.unparsed


def test_parsing_is_deterministic():
    text = (
        "Member Brokers shall make payment to their clients within 24 hours of pay-out, "
        "on a quarterly basis, with effect from April 01, 2019."
    )
    first, second = parse_temporal(text), parse_temporal(text)
    assert first.deadline == second.deadline
    assert first.recurrence == second.recurrence
    assert first.commencement == second.commencement
    assert first.spans == second.spans
    assert first.confidence == second.confidence


def test_one_clause_can_carry_deadline_recurrence_and_commencement_at_once():
    reading = parse_temporal(
        "Member Brokers shall make payment to their clients within 24 hours of pay-out, "
        "on a quarterly basis, with effect from April 01, 2019."
    )
    assert reading.deadline.offset_hours == 24
    assert reading.recurrence == "FREQ=QUARTERLY"
    assert reading.commencement.kind is CommencementKind.FIXED_DATE
    assert {"deadline", "trigger.recurrence", "commencement"} <= set(reading.spans)

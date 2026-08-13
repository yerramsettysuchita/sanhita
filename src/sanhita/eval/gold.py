"""The hand-labelled gold set: 40 clauses, 15 of them carrying no obligation.

Sampled with a fixed seed (20260804) across the numbered body of the
stock-broker master circular, stratified so that roughly a third of the set
carries no duty at all. Negative examples matter more than positive ones here:
an extractor is far more likely to fail by finding obligations everywhere than
by missing one, and a gold set of only real duties would never catch that.

Labels were assigned by reading each clause, not by running the extractor —
`auto` classifications were deliberately ignored while labelling, and two of
them turned out to be wrong (4.3.2 and 45.1), which is exactly what a gold set
is for.

Each label records only what a careful reader can defend from the clause's own
words. `actor=None` marks a clause whose duty-bearer is genuinely ambiguous;
those are excluded from the actor score rather than being guessed at.

REVIEW STATUS: awaiting sign-off. These are one annotator's labels.
"""

from __future__ import annotations

from dataclasses import dataclass

from sanhita.ir.enums import Actor, DeadlineKind, Modality

__all__ = ["GOLD_SET", "GoldLabel", "gold_by_clause"]

SAMPLE_SEED = 20260804
GOLD_VERSION = "gold-1.0.0"


@dataclass(frozen=True, slots=True)
class GoldLabel:
    """What a careful human says about one clause."""

    clause_id: str
    page: int
    has_obligation: bool
    #: Best-supported reading of the clause's primary duty. None when
    #: `has_obligation` is False, or when the field is genuinely ambiguous.
    actor: Actor | None = None
    modality: Modality | None = None
    deadline_kind: DeadlineKind | None = None
    #: Whether a compliance officer would expect at least one evidence artifact.
    #: False for prohibitions and permissions, which produce nothing to file.
    expects_evidence: bool = False
    note: str = ""


GOLD_SET: list[GoldLabel] = [
    # ---------------------------------------------------------- no obligation
    GoldLabel("5", 12, False, note="section heading"),
    GoldLabel("20", 59, False, note="section heading"),
    GoldLabel("15.5", 27, False, note="clause-numbered heading, no verb"),
    GoldLabel("15.6", 27, False, note="clause-numbered heading, no verb"),
    GoldLabel("19.5.4", 53, False, note="heading, 'Framework for orderly winding down:'"),
    GoldLabel("18.2.4(e)", 45, False, note="list item naming an alert type"),
    GoldLabel("19.6.3(vii)", 57, False, note="sentence fragment in a list"),
    GoldLabel("59.18(v)", 157, False, note="sentence fragment in a list"),
    GoldLabel("19.7", 57, False, note="recital introducing a table of dates"),
    GoldLabel("42.12", 102, False, note="lead-in: 'the framework ... is provided below:'"),
    GoldLabel("69.3", 190, False, note="recital: 'the following has been decided:'"),
    GoldLabel(
        "38.4", 88, False,
        note="deeming provision — says what will NOT be treated as a breach",
    ),
    GoldLabel(
        "36.10.1", 85, False,
        note="list item under a separate 'shall ensure' lead-in; no deontic verb of its own",
    ),
    GoldLabel(
        "45.1", 114, False,
        note="describes what happens to shares in the mechanism; commands no named party. "
             "The classifier calls this obligation-bearing — a known false positive.",
    ),
    GoldLabel(
        "57.47", 146, False,
        note="liability disclaimer ('shall not be liable'), not a duty. Hard negative: "
             "contains 'shall not' but imposes nothing.",
    ),
    # ------------------------------------------------------- carries a duty
    GoldLabel(
        "4.3.2", 12, True, Actor.STOCK_BROKER, Modality.MUST, None, True,
        note="'will have to individually or jointly hold at least 40%'. Labelled "
             "when 'will have to' was outside the deontic vocabulary and the "
             "classifier missed it. The classifier now agrees, so this is no "
             "longer a disagreement.",
    ),
    GoldLabel("7.2.3", 14, True, Actor.CLEARING_MEMBER, Modality.MUST, None, True,
              note="'the entity shall follow the procedure as prescribed'"),
    GoldLabel("13.3.3", 20, True, Actor.STOCK_EXCHANGE, Modality.MUST, None, True,
              note="'shall take appropriate action against the associates'"),
    GoldLabel("15.4.3", 25, True, Actor.STOCK_EXCHANGE, Modality.MUST, None, True,
              note="'shall ensure the following:' — duty carried by the lead-in"),
    GoldLabel(
        "15.9.1", 33, True, Actor.STOCK_EXCHANGE, Modality.MUST,
        DeadlineKind.END_OF_PERIOD, True,
        note="'shall put in place a mechanism ... upload ... on a monthly basis'",
    ),
    GoldLabel("17.2(vii)", 40, True, Actor.STOCK_BROKER, Modality.MUST, None, True,
              note="'SBs/TMs shall obtain SOC-II compliance from vendors'"),
    GoldLabel("17.2(viii)", 40, True, Actor.STOCK_EXCHANGE, Modality.MUST, None, True,
              note="'shall define standardized template for the system audit report'"),
    GoldLabel("19.5.2.3", 51, True, Actor.STOCK_BROKER, Modality.MUST, None, True,
              note="'The risk management framework shall have measures for ...'"),
    GoldLabel(
        "19.5.5.5", 54, True, Actor.STOCK_BROKER, Modality.MUST,
        DeadlineKind.END_OF_PERIOD, True,
        note="'shall submit a quarterly report to the BoD of QSB'",
    ),
    GoldLabel("19.5.5.7", 55, True, Actor.STOCK_BROKER, Modality.SHOULD, None, True,
              note="'The QSB should have well-defined and documented processes'"),
    GoldLabel("19.6.4", 57, True, Actor.STOCK_EXCHANGE, Modality.MUST, None, True,
              note="'shall take necessary steps to ensure that the same is corrected'"),
    GoldLabel("30.3", 73, True, Actor.STOCK_BROKER, Modality.MUST, None, True,
              note="'shall be required to disclose this to his clients'"),
    GoldLabel("33.2", 74, True, Actor.STOCK_BROKER, Modality.MAY, None, False,
              note="'may appoint one or more authorised person(s)' — a permission"),
    GoldLabel("36.8.1.7", 84, True, Actor.STOCK_BROKER, Modality.SHOULD, None, True,
              note="'revocation requests should be dated and time stamped by the brokers'"),
    GoldLabel("38.1", 87, True, Actor.STOCK_EXCHANGE, Modality.MAY, None, False,
              note="'may allow modifications of client codes' — a permission"),
    GoldLabel("45.2.6", 115, True, Actor.STOCK_BROKER, Modality.MUST_NOT, None, False,
              note="'TM shall not transfer the securities to any other pool account'"),
    GoldLabel("54.4.2", 134, True, Actor.STOCK_BROKER, Modality.SHOULD, None, True,
              note="'should be accessible from both systems' — actor implied, not named"),
    GoldLabel("60.2.1(ii)", 159, True, Actor.STOCK_EXCHANGE, Modality.MUST, None, True,
              note="'shall mandate a minimum time period for such testing'"),
    GoldLabel(
        "60.5.3", 163, True, Actor.STOCK_EXCHANGE, Modality.MUST,
        DeadlineKind.RELATIVE, True,
        note="'shall provide reasons in writing ... within a period of fifteen working days'",
    ),
    GoldLabel("62.26", 168, True, Actor.STOCK_BROKER, Modality.SHOULD, None, True,
              note="'should ensure that the perimeter of the critical equipment room ...'"),
    GoldLabel("62.32", 169, True, Actor.STOCK_BROKER, Modality.SHOULD, None, True,
              note="'should implement measures to prevent unauthorized access'"),
    GoldLabel("73.2.2", 196, True, None, Modality.MAY, None, False,
              note="'may register with US authorities' — actor is 'overseas branches of "
                   "Indian Financial Institutions', outside the closed vocabulary"),
    GoldLabel("86.7", 211, True, Actor.STOCK_BROKER, Modality.MUST, None, True,
              note="'An intermediary ... shall have in place a comprehensive policy'"),
    GoldLabel("86.7.2", 211, True, Actor.STOCK_BROKER, Modality.MUST, None, True,
              note="'The Board shall mandate a regular review of outsourcing policy'"),
    GoldLabel("86.10", 213, True, Actor.STOCK_BROKER, Modality.MUST, None, True,
              note="'shall conduct appropriate due diligence in selecting the third party'"),
]


def gold_by_clause() -> dict[str, GoldLabel]:
    return {label.clause_id: label for label in GOLD_SET}


assert len(GOLD_SET) == 40, "the gold set is specified as 40 clauses"
assert sum(1 for g in GOLD_SET if not g.has_obligation) >= 10, (
    "at least 10 clauses must carry no obligation"
)

"""Closed vocabularies for the Obligation IR.

Every enum here is deliberately closed. An extractor that cannot map a clause
onto these values must lower its confidence and let a human decide, rather than
inventing a category. Widening a vocabulary is a schema change with a version
bump, not a runtime accident.

All members are `str` enums so canonical JSON serialises them as their stable
wire value and never as a Python repr.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """A string enum whose value is its wire format.

    Defined locally rather than imported from `enum` so the wire format is
    pinned by this module and cannot shift with the Python version.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Actor(StrEnum):
    """The regulated entity that carries the obligation.

    Scoped to the intermediaries SEBI master circulars actually address. An
    obligation whose actor cannot be pinned down is not compilable.
    """

    STOCK_BROKER = "STOCK_BROKER"
    INVESTMENT_ADVISER = "INVESTMENT_ADVISER"
    AMC = "AMC"
    RTA = "RTA"
    DEPOSITORY = "DEPOSITORY"
    MII = "MII"

    # Present in the stock-broker circular as co-addressees of duties.
    STOCK_EXCHANGE = "STOCK_EXCHANGE"
    CLEARING_CORPORATION = "CLEARING_CORPORATION"
    CLEARING_MEMBER = "CLEARING_MEMBER"
    DEPOSITORY_PARTICIPANT = "DEPOSITORY_PARTICIPANT"


class Modality(StrEnum):
    """Deontic force, derived from the operative verb.

    SEBI drafting is consistent enough that this mapping is mechanical:
        "shall" / "is required to"  -> MUST
        "shall not" / "no ... may"  -> MUST_NOT
        "may"                       -> MAY
        "should" / "is advised to"  -> SHOULD
    """

    MUST = "MUST"
    MUST_NOT = "MUST_NOT"
    MAY = "MAY"
    SHOULD = "SHOULD"


class TriggerKind(StrEnum):
    """What causes the obligation to become live."""

    EVENT = "EVENT"
    SCHEDULE = "SCHEDULE"
    CONDITION = "CONDITION"
    CONTINUOUS = "CONTINUOUS"


class DeadlineKind(StrEnum):
    """How the compliance horizon is expressed.

    ABSOLUTE       a fixed calendar date
    RELATIVE       an offset from an anchor event, e.g. "T+1 working day"
    END_OF_PERIOD  the close of a named period, e.g. "end of day T", "quarterly"
    ON_DEMAND      no horizon until the regulator or client asks
    """

    ABSOLUTE = "ABSOLUTE"
    RELATIVE = "RELATIVE"
    END_OF_PERIOD = "END_OF_PERIOD"
    ON_DEMAND = "ON_DEMAND"


class DayCount(StrEnum):
    """Whether a day-valued offset counts working days or calendar days.

    Tri-state on purpose. SEBI often writes "T+5 day" or "within 30 days"
    without stating the convention, and the two readings give different due
    dates. Silently applying the market convention — T+n counts settlement
    days — would be an invisible interpretation baked into a signed artifact,
    which is precisely what this product exists to eliminate.

    UNSPECIFIED means the clause does not say. It blocks certification until a
    named officer resolves it, so the interpretation is recorded as a human
    decision rather than inherited from a parser's assumption.
    """

    BUSINESS = "BUSINESS"
    CALENDAR = "CALENDAR"
    UNSPECIFIED = "UNSPECIFIED"


class CommencementKind(StrEnum):
    """When an obligation starts to bite, independent of its per-instance deadline.

    A deadline says "within one day of the event". A commencement says "and this
    rule only starts applying three months after the exchanges publish
    operational guidelines". Amendment circulars routinely phase commencement
    across paragraph ranges, so this cannot be flattened into the deadline.
    """

    FIXED_DATE = "FIXED_DATE"
    RELATIVE_TO_ISSUANCE = "RELATIVE_TO_ISSUANCE"
    CONDITIONAL_ON_EVENT = "CONDITIONAL_ON_EVENT"


class ConditionKind(StrEnum):
    """Why an obligation might not apply to a given fact pattern."""

    PRECONDITION = "PRECONDITION"
    EXEMPTION = "EXEMPTION"
    THRESHOLD = "THRESHOLD"


class RuleStatus(StrEnum):
    """Lifecycle of a compiled rule.

    PROPOSED    extractor output, not yet reviewed
    CERTIFIED   a named human signed it; the artifact is frozen and signed
    REJECTED    a named human refused it; retained for audit, never executed
    SUPERSEDED  a later version replaced it; retained for historical replay
    """

    PROPOSED = "PROPOSED"
    CERTIFIED = "CERTIFIED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ClauseKind(StrEnum):
    """Node types in the parsed document tree.

    The stock-broker master circular is section-numbered, not chapter-numbered.
    SECTION is the top of the numbered hierarchy; ANNEXURE and APPENDIX are its
    peers, not its children.
    """

    PREAMBLE = "PREAMBLE"
    SECTION = "SECTION"
    CLAUSE = "CLAUSE"
    SUBCLAUSE = "SUBCLAUSE"
    ITEM = "ITEM"
    ANNEXURE = "ANNEXURE"
    APPENDIX = "APPENDIX"


class AmendmentAction(StrEnum):
    """How an amending circular changes a target paragraph.

    Retained for the later diff engine, which is validated against real
    amendment circulars issued after the master circular. The master circular
    itself carries no internal amendment history, so nothing in Phase 0 emits
    these values.
    """

    INSERTED = "INSERTED"
    SUBSTITUTED = "SUBSTITUTED"
    DELETED = "DELETED"
    AMENDED = "AMENDED"

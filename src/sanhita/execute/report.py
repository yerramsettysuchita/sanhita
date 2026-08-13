"""The gap report.

The output of the whole product. Everything upstream exists so that this file
can say, of a specific failure, exactly which words of the regulation were
broken and which named person signed the rule that says so.

A finding that cannot cite its clause is not a finding, it is an opinion. The
dataclass therefore requires the citation fields rather than accepting them as
optional decoration, and a test asserts every emitted finding carries them.

Four outcomes, kept apart on purpose, and the distinction between the middle
two is the one that matters most:

  MISSING       an occasion was recorded and its artifact never arrived
  LATE          the artifact arrived, after the due date
  NO_EVIDENCE   the duty fell due and there is no record of it either way
  UNEVALUABLE   the rule could not be checked, and here is why

MISSING and LATE are breaches: the firm's own records prove them, and
``breaches`` counts exactly those two.

NO_EVIDENCE is not, and used to be. It was folded into the breach count, so a
firm that had uploaded one register was told it had 30 breaches when 29 of them
were duties nobody had given us any record of. Meanwhile ``health.py``, built
from the same run, said in as many words that a duty with no record is very
often one discharged perfectly on paper that nobody uploaded. The product
contradicted itself on two screens, and the honest half won: an unknown is
reported as :attr:`GapReport.unverified` and never as a finding against the
firm.

UNEVALUABLE exists because the alternative is worse. A rule the engine cannot
evaluate must not be counted as passed. Silence there is how compliance tools
come to report reassuring numbers that mean nothing.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum

__all__ = ["Outcome", "Finding", "Unevaluable", "GapReport"]


class Outcome(str, Enum):
    SATISFIED = "SATISFIED"
    #: An occasion happened and the artifact was never produced.
    MISSING = "MISSING"
    #: It was produced, after the due date.
    LATE = "LATE"
    #: The obligation was owed on a date and no event for it exists at all.
    #:
    #: Distinct from MISSING, which is raised against a recorded occasion whose
    #: artifact never arrived. This one is raised where the occasion itself is
    #: absent from the evidence store: the firm did not file late, it has no
    #: record of the duty at all. Before this existed, that case produced
    #: nothing, so a firm that ignored a monthly duty for a year looked
    #: identical to one that never owed it.
    NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class Finding:
    """One breach, with the citation that proves it."""

    outcome: Outcome
    # -- what happened
    event_id: str
    entity: str
    occurred_on: _dt.date
    due_on: _dt.date
    filed_on: _dt.date | None
    days_late: int | None

    # -- the citation. None of these are optional.
    obligation_id: str
    clause_id: str
    section: str
    page: int
    verbatim: str
    requirement: str
    certified_by: str
    certified_at: _dt.datetime
    signature: str

    #: True when the due date was computed in working days against a calendar
    #: that does not know the exchange holidays for that period.
    due_date_is_approximate: bool = False

    @property
    def severity(self) -> str:
        # No record at all of a duty that fell due is the worst of the three.
        # A late filing is a process problem; nothing on file is a firm that
        # does not know it owes the duty.
        if self.outcome in (Outcome.MISSING, Outcome.NO_EVIDENCE):
            return "high"
        if self.days_late is not None and self.days_late > 5:
            return "high"
        return "medium"

    def to_json(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "severity": self.severity,
            "event_id": self.event_id,
            "entity": self.entity,
            "occurred_on": self.occurred_on.isoformat(),
            "due_on": self.due_on.isoformat(),
            "filed_on": self.filed_on.isoformat() if self.filed_on else None,
            "days_late": self.days_late,
            "due_date_is_approximate": self.due_date_is_approximate,
            "citation": {
                "obligation_id": self.obligation_id,
                "clause_id": self.clause_id,
                "section": self.section,
                "page": self.page,
                "requirement": self.requirement,
                "verbatim": self.verbatim,
                "certified_by": self.certified_by,
                "certified_at": self.certified_at.isoformat(),
                "signature": self.signature,
            },
        }


@dataclass(frozen=True)
class Unevaluable:
    """A certified rule the engine declined to check, and the reason."""

    obligation_id: str
    clause_id: str
    page: int
    reason: str

    def to_json(self) -> dict:
        return {
            "obligation_id": self.obligation_id,
            "clause_id": self.clause_id,
            "page": self.page,
            "reason": self.reason,
        }


@dataclass
class GapReport:
    """Everything one run produced, including what it refused to answer."""

    #: Where the events came from. Printed on the report.
    evidence_label: str
    #: Which calendar computed the due dates. Printed on the report.
    calendar_name: str
    run_at: _dt.datetime

    certified_rules: int = 0
    rules_evaluated: int = 0
    events_checked: int = 0
    #: Occasions that fell due under a rule the firm filed nothing at all for.
    #: Kept out of ``events_checked`` so the compliance rate has an honest
    #: denominator, and reported in its own right because it is usually the
    #: largest number on the screen and the one that matters most.
    occasions_unevidenced: int = 0
    satisfied: int = 0

    findings: list[Finding] = field(default_factory=list)
    unevaluable: list[Unevaluable] = field(default_factory=list)

    #: Rules that were owed nothing in this window, and why. Counted rather
    #: than listed, because on a real rulebook most rules are in here and a
    #: list of them is not something anybody reads.
    not_applicable: int = 0
    #: Rules whose applicability could not be decided without inventing a fact
    #: about the firm. Never counted as passes. See ``applicability``.
    undetermined: list[Unevaluable] = field(default_factory=list)
    #: Every applicability decision made during this run, so a reviewer can
    #: audit the judgement and not just its consequence.
    applicability: list = field(default_factory=list)

    @property
    def breaches(self) -> int:
        """Duties the records show were not discharged on time.

        **Deliberately narrower than ``len(self.findings)``**, which is what
        this used to return. That older reading put NO_EVIDENCE in the same
        number as MISSING and LATE, so a firm that had uploaded one register
        was told it had 30 breaches when 29 of them were duties nobody had
        given us any record of either way.

        That was the product contradicting itself. ``health.py`` says in as many
        words that a duty with no record is unknown and very often one
        discharged on paper that nobody uploaded; the engine then counted the
        same situation as a breach. Both screens were built from the same run.

        A breach here is a claim Sanhita can defend from the records it was
        given: an occasion fell due, a record of it exists, and that record
        shows the artifact was never filed or was filed late. Everything else
        is :attr:`unverified`.
        """
        return len(self.missing) + len(self.late)

    @property
    def unverified(self) -> int:
        """Duties that fell due and have no record at all, either way.

        Not a breach and not a pass. The firm may have discharged every one of
        them on paper nobody uploaded, and saying otherwise would fabricate a
        finding. What is true, and worth saying loudly, is that nothing here
        can tell.
        """
        return len(self.never_evidenced)

    @property
    def never_evidenced(self) -> list[Finding]:
        """Duties that fell due and have no record at all."""
        return [f for f in self.findings if f.outcome is Outcome.NO_EVIDENCE]

    @property
    def missing(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome is Outcome.MISSING]

    @property
    def late(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome is Outcome.LATE]

    @property
    def compliance_rate(self) -> float | None:
        """Satisfied over the occasions that had a record. ``None`` if none did.

        Deliberately returns None rather than 100% on an empty run. A tool that
        reports full compliance because it evaluated nothing is worse than one
        that reports nothing at all.

        The denominator is occasions the engine adjudicated one at a time. It
        excludes occasions under a rule the firm filed nothing for at all: those
        are counted in :attr:`occasions_unevidenced` and reported separately,
        because a duty nobody has any record of is not a duty that was checked
        and failed, it is a duty nobody looked at. Folding them in produced the
        contradiction of a firm with zero breaches reading 0% compliant.
        """
        if not self.events_checked:
            return None
        return self.satisfied / self.events_checked

    @property
    def approximate_findings(self) -> int:
        return sum(1 for f in self.findings if f.due_date_is_approximate)

    def by_section(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.section] = counts.get(finding.section, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def ranked(self) -> list[Finding]:
        """What the records prove first, then what they cannot settle.

        This used to lead with NO_EVIDENCE, on the reasoning that a firm with
        nothing on file does not know it owes the duty. That was defensible
        while NO_EVIDENCE counted as a breach. It is not any more: those rows
        are the ones this report explicitly declines to call findings, and
        putting them above the failures it can prove buried the one actionable
        gap under twenty-nine unknowns. On the demonstration state the single
        remediable finding did not appear on the screen at all.

        So a proven failure leads. An unknown is worth a person's attention and
        is not worth more of it than something the records settle.

        ``.get`` with a default rather than a bare lookup. A new outcome added
        later should sort to the end rather than raise a KeyError inside a
        sort key, which is how a missing entry here took the gaps screen down.
        """
        order = {
            Outcome.MISSING: 0,
            Outcome.LATE: 1,
            Outcome.NO_EVIDENCE: 2,
            Outcome.SATISFIED: 3,
        }
        return sorted(
            self.findings,
            key=lambda f: (order.get(f.outcome, 99), -(f.days_late or 0), f.clause_id),
        )

    def caveats(self) -> list[str]:
        """What a reader must know before quoting any number on this report."""
        notes: list[str] = []
        notes.append(f"Evidence: {self.evidence_label}.")
        notes.append(f"Due dates computed against: {self.calendar_name}.")
        if self.approximate_findings:
            notes.append(
                f"{self.approximate_findings} due date(s) fall outside the range "
                "the loaded holiday list covers, so they were computed from "
                "weekends alone and may be a day or two early."
            )
        if self.unevaluable:
            notes.append(
                f"{len(self.unevaluable)} certified rule(s) could not be checked "
                "and are listed separately. They are not counted as passing."
            )
        if self.occasions_unevidenced:
            notes.append(
                f"{self.occasions_unevidenced} occasion(s) fell due under rules "
                "with no record of any kind, so they are reported separately "
                "and are not in the denominator of the compliance rate. A duty "
                "nobody has a record of was not checked and passed; it was not "
                "looked at."
            )
        return notes

    def to_json(self) -> dict:
        return {
            "run_at": self.run_at.isoformat(),
            "evidence": self.evidence_label,
            "calendar": self.calendar_name,
            "certified_rules": self.certified_rules,
            "rules_evaluated": self.rules_evaluated,
            "events_checked": self.events_checked,
            "occasions_unevidenced": self.occasions_unevidenced,
            "satisfied": self.satisfied,
            "breaches": self.breaches,
            "missing": len(self.missing),
            "late": len(self.late),
            # Reported beside the breaches rather than inside them. A duty with
            # no record either way is not a finding against the firm.
            "unverified": self.unverified,
            "compliance_rate": self.compliance_rate,
            "caveats": self.caveats(),
            "findings": [f.to_json() for f in self.ranked()],
            "unevaluable": [u.to_json() for u in self.unevaluable],
        }

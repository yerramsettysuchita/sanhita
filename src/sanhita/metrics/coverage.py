"""Coverage, with the denominator in the open.

    clause_coverage   = clauses with >= 1 CERTIFIED obligation
                        ---------------------------------------
                        obligation-bearing clauses

    evidence_coverage = CERTIFIED obligations with >= 1 EvidenceReq
                        -------------------------------------------
                        CERTIFIED obligations

The interesting half is the first denominator. "Obligation-bearing" cannot mean
"whatever the extractor touched" — that makes coverage self-grading: an
extractor that finds fewer duties scores *higher*, which is exactly backwards.
It also cannot mean "every clause in the document", which would permanently cap
coverage at around a third because most clauses are headings and definitions.

So it is a **classification of the clause itself, computed independently of any
extraction result**. A clause is obligation-bearing when it contains a deontic
verb used to impose a duty. Headings, definitions, recitals, cross-references
and consequence statements are excluded, and each exclusion is counted and
reportable — `sanhita coverage --explain` prints the full census and lets a
sceptic audit any bucket by hand.

The honest consequence: this classifier is itself fallible, so the denominator
is an estimate. It is reported with its own accuracy measured against the gold
set (`sanhita eval`), which is the only defensible way to quote a ratio built on
a heuristic.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from sanhita.compile.extract import _DEONTIC_RE, _NON_DEONTIC_CONTEXT
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation
from sanhita.parse.clause_tree import ClauseNode, ClauseTree

__all__ = [
    "ClauseClass",
    "CoverageReport",
    "SectionCoverage",
    "classify_clause",
    "compute_coverage",
]

MIN_WORDS = 6

_DEFINITION_RE = re.compile(
    r"\b(?:shall\s+mean|means?\b|refers?\s+to|is\s+defined\s+as|"
    r"shall\s+have\s+the\s+(?:same\s+)?meaning|for\s+(?:this|the)\s+purpose)\b",
    re.I,
)
_CROSS_REF_RE = re.compile(
    r"^\s*(?:as\s+(?:per|specified|mentioned|provided|stated)|refer\s+to|"
    r"in\s+terms\s+of|pursuant\s+to|vide\s+)\b",
    re.I,
)
_ANNEXURE_POINTER_RE = re.compile(r"^\s*Annexure\s*[-–—]?\s*\d", re.I)


class ClauseClass(str, Enum):
    """Why a clause is or is not in the coverage denominator."""

    OBLIGATION_BEARING = "OBLIGATION_BEARING"
    HEADING = "HEADING"
    DEFINITION = "DEFINITION"
    CROSS_REFERENCE = "CROSS_REFERENCE"
    RECITAL = "RECITAL"
    CONSEQUENCE = "CONSEQUENCE"
    TOO_SHORT = "TOO_SHORT"

    @property
    def in_denominator(self) -> bool:
        return self is ClauseClass.OBLIGATION_BEARING


def classify_clause(node: ClauseNode) -> ClauseClass:
    """Decide whether a clause can carry a duty. Independent of any extraction.

    Deliberately *not* a call into the extractor: if the classifier asked the
    extractor, the denominator would move whenever the extractor did, and the
    ratio would measure nothing.
    """
    text = node.text

    if node.kind in ("SECTION", "ANNEXURE", "APPENDIX"):
        return ClauseClass.HEADING
    if len(text.split()) < MIN_WORDS:
        return ClauseClass.TOO_SHORT

    body = text
    match = re.match(r"^\s*[\d.]+\s*", body)
    if match:
        body = body[match.end() :]

    if _ANNEXURE_POINTER_RE.match(body) or _CROSS_REF_RE.match(body):
        return ClauseClass.CROSS_REFERENCE

    deontic = list(_DEONTIC_RE.finditer(text))
    if not deontic:
        # No modal verb at all: narrative, background, or a table row.
        return ClauseClass.DEFINITION if _DEFINITION_RE.search(text) else ClauseClass.RECITAL

    # A modal verb is present. Decide whether any occurrence imposes a duty
    # rather than defining a term or describing a consequence.
    for occurrence in deontic:
        following = text[occurrence.start() : occurrence.start() + 60]
        if not _NON_DEONTIC_CONTEXT.match(following):
            return ClauseClass.OBLIGATION_BEARING

    if _DEFINITION_RE.search(text):
        return ClauseClass.DEFINITION
    return ClauseClass.CONSEQUENCE


@dataclass(slots=True)
class SectionCoverage:
    section: str
    obligation_bearing: int = 0
    covered: int = 0
    proposed_only: int = 0

    @property
    def clause_coverage(self) -> float:
        if not self.obligation_bearing:
            return 0.0
        return round(self.covered / self.obligation_bearing, 4)


@dataclass(slots=True)
class CoverageReport:
    """Coverage with its denominator, its exclusions and its caveats attached."""

    # -- clause coverage
    obligation_bearing_clauses: int = 0
    clauses_with_certified: int = 0
    clauses_with_proposed_only: int = 0

    # -- evidence coverage
    certified_obligations: int = 0
    certified_with_evidence: int = 0

    # -- the denominator, itemised
    total_clauses: int = 0
    excluded: Counter = field(default_factory=Counter)
    by_section: dict[str, SectionCoverage] = field(default_factory=dict)

    #: Accuracy of the classifier that produced the denominator, when a gold set
    #: has been run. None means the denominator is unvalidated and must be
    #: quoted as such.
    classifier_accuracy: float | None = None

    @property
    def clause_coverage(self) -> float:
        if not self.obligation_bearing_clauses:
            return 0.0
        return round(self.clauses_with_certified / self.obligation_bearing_clauses, 4)

    @property
    def evidence_coverage(self) -> float:
        if not self.certified_obligations:
            return 0.0
        return round(self.certified_with_evidence / self.certified_obligations, 4)

    # -- the ladder
    #
    # One number cannot answer "how much of this rulebook is covered", because
    # three different things are being asked and they have three different
    # answers. Quoted alone, the last of them reads as a failure of the
    # compiler when it is in fact a count of how many hours one reviewer has
    # had. So all three are reported together, always, in the order the work
    # actually happens.

    @property
    def clauses_with_any_rule(self) -> int:
        """Clauses the compiler drew at least one rule from, signed or not."""
        return self.clauses_with_certified + self.clauses_with_proposed_only

    @property
    def compiled_coverage(self) -> float:
        """Reach of the compiler. Independent of how much review has happened."""
        if not self.obligation_bearing_clauses:
            return 0.0
        return round(self.clauses_with_any_rule / self.obligation_bearing_clauses, 4)

    @property
    def certification_backlog(self) -> int:
        """Clauses holding a rule that no person has yet signed or rejected."""
        return self.clauses_with_proposed_only

    def ladder(self) -> list[dict]:
        """Compiled, then certified, then evidence-mapped, with what limits each.

        The third rung has a different denominator from the first two, and the
        screen has to say so rather than implying one ratio narrowing three
        times.
        """
        return [
            {
                "key": "compiled",
                "label": "Compiled",
                "ratio": self.compiled_coverage,
                "numerator": self.clauses_with_any_rule,
                "denominator": self.obligation_bearing_clauses,
                "unit": "clauses that carry a duty",
                "limited_by": "the extractor, and the parse beneath it",
            },
            {
                "key": "certified",
                "label": "Certified",
                "ratio": self.clause_coverage,
                "numerator": self.clauses_with_certified,
                "denominator": self.obligation_bearing_clauses,
                "unit": "clauses that carry a duty",
                "limited_by": "reviewer hours, by design",
            },
            {
                "key": "evidence",
                "label": "Mapped to evidence",
                "ratio": self.evidence_coverage,
                "numerator": self.certified_with_evidence,
                "denominator": self.certified_obligations,
                "unit": "certified rules",
                "limited_by": "whether the clause names anything to keep",
            },
        ]

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())

    def denominator_statement(self) -> str:
        """The sentence to put in front of anyone who challenges the number."""
        parts = ", ".join(
            f"{count} {name.lower().replace('_', ' ')}"
            for name, count in sorted(self.excluded.items(), key=lambda kv: -kv[1])
        )
        accuracy = (
            f"{self.classifier_accuracy:.1%} accurate on the gold set"
            if self.classifier_accuracy is not None
            else "NOT YET VALIDATED against a gold set"
        )
        return (
            f"Denominator = {self.obligation_bearing_clauses} obligation-bearing "
            f"clauses, from {self.total_clauses} parsed clauses, excluding "
            f"{self.excluded_total} ({parts}). The classifier producing this "
            f"denominator is {accuracy}."
        )


def compute_coverage(
    tree: ClauseTree,
    obligations: list[Obligation],
    *,
    include_annexures: bool = False,
    classifier_accuracy: float | None = None,
) -> CoverageReport:
    """Coverage over a parsed tree and a set of rules at their current status."""
    report = CoverageReport(classifier_accuracy=classifier_accuracy)

    certified_by_clause: dict[str, list[Obligation]] = {}
    proposed_by_clause: dict[str, list[Obligation]] = {}
    for obligation in obligations:
        clause = obligation.source.clause_id
        if obligation.status is RuleStatus.CERTIFIED:
            certified_by_clause.setdefault(clause, []).append(obligation)
        elif obligation.status is RuleStatus.PROPOSED:
            proposed_by_clause.setdefault(clause, []).append(obligation)

    for node in tree.nodes.values():
        if not include_annexures and node.section.startswith("ANX-"):
            continue
        if node.kind == "APPENDIX":
            continue

        report.total_clauses += 1
        clause_class = classify_clause(node)
        if not clause_class.in_denominator:
            report.excluded[clause_class.value] += 1
            continue

        report.obligation_bearing_clauses += 1
        section = report.by_section.setdefault(
            node.section or "?", SectionCoverage(section=node.section or "?")
        )
        section.obligation_bearing += 1

        if node.id in certified_by_clause:
            report.clauses_with_certified += 1
            section.covered += 1
        elif node.id in proposed_by_clause:
            report.clauses_with_proposed_only += 1
            section.proposed_only += 1

    for obligation in obligations:
        if obligation.status is not RuleStatus.CERTIFIED:
            continue
        report.certified_obligations += 1
        if obligation.evidence:
            report.certified_with_evidence += 1

    return report

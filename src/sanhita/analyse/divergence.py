"""Where two firms reading the same clause will reach different answers.

Problem Statement 2 names the harm precisely:

    "The result is uneven implementation, delayed adaptation, and divergent
     interpretations across similarly situated intermediaries."

A shared certified corpus removes the cause of divergence: if every broker runs
the same signed rule, there is nothing left to disagree about. That is what the
rest of the product does. This module answers the question one step earlier.
**Which clauses were going to cause the disagreement in the first place?**

That is a drafting question, and the answer is useful before publication rather
than after. A regulator who knows that forty clauses in a draft are the ones the
market will read two ways can redraft those forty and the divergence never
happens.

**The signals.** All four are measured, none are guessed.

``ambiguity``      The extractor's own confidence in the rule it drew. Low
                   confidence means the sentence resisted mechanical reading,
                   and a sentence that resists a parser tends to resist a
                   compliance officer for the same reasons.
``unresolved``     Fields the extractor refused to fill. A blocking issue is a
                   place where the text does not determine the answer, which is
                   exactly where two firms supply different answers.
``judgement``      Conditions carrying no comparator and no number. Across this
                   corpus about 95% of conditions are prose gates of the form
                   "where the circumstances so warrant", and every one of them
                   is a decision somebody has to make.
``contested``      A human already overruled the machine on this clause. This
                   is the only backward-looking signal and by far the strongest:
                   it is not a prediction that reading is hard, it is a record
                   of someone finding it hard.

**What this is not.** It is not a probability. Nothing here says "62% of firms
will disagree", because that number cannot be known without observing firms.
It is a ranking, and the screen presents it as one: these clauses first, for the
reasons listed against each. A regulator redrafting the top twenty is acting on
better information than one redrafting nothing, and that is the whole claim.

Nothing here calls a model. Same rules in, same ranking out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sanhita.certify.ledger import AuditEntry, Transition
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation

__all__ = [
    "DivergenceReport",
    "DivergenceRisk",
    "Signal",
    "assess_divergence",
]

#: A condition shaped like a test a machine could run: a comparator and a
#: number. Everything else is a judgement somebody makes.
_NUMERIC_CONDITION = re.compile(
    r"(>=|<=|==|!=|>|<)"
    r"|(\bnot\s+less\s+than\b|\bat\s+least\b|\bexceed(?:s|ing)?\b"
    r"|\bmore\s+than\b|\bless\s+than\b|\bwithin\b)",
    re.I,
)
_HAS_NUMBER = re.compile(r"\d")

#: What counts as the extractor struggling.
#:
#: Not a fixed number. On this corpus confidence spans roughly 0.68 to 0.84,
#: with three quarters of all rules sitting inside a 0.1 band, so any absolute
#: threshold either flags almost everything or almost nothing depending on
#: which side of the cluster it lands. The threshold is therefore taken from
#: the distribution actually observed, and reported alongside the result so a
#: reader knows what "low" meant for this document.
AMBIGUITY_PERCENTILE = 0.25

#: Used only when there are too few rules for a percentile to mean anything.
FALLBACK_LOW_CONFIDENCE = 0.70


def _low_confidence_threshold(values: list[float]) -> float:
    """The confidence below which this document's extraction was struggling."""
    if len(values) < 20:
        return FALLBACK_LOW_CONFIDENCE
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * AMBIGUITY_PERCENTILE)))
    return ordered[index]

#: Weights. Deliberately flat and few: a scoring function with fifteen tuned
#: coefficients would be unauditable, and the ranking is meant to be arguable
#: by a person reading the reasons rather than trusted because it is a number.
WEIGHT_CONTESTED = 3.0
WEIGHT_UNRESOLVED = 2.0
WEIGHT_AMBIGUITY = 2.0
WEIGHT_JUDGEMENT = 1.0

#: A clause longer than this is a flattened summary table rather than a clause.
#: Same guard, and the same reason, as in ``conflicts``: clause 98.3 of the
#: stock broker circular is 13,723 characters of restated obligations and would
#: otherwise dominate every ranking in the product.
TABLE_LIKE_CHARS = 3000


@dataclass(frozen=True)
class Signal:
    """One reason a clause is expected to be read differently."""

    kind: str
    weight: float
    detail: str


@dataclass
class DivergenceRisk:
    """One clause, and why firms are expected to disagree about it."""

    clause_id: str
    section: str
    page: int
    obligation_ids: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    verbatim: str = ""
    certified: int = 0
    proposed: int = 0

    @property
    def score(self) -> float:
        return round(sum(s.weight for s in self.signals), 2)

    @property
    def contested(self) -> bool:
        return any(s.kind == "contested" for s in self.signals)

    def to_json(self) -> dict:
        return {
            "clause_id": self.clause_id,
            "section": self.section,
            "page": self.page,
            "score": self.score,
            "certified": self.certified,
            "proposed": self.proposed,
            "signals": [
                {"kind": s.kind, "weight": s.weight, "detail": s.detail}
                for s in self.signals
            ],
        }


#: Where the ranking is cut for reporting. A clause carrying two independent
#: reasons to be misread is worth a drafter's attention; one carrying a single
#: weak signal is not, and burying the first group inside the second is how a
#: useful list becomes an ignored one.
HIGH_SCORE = 4.0
MODERATE_SCORE = 3.0


@dataclass
class DivergenceReport:
    clauses_examined: int = 0
    rules_examined: int = 0
    excluded_table_like: int = 0
    risks: list[DivergenceRisk] = field(default_factory=list)

    #: The confidence threshold this document's own distribution produced.
    low_confidence_at: float = FALLBACK_LOW_CONFIDENCE

    @property
    def contested_count(self) -> int:
        return sum(1 for r in self.risks if r.contested)

    @property
    def high(self) -> list[DivergenceRisk]:
        """Clauses carrying two or more independent reasons to be read apart."""
        return [r for r in self.risks if r.score >= HIGH_SCORE]

    @property
    def moderate(self) -> list[DivergenceRisk]:
        return [r for r in self.risks if MODERATE_SCORE <= r.score < HIGH_SCORE]

    @property
    def low(self) -> list[DivergenceRisk]:
        return [r for r in self.risks if r.score < MODERATE_SCORE]

    def top(self, limit: int = 40) -> list[DivergenceRisk]:
        return self.risks[:limit]

    def caveats(self) -> list[str]:
        notes = [
            "This is a ranking, not a probability. Nothing here claims a "
            "percentage of firms will disagree, because that cannot be known "
            "without observing firms.",
            f"Low confidence means below {self.low_confidence_at:.0%} on this "
            "document. The threshold is the lower quartile of the confidence "
            "actually observed here rather than a fixed number, because the "
            "extractor's confidence clusters tightly and a fixed cut would flag "
            "either almost everything or almost nothing.",
            "Three of the four signals are properties of the text and of the "
            "extraction. The fourth, that a person already overruled the "
            "machine, is a record rather than a prediction and is weighted "
            "highest for that reason.",
            "A clause can be perfectly clear to a lawyer and still score here, "
            "because the signals measure mechanical readability. The reasons "
            "are listed against every entry so a reader can disagree with any "
            "one of them.",
        ]
        if self.excluded_table_like:
            notes.append(
                f"{self.excluded_table_like} rule(s) were excluded for coming "
                f"from clauses longer than {TABLE_LIKE_CHARS} characters. Those "
                "are summary tables the parser flattened into one node, not "
                "clauses, and they would otherwise dominate the ranking."
            )
        return notes

    def to_json(self) -> dict:
        return {
            "clauses_examined": self.clauses_examined,
            "rules_examined": self.rules_examined,
            "low_confidence_at": self.low_confidence_at,
            "high": len(self.high),
            "moderate": len(self.moderate),
            "contested": self.contested_count,
            "caveats": self.caveats(),
            "risks": [r.to_json() for r in self.risks],
        }


def _contested_clauses(ledger) -> dict[str, list[AuditEntry]]:
    """Obligation ids a person amended or rejected, with the entries."""
    contested: dict[str, list[AuditEntry]] = {}
    for entry in ledger:
        if entry.transition in (Transition.AMENDED, Transition.REJECTED):
            contested.setdefault(entry.obligation_id, []).append(entry)
    return contested


def assess_divergence(
    obligations: list[Obligation],
    *,
    ledger=(),
) -> DivergenceReport:
    """Rank clauses by how likely two firms are to read them differently."""
    report = DivergenceReport()
    contested = _contested_clauses(ledger)

    by_clause: dict[str, DivergenceRisk] = {}
    live = [
        o
        for o in obligations
        if o.status in (RuleStatus.PROPOSED, RuleStatus.CERTIFIED)
    ]

    # Taken from the rules that will actually be scored, so the threshold
    # describes this document rather than some other one.
    report.low_confidence_at = _low_confidence_threshold(
        [
            o.confidence
            for o in live
            if len(o.source.verbatim_text) <= TABLE_LIKE_CHARS
        ]
    )

    for obligation in live:
        if len(obligation.source.verbatim_text) > TABLE_LIKE_CHARS:
            report.excluded_table_like += 1
            continue

        report.rules_examined += 1
        clause_id = obligation.source.clause_id
        risk = by_clause.get(clause_id)
        if risk is None:
            risk = DivergenceRisk(
                clause_id=clause_id,
                section=obligation.source.section,
                page=obligation.source.page,
                verbatim=obligation.source.verbatim_text,
            )
            by_clause[clause_id] = risk

        risk.obligation_ids.append(obligation.id)
        if obligation.status is RuleStatus.CERTIFIED:
            risk.certified += 1
        else:
            risk.proposed += 1

        # -- contested: somebody already had to overrule the machine here
        entries = contested.get(obligation.id, [])
        if entries and not risk.contested:
            actions = sorted({e.transition.value.lower() for e in entries})
            risk.signals.append(
                Signal(
                    kind="contested",
                    weight=WEIGHT_CONTESTED,
                    detail=(
                        f"A reviewer {' and '.join(actions)} the proposed rule "
                        "here. Somebody has already read this clause "
                        "differently from the extractor."
                    ),
                )
            )

        # -- unresolved: the text did not determine an answer
        issues = obligation.blocking_issues()
        if issues and not any(s.kind == "unresolved" for s in risk.signals):
            risk.signals.append(
                Signal(
                    kind="unresolved",
                    weight=WEIGHT_UNRESOLVED,
                    detail=(
                        f"{len(issues)} field(s) the extractor would not fill: "
                        + "; ".join(issues[:3])
                    ),
                )
            )

        # -- ambiguity: the sentence resisted mechanical reading
        if obligation.confidence <= report.low_confidence_at and not any(
            s.kind == "ambiguity" for s in risk.signals
        ):
            risk.signals.append(
                Signal(
                    kind="ambiguity",
                    weight=WEIGHT_AMBIGUITY,
                    detail=(
                        f"Extractor confidence {obligation.confidence:.0%}. A "
                        "sentence that resists a parser tends to resist a "
                        "reader for the same reasons."
                    ),
                )
            )

        # -- judgement: prose gates with nothing to measure
        prose_conditions = [
            c
            for c in obligation.conditions
            if not (
                _NUMERIC_CONDITION.search(c.expression)
                and _HAS_NUMBER.search(c.expression)
            )
        ]
        if prose_conditions and not any(s.kind == "judgement" for s in risk.signals):
            risk.signals.append(
                Signal(
                    kind="judgement",
                    weight=WEIGHT_JUDGEMENT * min(len(prose_conditions), 3),
                    detail=(
                        f"{len(prose_conditions)} condition(s) stated in prose "
                        "with nothing to measure, so applying them is a "
                        "decision rather than a test."
                    ),
                )
            )

    report.clauses_examined = len(by_clause)
    report.risks = sorted(
        (r for r in by_clause.values() if r.signals),
        key=lambda r: (-r.score, r.clause_id),
    )
    return report

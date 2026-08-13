"""Rules that disagree with each other.

A master circular is a consolidation. It gathers thirty years of separate
circulars into one document, and when two of those circulars told the same
party to do the same thing on different timelines, both sentences survive into
the consolidated text. Nobody reading it front to back will notice, because the
two clauses are ninety pages apart.

This module finds them. It is only possible because the rules are typed: to do
this by reading, you would have to compare all 1,377 rules against each other
and do deadline arithmetic on every pair. Over an Obligation IR it is a grouping
and a comparison.

**These are questions, not accusations.** Two clauses that look contradictory
are often both correct, addressing different instruments or different segments
in ways the compiled fields do not capture. So every finding is phrased as
something for a person to confirm, and the report says plainly how many it
looked at and on what basis it paired them. A tool that announced defects in the
regulator's own document without that caveat would deserve to be ignored.

Nothing here calls a model. Same rules in, same conflicts out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from sanhita.ir.enums import DayCount, Modality, RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["ConflictKind", "Confidence", "Conflict", "ConflictReport", "find_conflicts"]


class ConflictKind(str, Enum):
    #: Same duty, two different numbers of days.
    DEADLINE = "DEADLINE"
    #: One says it must happen, another says it must not.
    MODALITY = "MODALITY"
    #: Same duty, but one officer resolved the day count as working days and
    #: another as calendar days. This is a disagreement between people, not in
    #: the regulation.
    DAY_COUNT = "DAY_COUNT"
    #: Same duty stated twice, identically. Not a conflict; worth knowing.
    DUPLICATE = "DUPLICATE"


class Confidence(str, Enum):
    #: Actor, verb and object all match after normalisation.
    DIRECT = "DIRECT"
    #: Actor and verb match and the objects overlap heavily.
    PROBABLE = "PROBABLE"


#: Words carrying no distinguishing meaning in an obligation's object.
_NOISE = frozenset(
    """
    a an the of to in on by for from with and or as at its their his her it
    all any such other same this that these those shall must may should be is
    are was were been being do does did done have has had having
    """.split()
)

_WORD = re.compile(r"[a-z0-9]+")

#: How much two objects must overlap to be treated as the same duty.
_OVERLAP = 0.6

#: A clause longer than this is not a clause. In the stock broker circular the
#: median body clause is 206 characters and the 99.5th percentile is 1,463;
#: three clauses then jump to 5,727, 7,070 and 13,723. Those three are summary
#: tables that the parser flattened into a single node. Clause 98.3 is the
#: worst: it is the circular's own table of reporting requirements, restating
#: forty-two other clauses, and the extractor drew 64 obligations out of it,
#: every one carrying the same meaningless one-day deadline.
#:
#: Comparing rules drawn from a table against the real clauses that table is
#: summarising produces nothing but false conflicts, because they are the same
#: obligations counted twice. They are excluded here, and the report says which
#: and how many, for the same reason the coverage denominator itemises its
#: exclusions: a number is only worth as much as the statement of what it left
#: out.
TABLE_LIKE_CHARS = 3000


def _terms(text: str) -> frozenset[str]:
    """The distinguishing words of an action object."""
    return frozenset(
        w for w in _WORD.findall(text.lower()) if w not in _NOISE and len(w) > 2
    )


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class Conflict:
    """Two rules that appear to disagree, and why."""

    kind: ConflictKind
    confidence: Confidence
    left: Obligation
    right: Obligation
    question: str

    @property
    def clauses(self) -> tuple[str, str]:
        return (self.left.source.clause_id, self.right.source.clause_id)

    @property
    def involves_certified(self) -> bool:
        """A conflict between signed rules is the serious kind."""
        return (
            self.left.status is RuleStatus.CERTIFIED
            and self.right.status is RuleStatus.CERTIFIED
        )

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "confidence": self.confidence.value,
            "question": self.question,
            "involves_certified": self.involves_certified,
            "left": _side(self.left),
            "right": _side(self.right),
        }


def _side(o: Obligation) -> dict:
    return {
        "obligation_id": o.id,
        "clause_id": o.source.clause_id,
        "page": o.source.page,
        "status": o.status.value,
        "actor": o.actor.value,
        "modality": o.modality.value,
        "action": f"{o.action.verb} {o.action.object}",
        "verbatim": o.source.verbatim_text,
    }


@dataclass
class ConflictReport:
    #: How many rules were considered at all.
    rules_examined: int = 0
    #: How many pairs shared an actor and a verb, and so were compared in full.
    pairs_compared: int = 0
    #: Rules drawn from clauses too long to be clauses, and the clauses they
    #: came from. Excluded, and reported rather than dropped quietly.
    excluded_rules: int = 0
    excluded_clauses: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    def of(self, kind: ConflictKind) -> list[Conflict]:
        return [c for c in self.conflicts if c.kind is kind]

    @property
    def between_certified(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.involves_certified]

    @property
    def contradictions(self) -> list[Conflict]:
        """Findings where the rulebook gives two different answers.

        Kept apart from duplications on purpose. A duplicate is the same duty
        stated twice, which is a consolidation artifact worth knowing about but
        is not a contradiction, and the module's own definition says so. Adding
        the two together and calling the total "contradictions" would overstate
        the finding, and on this corpus it would overstate it by a factor of
        six: eleven of the thirteen findings are duplications.
        """
        return [c for c in self.conflicts if c.kind is not ConflictKind.DUPLICATE]

    @property
    def duplications(self) -> list[Conflict]:
        """The same duty stated in more than one place. Not a contradiction."""
        return self.of(ConflictKind.DUPLICATE)

    def ranked(self) -> list[Conflict]:
        """Most serious first: signed rules, then contradictions, then timing."""
        weight = {
            ConflictKind.MODALITY: 0,
            ConflictKind.DEADLINE: 1,
            ConflictKind.DAY_COUNT: 2,
            ConflictKind.DUPLICATE: 3,
        }
        return sorted(
            self.conflicts,
            key=lambda c: (
                not c.involves_certified,
                weight[c.kind],
                c.confidence is not Confidence.DIRECT,
                c.left.source.clause_id,
            ),
        )

    def caveats(self) -> list[str]:
        """What a reader must know before treating any of this as a defect."""
        notes = [
            f"{self.rules_examined} rules were examined and {self.pairs_compared} "
            "pairs were compared in full. A pair is compared only when the two "
            "rules name the same actor and the same operative verb.",
            "Two clauses can look contradictory and both be correct, because "
            "they address different instruments or segments in ways the "
            "compiled fields do not capture. Every line below is a question "
            "for a person, not a defect in the regulation.",
            "Nothing here consults a model. The same rules always produce the "
            "same findings.",
        ]
        if self.excluded_clauses:
            notes.insert(
                1,
                f"{self.excluded_rules} rule(s) were left out, from "
                f"{len(self.excluded_clauses)} clause(s) too long to be clauses: "
                + ", ".join(self.excluded_clauses)
                + ". Those are summary tables the parser flattened into one "
                "node, and they restate obligations that appear properly "
                "elsewhere. Comparing them against the clauses they summarise "
                "would report the same duty conflicting with itself.",
            )
        return notes

    def to_json(self) -> dict:
        return {
            "rules_examined": self.rules_examined,
            "pairs_compared": self.pairs_compared,
            "excluded_rules": self.excluded_rules,
            "excluded_clauses": self.excluded_clauses,
            "found": len(self.conflicts),
            "contradictions": len(self.contradictions),
            "duplications": len(self.duplications),
            "between_certified": len(self.between_certified),
            "by_kind": {
                k.value: len(self.of(k)) for k in ConflictKind if self.of(k)
            },
            "caveats": self.caveats(),
            "conflicts": [c.to_json() for c in self.ranked()],
        }


def find_conflicts(
    obligations: list[Obligation], *, certified_only: bool = False
) -> ConflictReport:
    """Compare rules against each other and report the disagreements.

    Rejected and superseded rules are never considered: they are inert, and a
    conflict with something that will never run is not a finding.
    """
    live = [
        o
        for o in obligations
        if o.status in (RuleStatus.PROPOSED, RuleStatus.CERTIFIED)
    ]
    if certified_only:
        live = [o for o in live if o.status is RuleStatus.CERTIFIED]

    # Drop rules drawn from flattened tables before anything is compared, and
    # keep a record of what went, because an exclusion nobody can see is
    # indistinguishable from a result nobody can check.
    oversized = {
        o.source.clause_id
        for o in live
        if len(o.source.verbatim_text) > TABLE_LIKE_CHARS
    }
    kept = [o for o in live if o.source.clause_id not in oversized]

    report = ConflictReport(
        rules_examined=len(kept),
        excluded_rules=len(live) - len(kept),
        excluded_clauses=sorted(oversized),
    )
    live = kept

    # Group by the two fields that must match for two rules to be about the
    # same duty at all. Anything that disagrees on actor or verb is simply two
    # different obligations.
    buckets: dict[tuple[str, str], list[Obligation]] = {}
    for o in live:
        buckets.setdefault((o.actor.value, o.action.verb.strip().lower()), []).append(o)

    seen: set[tuple[str, str]] = set()

    for group in buckets.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda o: o.id)
        terms = {o.id: _terms(o.action.object) for o in ordered}

        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                # Two rules compiled from the same clause are alternatives the
                # extractor produced for one sentence, not a contradiction
                # between two parts of the regulation.
                if left.source.clause_id == right.source.clause_id:
                    continue

                key = (left.id, right.id)
                if key in seen:
                    continue
                seen.add(key)

                shared = _overlap(terms[left.id], terms[right.id])
                if terms[left.id] and terms[left.id] == terms[right.id]:
                    confidence = Confidence.DIRECT
                elif shared >= _OVERLAP:
                    confidence = Confidence.PROBABLE
                else:
                    continue

                report.pairs_compared += 1
                found = _compare(left, right, confidence)
                if found is not None:
                    report.conflicts.append(found)

    return report


def _compare(
    left: Obligation, right: Obligation, confidence: Confidence
) -> Conflict | None:
    """The actual test, applied to one pair that is about the same duty."""
    a, b = left.source.clause_id, right.source.clause_id

    # 1. One forbids what the other requires. The most serious kind.
    forbidding = {Modality.MUST_NOT}
    requiring = {Modality.MUST, Modality.SHOULD}
    if (
        (left.modality in forbidding and right.modality in requiring)
        or (right.modality in forbidding and left.modality in requiring)
    ):
        return Conflict(
            kind=ConflictKind.MODALITY,
            confidence=confidence,
            left=left,
            right=right,
            question=(
                f"Clause {a} says this {left.modality.value.replace('_', ' ').lower()} "
                f"happen and clause {b} says it "
                f"{right.modality.value.replace('_', ' ').lower()}. If both are "
                "meant to stand, they must be about different circumstances. "
                "Which?"
            ),
        )

    ld, rd = left.deadline, right.deadline
    if ld is None or rd is None:
        return None

    # 2. Same duty, two different numbers of days.
    if (
        ld.offset_days is not None
        and rd.offset_days is not None
        and ld.offset_days != rd.offset_days
    ):
        return Conflict(
            kind=ConflictKind.DEADLINE,
            confidence=confidence,
            left=left,
            right=right,
            question=(
                f"Clause {a} allows {ld.offset_days} day(s) and clause {b} allows "
                f"{rd.offset_days}. If both apply to the same situation, the "
                "shorter one governs and the longer one is misleading. Do they "
                "apply to the same situation?"
            ),
        )

    # 3. The same duty, resolved differently by different people. This is a
    #    disagreement between certifying officers rather than in the text, and
    #    it is the one kind here that is squarely our own problem.
    if (
        ld.offset_days is not None
        and ld.offset_days == rd.offset_days
        and DayCount.UNSPECIFIED not in (ld.business_days, rd.business_days)
        and ld.business_days is not rd.business_days
    ):
        return Conflict(
            kind=ConflictKind.DAY_COUNT,
            confidence=confidence,
            left=left,
            right=right,
            question=(
                f"Both clauses give {ld.offset_days} days, but {a} was resolved as "
                f"{ld.business_days.value.lower()} days and {b} as "
                f"{rd.business_days.value.lower()} days. The regulation did not "
                "say, so a person chose, and two people chose differently. One "
                "of them should change."
            ),
        )

    # 4. Identical duty stated in two places. Not a conflict, but a reviewer
    #    certifying both should know they are signing the same thing twice.
    if (
        confidence is Confidence.DIRECT
        and left.modality is right.modality
        and ld.kind is rd.kind
        and ld.offset_days == rd.offset_days
    ):
        return Conflict(
            kind=ConflictKind.DUPLICATE,
            confidence=confidence,
            left=left,
            right=right,
            question=(
                f"Clauses {a} and {b} state the same duty on the same timeline. "
                "Consolidation carries the same requirement into more than one "
                "place. Certifying both is not wrong, but it is one obligation, "
                "not two."
            ),
        )

    return None

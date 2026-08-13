"""What a draft amendment would cost, before it is published.

Governments call this Regulatory Impact Assessment. It is normally done by hand,
slowly, on the largest changes only, and often not at all. The reason is that
answering "what does this amendment actually do to the market" requires holding
an entire rulebook in your head and doing arithmetic across it.

Over a compiled corpus it is a comparison. Take the certified rules, apply a
proposed change to a copy of them, and run the analyses that already exist over
both versions. The difference is the impact:

  * how many rules change, and which
  * whether the change contradicts something the rulebook already says
  * how many compliance occasions a year it adds or removes, per actor
  * which actors and which evidence types are pulled in

Every one of those numbers comes from a module written for another purpose.
Nothing here is new machinery, which is exactly why it is trustworthy: the
conflict count in an impact assessment is produced by the same code that
produces the conflict count on the conflicts screen.

**Nothing is ever saved.** This module builds amended copies in memory and
returns a report. It deliberately does not return the amended obligations, so
there is no path by which a simulated rule reaches a store. The copies carry
their original certification objects and therefore their original signatures,
which are stale the instant a field changes. That is safe only because they are
never persisted and never verified, and it is why they are not handed back.

Nothing here calls a model. Same draft in, same assessment out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sanhita.analyse.burden import measure_burden
from sanhita.analyse.conflicts import find_conflicts
from sanhita.ir.enums import DayCount, DeadlineKind, Modality, RuleStatus
from sanhita.ir.schema import Deadline, Obligation

__all__ = [
    "Change",
    "ChangeKind",
    "ImpactAssessment",
    "assess_amendment",
]


class ChangeKind:
    """What a draft change does to a rule."""

    DEADLINE_DAYS = "DEADLINE_DAYS"
    DEADLINE_PERIOD = "DEADLINE_PERIOD"
    MODALITY = "MODALITY"


@dataclass(frozen=True)
class Change:
    """One proposed edit to one rule.

    Kept deliberately small. A drafter changing a deadline from T+5 to T+1 is
    the case worth answering well, and a general-purpose editor over the whole
    IR would be a worse tool for it.
    """

    obligation_id: str
    kind: str
    #: New number of days, for DEADLINE_DAYS.
    days: int | None = None
    #: New recurrence period, for DEADLINE_PERIOD, e.g. "MONTH".
    period: str | None = None
    #: New modality, for MODALITY.
    modality: Modality | None = None
    #: Working days or calendar days, for DEADLINE_DAYS.
    day_count: DayCount = DayCount.BUSINESS

    def describe(self) -> str:
        if self.kind == ChangeKind.DEADLINE_DAYS:
            unit = self.day_count.value.lower()
            return f"deadline becomes {self.days} {unit} day(s)"
        if self.kind == ChangeKind.DEADLINE_PERIOD:
            return f"recurrence becomes {str(self.period).lower()}"
        if self.kind == ChangeKind.MODALITY:
            return f"modality becomes {self.modality.value.replace('_', ' ').lower()}"
        return "unrecognised change"


@dataclass
class ImpactAssessment:
    """What the draft would do, measured against what the rulebook says today."""

    changes: list[Change] = field(default_factory=list)

    #: Rules the draft edits directly.
    directly_changed: list[dict] = field(default_factory=list)
    #: Rules that cite, or are cited by, a changed clause and so need re-reading.
    needs_reread: list[dict] = field(default_factory=list)

    #: Conflicts the draft creates that did not exist before.
    new_conflicts: list[dict] = field(default_factory=list)
    #: Conflicts the draft resolves.
    resolved_conflicts: list[dict] = field(default_factory=list)

    #: Compliance occasions a year, before and after, per actor.
    occasions_before: dict[str, int] = field(default_factory=dict)
    occasions_after: dict[str, int] = field(default_factory=dict)

    actors_affected: list[str] = field(default_factory=list)
    evidence_types: list[str] = field(default_factory=list)
    clauses_touched: list[str] = field(default_factory=list)

    #: True where the corpus is too loosely cross-referenced for a transitive
    #: impact figure to mean anything. Stated rather than hidden.
    reference_graph_available: bool = False

    @property
    def occasions_delta(self) -> dict[str, int]:
        """Change in yearly compliance occasions, per actor, non-zero only."""
        actors = set(self.occasions_before) | set(self.occasions_after)
        delta = {
            actor: self.occasions_after.get(actor, 0) - self.occasions_before.get(actor, 0)
            for actor in actors
        }
        return {a: d for a, d in sorted(delta.items()) if d}

    @property
    def total_occasions_delta(self) -> int:
        return sum(self.occasions_delta.values())

    @property
    def is_material(self) -> bool:
        """Whether this draft does anything a supervisor would want to know about."""
        return bool(
            self.new_conflicts
            or self.total_occasions_delta
            or len(self.directly_changed) > 1
            or self.needs_reread
        )

    def caveats(self) -> list[str]:
        notes = [
            "Every figure here is produced by the same code that produces the "
            "corresponding screen elsewhere in the product. The conflict count "
            "comes from the conflict detector, the yearly occasions from the "
            "burden metric.",
            "This is a simulation. Nothing is written, no signature is touched, "
            "and the amended rules exist only for the duration of this "
            "calculation.",
            "Occasions a year counts how often the regulation requires "
            "something, not how much work it is. A one-line confirmation and a "
            "cyber-resilience framework each count as one.",
        ]
        if not self.reference_graph_available:
            notes.append(
                "No clause tree was supplied, so rules that merely cite a "
                "changed clause were not traced. The directly changed list is "
                "complete; the re-read list is not attempted."
            )
        elif not self.needs_reread:
            notes.append(
                "No other rule cites the changed clauses. This corpus is very "
                "loosely cross-referenced, so a transitive impact of zero is "
                "the normal result here rather than a sign the trace failed."
            )
        return notes

    def to_json(self) -> dict:
        return {
            "changes": [
                {"obligation_id": c.obligation_id, "describes": c.describe()}
                for c in self.changes
            ],
            "directly_changed": self.directly_changed,
            "needs_reread": self.needs_reread,
            "new_conflicts": self.new_conflicts,
            "resolved_conflicts": self.resolved_conflicts,
            "occasions_delta": self.occasions_delta,
            "total_occasions_delta": self.total_occasions_delta,
            "actors_affected": self.actors_affected,
            "evidence_types": self.evidence_types,
            "clauses_touched": self.clauses_touched,
            "caveats": self.caveats(),
        }


def _apply(obligation: Obligation, change: Change) -> Obligation:
    """A copy of this rule with the change applied.

    The copy keeps its certification object and therefore a signature that no
    longer matches its bytes. That is acceptable only because the result never
    leaves this module. See the module docstring.
    """
    if change.kind == ChangeKind.DEADLINE_DAYS:
        existing = obligation.deadline
        deadline = Deadline(
            kind=DeadlineKind.RELATIVE,
            offset_days=change.days,
            business_days=change.day_count,
            anchor_event=(
                existing.anchor_event
                if existing and existing.anchor_event
                else "the triggering event"
            ),
        )
        return obligation.model_copy(update={"deadline": deadline})

    if change.kind == ChangeKind.DEADLINE_PERIOD:
        deadline = Deadline(
            kind=DeadlineKind.END_OF_PERIOD,
            period=change.period,
        )
        return obligation.model_copy(update={"deadline": deadline})

    if change.kind == ChangeKind.MODALITY:
        return obligation.model_copy(update={"modality": change.modality})

    return obligation


def _conflict_key(conflict) -> tuple:
    return (
        conflict.kind.value,
        *sorted(conflict.clauses),
        conflict.left.id,
        conflict.right.id,
    )


def assess_amendment(
    obligations: list[Obligation],
    changes: list[Change],
    *,
    references=None,
) -> ImpactAssessment:
    """What this draft would do to this rulebook.

    ``references`` is an optional :class:`ReferenceGraph`. Without it the
    transitive re-read list is not attempted, and the report says so rather
    than reporting zero as though it had looked.
    """
    assessment = ImpactAssessment(
        changes=list(changes),
        reference_graph_available=references is not None,
    )

    by_id = {o.id: o for o in obligations}
    edits = {c.obligation_id: c for c in changes}

    amended: list[Obligation] = []
    touched_clauses: set[str] = set()
    actors: set[str] = set()
    evidence: set[str] = set()

    for obligation in obligations:
        change = edits.get(obligation.id)
        if change is None:
            amended.append(obligation)
            continue

        updated = _apply(obligation, change)
        amended.append(updated)
        touched_clauses.add(obligation.source.clause_id)
        actors.add(obligation.actor.value)
        for requirement in obligation.evidence:
            evidence.add(requirement.artifact_type)

        assessment.directly_changed.append(
            {
                "obligation_id": obligation.id,
                "clause_id": obligation.source.clause_id,
                "page": obligation.source.page,
                "status": obligation.status.value,
                "actor": obligation.actor.value,
                "action": f"{obligation.action.verb} {obligation.action.object}",
                "was": _deadline_phrase(obligation),
                "becomes": _deadline_phrase(updated),
                "change": change.describe(),
            }
        )

    # Anything missing from the store is a drafting error worth surfacing
    # rather than silently ignoring.
    for obligation_id in edits:
        if obligation_id not in by_id:
            assessment.directly_changed.append(
                {
                    "obligation_id": obligation_id,
                    "clause_id": "?",
                    "page": 0,
                    "status": "NOT FOUND",
                    "actor": "",
                    "action": "no rule with this id exists in this rulebook",
                    "was": "",
                    "becomes": "",
                    "change": edits[obligation_id].describe(),
                }
            )

    # -- what else has to be re-read
    if references is not None:
        for clause_id in touched_clauses:
            for dependent in references.dependents_of(clause_id):
                for obligation in obligations:
                    if (
                        obligation.source.clause_id == dependent
                        and obligation.id not in edits
                    ):
                        assessment.needs_reread.append(
                            {
                                "obligation_id": obligation.id,
                                "clause_id": dependent,
                                "cites": clause_id,
                                "actor": obligation.actor.value,
                            }
                        )

    # -- conflicts, before and after, from the same detector as everywhere else
    before = find_conflicts(obligations)
    after = find_conflicts(amended)
    before_keys = {_conflict_key(c) for c in before.conflicts}
    after_keys = {_conflict_key(c) for c in after.conflicts}

    for conflict in after.conflicts:
        if _conflict_key(conflict) not in before_keys:
            assessment.new_conflicts.append(conflict.to_json())
    for conflict in before.conflicts:
        if _conflict_key(conflict) not in after_keys:
            assessment.resolved_conflicts.append(conflict.to_json())

    # -- what it costs the market, per year, from the same burden metric
    burden_before = measure_burden(obligations)
    burden_after = measure_burden(amended)
    assessment.occasions_before = {
        a.actor: a.filings_per_year for a in burden_before.actors
    }
    assessment.occasions_after = {
        a.actor: a.filings_per_year for a in burden_after.actors
    }

    assessment.actors_affected = sorted(actors)
    assessment.evidence_types = sorted(evidence)
    assessment.clauses_touched = sorted(touched_clauses)
    return assessment


def _deadline_phrase(obligation: Obligation) -> str:
    """How this rule's deadline reads, in one short phrase."""
    deadline = obligation.deadline
    if deadline is None:
        return "no deadline"
    if deadline.kind is DeadlineKind.RELATIVE and deadline.offset_days is not None:
        unit = deadline.business_days.value.lower()
        plural = "s" if deadline.offset_days != 1 else ""
        return f"{deadline.offset_days} {unit} day{plural}"
    if deadline.kind is DeadlineKind.END_OF_PERIOD and deadline.period:
        return f"end of each {deadline.period.lower()}"
    if deadline.kind is DeadlineKind.ON_DEMAND:
        return "on demand"
    return deadline.kind.value.replace("_", " ").lower()

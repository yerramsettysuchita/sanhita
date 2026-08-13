"""What an amendment does to rules that were already signed.

A certification signs over the clause's own characters. That is the entire
reason `SourceAnchor.sha256` exists. So when a clause is amended, the question
is not whether the rule is *still roughly right*. It is whether the officer who
signed it signed *this* text. If the characters moved, they did not, and the
rule goes back to a human.

Nothing in this module is a judgement call. It compares two hashes.

The point of stating it this bluntly: every other approach to regulatory change
management degrades into similarity scoring, and a similarity score is exactly
the invisible interpretation this product exists to eliminate. "97% similar, so
we carried the certification forward" is how a firm ends up enforcing a rule
nobody signed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sanhita.diff.tree_diff import ChangeKind, TreeDiff
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["Consequence", "AffectedRule", "ImpactReport", "assess_impact"]


class Consequence(str, Enum):
    """What has to happen to a rule because its source clause changed."""

    #: The text it was signed over is gone. The signature no longer covers
    #: anything, so it must be reviewed and signed again.
    RECERTIFY = "RECERTIFY"
    #: The clause was deleted. The rule should be withdrawn.
    WITHDRAW = "WITHDRAW"
    #: Same words, new clause number. The rule's anchor has to be repointed.
    REPOINT = "REPOINT"
    #: Proposed, not signed. Recompiling handles it, no human decision lost.
    RECOMPILE = "RECOMPILE"
    #: This clause did not change, but a clause it points at did. Its own hash
    #: still matches, so a text diff calls it untouched, and yet the duty it
    #: creates has moved. This is the one a reader would never find alone.
    REREAD = "REREAD"


@dataclass(frozen=True)
class AffectedRule:
    obligation_id: str
    clause_id: str
    consequence: Consequence
    change: ChangeKind
    was_certified: bool
    certified_by: str | None
    signature: str | None
    now_at: str | None = None
    #: For REREAD: the clause that actually changed, and how many references
    #: away it is.
    via: str | None = None
    hops: int | None = None

    @property
    def loses_a_signature(self) -> bool:
        """Whether the certification no longer covers what it was signed over.

        A REREAD does not. Its own clause is untouched, so the signature still
        covers exactly the characters the officer read. What changed is the
        meaning of a clause it points at, which needs a person to look again
        but is not the same as an invalidated signature. Counting the two
        together would overstate the damage, and overstating damage is how a
        tool teaches people to ignore it.
        """
        return self.was_certified and self.consequence is not Consequence.REREAD

    def to_json(self) -> dict:
        return {
            "obligation_id": self.obligation_id,
            "clause_id": self.clause_id,
            "consequence": self.consequence.value,
            "change": self.change.value,
            "was_certified": self.was_certified,
            "certified_by": self.certified_by,
            "signature": self.signature,
            "now_at": self.now_at,
            "via": self.via,
            "hops": self.hops,
        }


@dataclass
class ImpactReport:
    before_label: str
    after_label: str
    certified_before: int = 0
    affected: list[AffectedRule] = field(default_factory=list)
    #: Clauses that appeared and have no rule compiled from them yet.
    new_clauses: list[str] = field(default_factory=list)

    @property
    def signatures_lost(self) -> int:
        return sum(1 for a in self.affected if a.loses_a_signature)

    @property
    def certified_after(self) -> int:
        return self.certified_before - self.signatures_lost

    def of(self, consequence: Consequence) -> list[AffectedRule]:
        return [a for a in self.affected if a.consequence is consequence]

    @property
    def needing_reread(self) -> list[AffectedRule]:
        """Signed rules whose own text is intact but whose meaning moved."""
        return [
            a
            for a in self.affected
            if a.consequence is Consequence.REREAD and a.was_certified
        ]

    def headline(self) -> str:
        if not self.affected:
            return "No compiled rule points at a clause that changed."
        lost = self.signatures_lost
        second = len(self.needing_reread)
        tail = (
            f" A further {second} rule(s) did not change at all, but point at a "
            "clause that did, so what they require has moved underneath them."
            if second
            else ""
        )
        if not lost:
            return (
                f"{len(self.affected)} rule(s) are affected. No certification is "
                "lost, because none of the directly amended ones were signed." + tail
            )
        return (
            f"{lost} certification(s) no longer cover the text they were signed "
            f"over, out of {self.certified_before}. Those rules must be reviewed "
            "and signed again before they can execute." + tail
        )

    def to_json(self) -> dict:
        return {
            "before": self.before_label,
            "after": self.after_label,
            "headline": self.headline(),
            "certified_before": self.certified_before,
            "certified_after": self.certified_after,
            "signatures_lost": self.signatures_lost,
            "by_consequence": {
                c.value: len(self.of(c)) for c in Consequence if self.of(c)
            },
            "new_clauses_with_no_rule": self.new_clauses,
            "affected": [a.to_json() for a in self.affected],
        }


_CONSEQUENCE = {
    ChangeKind.MODIFIED: Consequence.RECERTIFY,
    ChangeKind.REMOVED: Consequence.WITHDRAW,
    ChangeKind.RENUMBERED: Consequence.REPOINT,
}


def assess_impact(
    diff: TreeDiff,
    obligations: list[Obligation],
    *,
    references=None,
) -> ImpactReport:
    """Map clause changes onto the rules compiled from them.

    ``references`` is an optional :class:`ReferenceGraph`. Given one, the
    report also follows citations: a clause that incorporates an amended clause
    by reference is reported even though its own text never moved. That is the
    case a text diff cannot see and a reader will not find.
    """
    by_clause: dict[str, list[Obligation]] = {}
    for obligation in obligations:
        by_clause.setdefault(obligation.source.clause_id, []).append(obligation)

    report = ImpactReport(
        before_label=diff.before_label,
        after_label=diff.after_label,
        certified_before=sum(
            1 for o in obligations if o.status is RuleStatus.CERTIFIED
        ),
    )

    for change in diff.changes:
        if change.kind is ChangeKind.ADDED:
            if change.clause_id not in by_clause:
                report.new_clauses.append(change.clause_id)
            continue

        consequence = _CONSEQUENCE.get(change.kind)
        if consequence is None:
            continue  # UNCHANGED

        for obligation in by_clause.get(change.clause_id, []):
            if obligation.status in (RuleStatus.REJECTED, RuleStatus.SUPERSEDED):
                continue  # already inert, nothing to lose

            certified = obligation.status is RuleStatus.CERTIFIED
            report.affected.append(
                AffectedRule(
                    obligation_id=obligation.id,
                    clause_id=change.clause_id,
                    # A proposed rule loses nothing a human decided, so
                    # recompiling is enough. A certified one always goes back.
                    consequence=consequence if certified else Consequence.RECOMPILE,
                    change=change.kind,
                    was_certified=certified,
                    certified_by=(
                        obligation.certification.certified_by
                        if certified and obligation.certification
                        else None
                    ),
                    signature=(
                        obligation.certification.signature
                        if certified and obligation.certification
                        else None
                    ),
                    now_at=change.now_at,
                )
            )

    # Second order: clauses whose own text is untouched but which point at a
    # clause that moved. Only computed when a reference graph is supplied, and
    # kept strictly after the direct hits so the two are never conflated.
    if references is not None:
        directly_hit = {
            c.clause_id
            for c in diff.changes
            if c.kind in (ChangeKind.MODIFIED, ChangeKind.REMOVED, ChangeKind.RENUMBERED)
        }
        already = {a.obligation_id for a in report.affected}

        for changed in sorted(directly_hit):
            for citer, hops in sorted(references.dependents_of(changed).items()):
                if citer in directly_hit:
                    continue  # it changed on its own account, already reported
                for obligation in by_clause.get(citer, []):
                    if obligation.id in already:
                        continue
                    if obligation.status in (RuleStatus.REJECTED, RuleStatus.SUPERSEDED):
                        continue
                    already.add(obligation.id)
                    certified = obligation.status is RuleStatus.CERTIFIED
                    report.affected.append(
                        AffectedRule(
                            obligation_id=obligation.id,
                            clause_id=citer,
                            consequence=Consequence.REREAD,
                            change=ChangeKind.UNCHANGED,
                            was_certified=certified,
                            certified_by=(
                                obligation.certification.certified_by
                                if certified and obligation.certification
                                else None
                            ),
                            signature=(
                                obligation.certification.signature
                                if certified and obligation.certification
                                else None
                            ),
                            via=changed,
                            hops=hops,
                        )
                    )

    report.affected.sort(key=lambda a: (not a.was_certified, a.clause_id))
    report.new_clauses.sort()
    return report

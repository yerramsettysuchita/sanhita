"""What an amendment means for one firm, and what that firm must now do.

The problem statement is called *From Regulatory Text to Operational Action*,
and until this module the product stopped one step short of the title. It could
say what changed between two editions of a circular, and which certified rules
were affected. It could not say what **this firm** has to do about it, who owns
that, or when it is done.

That last step is the whole difficulty. A diff is a fact about a document. An
action is a fact about an organisation, and getting from one to the other needs
three things the product already holds separately:

    the change      which clause moved, and how          (diff.tree_diff)
    the rule        whether a signature still covers it  (diff.impact)
    the firm        who runs the process, on what system (controls.ControlStore)

This module is the join. It answers, for one declared framework and one firm:

    clause changed
        -> which certified obligation it feeds
        -> is that obligation one this firm is actually assessed against
        -> which process, team, system and control it touches
        -> what the firm must do about it, in words a person can act on
        -> and is that done yet

**What it does not do.** It does not decide anything a person should decide. It
does not re-certify a rule, it does not edit a control, and it does not close
its own work. The consequence of a change is computed from the signature and
the text, both of which are facts; everything after that is a task somebody
owns. The module ranks and explains, and a person acts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sanhita.diff.impact import AffectedRule, Consequence, ImpactReport

__all__ = ["ActionKind", "RequiredAction", "ChangePlan", "plan_for_firm"]


class ActionKind(str, Enum):
    """What a firm has to do about one changed rule.

    Deliberately small. Each one is a different kind of work, done by a
    different person, and a list that blurs them is a list nobody can assign.
    """

    #: The words a named officer signed are gone. Somebody has to read the new
    #: text and sign again before the rule can produce a finding for this firm.
    RECERTIFY = "RECERTIFY"
    #: The clause was deleted. The duty is gone and the rule should be
    #: withdrawn, which is work: a control built for it may still be running.
    WITHDRAW = "WITHDRAW"
    #: Same words, new number. Nothing about the duty changed, so this is a
    #: clerical repoint rather than a compliance question.
    REPOINT = "REPOINT"
    #: The clause is untouched but something it refers to moved. Nobody's
    #: signature is invalid; a person has to read it again and decide.
    REREAD = "REREAD"
    #: A new clause with no rule yet. Somebody has to decide whether it carries
    #: a duty for this firm at all.
    ASSESS_NEW = "ASSESS_NEW"

    @property
    def label(self) -> str:
        return {
            ActionKind.RECERTIFY: "Re-certify the rule",
            ActionKind.WITHDRAW: "Withdraw the rule",
            ActionKind.REPOINT: "Repoint the citation",
            ActionKind.REREAD: "Read it again",
            ActionKind.ASSESS_NEW: "Decide whether this applies",
        }[self]

    @property
    def urgency(self) -> int:
        """Sort order. Lost signatures first, clerical work last."""
        return {
            ActionKind.RECERTIFY: 0,
            ActionKind.WITHDRAW: 1,
            ActionKind.REREAD: 2,
            ActionKind.ASSESS_NEW: 3,
            ActionKind.REPOINT: 4,
        }[self]


_FROM_CONSEQUENCE = {
    Consequence.RECERTIFY: ActionKind.RECERTIFY,
    Consequence.WITHDRAW: ActionKind.WITHDRAW,
    Consequence.REPOINT: ActionKind.REPOINT,
    Consequence.REREAD: ActionKind.REREAD,
}


@dataclass(frozen=True)
class RequiredAction:
    """One thing this firm has to do because the regulation changed."""

    kind: ActionKind
    obligation_id: str
    clause_id: str
    #: What the rule asks for, so the row is readable without opening it.
    requirement: str = ""
    #: Whether a named person had signed this rule before the amendment. An
    #: unsigned proposal changing is not the same event as a signed rule losing
    #: the text it was signed over.
    was_certified: bool = False
    certified_by: str | None = None
    #: Where the clause moved to, for a repoint.
    now_at: str | None = None
    #: For a re-read: the clause that actually changed, and how far away it is.
    via: str | None = None
    hops: int | None = None

    # -- the firm's side of it
    #
    # Empty when the firm has never said who runs this duty. That is itself
    # worth showing: an amendment to a rule nobody owns is an amendment nobody
    # will act on.
    process: str = ""
    function: str = ""
    system: str = ""
    control_ref: str = ""

    @property
    def is_owned(self) -> bool:
        return bool(self.function or self.process)

    @property
    def touches_operations(self) -> bool:
        """Whether acting on this means changing how the firm runs, not just
        how the rulebook is filed."""
        return self.kind in (ActionKind.RECERTIFY, ActionKind.WITHDRAW) and self.is_owned

    def describe(self) -> str:
        """One sentence a person could put in a task, without adjectives."""
        where = ""
        if self.process and self.function:
            where = f" It is run by {self.function} as part of {self.process}."
        elif self.function:
            where = f" It is run by {self.function}."
        elif self.process:
            where = f" It sits in {self.process}."

        if self.kind is ActionKind.RECERTIFY:
            who = f" {self.certified_by} signed the previous text." if self.certified_by else ""
            return (
                f"Clause {self.clause_id} changed, so the words this rule was "
                f"signed over are gone.{who} It has to be read and signed again "
                f"before it can produce a finding.{where}"
            )
        if self.kind is ActionKind.WITHDRAW:
            return (
                f"Clause {self.clause_id} was removed, so this duty no longer "
                f"exists. Withdraw the rule and check whether anything the firm "
                f"still does for it can stop.{where}"
            )
        if self.kind is ActionKind.REPOINT:
            return (
                f"Clause {self.clause_id} kept its words and moved to "
                f"{self.now_at}. Nothing about the duty changed; the citation "
                f"has to point at the new number.{where}"
            )
        if self.kind is ActionKind.REREAD:
            hop = f", {self.hops} reference away" if self.hops else ""
            return (
                f"Clause {self.clause_id} did not change, but {self.via} did"
                f"{hop}, and this rule depends on it. A text diff calls this "
                f"clause untouched. Somebody has to read it again.{where}"
            )
        return (
            f"Clause {self.clause_id} is new and no rule has been drawn from it. "
            "Somebody has to decide whether it carries a duty for this firm."
        )


@dataclass
class ChangePlan:
    """Everything one firm has to do about one amendment."""

    firm: str
    framework: str
    before_label: str
    after_label: str
    actions: list[RequiredAction] = field(default_factory=list)
    #: Certified rules that came through the amendment untouched. Reported
    #: because "nothing to do here" is an answer somebody needs, and a plan
    #: that only lists work makes an amendment look larger than it is.
    unaffected: int = 0

    @property
    def total(self) -> int:
        return len(self.actions)

    @property
    def signatures_lost(self) -> int:
        """Actions where a certification no longer covers what it was signed over.

        Deliberately the same set as ``AffectedRule.loses_a_signature``, so this
        number and ``unaffected`` come out of one arithmetic. A re-certify, a
        withdraw and a repoint all break the signature. A re-read does not: its
        own clause is untouched, so the officer still signed exactly the
        characters that are there. Counting the two together would overstate the
        damage, and overstating damage is how a tool teaches people to ignore it.
        """
        return sum(
            1
            for a in self.actions
            if a.was_certified and a.kind is not ActionKind.REREAD
        )

    @property
    def unowned(self) -> int:
        """Actions on duties nobody in the firm has been made responsible for."""
        return sum(1 for a in self.actions if not a.is_owned)

    def of(self, kind: ActionKind) -> list[RequiredAction]:
        return [a for a in self.actions if a.kind is kind]

    def by_process(self) -> dict[str, list[RequiredAction]]:
        """Grouped the way the firm is organised rather than the way the
        circular is numbered, because the work is assigned by team."""
        grouped: dict[str, list[RequiredAction]] = {}
        for action in self.actions:
            grouped.setdefault(action.process or "Not yet mapped", []).append(action)
        return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == "Not yet mapped", kv[0])))


def plan_for_firm(
    report: ImpactReport,
    obligations,
    controls,
    *,
    firm: str,
    framework: str,
    include_uncertified: bool = False,
) -> ChangePlan:
    """Turn a regulatory impact report into work this firm owns.

    ``include_uncertified`` is off by default. A proposal changing is not an
    event for a firm: nobody signed it, nothing was running against it, and
    recompiling handles it. Including it would bury the signatures that were
    actually lost under a much larger pile of nothing.
    """
    by_id = {o.id: o for o in obligations}
    plan = ChangePlan(
        firm=firm,
        framework=framework,
        before_label=report.before_label,
        after_label=report.after_label,
    )

    for affected in report.affected:
        if affected.consequence is Consequence.RECOMPILE and not include_uncertified:
            continue
        kind = _FROM_CONSEQUENCE.get(affected.consequence)
        if kind is None:
            continue
        plan.actions.append(_action(affected, kind, by_id, controls))

    for clause_id in report.new_clauses:
        plan.actions.append(
            RequiredAction(
                kind=ActionKind.ASSESS_NEW,
                obligation_id="",
                clause_id=clause_id,
            )
        )

    plan.actions.sort(key=lambda a: (a.kind.urgency, a.clause_id))
    plan.unaffected = max(0, report.certified_before - report.signatures_lost)
    return plan


def _action(affected: AffectedRule, kind: ActionKind, by_id, controls) -> RequiredAction:
    obligation = by_id.get(affected.obligation_id)
    binding = controls.get(affected.obligation_id) if controls is not None else None
    requirement = ""
    if obligation is not None and obligation.action is not None:
        verb = obligation.action.verb or ""
        obj = obligation.action.object or ""
        requirement = f"{verb} {obj}".strip()

    return RequiredAction(
        kind=kind,
        obligation_id=affected.obligation_id,
        clause_id=affected.clause_id,
        requirement=requirement,
        was_certified=affected.was_certified,
        certified_by=affected.certified_by,
        now_at=affected.now_at,
        via=affected.via,
        hops=affected.hops,
        process=getattr(binding, "process", "") or "",
        function=getattr(binding, "function", "") or "",
        system=getattr(binding, "system", "") or "",
        control_ref=getattr(binding, "control_ref", "") or "",
    )

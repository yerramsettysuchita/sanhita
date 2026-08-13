"""The agentic layer, and the precise limit on what it is allowed to decide.

The problem statement is called *Agentic Compliance*, and the honest reading of
that word is the difficult part. A system that lets a model decide whether a
firm complies is not agentic, it is unaccountable: it will give a fluent answer,
a different one next Tuesday, and no way to show an inspector why.

So the agency here is in the **coordination**, never in the judgement:

    detect      a later edition exists                   (discover, monitor)
    investigate what changed, clause by clause and hash  (diff)
    gather      which signed rules, whose process, team  (impact, controls)
    recommend   what this firm would have to do about it (change)
    APPROVE     a named person says yes                  <- the boundary
    execute     tasks are created, deterministically     (remediate)
    verify      the store and the document agree         (remediate.amendment)

Everything above the boundary is arithmetic over hashes and bindings. Nothing
above it is a model's opinion, and nothing above it changes the firm's records.
Everything below it happens only because somebody approved.

**Why a plan object and not just the button.** The comparison screen could
already raise one task at a time, which is fine for three actions and useless
for eighty-two. What was missing is the thing a compliance officer actually
needs: one artifact that says "here is everything this amendment does to us,
here is who it lands on, approve it or do not", carrying the identity of the
person who approved and the fingerprints of the two editions it was computed
from. That artifact is auditable in a way a sequence of individual clicks is
not, because it records the decision rather than only its consequences.

**The plan is a proposal until approved and a record afterwards.** It is
recomputed from the diff every time it is displayed, so it cannot drift from
what the documents say. Only approval is stored.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = ["PlanStatus", "ActionPlan", "PlanStore", "plan_from_change"]


class PlanStatus(str, Enum):
    """Where one action plan has got to.

    Deliberately short. A status set that distinguishes five kinds of
    in-progress is a status set nobody maintains.
    """

    #: Computed from the diff and shown. Nothing has been created.
    PROPOSED = "PROPOSED"
    #: A named person approved it, and the tasks it named exist.
    APPROVED = "APPROVED"
    #: A named person looked and decided this firm does not act on it.
    DECLINED = "DECLINED"

    @property
    def label(self) -> str:
        return {
            PlanStatus.PROPOSED: "Awaiting approval",
            PlanStatus.APPROVED: "Approved",
            PlanStatus.DECLINED: "Declined",
        }[self]


def plan_id(*, firm: str, before: str, after: str) -> str:
    """A stable handle for one firm's response to one amendment.

    Derived from the firm and the two tree fingerprints, so re-opening the same
    comparison finds the same plan rather than proposing a second one. Two
    approvals of one amendment is two sets of tasks and nobody sure which is
    live.
    """
    material = "\x1f".join((firm, before, after))
    return "PLAN-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


@dataclass
class ActionPlan:
    """Everything one amendment asks of one firm, as one decision."""

    id: str
    firm: str
    framework: str
    before_label: str
    after_label: str
    before_fingerprint: str
    after_fingerprint: str
    status: PlanStatus = PlanStatus.PROPOSED

    # -- what the comparison found. Recomputed on every view; stored only so a
    #    closed plan can still say what it was about.
    obligations_affected: int = 0
    processes_affected: int = 0
    controls_affected: int = 0
    actions_recommended: int = 0
    signatures_lost: int = 0
    unowned: int = 0

    approved_by: str = ""
    approved_at: _dt.datetime | None = None
    note: str = ""
    #: The remediation tasks approving it created.
    task_ids: list[str] = field(default_factory=list)
    created_at: _dt.datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status is PlanStatus.PROPOSED

    def headline(self) -> str:
        """The sentence a compliance officer reads first."""
        if self.status is PlanStatus.APPROVED:
            when = self.approved_at.date().isoformat() if self.approved_at else ""
            return (
                f"{self.approved_by} approved this on {when}, and "
                f"{len(self.task_ids)} task(s) were created from it."
            )
        if self.status is PlanStatus.DECLINED:
            return (
                f"{self.approved_by} reviewed this and recorded that "
                f"{self.firm} takes no action on it."
            )
        return (
            f"{self.after_label} changes {self.obligations_affected} of "
            f"{self.firm}'s signed rules, across {self.processes_affected} "
            f"process(es) and {self.controls_affected} control(s). "
            f"{self.actions_recommended} action(s) are recommended. Nothing has "
            "been created."
        )

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "firm": self.firm,
            "framework": self.framework,
            "before_label": self.before_label,
            "after_label": self.after_label,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "status": self.status.value,
            "obligations_affected": self.obligations_affected,
            "processes_affected": self.processes_affected,
            "controls_affected": self.controls_affected,
            "actions_recommended": self.actions_recommended,
            "signatures_lost": self.signatures_lost,
            "unowned": self.unowned,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "note": self.note,
            "task_ids": list(self.task_ids),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_json(cls, raw: dict) -> ActionPlan:
        def when(key):
            value = raw.get(key)
            return _dt.datetime.fromisoformat(value) if value else None

        return cls(
            id=raw["id"],
            firm=raw.get("firm", ""),
            framework=raw.get("framework", ""),
            before_label=raw.get("before_label", ""),
            after_label=raw.get("after_label", ""),
            before_fingerprint=raw.get("before_fingerprint", ""),
            after_fingerprint=raw.get("after_fingerprint", ""),
            status=PlanStatus(raw.get("status", "PROPOSED")),
            obligations_affected=raw.get("obligations_affected", 0),
            processes_affected=raw.get("processes_affected", 0),
            controls_affected=raw.get("controls_affected", 0),
            actions_recommended=raw.get("actions_recommended", 0),
            signatures_lost=raw.get("signatures_lost", 0),
            unowned=raw.get("unowned", 0),
            approved_by=raw.get("approved_by", ""),
            approved_at=when("approved_at"),
            note=raw.get("note", ""),
            task_ids=list(raw.get("task_ids", [])),
            created_at=when("created_at"),
        )


def plan_from_change(
    change_plan,
    *,
    firm: str,
    framework: str,
    before_fingerprint: str,
    after_fingerprint: str,
) -> ActionPlan:
    """Turn the recomputed change plan into the decision a person is offered.

    Nothing is stored by calling this. The counts come straight off the diff,
    so a plan on screen can never disagree with the comparison beneath it.
    """
    processes = {a.process for a in change_plan.actions if a.process}
    controls = {a.control_ref for a in change_plan.actions if a.control_ref}
    return ActionPlan(
        id=plan_id(firm=firm, before=before_fingerprint, after=after_fingerprint),
        firm=firm,
        framework=framework,
        before_label=change_plan.before_label,
        after_label=change_plan.after_label,
        before_fingerprint=before_fingerprint,
        after_fingerprint=after_fingerprint,
        obligations_affected=len(
            {a.obligation_id for a in change_plan.actions if a.obligation_id}
        ),
        processes_affected=len(processes),
        controls_affected=len(controls),
        actions_recommended=change_plan.total,
        signatures_lost=change_plan.signatures_lost,
        unowned=change_plan.unowned,
    )


@dataclass
class PlanStore:
    """Approvals, kept beside the firm's other records.

    Only decisions live here. The plan's contents are recomputed from the two
    documents every time, because a stored copy of what an amendment means is a
    copy that can fall out of step with the amendment.
    """

    path: Path
    plans: dict[str, ActionPlan] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> PlanStore:
        store = cls(path=path)
        if not path.is_file():
            return store
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return store
        for row in data.get("plans", []):
            try:
                plan = ActionPlan.from_json(row)
            except (KeyError, ValueError):
                continue
            store.plans[plan.id] = plan
        return store

    def save(self) -> None:
        from sanhita.cli_compile import _write_atomically

        payload = {
            "version": 1,
            "plans": [p.to_json() for p in sorted(self.plans.values(), key=lambda x: x.id)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def get(self, plan_identifier: str) -> ActionPlan | None:
        return self.plans.get(plan_identifier)

    def decision_on(self, plan: ActionPlan) -> ActionPlan:
        """The stored decision for this plan, or the proposal unchanged."""
        return self.plans.get(plan.id, plan)

    def approve(
        self,
        plan: ActionPlan,
        *,
        by: str,
        task_ids=(),
        note: str = "",
        at: _dt.datetime | None = None,
    ) -> ActionPlan:
        """Record that a named person accepted this plan.

        Approving is the only thing in this module that writes anything, which
        is the whole design: the machine may compute and recommend all day, and
        the firm's records change when a person says so.
        """
        if not by.strip():
            raise ValueError("an action plan is approved by a named person")
        existing = self.plans.get(plan.id)
        if existing is not None and existing.status is not PlanStatus.PROPOSED:
            return existing
        plan.status = PlanStatus.APPROVED
        plan.approved_by = by.strip()
        plan.approved_at = at or _dt.datetime.now(_dt.timezone.utc)
        plan.note = note.strip()
        plan.task_ids = list(task_ids)
        plan.created_at = plan.created_at or plan.approved_at
        self.plans[plan.id] = plan
        return plan

    def decline(
        self,
        plan: ActionPlan,
        *,
        by: str,
        note: str = "",
        at: _dt.datetime | None = None,
    ) -> ActionPlan:
        """Record that a named person decided this firm does not act on it.

        Kept, rather than simply not approving. "We looked at the February
        amendment and concluded it does not touch us" is a defensible position
        and an answerable one; silence is neither.
        """
        if not by.strip():
            raise ValueError("a decision is recorded against a named person")
        existing = self.plans.get(plan.id)
        if existing is not None and existing.status is not PlanStatus.PROPOSED:
            return existing
        plan.status = PlanStatus.DECLINED
        plan.approved_by = by.strip()
        plan.approved_at = at or _dt.datetime.now(_dt.timezone.utc)
        plan.note = note.strip()
        plan.created_at = plan.created_at or plan.approved_at
        self.plans[plan.id] = plan
        return plan

    def all(self) -> list[ActionPlan]:
        return sorted(
            self.plans.values(),
            key=lambda p: (p.approved_at or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)),
            reverse=True,
        )

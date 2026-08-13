"""An amendment becomes a task somebody owns, and only a fact closes it.

The diff screen could say what a new edition costs a firm. Saying it is not
doing it. A compliance officer reading "25 signatures no longer cover their
clauses" has a list, not a workflow: nobody owns any line of it, nothing is
dated, and next quarter's inspection has no way to tell whether any of it was
ever acted on.

This module raises those lines as remediation tasks, in the same store and on
the same hash-chained log as the evidence findings, and then answers the only
question that matters afterwards: **is it done yet?**

The answer is never somebody's opinion. Each kind of amendment work has an end
state that is a fact about the store and the later document, and the re-check
below tests exactly that fact:

    RECERTIFY    a certification exists over the clause's *new* characters
    REPOINT      the rule's anchor is the new clause number, signed over it
    WITHDRAW     the rule is no longer live in the store
    REREAD       a person signed the rule again after the task was raised
    ASSESS_NEW   a rule from the new clause reached certified or rejected

Nobody can mark one of these complete. There is no button that closes a task,
here or anywhere else in the product, because a task you can close by saying so
records that you said so and nothing else. The REREAD case is the interesting
one: its clause never changed, so no hash can prove a person re-read it. What
can be proved is that they signed it again, on a date after the amendment
landed, and that is what closure requires.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass

from sanhita.ir.enums import RuleStatus
from sanhita.remediate.tasks import (
    Priority,
    RemediationError,
    RemediationStore,
    RemediationTask,
    Transition,
)

__all__ = [
    "AmendmentRecheck",
    "action_gap_id",
    "open_for_action",
    "recheck_amendment_task",
]


# --------------------------------------------------------------- identity


def action_gap_id(action, *, before_fingerprint: str, after_fingerprint: str) -> str:
    """A stable id for one required action on one amendment.

    Derived rather than counted, so raising the same action twice is the same
    id and :meth:`RemediationStore.open_for_gap` returns the task that already
    exists. Two tasks for one lost signature means two people re-certifying it
    and neither certain the other did.

    Both fingerprints are inside it, so the same clause losing its signature
    again in a later edition is a different piece of work with a different id.
    """
    material = "\x1f".join(
        (
            before_fingerprint,
            after_fingerprint,
            action.kind.value,
            action.obligation_id or "",
            action.clause_id,
        )
    )
    return "AMD-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


_EVIDENCE_REQUIRED = {
    "RECERTIFY": (
        "A certification over the clause's new characters, signed by a named "
        "officer. Nothing else closes this."
    ),
    "REPOINT": (
        "The rule anchored to the new clause number and signed over the text "
        "found there."
    ),
    "WITHDRAW": "The rule withdrawn from the live store.",
    "REREAD": (
        "The rule signed again, dated after this amendment. The clause did not "
        "change, so a signature is the only record that a person read it again."
    ),
    "ASSESS_NEW": (
        "A rule compiled from the new clause and either certified or rejected. "
        "Either is a decision; leaving it proposed is not."
    ),
}


def open_for_action(
    store: RemediationStore,
    action,
    *,
    company: str,
    by: str,
    before_fingerprint: str,
    after_fingerprint: str,
    priority: Priority | None = None,
    due_date: _dt.date | None = None,
    source_rule_version: str = "",
    at: _dt.datetime | None = None,
) -> RemediationTask:
    """Raise one required action as a task in the remediation store.

    The task carries the amendment it came from, so a re-check months later can
    still say which two editions this was about.
    """
    gap_id = action_gap_id(
        action,
        before_fingerprint=before_fingerprint,
        after_fingerprint=after_fingerprint,
    )
    if priority is None:
        priority = (
            Priority.HIGH
            if action.kind.value in ("RECERTIFY", "WITHDRAW")
            else Priority.MEDIUM
        )

    task = store.open_for_gap(
        gap_id=gap_id,
        obligation_id=action.obligation_id,
        clause_id=action.clause_id,
        company=company,
        title=f"{action.kind.label}: clause {action.clause_id}",
        by=by,
        remediation_action=action.describe(),
        evidence_required=_EVIDENCE_REQUIRED.get(action.kind.value, ""),
        assigned_team=action.function,
        priority=priority,
        due_date=due_date,
        source_rule_version=source_rule_version,
        at=at,
    )
    # Set on the returned task whether it was created now or already existed,
    # so a store written before this module gained the fields is repaired
    # rather than left half described.
    task.change_kind = action.kind.value
    task.change_now_at = action.now_at or ""
    task.amended_from = before_fingerprint
    task.amended_to = after_fingerprint
    return task


# ---------------------------------------------------------------- closure


@dataclass(frozen=True)
class AmendmentRecheck:
    """What the store and the later document actually say about this task."""

    task: RemediationTask
    done: bool
    detail: str
    #: False where the question could not be answered at all, which is not a
    #: pass. A clause that is missing from the later tree, or a task whose kind
    #: predates this module, leaves the task exactly where it was.
    evaluated: bool = True

    @property
    def closes(self) -> bool:
        return self.done and self.evaluated


def _clause_sha(tree, clause_id: str) -> str | None:
    node = tree.get(clause_id) if tree is not None else None
    return node.sha256 if node is not None else None


def _current(obligations, obligation_id: str):
    for obligation in obligations:
        if obligation.id == obligation_id:
            return obligation
    return None


def _verdict(task: RemediationTask, obligations, tree) -> AmendmentRecheck:
    """The whole judgement, as a pure function of the store and the tree."""
    kind = task.change_kind
    rule = _current(obligations, task.obligation_id) if task.obligation_id else None

    if kind == "WITHDRAW":
        if rule is None:
            return AmendmentRecheck(task, True, "the rule is no longer in the store")
        if rule.status in (RuleStatus.REJECTED, RuleStatus.SUPERSEDED):
            return AmendmentRecheck(
                task, True, f"the rule is {rule.status.value.lower()}"
            )
        return AmendmentRecheck(
            task, False, f"the rule is still {rule.status.value.lower()} in the store"
        )

    if kind == "ASSESS_NEW":
        decided = [
            o
            for o in obligations
            if o.source.clause_id == task.clause_id
            and o.status in (RuleStatus.CERTIFIED, RuleStatus.REJECTED)
        ]
        if decided:
            return AmendmentRecheck(
                task,
                True,
                f"{len(decided)} rule(s) from clause {task.clause_id} have been "
                f"decided: {', '.join(sorted({o.status.value.lower() for o in decided}))}",
            )
        return AmendmentRecheck(
            task,
            False,
            f"nothing compiled from clause {task.clause_id} has been certified "
            "or rejected yet",
        )

    if rule is None:
        return AmendmentRecheck(
            task,
            False,
            f"rule {task.obligation_id} is not in this store, so there is "
            "nothing to check",
            evaluated=False,
        )

    if kind == "REREAD":
        # No hash can prove a person read a clause. A signature dated after the
        # amendment can prove they signed it knowing about the amendment.
        if rule.status is not RuleStatus.CERTIFIED or rule.certification is None:
            return AmendmentRecheck(task, False, "the rule is not certified")
        signed_at = rule.certification.certified_at
        raised = task.created_at
        if raised is None or signed_at > raised:
            return AmendmentRecheck(
                task,
                True,
                f"signed again by {rule.certification.certified_by} on "
                f"{signed_at.date().isoformat()}, after this was raised",
            )
        return AmendmentRecheck(
            task,
            False,
            "the standing signature predates this amendment, so nothing records "
            "that anybody read the clause again",
        )

    target_clause = task.change_now_at if kind == "REPOINT" else task.clause_id
    expected = _clause_sha(tree, target_clause)
    if expected is None:
        return AmendmentRecheck(
            task,
            False,
            f"clause {target_clause} is not in the later document, so its text "
            "cannot be compared",
            evaluated=False,
        )
    if rule.status is not RuleStatus.CERTIFIED:
        return AmendmentRecheck(
            task, False, f"the rule is {rule.status.value.lower()}, not certified"
        )
    if kind == "REPOINT" and rule.source.clause_id != target_clause:
        return AmendmentRecheck(
            task,
            False,
            f"the rule still points at clause {rule.source.clause_id} rather "
            f"than {target_clause}",
        )
    if rule.source.sha256 != expected:
        return AmendmentRecheck(
            task,
            False,
            "the certification still covers the earlier text: signed over "
            f"{rule.source.sha256[:12]}, the document now holds {expected[:12]}",
        )
    return AmendmentRecheck(
        task,
        True,
        f"certified by {rule.certification.certified_by} over the current text "
        f"of clause {target_clause} ({expected[:12]})",
    )


def recheck_amendment_task(
    store: RemediationStore,
    task_id: str,
    obligations,
    tree,
    *,
    by: str,
    at: _dt.datetime | None = None,
) -> AmendmentRecheck:
    """Ask the store and the later document whether this work is done.

    Records the outcome on the same hash-chained log as every other transition,
    and closes the task only when the fact it named is true. An unanswerable
    question leaves the task exactly where it was, because "could not check" is
    not "checked and passed".
    """
    task = store.get(task_id)
    if task is None:
        raise RemediationError(f"No remediation task {task_id!r}.")
    if not task.change_kind:
        raise RemediationError(
            f"{task_id} was raised from an evidence finding, not an amendment. "
            "Re-check it with sanhita.remediate.service.recheck_task, which "
            "runs the rule against the records."
        )

    verdict = _verdict(task, obligations, tree)
    moment = at or _dt.datetime.now(_dt.timezone.utc)
    before = task.status
    task.recheck_count += 1
    task.last_recheck_at = moment
    task.last_recheck_result = verdict.detail

    store.log.append(
        task_id=task_id,
        transition=Transition.RECHECKED,
        actor=by,
        from_status=before.value,
        to_status=before.value,
        note=verdict.detail,
        detail={
            "change_kind": task.change_kind,
            "evaluated": verdict.evaluated,
            "done": verdict.done,
            "run": task.recheck_count,
        },
        at=moment,
    )

    if verdict.closes:
        from sanhita.remediate.tasks import TaskStatus

        task.status = TaskStatus.VERIFIED
        task.verified_by = by or "the store"
        task.verified_at = moment
        store.log.append(
            task_id=task_id,
            transition=Transition.VERIFIED,
            actor=by,
            from_status=before.value,
            to_status=task.status.value,
            note=verdict.detail,
            at=moment,
        )
        task.status = TaskStatus.CLOSED
        task.closed_at = moment
        store.log.append(
            task_id=task_id,
            transition=Transition.CLOSED,
            actor=by,
            from_status=TaskStatus.VERIFIED.value,
            to_status=task.status.value,
            note="closed because the store and the document say it is done",
            at=moment,
        )
    return verdict

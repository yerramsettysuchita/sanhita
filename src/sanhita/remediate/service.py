"""Where a remediation task meets the deterministic engine.

:meth:`RemediationStore.recheck` takes a boolean and writes the consequence. It
does not decide anything. This module is what decides, and it decides by running
the same certified rule through the same engine that raised the finding in the
first place.

That separation is the whole point. If the store could conclude "fixed" on its
own, closure would be an opinion held by whoever clicked the button. Here the
only input to the conclusion is what
:class:`~sanhita.execute.engine.RuleEngine` returned, so a closed gap means the
regulation's own test was applied again and passed.
"""

from __future__ import annotations

import datetime as _dt

from sanhita.execute import WEEKENDS_ONLY, EvidenceStore, RuleEngine
from sanhita.execute.report import Outcome
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation
from sanhita.remediate.tasks import (
    RemediationError,
    RemediationStore,
    RemediationTask,
    Transition,
)

__all__ = ["RecheckResult", "recheck_task", "suggested_due_date"]

#: How long a firm gets to fix a gap, by severity of the finding. Short enough
#: to be a deadline, long enough to be met.
_DAYS_BY_PRIORITY = {"HIGH": 7, "MEDIUM": 21, "LOW": 45}


def suggested_due_date(priority: str, *, today: _dt.date | None = None) -> _dt.date:
    """A default deadline. The officer setting the task can override it."""
    start = today or _dt.date.today()
    return start + _dt.timedelta(days=_DAYS_BY_PRIORITY.get(priority.upper(), 21))


class RecheckResult:
    """What running the rule again actually found."""

    def __init__(
        self,
        *,
        task: RemediationTask,
        still_failing: bool,
        findings: list,
        detail: str,
        evaluated: bool,
    ) -> None:
        self.task = task
        self.still_failing = still_failing
        self.findings = findings
        self.detail = detail
        #: False where the engine declined to evaluate the rule at all. That is
        #: not a pass, and it must not close a task.
        self.evaluated = evaluated

    @property
    def closed(self) -> bool:
        return not self.still_failing and self.evaluated


def recheck_task(
    store: RemediationStore,
    task_id: str,
    obligations: list[Obligation],
    evidence: EvidenceStore,
    *,
    by: str,
    as_of: _dt.date | None = None,
    calendar=None,
) -> RecheckResult:
    """Run the certified rule behind this task again, and record what happened.

    Three outcomes, and only one of them closes anything:

    * the engine finds no breach for this obligation, and the task closes
    * the engine still finds a breach, and the task reopens
    * the engine declines to evaluate the rule, and the task stays where it is

    The third case matters. A rule the engine will not evaluate produces no
    findings, and treating "no findings" as "compliant" would let a task be
    closed by making the rule unevaluable. So an unevaluated rule is reported
    as such and the task is left alone.
    """
    task = store.get(task_id)
    if task is None:
        raise RemediationError(f"No remediation task {task_id!r}.")

    subject = [
        o
        for o in obligations
        if o.id == task.obligation_id and o.status is RuleStatus.CERTIFIED
    ]
    if not subject:
        raise RemediationError(
            f"Rule {task.obligation_id} is not certified in this document, so "
            "there is nothing to re-run. A gap raised against a rule that was "
            "since rejected has to be closed by withdrawing the task, not by "
            "passing a check."
        )

    engine = RuleEngine(calendar or WEEKENDS_ONLY)
    report = engine.run(subject, evidence, as_of=as_of or _dt.date.today())

    unevaluable = [u for u in report.unevaluable if u.obligation_id == task.obligation_id]
    if unevaluable:
        reason = unevaluable[0].reason
        store.log.append(
            task_id=task_id,
            transition=Transition.RECHECKED,
            actor=by,
            from_status=task.status.value,
            to_status=task.status.value,
            note=f"the engine would not evaluate this rule: {reason}",
            detail={"evaluated": False},
        )
        return RecheckResult(
            task=task,
            still_failing=True,
            findings=[],
            detail=f"not evaluated: {reason}",
            evaluated=False,
        )

    # "No findings" is not the same as "checked and passed".
    #
    # A rule the engine considered not applicable produces no findings either,
    # and so does one it declined to evaluate. Reading either as a pass would
    # make closure obtainable by removing the rule's evidence requirement,
    # which is the cheapest possible way to fake compliance. The engine counts
    # what it actually evaluated, so require that count to be non-zero before
    # any of this can conclude anything.
    if report.rules_evaluated == 0:
        reason = (
            "the engine did not evaluate this rule in the re-check, so there is "
            "nothing to conclude from the absence of findings"
        )
        store.log.append(
            task_id=task_id,
            transition=Transition.RECHECKED,
            actor=by,
            from_status=task.status.value,
            to_status=task.status.value,
            note=reason,
            detail={"evaluated": False, "not_applicable": report.not_applicable},
        )
        return RecheckResult(
            task=task,
            still_failing=True,
            findings=[],
            detail=reason,
            evaluated=False,
        )

    findings = [
        f
        for f in report.findings
        if f.obligation_id == task.obligation_id
        and f.outcome is not Outcome.SATISFIED
    ]
    still_failing = bool(findings)

    if still_failing:
        worst = findings[0]
        detail = (
            f"{len(findings)} finding(s) remain, worst is "
            f"{worst.outcome.value.lower()} on {worst.due_on.isoformat()}"
        )
    else:
        detail = (
            f"the rule ran against {report.events_checked} occasion(s) and "
            "found no breach"
        )

    store.recheck(task_id, still_failing=still_failing, by=by, detail=detail)
    return RecheckResult(
        task=store.get(task_id),
        still_failing=still_failing,
        findings=findings,
        detail=detail,
        evaluated=True,
    )

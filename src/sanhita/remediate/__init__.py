"""Closing a compliance gap, not just reporting one.

Problem Statement 2 asks for two things about gaps, and the product only did
the first:

    "...identifying and remediating compliance gaps before they become
     regulatory findings."

Finding a gap is a report. Closing one is a piece of work somebody owns, with
a deadline, that ends in evidence and a re-run of the same certified rule. Until
that loop exists, a compliance officer reads the gap screen, writes the gap into
a spreadsheet, and the product has moved the problem rather than solved it.

**The loop is deterministic at the point that matters.** A task can be created,
assigned and worked by people, but a task is never closed because somebody says
the problem is fixed. It closes only when the certified rule is executed again
against the new evidence and the same deterministic engine returns no finding.
Nobody, including the person who owns the task, can mark a gap closed by
assertion. That is the same rule the rest of Sanhita follows: a human certifies
the interpretation once, and the machine decides the outcome every time after.

Every transition is written to an append-only, hash-chained log, built the same
way as the certification ledger and for the same reason. A remediation history
that can be edited afterwards proves nothing to an inspector.
"""

from sanhita.remediate.amendment import (
    AmendmentRecheck,
    action_gap_id,
    open_for_action,
    recheck_amendment_task,
)
from sanhita.remediate.tasks import (
    Priority,
    RemediationError,
    RemediationLog,
    RemediationStore,
    RemediationTask,
    TaskEntry,
    TaskStatus,
    Transition,
)

__all__ = [
    "AmendmentRecheck",
    "Priority",
    "RemediationError",
    "RemediationLog",
    "RemediationStore",
    "RemediationTask",
    "TaskEntry",
    "TaskStatus",
    "Transition",
    "action_gap_id",
    "open_for_action",
    "recheck_amendment_task",
]

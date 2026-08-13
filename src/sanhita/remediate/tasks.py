"""The remediation task, its lifecycle, and the log that proves it happened.

See the package docstring for why this exists. The important design decision is
in :meth:`RemediationStore.recheck`: a task cannot be closed by a person
asserting the problem is fixed. It closes when the certified rule runs again and
the deterministic engine returns no finding for it.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from enum import Enum
from pathlib import Path
from typing import Any

from sanhita.ir.canonical import sha256_hex

__all__ = [
    "Priority",
    "RemediationLog",
    "RemediationStore",
    "RemediationTask",
    "TaskEntry",
    "TaskStatus",
    "Transition",
]

GENESIS = "0" * 64


class TaskStatus(str, Enum):
    """Where a remediation task is in its life."""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    #: Somebody has done the work and the firm now owes the artifact.
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    #: Evidence has arrived. The rule has not been run against it yet.
    READY_FOR_RECHECK = "READY_FOR_RECHECK"
    #: The rule ran and returned no finding.
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"
    #: The rule ran and still finds a breach. Back to the owner.
    REOPENED = "REOPENED"

    @property
    def is_open(self) -> bool:
        return self not in (TaskStatus.VERIFIED, TaskStatus.CLOSED)

    @property
    def awaiting_work(self) -> bool:
        return self in (
            TaskStatus.OPEN,
            TaskStatus.IN_PROGRESS,
            TaskStatus.AWAITING_EVIDENCE,
            TaskStatus.REOPENED,
        )


class Priority(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Transition(str, Enum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    STATUS_CHANGED = "STATUS_CHANGED"
    EVIDENCE_ATTACHED = "EVIDENCE_ATTACHED"
    RECHECKED = "RECHECKED"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"


@dataclass(frozen=True, slots=True)
class TaskEntry:
    """One immutable record of one thing that happened to one task."""

    sequence: int
    task_id: str
    transition: Transition
    actor: str
    at: _dt.datetime
    from_status: str | None
    to_status: str
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    previous_hash: str = GENESIS
    entry_hash: str = ""

    def payload(self) -> dict[str, Any]:
        """Exactly the bytes the entry hash covers."""
        return {
            "sequence": self.sequence,
            "task_id": self.task_id,
            "transition": self.transition.value,
            "actor": self.actor,
            "at": self.at,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "note": self.note,
            "detail": self.detail,
            "previous_hash": self.previous_hash,
        }

    def compute_hash(self) -> str:
        return sha256_hex(self.payload())

    def to_json(self) -> dict:
        return {
            "sequence": self.sequence,
            "task_id": self.task_id,
            "transition": self.transition.value,
            "actor": self.actor,
            "at": self.at.isoformat(),
            "from_status": self.from_status,
            "to_status": self.to_status,
            "note": self.note,
            "detail": self.detail,
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }

    @classmethod
    def from_json(cls, raw: dict) -> TaskEntry:
        return cls(
            sequence=raw["sequence"],
            task_id=raw["task_id"],
            transition=Transition(raw["transition"]),
            actor=raw["actor"],
            at=_dt.datetime.fromisoformat(raw["at"]),
            from_status=raw.get("from_status"),
            to_status=raw["to_status"],
            note=raw.get("note", ""),
            detail=raw.get("detail", {}),
            previous_hash=raw.get("previous_hash", GENESIS),
            entry_hash=raw.get("entry_hash", ""),
        )


@dataclass
class RemediationLog:
    """An append-only, hash-chained sequence of task transitions.

    Deliberately a second chain rather than entries added to the certification
    ledger. That ledger's entries are covered by signatures over obligations,
    and remediation is a different kind of fact: what a firm did about a
    finding, not what the regulation says. Mixing them would put operational
    workflow inside the artifact a regulator relies on.
    """

    entries: list[TaskEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def append(
        self,
        *,
        task_id: str,
        transition: Transition,
        actor: str,
        from_status: str | None,
        to_status: str,
        note: str = "",
        detail: dict[str, Any] | None = None,
        at: _dt.datetime | None = None,
    ) -> TaskEntry:
        previous = self.entries[-1].entry_hash if self.entries else GENESIS
        entry = TaskEntry(
            sequence=len(self.entries) + 1,
            task_id=task_id,
            transition=transition,
            actor=actor or "unattributed",
            at=at or _dt.datetime.now(_dt.timezone.utc),
            from_status=from_status,
            to_status=to_status,
            note=note,
            detail=detail or {},
            previous_hash=previous,
        )
        # `replace` rather than mutation: the entry is frozen on purpose, so
        # the hash is sealed onto a copy and the original never existed
        # unhashed anywhere the caller can reach.
        entry = _replace(entry, entry_hash=entry.compute_hash())
        self.entries.append(entry)
        return entry

    def for_task(self, task_id: str) -> list[TaskEntry]:
        return [e for e in self.entries if e.task_id == task_id]

    def verify(self) -> tuple[bool, str]:
        """Walk the chain. Returns (intact, first problem or empty string)."""
        previous = GENESIS
        for index, entry in enumerate(self.entries, start=1):
            if entry.sequence != index:
                return False, f"entry {index} is numbered {entry.sequence}"
            if entry.previous_hash != previous:
                return False, f"entry {index} does not follow the one before it"
            if entry.entry_hash != entry.compute_hash():
                return False, f"entry {index} has been altered since it was written"
            previous = entry.entry_hash
        return True, ""


@dataclass
class RemediationTask:
    """One piece of work that closes one gap."""

    task_id: str
    #: The finding this came from, as the engine identified it.
    gap_id: str
    obligation_id: str
    clause_id: str
    company: str

    title: str
    #: What the firm has to actually do.
    remediation_action: str = ""
    #: What has to arrive before a re-check can mean anything.
    evidence_required: str = ""

    owner: str = ""
    assigned_team: str = ""
    priority: Priority = Priority.MEDIUM
    status: TaskStatus = TaskStatus.OPEN

    created_at: _dt.datetime | None = None
    due_date: _dt.date | None = None
    closed_at: _dt.datetime | None = None

    #: Evidence event ids attached in the course of fixing this.
    evidence_ids: list[str] = field(default_factory=list)

    verified_by: str = ""
    verified_at: _dt.datetime | None = None
    #: What the last re-check found. Empty until one has run.
    last_recheck_result: str = ""
    last_recheck_at: _dt.datetime | None = None
    recheck_count: int = 0

    #: The version of the certified rule this gap was raised against. If the
    #: rule is amended later, a reviewer needs to know which text the task was
    #: written for.
    source_rule_version: str = ""

    # -- set only on tasks raised from an amendment rather than an evidence
    #    finding. A firm's remediation queue holds both kinds, and they close
    #    on different facts: an evidence task closes when the rule is run again
    #    and finds no breach, an amendment task when the store and the later
    #    document agree the rulebook was put right. See
    #    :mod:`sanhita.remediate.amendment`.
    #: RECERTIFY, WITHDRAW, REPOINT, REREAD or ASSESS_NEW. Empty on an
    #: evidence task, which is how the two are told apart.
    change_kind: str = ""
    #: The clause number the text moved to, for a repoint.
    change_now_at: str = ""
    #: Tree fingerprints of the two editions compared, so a re-check months
    #: later can still name the amendment this was about.
    amended_from: str = ""
    amended_to: str = ""

    @property
    def is_from_an_amendment(self) -> bool:
        return bool(self.change_kind)

    def is_overdue(self, today: _dt.date | None = None) -> bool:
        """Past its due date and not yet closed.

        Derived rather than stored. A stored OVERDUE status would be wrong the
        moment the clock passed midnight and nobody had run a job.
        """
        if self.due_date is None or not self.status.is_open:
            return False
        return (today or _dt.date.today()) > self.due_date

    def days_remaining(self, today: _dt.date | None = None) -> int | None:
        if self.due_date is None:
            return None
        return (self.due_date - (today or _dt.date.today())).days

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "gap_id": self.gap_id,
            "obligation_id": self.obligation_id,
            "clause_id": self.clause_id,
            "company": self.company,
            "title": self.title,
            "remediation_action": self.remediation_action,
            "evidence_required": self.evidence_required,
            "owner": self.owner,
            "assigned_team": self.assigned_team,
            "priority": self.priority.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "evidence_ids": list(self.evidence_ids),
            "verified_by": self.verified_by,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "last_recheck_result": self.last_recheck_result,
            "last_recheck_at": (
                self.last_recheck_at.isoformat() if self.last_recheck_at else None
            ),
            "recheck_count": self.recheck_count,
            "source_rule_version": self.source_rule_version,
            "change_kind": self.change_kind,
            "change_now_at": self.change_now_at,
            "amended_from": self.amended_from,
            "amended_to": self.amended_to,
        }

    @classmethod
    def from_json(cls, raw: dict) -> RemediationTask:
        def when(key: str):
            value = raw.get(key)
            return _dt.datetime.fromisoformat(value) if value else None

        return cls(
            task_id=raw["task_id"],
            gap_id=raw["gap_id"],
            obligation_id=raw["obligation_id"],
            clause_id=raw.get("clause_id", ""),
            company=raw.get("company", ""),
            title=raw.get("title", ""),
            remediation_action=raw.get("remediation_action", ""),
            evidence_required=raw.get("evidence_required", ""),
            owner=raw.get("owner", ""),
            assigned_team=raw.get("assigned_team", ""),
            priority=Priority(raw.get("priority", "MEDIUM")),
            status=TaskStatus(raw.get("status", "OPEN")),
            created_at=when("created_at"),
            due_date=(
                _dt.date.fromisoformat(raw["due_date"]) if raw.get("due_date") else None
            ),
            closed_at=when("closed_at"),
            evidence_ids=list(raw.get("evidence_ids", [])),
            verified_by=raw.get("verified_by", ""),
            verified_at=when("verified_at"),
            last_recheck_result=raw.get("last_recheck_result", ""),
            last_recheck_at=when("last_recheck_at"),
            recheck_count=raw.get("recheck_count", 0),
            source_rule_version=raw.get("source_rule_version", ""),
            change_kind=raw.get("change_kind", ""),
            change_now_at=raw.get("change_now_at", ""),
            amended_from=raw.get("amended_from", ""),
            amended_to=raw.get("amended_to", ""),
        )


class RemediationError(RuntimeError):
    """A transition that the lifecycle does not allow."""


@dataclass
class RemediationStore:
    """Every remediation task for one workspace, plus the log that proves it."""

    path: Path
    tasks: dict[str, RemediationTask] = field(default_factory=dict)
    log: RemediationLog = field(default_factory=RemediationLog)

    # -------------------------------------------------------------- on disk

    @classmethod
    def load(cls, path: Path) -> RemediationStore:
        store = cls(path=path)
        if not path.is_file():
            return store
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt sidecar must never take the rulebook down with it.
            return store
        for row in data.get("tasks", []):
            try:
                task = RemediationTask.from_json(row)
            except (KeyError, ValueError):
                continue
            store.tasks[task.task_id] = task
        for row in data.get("log", []):
            try:
                store.log.entries.append(TaskEntry.from_json(row))
            except (KeyError, ValueError):
                continue
        return store

    def save(self) -> None:
        from sanhita.cli_compile import _write_atomically

        payload = {
            "version": 1,
            "tasks": [
                t.to_json() for t in sorted(self.tasks.values(), key=lambda x: x.task_id)
            ],
            "log": [e.to_json() for e in self.log.entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    # ------------------------------------------------------------ lifecycle

    def _next_id(self, clause_id: str) -> str:
        stem = clause_id.replace(" ", "")
        existing = sum(1 for t in self.tasks.values() if t.clause_id == clause_id)
        return f"REM-{stem}-{existing + 1:03d}"

    def open_for_gap(
        self,
        *,
        gap_id: str,
        obligation_id: str,
        clause_id: str,
        company: str,
        title: str,
        by: str,
        remediation_action: str = "",
        evidence_required: str = "",
        owner: str = "",
        assigned_team: str = "",
        priority: Priority = Priority.MEDIUM,
        due_date: _dt.date | None = None,
        source_rule_version: str = "",
        at: _dt.datetime | None = None,
    ) -> RemediationTask:
        """Raise a task against one finding.

        Raising the same gap twice returns the existing open task rather than
        creating a second one. Two tasks for one breach means two people fixing
        it and neither certain the other did.
        """
        for task in self.tasks.values():
            if task.gap_id == gap_id and task.status.is_open:
                return task

        moment = at or _dt.datetime.now(_dt.timezone.utc)
        task = RemediationTask(
            task_id=self._next_id(clause_id),
            gap_id=gap_id,
            obligation_id=obligation_id,
            clause_id=clause_id,
            company=company,
            title=title,
            remediation_action=remediation_action,
            evidence_required=evidence_required,
            owner=owner.strip(),
            assigned_team=assigned_team.strip(),
            priority=priority,
            status=TaskStatus.OPEN,
            created_at=moment,
            due_date=due_date,
            source_rule_version=source_rule_version,
        )
        self.tasks[task.task_id] = task
        self.log.append(
            task_id=task.task_id,
            transition=Transition.CREATED,
            actor=by,
            from_status=None,
            to_status=task.status.value,
            note=title,
            detail={
                "obligation_id": obligation_id,
                "clause_id": clause_id,
                "gap_id": gap_id,
                "due_date": due_date.isoformat() if due_date else None,
                "priority": priority.value,
            },
            at=moment,
        )
        return task

    def get(self, task_id: str) -> RemediationTask | None:
        return self.tasks.get(task_id)

    def _require(self, task_id: str) -> RemediationTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise RemediationError(f"No remediation task {task_id!r}.")
        return task

    def assign(
        self, task_id: str, *, owner: str, by: str, team: str = "", note: str = ""
    ) -> RemediationTask:
        task = self._require(task_id)
        if not owner.strip():
            raise RemediationError("A task needs a named owner.")
        before = task.status
        task.owner = owner.strip()
        if team:
            task.assigned_team = team.strip()
        if task.status is TaskStatus.OPEN:
            task.status = TaskStatus.IN_PROGRESS
        self.log.append(
            task_id=task_id,
            transition=Transition.ASSIGNED,
            actor=by,
            from_status=before.value,
            to_status=task.status.value,
            note=note or f"assigned to {task.owner}",
            detail={"owner": task.owner, "team": task.assigned_team},
        )
        return task

    def set_status(
        self, task_id: str, status: TaskStatus, *, by: str, note: str = ""
    ) -> RemediationTask:
        """Move a task by hand.

        Deliberately refuses the two transitions that must be earned rather
        than declared. Nobody marks their own work verified or closed; the
        engine does that in ``recheck``.
        """
        task = self._require(task_id)
        if status in (TaskStatus.VERIFIED, TaskStatus.CLOSED):
            raise RemediationError(
                f"{status.value} cannot be set by hand. A task is verified only "
                "when the certified rule is run again and returns no finding. "
                "Attach the evidence and re-check."
            )
        before = task.status
        task.status = status
        self.log.append(
            task_id=task_id,
            transition=Transition.STATUS_CHANGED,
            actor=by,
            from_status=before.value,
            to_status=status.value,
            note=note,
        )
        return task

    def attach_evidence(
        self, task_id: str, evidence_ids: list[str], *, by: str, note: str = ""
    ) -> RemediationTask:
        """Record that the artifact this task was waiting for has arrived."""
        task = self._require(task_id)
        if not evidence_ids:
            raise RemediationError("Attaching no evidence is not attaching evidence.")
        before = task.status
        for eid in evidence_ids:
            if eid not in task.evidence_ids:
                task.evidence_ids.append(eid)
        task.status = TaskStatus.READY_FOR_RECHECK
        self.log.append(
            task_id=task_id,
            transition=Transition.EVIDENCE_ATTACHED,
            actor=by,
            from_status=before.value,
            to_status=task.status.value,
            note=note or f"{len(evidence_ids)} evidence item(s) attached",
            detail={"evidence_ids": list(evidence_ids)},
        )
        return task

    def recheck(
        self,
        task_id: str,
        *,
        still_failing: bool,
        by: str,
        detail: str = "",
        at: _dt.datetime | None = None,
    ) -> RemediationTask:
        """Record the outcome of running the certified rule again.

        **This is the only path to VERIFIED and CLOSED.** ``still_failing`` is
        not an opinion: the caller must have run the deterministic engine and
        be reporting what it returned. See
        :func:`sanhita.remediate.service.recheck_task`, which is the only thing
        that should call this in the application.
        """
        task = self._require(task_id)
        moment = at or _dt.datetime.now(_dt.timezone.utc)
        before = task.status
        task.recheck_count += 1
        task.last_recheck_at = moment
        task.last_recheck_result = detail or (
            "the rule still finds a breach" if still_failing else "no finding"
        )

        self.log.append(
            task_id=task_id,
            transition=Transition.RECHECKED,
            actor=by,
            from_status=before.value,
            to_status=before.value,
            note=task.last_recheck_result,
            detail={"still_failing": still_failing, "run": task.recheck_count},
            at=moment,
        )

        if still_failing:
            task.status = TaskStatus.REOPENED
            self.log.append(
                task_id=task_id,
                transition=Transition.REOPENED,
                actor=by,
                from_status=before.value,
                to_status=task.status.value,
                note="the rule was run again and still finds a breach",
                at=moment,
            )
            return task

        task.status = TaskStatus.VERIFIED
        task.verified_by = by or "the engine"
        task.verified_at = moment
        self.log.append(
            task_id=task_id,
            transition=Transition.VERIFIED,
            actor=by,
            from_status=before.value,
            to_status=task.status.value,
            note="the certified rule was run again and returned no finding",
            at=moment,
        )
        task.status = TaskStatus.CLOSED
        task.closed_at = moment
        self.log.append(
            task_id=task_id,
            transition=Transition.CLOSED,
            actor=by,
            from_status=TaskStatus.VERIFIED.value,
            to_status=task.status.value,
            note="closed on a verified re-check",
            at=moment,
        )
        return task

    # ------------------------------------------------------------- reading

    def all(self) -> list[RemediationTask]:
        order = {
            TaskStatus.REOPENED: 0,
            TaskStatus.OPEN: 1,
            TaskStatus.IN_PROGRESS: 2,
            TaskStatus.AWAITING_EVIDENCE: 3,
            TaskStatus.READY_FOR_RECHECK: 4,
            TaskStatus.VERIFIED: 5,
            TaskStatus.CLOSED: 6,
        }
        return sorted(
            self.tasks.values(),
            key=lambda t: (order.get(t.status, 9), t.due_date or _dt.date.max, t.task_id),
        )

    def open_tasks(self) -> list[RemediationTask]:
        return [t for t in self.all() if t.status.is_open]

    def for_obligation(self, obligation_id: str) -> list[RemediationTask]:
        return [t for t in self.all() if t.obligation_id == obligation_id]

    def for_gap(self, gap_id: str) -> RemediationTask | None:
        for task in self.all():
            if task.gap_id == gap_id:
                return task
        return None

    def summary(self, today: _dt.date | None = None) -> dict:
        """Counts computed from the tasks themselves, never stored."""
        tasks = list(self.tasks.values())
        by_status = {s.value: 0 for s in TaskStatus}
        for task in tasks:
            by_status[task.status.value] += 1
        return {
            "total": len(tasks),
            "open": sum(1 for t in tasks if t.status.is_open),
            "closed": sum(1 for t in tasks if t.status is TaskStatus.CLOSED),
            "overdue": sum(1 for t in tasks if t.is_overdue(today)),
            "awaiting_evidence": sum(
                1 for t in tasks if t.status is TaskStatus.AWAITING_EVIDENCE
            ),
            "ready_for_recheck": sum(
                1 for t in tasks if t.status is TaskStatus.READY_FOR_RECHECK
            ),
            "by_status": by_status,
        }

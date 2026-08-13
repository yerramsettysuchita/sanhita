"""When this firm was assessed, against which rulebook, on which records.

A compliance position is not a page. It is an event that happened at a moment,
against a specific rulebook and a specific set of records, and somebody may be
asked to account for it a year later. Until now Sanhita recomputed the position
on every page view and wrote nothing down, so the honest answer to "when were
we last assessed" was "just now, and we have no idea about before".

This module records the event. Each run stores the SHA-256 of both inputs, the
rulebook and the evidence, so the claim is checkable rather than trusted. Feed
the same two hashes back into the same engine version and you must get the same
counts. If you do not, one of the three has changed and the record says which.

What this is not:

* Not a cache. Nothing reads the stored counts to render a verdict. The engine
  is still run on every page and the screens show what it returned. A stored
  number that a screen trusts is a number that will one day disagree with the
  engine, and the engine would be right.
* Not a hash chain. The certification and remediation ledgers are chained
  because a person's signature and a task's closure are accountability claims
  that somebody has an incentive to rewrite. An assessment is reproducible from
  its two inputs, which is a stronger property than a chain, so it carries the
  inputs instead.

The log is append only and deduplicated: a run is recorded when the pair of
input hashes differs from the most recent run. Refreshing the page fifty times
produces one run, uploading a corrected register produces a second.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sanhita.execute.evidence import EvidenceStore
    from sanhita.execute.report import ComplianceReport

__all__ = [
    "AssessmentRun",
    "FindingRecord",
    "AssessmentLog",
    "evidence_fingerprint",
    "rulebook_fingerprint",
]


def rulebook_fingerprint(obligations) -> str:
    """A stable hash of the certified rules that were actually enforceable.

    Only certified rules go in, because only certified rules can produce a
    finding. Compiling a hundred more proposals does not change what the engine
    did, so it must not change the fingerprint of what the engine ran.

    Each rule contributes its id, its version and its signature. The signature
    already covers the rule's bytes, so a rule edited and re-signed changes this
    hash, while the same rulebook loaded twice does not.
    """
    from sanhita.ir.enums import RuleStatus

    digest = hashlib.sha256()
    parts = sorted(
        f"{o.id}|{o.version}|{(o.certification.signature if o.certification else '')}"
        for o in obligations
        if o.status is RuleStatus.CERTIFIED
    )
    if not parts:
        return hashlib.sha256(b"no-certified-rules").hexdigest()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def evidence_fingerprint(evidence: EvidenceStore | None) -> str:
    """A stable hash of the records an assessment ran against.

    Taken over the sorted event identities rather than the file bytes, so that
    re-saving the same evidence store, or importing the same rows in a
    different order, does not read as a different assessment. What changes the
    fingerprint is a different set of facts, which is the thing that should.
    """
    if evidence is None or not evidence.events:
        return hashlib.sha256(b"no-evidence").hexdigest()
    digest = hashlib.sha256()
    for line in sorted(
        "|".join(
            (
                event.id,
                event.obligation_id,
                event.entity,
                event.occurred_on.isoformat(),
                event.filed_on.isoformat() if event.filed_on else "",
                event.artifact_type,
            )
        )
        for event in evidence.events
    ):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(slots=True)
class FindingRecord:
    """One breach as it stood at the moment of a run, kept verbatim.

    Counts prove a run happened. They do not answer the question an inspector
    actually asks, which is "what exactly were you told in March, and did you
    act on it". Recomputing today cannot answer that: the rulebook may have been
    amended and the evidence corrected, and the whole point of remediation is
    that the findings change.

    So each run keeps the findings it produced, with the citation that proved
    them. The clause text and the signature are copied in rather than looked up,
    because a rule superseded next year must not silently rewrite what a firm
    was told this year.
    """

    outcome: str
    obligation_id: str
    #: The occasion this finding is about, which is what a remediation task is
    #: raised against. Without it a stored run could not be asked "did you
    #: actually report this gap", so a task could cite a finding no assessment
    #: ever made.
    event_id: str
    clause_id: str
    page: int
    entity: str
    occurred_on: str
    due_on: str
    filed_on: str | None
    days_late: int | None
    requirement: str
    certified_by: str
    #: The signature over the rule as it stood for this run. If a later run
    #: cites the same clause with a different signature, the rule changed.
    signature: str

    @classmethod
    def of(cls, finding) -> FindingRecord:
        return cls(
            outcome=finding.outcome.value,
            obligation_id=finding.obligation_id,
            event_id=finding.event_id,
            clause_id=finding.clause_id,
            page=finding.page,
            entity=finding.entity,
            occurred_on=finding.occurred_on.isoformat(),
            due_on=finding.due_on.isoformat(),
            filed_on=finding.filed_on.isoformat() if finding.filed_on else None,
            days_late=finding.days_late,
            requirement=finding.requirement,
            certified_by=finding.certified_by,
            signature=finding.signature,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> FindingRecord:
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        # Absent from runs recorded before findings carried their occasion.
        data.setdefault("event_id", "")
        return cls(**data)


@dataclass(slots=True)
class AssessmentRun:
    """One execution of the certified rulebook against one set of records."""

    run_id: str
    ran_at: _dt.datetime
    ran_by: str

    # What it ran against, both sides hashed so the run can be reproduced.
    document: str
    document_sha256: str
    rulebook_sha256: str
    evidence_label: str
    evidence_sha256: str
    events_checked: int

    # What it found. These counts are a record of what happened, never the
    # source of anything on screen: the engine is re-run on every page view and
    # the screens show what it returned. A stored number a screen trusts is a
    # number that will one day disagree with the engine.
    rules_certified: int
    rules_evaluated: int
    satisfied: int
    breaches: int
    missing: int
    late: int
    no_evidence: int
    undetermined: int
    unevaluable: int

    #: The findings this run produced, as they stood. Empty on runs recorded
    #: before findings were kept, which the screens report as "counts only"
    #: rather than as a run that found nothing.
    findings: list[FindingRecord] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        """Whether this run kept its findings, as against merely counting them."""
        return bool(self.findings)

    @property
    def compliance_rate(self) -> float | None:
        """Met on time as a share of what was actually checkable."""
        checked = self.satisfied + self.breaches
        return (self.satisfied / checked) if checked else None

    @property
    def short_id(self) -> str:
        return self.run_id.split("-")[-1][:8]

    def inputs(self) -> tuple[str, str]:
        """The pair that decides whether this is a new assessment or the same one."""
        return (self.rulebook_sha256, self.evidence_sha256)

    def to_dict(self) -> dict:
        raw = asdict(self)
        raw["ran_at"] = self.ran_at.isoformat()
        raw["findings"] = [f.to_dict() for f in self.findings]
        return raw

    @classmethod
    def from_dict(cls, raw: dict) -> AssessmentRun:
        data = dict(raw)
        data["ran_at"] = _dt.datetime.fromisoformat(data["ran_at"])
        # Absent from runs recorded before findings were kept.
        data["findings"] = [
            FindingRecord.from_dict(f) for f in data.get("findings", [])
        ]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class AssessmentLog:
    """Every assessment this workspace has run, oldest first."""

    runs: list[AssessmentRun] = field(default_factory=list)
    path: Path | None = None

    # ------------------------------------------------------------- reading

    @property
    def latest(self) -> AssessmentRun | None:
        return self.runs[-1] if self.runs else None

    @property
    def first(self) -> AssessmentRun | None:
        return self.runs[0] if self.runs else None

    def __len__(self) -> int:
        return len(self.runs)

    def recent(self, count: int = 10) -> list[AssessmentRun]:
        """Newest first, which is the order a person reads a history in."""
        return list(reversed(self.runs))[:count]

    def movement(self) -> tuple[int, int] | None:
        """Breaches now against breaches at the previous assessment.

        The number a compliance officer is actually asked for is not how many
        breaches there are, it is whether the number went down since last time.
        Returns ``None`` until there is a previous run to compare against,
        because a first assessment has no trend and inventing one would be a
        fabricated number.
        """
        if len(self.runs) < 2:
            return None
        return (self.runs[-1].breaches, self.runs[-2].breaches)

    # ------------------------------------------------------------- writing

    def record(
        self,
        report: ComplianceReport,
        *,
        evidence: EvidenceStore,
        document: str,
        document_sha256: str,
        rulebook_sha256: str,
        rules_certified: int,
        by: str,
        at: _dt.datetime | None = None,
    ) -> AssessmentRun | None:
        """Append a run, unless the same inputs were assessed last time.

        Returns the run that was appended, or ``None`` when nothing changed.
        The caller does not need to care which: the position on screen comes
        from the engine either way.
        """
        from sanhita.execute.report import Outcome

        fingerprint = evidence_fingerprint(evidence)
        if self.runs and self.runs[-1].inputs() == (rulebook_sha256, fingerprint):
            return None

        counts = {outcome: 0 for outcome in Outcome}
        for finding in report.findings:
            counts[finding.outcome] = counts.get(finding.outcome, 0) + 1

        run = AssessmentRun(
            run_id=f"AR-{uuid.uuid4().hex[:12]}",
            ran_at=at or _dt.datetime.now(_dt.timezone.utc),
            ran_by=by or "unattributed",
            document=document,
            document_sha256=document_sha256,
            rulebook_sha256=rulebook_sha256,
            evidence_label=evidence.label,
            evidence_sha256=fingerprint,
            events_checked=report.events_checked,
            rules_certified=rules_certified,
            rules_evaluated=report.rules_evaluated,
            satisfied=report.satisfied,
            breaches=report.breaches,
            missing=counts.get(Outcome.MISSING, 0),
            late=counts.get(Outcome.LATE, 0),
            no_evidence=counts.get(Outcome.NO_EVIDENCE, 0),
            undetermined=len(report.undetermined),
            unevaluable=len(report.unevaluable),
            # Worst first, the same order the gaps screen showed at the time,
            # so the record reads as what the firm was actually told.
            findings=[FindingRecord.of(f) for f in report.ranked()],
        )
        self.runs.append(run)
        return run

    # ------------------------------------------------------------ on disk

    @classmethod
    def load(cls, path: Path) -> AssessmentLog:
        if not path.is_file():
            return cls(runs=[], path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            runs=[AssessmentRun.from_dict(item) for item in raw.get("runs", [])],
            path=path,
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        if target is None:
            raise ValueError("An assessment log needs a path before it can be saved.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"runs": [run.to_dict() for run in self.runs]},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.path = target
        return target

"""The certification lifecycle.

    propose(obligation)        -> PROPOSED
    certify(id, by, key)       -> CERTIFIED, version-locked, signed, immutable
    amend(id, edits, by)       -> a NEW version; the old one becomes SUPERSEDED
    reject(id, by, reason)     -> REJECTED, reason retained for audit

Two invariants hold throughout, and they are the reason this module exists
rather than being a few methods on Obligation:

**Nothing is mutated.** Every transition produces a new object. The registry
keeps every version ever made, so the pre-certification state of a rule and the
text a reviewer actually saw remain recoverable years later.

**Nothing is unrecorded.** Every transition appends to the audit ledger before
the registry is updated, with a field-level diff of what changed.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from sanhita.certify.ledger import AuditLedger, Transition, diff_obligations
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import CertifiedImmutableError, Obligation

__all__ = [
    "CertificationError",
    "RuleRegistry",
    "amend",
    "bump_version",
    "certify",
    "propose",
    "reject",
    "verify_signatures",
]


class CertificationError(RuntimeError):
    """An illegal lifecycle transition."""


def bump_version(version: str, *, level: str = "minor") -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


@dataclass(slots=True)
class RuleRegistry:
    """Every version of every rule, plus the audit ledger over all of them."""

    ledger: AuditLedger = field(default_factory=AuditLedger)
    #: obligation id -> versions, oldest first. Append-only.
    _versions: dict[str, list[Obligation]] = field(default_factory=dict)

    # ------------------------------------------------------------- access

    def current(self, obligation_id: str) -> Obligation | None:
        versions = self._versions.get(obligation_id)
        return versions[-1] if versions else None

    def history(self, obligation_id: str) -> list[Obligation]:
        return list(self._versions.get(obligation_id, ()))

    def all_current(self) -> list[Obligation]:
        return [v[-1] for v in self._versions.values() if v]

    def by_status(self, status: RuleStatus) -> list[Obligation]:
        return [o for o in self.all_current() if o.status is status]

    def as_of(self, moment: _dt.datetime) -> list[Obligation]:
        """The rulebook as it stood at a past instant.

        After an incident the question a regulator asks is not what you believe
        today. It is what you believed on the day it happened. Answering that
        from a system that only stores the current state means reconstructing it
        from memory, which is worth nothing.

        Here it is a replay. Every version of every rule is kept, and the ledger
        records the moment of each transition, so the state at any instant is
        the last version of each rule whose transition had already happened. The
        ledger is hash-chained, so the answer is not merely recorded, it is
        tamper evident.

        A rule proposed after ``moment`` does not appear at all: it did not
        exist yet, and showing it would be inventing a duty nobody had.
        """
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_dt.timezone.utc)

        # Counted, not matched on the version string. Certifying and rejecting
        # both produce a new object while leaving the version unchanged, so
        # "the object whose version is 1.0.0" is ambiguous the moment a rule is
        # signed. What is unambiguous is position: every transition appends
        # exactly one ledger entry and exactly one version, in the same order,
        # so the Nth entry for a rule produced the Nth version of it.
        transitions: dict[str, int] = {}
        for entry in self.ledger:
            at = entry.at
            if at.tzinfo is None:
                at = at.replace(tzinfo=_dt.timezone.utc)
            if at <= moment:
                transitions[entry.obligation_id] = transitions.get(entry.obligation_id, 0) + 1

        out: list[Obligation] = []
        for obligation_id, count in transitions.items():
            versions = self._versions.get(obligation_id, ())
            if not versions:
                continue
            out.append(versions[min(count, len(versions)) - 1])
        out.sort(key=lambda o: o.id)
        return out

    def __len__(self) -> int:
        return len(self._versions)

    def _push(self, obligation: Obligation) -> None:
        self._versions.setdefault(obligation.id, []).append(obligation)

    # -------------------------------------------------------- transitions

    def propose(self, obligation: Obligation, *, by: str = "extractor") -> Obligation:
        """Register a freshly extracted proposal."""
        if obligation.status is not RuleStatus.PROPOSED:
            raise CertificationError(
                f"{obligation.id}: propose() takes a PROPOSED obligation, "
                f"got {obligation.status.value}"
            )
        existing = self.current(obligation.id)
        if existing is not None and existing.status is RuleStatus.CERTIFIED:
            raise CertificationError(
                f"{obligation.id} is already CERTIFIED; use amend() to change it"
            )

        self.ledger.append(
            obligation_id=obligation.id,
            transition=Transition.PROPOSED,
            actor=by,
            from_state=existing.status.value if existing else None,
            to_state=RuleStatus.PROPOSED.value,
            version=obligation.version,
            changes=diff_obligations(existing, obligation) if existing else {},
            note=(obligation.extraction.engine if obligation.extraction else None),
        )
        self._push(obligation)
        return obligation

    def certify(
        self,
        obligation_id: str,
        *,
        by: str,
        key: bytes | str,
        note: str | None = None,
        at: _dt.datetime | None = None,
    ) -> Obligation:
        """Sign and version-lock a proposal. The result is immutable."""
        current = self._require(obligation_id)
        if current.status is RuleStatus.CERTIFIED:
            raise CertifiedImmutableError(f"{obligation_id} is already certified")
        if current.status is RuleStatus.REJECTED:
            raise CertificationError(
                f"{obligation_id} was rejected and cannot be certified; propose a new version"
            )

        certified = current.certify(certified_by=by, key=key, at=at, note=note)
        self.ledger.append(
            obligation_id=obligation_id,
            transition=Transition.CERTIFIED,
            actor=by,
            from_state=current.status.value,
            to_state=RuleStatus.CERTIFIED.value,
            version=certified.version,
            note=note,
            signature=certified.certification.signature,
        )
        self._push(certified)
        return certified

    def reject(
        self, obligation_id: str, *, by: str, reason: str
    ) -> Obligation:
        """Refuse a proposal. Retained, never executed, reason preserved."""
        if not reason.strip():
            raise CertificationError("a rejection must carry a reason")
        current = self._require(obligation_id)
        if current.status is RuleStatus.CERTIFIED:
            raise CertifiedImmutableError(
                f"{obligation_id} is CERTIFIED; supersede it with amend() instead"
            )

        rejected = current.model_copy(update={"status": RuleStatus.REJECTED}, deep=True)
        self.ledger.append(
            obligation_id=obligation_id,
            transition=Transition.REJECTED,
            actor=by,
            from_state=current.status.value,
            to_state=RuleStatus.REJECTED.value,
            version=current.version,
            note=reason,
        )
        self._push(rejected)
        return rejected

    def amend(
        self,
        obligation_id: str,
        edits: dict,
        *,
        by: str,
        level: str = "minor",
        note: str | None = None,
    ) -> Obligation:
        """Create a NEW version. Never mutates the existing one.

        A certified rule is superseded rather than edited: the old version keeps
        its signature and its SUPERSEDED status, so a historical evaluation can
        still be replayed exactly as it ran.
        """
        current = self._require(obligation_id)
        if not edits:
            raise CertificationError(f"{obligation_id}: amend() requires at least one edit")

        forbidden = {"id", "status", "certification", "version"} & set(edits)
        if forbidden:
            raise CertificationError(
                f"{obligation_id}: amend() cannot set {sorted(forbidden)} directly"
            )

        amended = current.model_copy(
            update={
                **edits,
                "status": RuleStatus.PROPOSED,
                "certification": None,
                "version": bump_version(current.version, level=level),
            },
            deep=True,
        )
        # Re-validate: an edit that breaks an IR invariant must fail here, not
        # silently produce an unexecutable rule.
        amended = Obligation.model_validate(amended.model_dump(mode="python"))

        changes = diff_obligations(current, amended)

        if current.status is RuleStatus.CERTIFIED:
            superseded = current.model_copy(
                update={"status": RuleStatus.SUPERSEDED}, deep=True
            )
            self.ledger.append(
                obligation_id=obligation_id,
                transition=Transition.SUPERSEDED,
                actor=by,
                from_state=RuleStatus.CERTIFIED.value,
                to_state=RuleStatus.SUPERSEDED.value,
                version=current.version,
                note=f"superseded by {amended.version}",
            )
            self._push(superseded)

        self.ledger.append(
            obligation_id=obligation_id,
            transition=Transition.AMENDED,
            actor=by,
            from_state=current.status.value,
            to_state=RuleStatus.PROPOSED.value,
            version=amended.version,
            changes=changes,
            note=note,
        )
        self._push(amended)
        return amended

    def _require(self, obligation_id: str) -> Obligation:
        current = self.current(obligation_id)
        if current is None:
            raise CertificationError(f"unknown obligation {obligation_id!r}")
        return current

    # ------------------------------------------------------------ auditing

    def verify_signatures(self, key: bytes | str) -> "SignatureReport":
        """Recompute every signature and report anything that no longer matches."""
        report = SignatureReport()
        for versions in self._versions.values():
            for obligation in versions:
                if obligation.certification is None:
                    continue
                report.checked += 1
                if obligation.verify_signature(key):
                    report.valid += 1
                else:
                    report.tampered.append(obligation.id)
        report.ledger_problems = self.ledger.verify_chain()
        return report


@dataclass(slots=True)
class SignatureReport:
    checked: int = 0
    valid: int = 0
    tampered: list[str] = field(default_factory=list)
    ledger_problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.tampered and not self.ledger_problems


# --------------------------------------------------------------------------
# Module-level convenience wrappers
# --------------------------------------------------------------------------


def propose(registry: RuleRegistry, obligation: Obligation, *, by: str = "extractor") -> Obligation:
    return registry.propose(obligation, by=by)


def certify(registry: RuleRegistry, obligation_id: str, *, by: str, key: bytes | str, **kw) -> Obligation:
    return registry.certify(obligation_id, by=by, key=key, **kw)


def amend(registry: RuleRegistry, obligation_id: str, edits: dict, *, by: str, **kw) -> Obligation:
    return registry.amend(obligation_id, edits, by=by, **kw)


def reject(registry: RuleRegistry, obligation_id: str, *, by: str, reason: str) -> Obligation:
    return registry.reject(obligation_id, by=by, reason=reason)


def verify_signatures(registry: RuleRegistry, key: bytes | str) -> SignatureReport:
    return registry.verify_signatures(key)

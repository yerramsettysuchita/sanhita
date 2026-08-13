"""The append-only audit ledger.

Every lifecycle transition is written here and nothing is ever updated or
deleted. Two properties make the record worth having:

**It is hash-chained.** Each entry carries the hash of its predecessor, so
removing or editing an entry in the middle breaks every hash after it. An audit
log you can quietly rewrite is not an audit log.

**It records the diff, not just the verb.** "amended by A. Mehta" tells a
regulator nothing. "amended by A. Mehta: deadline.offset_days 5 -> 2,
evidence[0].retention_period_days 1825 -> 2555" tells them what changed.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Iterator

from sanhita.ir.canonical import canonical_json, sha256_hex

__all__ = ["AuditEntry", "AuditLedger", "Transition", "diff_obligations"]

GENESIS = "0" * 64


class Transition(str, Enum):
    PROPOSED = "PROPOSED"
    CERTIFIED = "CERTIFIED"
    AMENDED = "AMENDED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One immutable record of one transition."""

    sequence: int
    obligation_id: str
    transition: Transition
    actor: str
    at: _dt.datetime
    from_state: str | None
    to_state: str
    version: str
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    note: str | None = None
    signature: str | None = None
    previous_hash: str = GENESIS
    entry_hash: str = ""

    def payload(self) -> dict[str, Any]:
        """Exactly the bytes the entry hash covers."""
        return {
            "sequence": self.sequence,
            "obligation_id": self.obligation_id,
            "transition": self.transition.value,
            "actor": self.actor,
            "at": self.at,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "version": self.version,
            "changes": {k: list(v) for k, v in sorted(self.changes.items())},
            "note": self.note,
            "signature": self.signature,
            "previous_hash": self.previous_hash,
        }

    def compute_hash(self) -> str:
        return sha256_hex(self.payload())

    def __str__(self) -> str:  # pragma: no cover - display only
        when = self.at.isoformat()
        arrow = f"{self.from_state or '-'} -> {self.to_state}"
        return f"[{self.sequence:04d}] {when} {self.obligation_id} {arrow} by {self.actor}"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a model dump to dotted paths so diffs are field-level."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        out[prefix] = value
    return out


def diff_obligations(before: Any, after: Any) -> dict[str, tuple[Any, Any]]:
    """Field-level diff between two obligations, as {path: (before, after)}.

    Audit metadata is excluded — a re-extraction that changed nothing normative
    should not read as an amendment.
    """
    drop = {"extraction", "field_confidence", "field_provenance", "confidence"}
    left = _flatten(before.model_dump(mode="json", exclude=drop))
    right = _flatten(after.model_dump(mode="json", exclude=drop))

    changes: dict[str, tuple[Any, Any]] = {}
    for key in sorted(set(left) | set(right)):
        old, new = left.get(key), right.get(key)
        if old != new:
            changes[key] = (old, new)
    return changes


class AuditLedger:
    """An append-only, hash-chained sequence of transitions."""

    def __init__(self, entries: Iterable[AuditEntry] | None = None) -> None:
        self._entries: list[AuditEntry] = list(entries or [])

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[AuditEntry]:
        return iter(self._entries)

    @property
    def head(self) -> str:
        return self._entries[-1].entry_hash if self._entries else GENESIS

    def append(
        self,
        *,
        obligation_id: str,
        transition: Transition,
        actor: str,
        to_state: str,
        version: str,
        from_state: str | None = None,
        changes: dict[str, tuple[Any, Any]] | None = None,
        note: str | None = None,
        signature: str | None = None,
        at: _dt.datetime | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            sequence=len(self._entries),
            obligation_id=obligation_id,
            transition=transition,
            actor=actor,
            at=at or _dt.datetime.now(_dt.timezone.utc),
            from_state=from_state,
            to_state=to_state,
            version=version,
            changes=changes or {},
            note=note,
            signature=signature,
            previous_hash=self.head,
        )
        entry = replace(entry, entry_hash=entry.compute_hash())
        self._entries.append(entry)
        return entry

    def for_obligation(self, obligation_id: str) -> list[AuditEntry]:
        """Every entry touching one rule, oldest first."""
        base = obligation_id.split("@")[0]
        return [e for e in self._entries if e.obligation_id.split("@")[0] == base]

    def verify_chain(self) -> list[str]:
        """Re-hash the whole ledger. Returns a list of problems; empty is good."""
        problems: list[str] = []
        previous = GENESIS
        for index, entry in enumerate(self._entries):
            if entry.sequence != index:
                problems.append(f"entry {index}: sequence is {entry.sequence}")
            if entry.previous_hash != previous:
                problems.append(
                    f"entry {index} ({entry.obligation_id}): chain break — "
                    f"previous_hash {entry.previous_hash[:12]} != {previous[:12]}"
                )
            recomputed = entry.compute_hash()
            if recomputed != entry.entry_hash:
                problems.append(
                    f"entry {index} ({entry.obligation_id}): content altered — "
                    f"hash {entry.entry_hash[:12]} != recomputed {recomputed[:12]}"
                )
            previous = entry.entry_hash
        return problems

    def to_json(self) -> str:
        return canonical_json([e.payload() | {"entry_hash": e.entry_hash} for e in self._entries])

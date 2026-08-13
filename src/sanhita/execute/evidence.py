"""What the regulated entity actually did.

A certified rule says what must happen and by when. An evidence store says what
happened. The engine compares the two. Nothing in this module knows anything
about SEBI, PDFs or extraction: it is a plain record of events and the artifacts
filed against them, which is the shape a real firm's systems already produce.

One compliance event is one occasion on which an obligation became live. A trade
that must be reported by T+1 produces one event per trade. A quarterly return
produces one event per quarter. The event carries the date the clock started and,
if the artifact was ever produced, the date it was produced.

``filed_on = None`` means it was never filed. That is not the same as "filed
late", and the report keeps them apart.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ComplianceEvent", "EvidenceStore"]


@dataclass(frozen=True)
class ComplianceEvent:
    """One occasion on which an obligation became live for one entity."""

    #: Stable id for the occasion, e.g. "EV-0001".
    id: str
    #: The rule this event is an instance of, e.g. "SB-40.1.8-a".
    obligation_id: str
    #: The regulated entity the duty fell on.
    entity: str
    #: The day the clock started: the trade, the breach, the end of the quarter.
    occurred_on: _dt.date
    #: What was required, matched against the rule's EvidenceReq.artifact_type.
    artifact_type: str
    #: When the artifact was produced. ``None`` means it never was.
    filed_on: _dt.date | None = None
    #: The firm's own reference for the filing, carried through to the report.
    reference: str | None = None

    # -- where this came from
    #
    # A finding cites the clause it came from down to the byte. Until now the
    # evidence on the other side of that comparison cited nothing at all, so a
    # gap report could prove what the regulation said and not where the firm's
    # side of the story came from. An inspector asking "which document told you
    # the return was filed on the second" had no answer.
    #
    # All optional, because a CSV row genuinely has no page number and
    # inventing one would be worse than leaving it empty.
    source_document: str = ""
    source_page: int | None = None
    source_row: int | None = None
    #: The words in the document that produced this event.
    source_excerpt: str = ""
    #: Who mapped it to this obligation, where a person did. Empty when the
    #: document named the rule itself.
    mapped_by: str = ""
    mapped_at: _dt.datetime | None = None

    def where(self) -> str:
        """Where this came from, in one phrase, or empty if unrecorded."""
        if not self.source_document:
            return ""
        parts = [self.source_document]
        if self.source_page is not None:
            parts.append(f"page {self.source_page}")
        if self.source_row is not None:
            parts.append(f"row {self.source_row}")
        return ", ".join(parts)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "obligation_id": self.obligation_id,
            "entity": self.entity,
            "occurred_on": self.occurred_on.isoformat(),
            "artifact_type": self.artifact_type,
            "filed_on": self.filed_on.isoformat() if self.filed_on else None,
            "reference": self.reference,
            "source_document": self.source_document,
            "source_page": self.source_page,
            "source_row": self.source_row,
            "source_excerpt": self.source_excerpt,
            "mapped_by": self.mapped_by,
            "mapped_at": self.mapped_at.isoformat() if self.mapped_at else None,
        }

    @classmethod
    def from_json(cls, raw: dict) -> ComplianceEvent:
        mapped = raw.get("mapped_at")
        return cls(
            id=raw["id"],
            obligation_id=raw["obligation_id"],
            entity=raw["entity"],
            occurred_on=_dt.date.fromisoformat(raw["occurred_on"]),
            artifact_type=raw["artifact_type"],
            filed_on=(
                _dt.date.fromisoformat(raw["filed_on"]) if raw.get("filed_on") else None
            ),
            reference=raw.get("reference"),
            # Absent from stores written before provenance existed. Reading one
            # must not fail, so every field defaults.
            source_document=raw.get("source_document", ""),
            source_page=raw.get("source_page"),
            source_row=raw.get("source_row"),
            source_excerpt=raw.get("source_excerpt", ""),
            mapped_by=raw.get("mapped_by", ""),
            mapped_at=_dt.datetime.fromisoformat(mapped) if mapped else None,
        )


@dataclass
class EvidenceStore:
    """Every compliance event, indexed by the rule it belongs to.

    ``label`` describes where these events came from and is printed on every
    report built from them. A report run against generated events must never be
    mistakable for one run against a firm's real books.
    """

    label: str
    events: list[ComplianceEvent] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.events)

    def add(self, event: ComplianceEvent) -> None:
        self.events.append(event)

    def occasion_of(self, event: ComplianceEvent) -> tuple[str, str, _dt.date]:
        """What makes two records statements about the same event.

        A duty falling due on one date, for one entity, under one rule. The
        artifact type is deliberately not part of it: a firm that filed the
        wrong kind of document and then the right one has corrected one
        occasion, not created a second.
        """
        return (event.obligation_id, event.entity, event.occurred_on)

    def supersede(self, event: ComplianceEvent) -> ComplianceEvent | None:
        """Record ``event``, replacing any earlier record of the same occasion.

        A firm that files late and then uploads the corrected register is making
        a second statement about one occasion, not reporting a second occasion.
        Appending both leaves the engine reading a duty as never filed and also
        filed, and the breach never clears no matter what the firm does. That is
        the defect this fixes.

        Nothing is lost by superseding. The assessment log stores the hash of
        the records each run used, so the earlier position, breach and all, is
        still on the record and still reproducible. A firm cannot quietly
        rewrite its own history by re-uploading; it can only state a new
        current position, and the old one keeps its own hash.

        Returns whatever was displaced, so a caller can say how many rows
        superseded an earlier one rather than letting it happen silently.
        """
        key = self.occasion_of(event)
        for index, existing in enumerate(self.events):
            if self.occasion_of(existing) == key:
                self.events[index] = event
                return existing
        self.events.append(event)
        return None

    def for_obligation(self, obligation_id: str) -> list[ComplianceEvent]:
        found = [e for e in self.events if e.obligation_id == obligation_id]
        found.sort(key=lambda e: (e.occurred_on, e.id))
        return found

    @property
    def obligation_ids(self) -> set[str]:
        return {e.obligation_id for e in self.events}

    @property
    def entities(self) -> list[str]:
        return sorted({e.entity for e in self.events})

    @property
    def window(self) -> tuple[_dt.date, _dt.date] | None:
        if not self.events:
            return None
        days = [e.occurred_on for e in self.events]
        return (min(days), max(days))

    # --------------------------------------------------------------- on disk

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "label": self.label,
            "events": [e.to_json() for e in self.events],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> EvidenceStore:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            label=payload.get("label", str(path)),
            events=[ComplianceEvent.from_json(raw) for raw in payload.get("events", [])],
        )

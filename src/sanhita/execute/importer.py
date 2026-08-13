"""Reading a firm's own filing records into an evidence store.

The gap report is only as good as what it is run against, and generated events
prove the engine works without proving anything about a firm. This module takes
the thing a compliance function already has, a spreadsheet of what was filed and
when, and turns it into compliance events.

CSV rather than a database connector, because every risk system in the country
can export one and none of them agree on anything else.

Two decisions worth stating:

  **A row that cannot be read is reported, never skipped.** An importer that
  drops malformed rows quietly will one day drop the row that mattered, and the
  gap report will look cleaner for it. Every rejected row comes back with its
  line number and what was wrong with it.

  **An unknown obligation id is an error, not a warning.** Evidence that points
  at a rule which does not exist cannot be checked against anything, and
  counting it would inflate the denominator of the compliance rate.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass, field

from sanhita.execute.evidence import ComplianceEvent, EvidenceStore

__all__ = ["ImportResult", "RowError", "read_csv", "TEMPLATE_CSV"]

#: The columns we require, and the ones we will use if present.
REQUIRED = ("obligation_id", "entity", "occurred_on")
OPTIONAL = ("filed_on", "artifact_type", "reference", "event_id")

TEMPLATE_CSV = (
    "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
    "SB-19.5.2.8-a,Demo Broking Pvt Ltd,2026-03-16,2026-03-20,report,REF-00001\n"
    "SB-19.5.2.8-a,Demo Broking Pvt Ltd,2026-04-16,,report,\n"
)


@dataclass(frozen=True)
class RowError:
    line: int
    problem: str
    raw: str = ""


@dataclass
class ImportResult:
    store: EvidenceStore | None = None
    accepted: int = 0
    errors: list[RowError] = field(default_factory=list)
    unknown_obligations: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return self.store is not None and not self.errors

    def summary(self) -> str:
        if self.store is None:
            return "Nothing could be read from that file."
        if not self.errors:
            return f"Read {self.accepted} compliance event(s)."
        return (
            f"Read {self.accepted} event(s). {len(self.errors)} row(s) could not "
            "be read and are listed below. Nothing was imported from them."
        )


def _date(value: str, field_name: str, line: int) -> _dt.date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} reads {text!r}. Dates must be written as "
            "YYYY-MM-DD, for example 2026-03-16."
        ) from exc


def read_csv(
    text: str,
    *,
    known_obligations: set[str] | None = None,
    label: str | None = None,
) -> ImportResult:
    """Parse a filing export into compliance events.

    ``known_obligations`` is the set of rule ids in the workspace. When given,
    a row pointing at anything else is rejected rather than silently kept.
    """
    result = ImportResult()

    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = [h.strip() for h in (reader.fieldnames or [])]
    except csv.Error as exc:
        result.errors.append(RowError(0, f"That file is not readable as CSV: {exc}"))
        return result

    if not headers:
        result.errors.append(RowError(0, "That file has no header row."))
        return result

    missing = [c for c in REQUIRED if c not in headers]
    if missing:
        result.errors.append(
            RowError(
                1,
                "The header row is missing "
                + ", ".join(missing)
                + ". Required columns are "
                + ", ".join(REQUIRED)
                + ".",
            )
        )
        return result

    events: list[ComplianceEvent] = []
    for index, row in enumerate(reader, start=2):
        raw = ",".join((row.get(h) or "") for h in headers)
        try:
            obligation_id = (row.get("obligation_id") or "").strip()
            entity = (row.get("entity") or "").strip()
            if not obligation_id:
                raise ValueError("obligation_id is empty.")
            if not entity:
                raise ValueError("entity is empty. Name the regulated entity.")

            if known_obligations is not None and obligation_id not in known_obligations:
                result.unknown_obligations.add(obligation_id)
                raise ValueError(
                    f"{obligation_id} is not a rule in this document. Evidence "
                    "for a rule that does not exist cannot be checked against "
                    "anything."
                )

            occurred = _date(row.get("occurred_on", ""), "occurred_on", index)
            if occurred is None:
                raise ValueError("occurred_on is empty. It is when the clock started.")
            filed = _date(row.get("filed_on", ""), "filed_on", index)
            if filed is not None and filed < occurred:
                raise ValueError(
                    f"filed_on {filed} is before occurred_on {occurred}. An "
                    "artifact cannot be filed before the event that required it."
                )

            events.append(
                ComplianceEvent(
                    id=(row.get("event_id") or "").strip() or f"EV-{index - 1:05d}",
                    obligation_id=obligation_id,
                    entity=entity,
                    occurred_on=occurred,
                    artifact_type=(row.get("artifact_type") or "").strip() or "artifact",
                    filed_on=filed,
                    reference=(row.get("reference") or "").strip() or None,
                )
            )
        except ValueError as exc:
            result.errors.append(RowError(index, str(exc), raw[:120]))

    if events:
        result.store = EvidenceStore(
            label=label or f"imported from a filing export, {len(events)} events",
            events=events,
        )
        result.accepted = len(events)
    return result

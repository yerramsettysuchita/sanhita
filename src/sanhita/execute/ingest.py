"""Reading a firm's evidence out of whatever it actually has.

`importer.py` reads a CSV whose columns already name an obligation. That is the
easy case and it is not the common one. A compliance function has margin
reports as PDFs, filing registers as spreadsheets, and API exports as JSON, and
none of those carry a Sanhita rule id because they were produced years before
Sanhita existed.

This module reads those. It is deliberately more careful than the CSV path,
because the CSV path is told which obligation a row belongs to and this one has
to work it out.

**The rule that governs everything here.** A document that cannot be confidently
tied to an obligation produces an `UNRESOLVED` candidate for a person to
confirm, never a compliance conclusion. Two texts being similar is not evidence
that a duty was discharged, and a tool that treats it as such will eventually
tell a firm it is compliant when it is not.

So the pipeline is

    document -> extracted rows -> candidates -> HUMAN CONFIRMS -> events

and the arrow into the engine only exists after the human step. Nothing in this
file writes a `ComplianceEvent` that the engine will act on without an
obligation id somebody put there.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
from dataclasses import dataclass, field
from enum import Enum

from sanhita.execute.evidence import ComplianceEvent, EvidenceStore

__all__ = [
    "Candidate",
    "Confidence",
    "IngestResult",
    "SUPPORTED",
    "read_json",
    "read_pdf",
    "read_xlsx",
    "sniff",
    "xlsx_available",
]

#: What we will attempt, by extension.
SUPPORTED = ("csv", "json", "xlsx", "pdf")

#: A date in any of the forms a filing register actually uses.
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"), ("d", "m", "y")),
    (re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b"), ("d", "m", "y")),
]

#: A reference number, which is what a firm's own systems index filings by.
_REFERENCE = re.compile(r"\b([A-Z]{2,6}[-/]\d{3,10})\b")


class Confidence(str, Enum):
    """How sure the extractor is that a row means what it looks like."""

    #: An obligation id was written in the document itself.
    STATED = "STATED"
    #: A date and a reference were found together on one line.
    PROBABLE = "PROBABLE"
    #: Something was found, but not enough to act on.
    UNRESOLVED = "UNRESOLVED"

    @property
    def needs_human(self) -> bool:
        """Everything except a stated id needs somebody to confirm it.

        Even PROBABLE. A date next to a reference number is a strong hint that
        a filing happened, and it is not proof of which duty it discharged.
        """
        return self is not Confidence.STATED


@dataclass
class Candidate:
    """One possible piece of evidence, with where it came from.

    Deliberately not a ``ComplianceEvent``. An event is something the engine
    will act on, and nothing becomes one until a person has attached an
    obligation id.
    """

    #: Where in the document. A candidate a reviewer cannot find is useless.
    source_document: str
    page: int | None = None
    row: int | None = None
    excerpt: str = ""

    occurred_on: _dt.date | None = None
    filed_on: _dt.date | None = None
    reference: str | None = None
    artifact_type: str = ""
    entity: str = ""

    #: Filled only where the document said so, or a person said so later.
    obligation_id: str | None = None
    confidence: Confidence = Confidence.UNRESOLVED
    why: str = ""

    @property
    def can_become_an_event(self) -> bool:
        """An obligation and a date. Without both, the engine can do nothing."""
        return bool(self.obligation_id) and self.occurred_on is not None

    def to_event(
        self, event_id: str, *, mapped_by: str = "", at: _dt.datetime | None = None
    ) -> ComplianceEvent:
        """Become evidence the engine will act on.

        Carries the provenance across. A gap report cites the clause down to
        the byte, and until the event carried its own source the other half of
        that comparison cited nothing. An inspector asking which document said
        the return was filed had no answer.
        """
        if not self.can_become_an_event:
            raise ValueError(
                "This candidate has no obligation id or no date. It cannot "
                "become an event a rule is checked against."
            )
        return ComplianceEvent(
            id=event_id,
            obligation_id=self.obligation_id or "",
            entity=self.entity or "unnamed entity",
            occurred_on=self.occurred_on,  # type: ignore[arg-type]
            artifact_type=self.artifact_type or "artifact",
            filed_on=self.filed_on,
            reference=self.reference,
            source_document=self.source_document,
            source_page=self.page,
            source_row=self.row,
            source_excerpt=self.excerpt,
            mapped_by=mapped_by,
            mapped_at=(at or _dt.datetime.now(_dt.timezone.utc)) if mapped_by else None,
        )

    def where(self) -> str:
        parts = [self.source_document]
        if self.page is not None:
            parts.append(f"page {self.page}")
        if self.row is not None:
            parts.append(f"row {self.row}")
        return ", ".join(parts)

    def to_json(self) -> dict:
        return {
            "source_document": self.source_document,
            "page": self.page,
            "row": self.row,
            "excerpt": self.excerpt,
            "occurred_on": self.occurred_on.isoformat() if self.occurred_on else None,
            "filed_on": self.filed_on.isoformat() if self.filed_on else None,
            "reference": self.reference,
            "artifact_type": self.artifact_type,
            "entity": self.entity,
            "obligation_id": self.obligation_id,
            "confidence": self.confidence.value,
            "why": self.why,
        }


@dataclass
class IngestResult:
    """What one uploaded document yielded."""

    source: str
    fmt: str
    candidates: list[Candidate] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def stated(self) -> list[Candidate]:
        return [c for c in self.candidates if c.confidence is Confidence.STATED]

    @property
    def needing_review(self) -> list[Candidate]:
        return [c for c in self.candidates if c.confidence.needs_human]

    @property
    def ready(self) -> list[Candidate]:
        return [c for c in self.candidates if c.can_become_an_event]

    def to_store(self, label: str) -> EvidenceStore:
        """Only the candidates carrying an obligation id become events.

        Everything else stays a candidate. This is the boundary the module
        docstring describes and it is enforced here rather than by convention.
        """
        events = [
            candidate.to_event(f"EV-{index:05d}")
            for index, candidate in enumerate(self.ready, start=1)
        ]
        return EvidenceStore(label=label, events=events)

    def summary(self) -> str:
        if not self.candidates:
            return f"Nothing readable was found in {self.source}."
        parts = [f"{len(self.candidates)} candidate(s) found"]
        if self.stated:
            parts.append(f"{len(self.stated)} name a rule outright")
        if self.needing_review:
            parts.append(f"{len(self.needing_review)} need a person to confirm them")
        return ". ".join(parts) + "."


# ───────────────────────────────────────────────────────────── helpers ──


def _parse_date(text: str) -> _dt.date | None:
    """The first date in a string, in any of the forms a register uses."""
    for pattern, order in _DATE_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        parts = dict(zip(order, match.groups()))
        try:
            return _dt.date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
        except ValueError:
            # 32/13/2026 and similar. A malformed date is not a date.
            continue
    return None


def _obligation_in(text: str, known: set[str] | None) -> str | None:
    """An obligation id the document names itself.

    Matched against ids that actually exist rather than by shape, because a
    string that merely looks like a rule id is not one.
    """
    if not known:
        return None
    for rule_id in known:
        if rule_id in text:
            return rule_id
    return None


def sniff(filename: str) -> str:
    """Which reader to use. Extension only, because content sniffing a
    spreadsheet and a zip archive apart is not worth the failure modes."""
    lowered = (filename or "").lower()
    for extension in SUPPORTED:
        if lowered.endswith("." + extension):
            return extension
    return ""


def xlsx_available() -> bool:
    """Whether the optional spreadsheet reader is installed."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


# ─────────────────────────────────────────────────────────────── JSON ──


def read_json(
    text: str,
    *,
    source: str = "upload.json",
    known_obligations: set[str] | None = None,
) -> IngestResult:
    """Read an API export.

    Accepts a bare list of objects or an object with an ``events`` key, because
    both are what real exports look like and arguing about it helps nobody.
    """
    result = IngestResult(source=source, fmt="json")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        result.problems.append(f"That file is not valid JSON. {exc}")
        return result

    rows = payload if isinstance(payload, list) else payload.get("events", [])
    if not isinstance(rows, list):
        result.problems.append(
            "Expected a list of records, or an object with an 'events' list."
        )
        return result

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            result.problems.append(f"Record {index} is not an object.")
            continue

        text_of_row = " ".join(str(v) for v in row.values())
        obligation = (row.get("obligation_id") or "").strip() or _obligation_in(
            text_of_row, known_obligations
        )
        occurred = _parse_date(str(row.get("occurred_on", "")))
        filed = _parse_date(str(row.get("filed_on", "")))

        if obligation and known_obligations and obligation not in known_obligations:
            result.problems.append(
                f"Record {index} names {obligation}, which is not a rule in this "
                "document."
            )
            obligation = None

        if obligation and occurred:
            confidence, why = Confidence.STATED, "the record names the rule and a date"
        elif occurred:
            confidence, why = (
                Confidence.UNRESOLVED,
                "a date was found but no rule is named, so somebody has to say "
                "which duty this belongs to",
            )
        else:
            confidence, why = (
                Confidence.UNRESOLVED,
                "no usable date was found in this record",
            )

        result.candidates.append(
            Candidate(
                source_document=source,
                row=index,
                excerpt=text_of_row[:160],
                occurred_on=occurred,
                filed_on=filed,
                reference=(row.get("reference") or None),
                artifact_type=(row.get("artifact_type") or "").strip(),
                entity=(row.get("entity") or "").strip(),
                obligation_id=obligation,
                confidence=confidence,
                why=why,
            )
        )
    return result


# ─────────────────────────────────────────────────────────────── XLSX ──


def read_xlsx(
    data: bytes,
    *,
    source: str = "upload.xlsx",
    known_obligations: set[str] | None = None,
) -> IngestResult:
    """Read a filing register spreadsheet.

    Optional. ``openpyxl`` is not a core dependency, because the product must
    install and run without it, so this returns a clear problem rather than an
    ImportError when it is absent.
    """
    result = IngestResult(source=source, fmt="xlsx")
    try:
        import openpyxl
    except ImportError:
        result.problems.append(
            "Reading .xlsx needs the optional openpyxl package. Install it with "
            "pip install 'sanhita[xlsx]', or export the sheet as CSV, which "
            "needs nothing extra."
        )
        return result

    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many shapes
        result.problems.append(f"That file could not be opened as a spreadsheet. {exc}")
        return result

    try:
        sheet = book.worksheets[0]
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        book.close()

    if not rows:
        result.problems.append("The first sheet is empty.")
        return result

    headers = [str(cell or "").strip().lower() for cell in rows[0]]
    for index, raw in enumerate(rows[1:], start=2):
        cells = ["" if cell is None else str(cell) for cell in raw]
        if not any(cell.strip() for cell in cells):
            continue
        record = dict(zip(headers, cells))
        joined = " ".join(cells)

        obligation = (record.get("obligation_id") or "").strip() or _obligation_in(
            joined, known_obligations
        )
        if obligation and known_obligations and obligation not in known_obligations:
            obligation = None

        occurred = _parse_date(record.get("occurred_on", "")) or _parse_date(joined)
        filed = _parse_date(record.get("filed_on", ""))
        reference_match = _REFERENCE.search(joined)

        if obligation and occurred:
            confidence, why = Confidence.STATED, "the row names the rule and a date"
        elif occurred and reference_match:
            confidence, why = (
                Confidence.PROBABLE,
                "a date and a reference were found on one row, which looks like a "
                "filing, but nothing says which duty it discharged",
            )
        else:
            confidence, why = (
                Confidence.UNRESOLVED,
                "not enough on this row to tell what it is",
            )

        result.candidates.append(
            Candidate(
                source_document=source,
                row=index,
                excerpt=joined[:160],
                occurred_on=occurred,
                filed_on=filed,
                reference=(record.get("reference") or "").strip()
                or (reference_match.group(1) if reference_match else None),
                artifact_type=(record.get("artifact_type") or "").strip(),
                entity=(record.get("entity") or "").strip(),
                obligation_id=obligation,
                confidence=confidence,
                why=why,
            )
        )
    return result


# ──────────────────────────────────────────────────────────────── PDF ──


def read_pdf(
    data: bytes,
    *,
    source: str = "upload.pdf",
    known_obligations: set[str] | None = None,
    max_pages: int = 60,
) -> IngestResult:
    """Read a company report.

    The least certain of the four readers, and the one where restraint matters
    most. A margin statement is prose and tables laid out for a human, with no
    column called ``obligation_id`` anywhere in it.

    So this finds lines that look like filings, records where each came from,
    and marks almost all of them UNRESOLVED. It does not attempt to match text
    against clause wording. Two passages being similar is not evidence that a
    duty was discharged, and inferring one from the other is exactly the
    mistake that would let this product tell a firm it is compliant when it is
    not.
    """
    result = IngestResult(source=source, fmt="pdf")
    try:
        import fitz
    except ImportError:  # pragma: no cover - PyMuPDF is a core dependency
        result.problems.append("PyMuPDF is not installed, so PDFs cannot be read.")
        return result

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - fitz raises many shapes
        result.problems.append(f"That file could not be opened as a PDF. {exc}")
        return result

    try:
        if document.page_count > max_pages:
            result.problems.append(
                f"That document is {document.page_count} pages. Only the first "
                f"{max_pages} were read, because a report longer than that is "
                "usually a bundle and is better uploaded in parts."
            )
        for page_number in range(min(document.page_count, max_pages)):
            text = document[page_number].get_text()
            for line in text.splitlines():
                stripped = line.strip()
                if len(stripped) < 8:
                    continue

                occurred = _parse_date(stripped)
                reference_match = _REFERENCE.search(stripped)
                obligation = _obligation_in(stripped, known_obligations)

                # A line with neither a date nor a reference is prose.
                if occurred is None and reference_match is None:
                    continue

                if obligation and occurred:
                    confidence = Confidence.STATED
                    why = "this line names a rule in this document and a date"
                elif occurred and reference_match:
                    confidence = Confidence.PROBABLE
                    why = (
                        "a date and a reference on one line, which looks like a "
                        "filing record. Which duty it discharged is not stated "
                        "and has to be confirmed"
                    )
                else:
                    confidence = Confidence.UNRESOLVED
                    why = (
                        "part of a filing record was found, but not enough to say "
                        "what it is without a person reading it"
                    )

                result.candidates.append(
                    Candidate(
                        source_document=source,
                        page=page_number + 1,
                        excerpt=stripped[:160],
                        occurred_on=occurred,
                        reference=reference_match.group(1) if reference_match else None,
                        obligation_id=obligation,
                        confidence=confidence,
                        why=why,
                    )
                )
    finally:
        document.close()

    if not result.candidates and not result.problems:
        result.problems.append(
            "No dates or reference numbers were found. If this is a scanned "
            "document there is no text layer to read, and Sanhita does no OCR."
        )
    return result


# ─────────────────────────────────────────────────────────── dispatch ──


def read_any(
    data: bytes,
    filename: str,
    *,
    known_obligations: set[str] | None = None,
) -> IngestResult:
    """Read whatever was uploaded, by extension."""
    fmt = sniff(filename)
    if fmt == "json":
        return read_json(
            data.decode("utf-8", "replace"),
            source=filename,
            known_obligations=known_obligations,
        )
    if fmt == "xlsx":
        return read_xlsx(data, source=filename, known_obligations=known_obligations)
    if fmt == "pdf":
        return read_pdf(data, source=filename, known_obligations=known_obligations)
    if fmt == "csv":
        # The CSV path is the strict one and already exists. Reuse it rather
        # than growing a second, laxer reader for the same format.
        from sanhita.execute.importer import read_csv

        strict = read_csv(
            data.decode("utf-8-sig", "replace"),
            known_obligations=known_obligations,
            label=filename,
        )
        result = IngestResult(source=filename, fmt="csv")
        result.problems.extend(f"row {e.line}, {e.problem}" for e in strict.errors)
        if strict.store:
            for index, event in enumerate(strict.store.events, start=1):
                result.candidates.append(
                    Candidate(
                        source_document=filename,
                        row=index,
                        occurred_on=event.occurred_on,
                        filed_on=event.filed_on,
                        reference=event.reference,
                        artifact_type=event.artifact_type,
                        entity=event.entity,
                        obligation_id=event.obligation_id,
                        confidence=Confidence.STATED,
                        why="the CSV names the rule outright",
                    )
                )
        return result

    result = IngestResult(source=filename, fmt="")
    result.problems.append(
        f"Sanhita does not read {filename!r}. Supported formats are CSV, JSON, "
        "XLSX and PDF."
    )
    return result

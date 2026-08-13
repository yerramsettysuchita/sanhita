"""Reading a firm's evidence out of the formats it actually has.

The property these tests exist to defend is restraint. A document that cannot be
confidently tied to an obligation must produce a candidate for a person to
confirm, never a compliance conclusion. Two texts being similar is not evidence
that a duty was discharged.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from sanhita.execute.ingest import (
    Candidate,
    Confidence,
    IngestResult,
    read_any,
    read_json,
    read_pdf,
    read_xlsx,
    sniff,
    xlsx_available,
)

KNOWN = {"SB-40.1.8-a", "SB-15.9.1-a"}


# ══════════════════════════════════════════════════ the central rule ══


def test_a_candidate_without_an_obligation_never_becomes_an_event():
    """The boundary the whole module exists to hold."""
    candidate = Candidate(
        source_document="report.pdf",
        occurred_on=_dt.date(2026, 3, 31),
        confidence=Confidence.UNRESOLVED,
    )

    assert not candidate.can_become_an_event
    with pytest.raises(ValueError, match="no obligation id"):
        candidate.to_event("EV-00001")


def test_only_confirmed_candidates_reach_the_engine():
    """An unresolved candidate must not silently become evidence."""
    result = IngestResult(source="x.pdf", fmt="pdf")
    result.candidates = [
        Candidate("x.pdf", occurred_on=_dt.date(2026, 1, 31), confidence=Confidence.UNRESOLVED),
        Candidate(
            "x.pdf",
            occurred_on=_dt.date(2026, 2, 28),
            obligation_id="SB-40.1.8-a",
            confidence=Confidence.STATED,
        ),
    ]

    store = result.to_store("test")

    assert len(store) == 1, "only the candidate naming a rule became an event"
    assert store.events[0].obligation_id == "SB-40.1.8-a"


def test_probable_still_needs_a_human():
    """A date beside a reference is a strong hint and not proof."""
    assert Confidence.PROBABLE.needs_human
    assert Confidence.UNRESOLVED.needs_human
    assert not Confidence.STATED.needs_human


# ═════════════════════════════════════════════════════════════ JSON ══


def test_json_reads_a_bare_list_and_an_events_object():
    rows = [
        {
            "obligation_id": "SB-40.1.8-a",
            "entity": "Demo Broking",
            "occurred_on": "2026-03-31",
            "filed_on": "2026-04-02",
            "reference": "RET-001",
        }
    ]
    bare = read_json(json.dumps(rows), known_obligations=KNOWN)
    wrapped = read_json(json.dumps({"events": rows}), known_obligations=KNOWN)

    for result in (bare, wrapped):
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.confidence is Confidence.STATED
        assert candidate.occurred_on == _dt.date(2026, 3, 31)
        assert candidate.filed_on == _dt.date(2026, 4, 2)


def test_json_naming_an_unknown_rule_is_reported_not_accepted():
    rows = [{"obligation_id": "SB-NOPE-z", "occurred_on": "2026-03-31"}]

    result = read_json(json.dumps(rows), known_obligations=KNOWN)

    assert result.problems, "an unknown rule must be reported"
    assert result.candidates[0].obligation_id is None
    assert result.candidates[0].confidence is Confidence.UNRESOLVED


def test_json_with_a_date_but_no_rule_is_unresolved():
    rows = [{"entity": "Demo Broking", "occurred_on": "2026-03-31"}]

    result = read_json(json.dumps(rows), known_obligations=KNOWN)

    assert result.candidates[0].confidence is Confidence.UNRESOLVED
    assert "no rule is named" in result.candidates[0].why


def test_malformed_json_says_so_rather_than_raising():
    result = read_json("{ not json", known_obligations=KNOWN)

    assert result.problems
    assert not result.candidates


# ══════════════════════════════════════════════════════════════ PDF ══


def _pdf_with(lines: list[str]) -> bytes:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 80), "\n".join(lines), fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def test_pdf_finds_filing_lines_and_records_the_page():
    data = _pdf_with(
        [
            "Daily Margin Statement Register",
            "2026-03-31   RET-001   dispatched",
            "2026-04-30   RET-002   dispatched",
        ]
    )

    result = read_pdf(data, source="margin.pdf", known_obligations=KNOWN)

    assert len(result.candidates) >= 2
    for candidate in result.candidates:
        assert candidate.page == 1
        assert candidate.source_document == "margin.pdf"
        assert candidate.excerpt


def test_pdf_lines_are_probable_not_stated():
    """A date and a reference do not say which duty they discharged."""
    data = _pdf_with(["2026-03-31   RET-001   dispatched to client"])

    result = read_pdf(data, source="margin.pdf", known_obligations=KNOWN)

    assert result.candidates
    assert all(c.confidence.needs_human for c in result.candidates)
    assert not result.ready, "nothing from a PDF reaches the engine unconfirmed"


def test_a_pdf_naming_a_rule_outright_is_stated():
    data = _pdf_with(["SB-40.1.8-a filed on 2026-03-31 under RET-001"])

    result = read_pdf(data, source="margin.pdf", known_obligations=KNOWN)

    stated = [c for c in result.candidates if c.confidence is Confidence.STATED]
    assert stated, "an id written in the document should be recognised"
    assert stated[0].obligation_id == "SB-40.1.8-a"


def test_a_pdf_with_no_text_layer_says_there_is_no_ocr():
    import fitz

    document = fitz.open()
    document.new_page()
    data = document.tobytes()
    document.close()

    result = read_pdf(data, source="scan.pdf")

    assert not result.candidates
    assert any("OCR" in p for p in result.problems)


def test_prose_without_dates_or_references_is_ignored():
    """Otherwise every sentence in a report becomes a candidate."""
    data = _pdf_with(
        [
            "This report summarises the margin position of the firm.",
            "It was prepared by the operations department.",
        ]
    )

    result = read_pdf(data, source="prose.pdf")

    assert not result.candidates


# ═════════════════════════════════════════════════════════════ XLSX ══


@pytest.mark.skipif(not xlsx_available(), reason="openpyxl is optional")
def test_xlsx_reads_a_filing_register():
    import io

    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["obligation_id", "entity", "occurred_on", "filed_on", "reference"])
    sheet.append(["SB-40.1.8-a", "Demo Broking", "2026-03-31", "2026-04-02", "RET-001"])
    sheet.append(["", "Demo Broking", "2026-04-30", "", "RET-002"])
    buffer = io.BytesIO()
    book.save(buffer)

    result = read_xlsx(buffer.getvalue(), known_obligations=KNOWN)

    assert len(result.candidates) == 2
    assert result.candidates[0].confidence is Confidence.STATED
    assert result.candidates[1].confidence in (
        Confidence.PROBABLE,
        Confidence.UNRESOLVED,
    )


def test_xlsx_without_openpyxl_explains_rather_than_crashing(monkeypatch):
    """The core install must work without the optional reader."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    result = read_xlsx(b"not really a workbook")

    assert result.problems
    assert "openpyxl" in result.problems[0]
    assert "CSV" in result.problems[0], "it should offer the way out"


# ═════════════════════════════════════════════════════════ dispatch ══


def test_sniff_picks_the_reader_by_extension():
    assert sniff("register.CSV") == "csv"
    assert sniff("export.json") == "json"
    assert sniff("book.xlsx") == "xlsx"
    assert sniff("report.pdf") == "pdf"
    assert sniff("notes.docx") == ""


def test_an_unsupported_format_is_refused_by_name():
    result = read_any(b"anything", "notes.docx")

    assert not result.candidates
    assert "does not read" in result.problems[0]
    assert "CSV, JSON, XLSX and PDF" in result.problems[0]


def test_csv_goes_through_the_strict_reader():
    """There must not be a second, laxer CSV path."""
    csv_text = (
        "obligation_id,entity,occurred_on,filed_on,artifact_type,reference\n"
        "SB-40.1.8-a,Demo Broking,2026-03-31,2026-04-02,report,RET-001\n"
    )

    result = read_any(csv_text.encode(), "register.csv", known_obligations=KNOWN)

    assert len(result.candidates) == 1
    assert result.candidates[0].confidence is Confidence.STATED
    assert result.candidates[0].obligation_id == "SB-40.1.8-a"


def test_a_bad_csv_row_surfaces_as_a_problem():
    csv_text = (
        "obligation_id,entity,occurred_on\n"
        "SB-40.1.8-a,Demo Broking,not-a-date\n"
    )

    result = read_any(csv_text.encode(), "register.csv", known_obligations=KNOWN)

    assert result.problems, "a malformed row must be reported, never dropped"

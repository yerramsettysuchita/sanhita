"""Deterministic page and line model for the source PDF.

PyMuPDF hands back "lines" that are really runs: it splits at every horizontal
jump, so a section heading whose number sits in one tab stop and whose title
sits in the next arrives as two separate lines::

    x0= 72.0 y=117.7 '20.  '
    x0=108.0 y=117.3 'Unique Client Code26 '

Reading those as two nodes would lose every section title in the document. This
module rebuilds visual lines by grouping runs that share a baseline, then sorts
them left to right. Everything downstream sees whole lines.

It also classifies the page furniture that must never enter a clause hash:

  page number     a centred numeric line below y=770
  part header     a centred roman-numeral banner at the top of the page,
                  e.g. "III.  DEALING WITH CLIENT" — Word running-header text
  footnote block  everything below the footnote separator rule, which Word
                  draws as a thin filled rect at x0=72.02, width 144.02

Superscript footnote markers are identified structurally, by the PDF's own
superscript flag (bit 0 of a span's flags), not by guessing from font size. Each
line therefore exposes both its raw text and a `text` with marker digits
removed, so clause hashes are computed on the regulation's sentence rather than
on our reading of the page furniture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

__all__ = ["Line", "Page", "Document", "load_document"]

# Word draws the footnote separator as a thin filled rectangle. These constants
# are measured from the corpus and asserted, not guessed; `load_document`
# reports how many pages matched so a format change cannot pass silently.
_SEP_X0 = 72.02
_SEP_WIDTH = 144.02
_SEP_TOL = 2.0
_SEP_MAX_HEIGHT = 2.5

_PAGE_NUMBER_MIN_Y = 770.0
_PART_HEADER_MAX_Y = 92.0
_PART_HEADER_MIN_X = 200.0

#: Runs whose baselines differ by less than this belong to the same visual line.
_BASELINE_TOL = 3.5

#: Fallback only, for a document too short to measure.
#:
#: This used to be the whole story: body copy was declared to be 12pt because
#: that is what the stock broker circular uses, and anything under 11.5 was
#: treated as a footnote or a table cell. That is a fact about one PDF, not
#: about regulation. SEBI's June 2025 research analyst circular is typeset at
#: 11.3pt, so every line in it fell below this number, every line was
#: classified as not-body, and a document containing 139 perfectly well-formed
#: numbered clauses parsed to zero clauses and reported "no numbered clauses
#: were found".
#:
#: The threshold is now measured per document. See ``body_min_size``.
BODY_MIN_SIZE = 11.5

#: How far below the document's own body size a line may still count as body.
#:
#: 0.7 is not arbitrary. The old fixed floor of 11.5 sat 0.5 under the 12pt
#: body of the two circulars that already parse, and every rule certified
#: against the stock broker circular depends on that parse staying exactly as
#: it was. A wider tolerance would pull those documents' 11pt table and caption
#: lines into the body and change a tree that 183 signatures are anchored to.
#: 0.7 keeps a 12pt document's floor at 11.3, which admits and excludes exactly
#: what 11.5 did, while giving an 11.3pt document a floor of 10.6 so it parses
#: at all. The parser regression tests assert both halves of that.
BODY_SIZE_TOLERANCE = 0.7


def body_min_size(sizes: list[float]) -> float:
    """The smallest font size that still counts as body copy, for one document.

    Body copy is whatever the document is mostly set in, so the modal size is
    the measurement rather than any constant. Rounded to a tenth before
    counting, because PyMuPDF reports 11.999999 and 12.0 as different floats
    and a raw mode would split one typeface across several buckets.
    """
    if not sizes:
        return BODY_MIN_SIZE
    counts: dict[float, int] = {}
    for size in sizes:
        key = round(size, 1)
        counts[key] = counts.get(key, 0) + 1
    # Ties go to the larger size: a document whose body and footnotes appear
    # equally often should not decide that the footnotes are the body.
    mode = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return round(mode - BODY_SIZE_TOLERANCE, 2)

_SUPERSCRIPT_BIT = 1
_BOLD_BIT = 16

_PART_HEADER_RE = re.compile(r"^[IVXL]{1,6}\.\s+[A-Z][A-Z0-9 \-/&,'()]{4,}$")


@dataclass(slots=True)
class Line:
    """One visual line of text, reassembled from PyMuPDF runs."""

    page: int
    #: Index of the PyMuPDF block this line came from. A block is a paragraph or
    #: a single table cell, which makes it the unit that may be safely joined.
    block: int
    x0: float
    y0: float
    y1: float
    raw_text: str
    text: str
    max_size: float
    is_bold: bool
    #: Size of the leading run. In the footnote block the marker glyph is 6.5pt
    #: against 10pt body, which is what distinguishes a new footnote entry from
    #: the wrapped continuation of the previous one.
    first_size: float = 0.0
    markers: list[int] = field(default_factory=list)
    fonts: tuple[str, ...] = ()
    #: [start, end) offsets of this line within `Document.text`. Body lines only;
    #: furniture and footnote lines keep -1 because nothing may anchor to them.
    start: int = -1
    end: int = -1
    #: The body-copy threshold measured from the document this line came from.
    #: Set once during ``load_document``. It is carried on the line rather than
    #: read from a module constant so that two documents with different
    #: typography can be open at the same time and each be judged on its own.
    body_floor: float = BODY_MIN_SIZE

    @property
    def is_body(self) -> bool:
        return self.max_size >= self.body_floor

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"p{self.page} x{self.x0:.0f} {self.text!r}"


@dataclass(slots=True)
class Page:
    """One page, separated into body, footnote block and furniture."""

    number: int
    body: list[Line] = field(default_factory=list)
    footnotes: list[Line] = field(default_factory=list)
    separator_y: float | None = None
    page_label: str | None = None
    part_header: str | None = None


@dataclass(slots=True)
class Document:
    """The whole PDF as pages, plus the flat text every char_span indexes into.

    `text` is the parser's canonical reading-order rendering: body lines only,
    footnote markers stripped, one line per newline. It is built once and every
    span in the compiled output refers to it, so a span is reproducible rather
    than being an offset into whatever extraction mode happened to run.
    """

    path: str
    page_count: int
    pages: list[Page]
    text: str
    page_starts: dict[int, int]
    separator_pages: int
    pdf_metadata: dict[str, str]


def _merge_runs(blocks: list[tuple[int, list[dict]]], page_no: int) -> list[Line]:
    """Group PyMuPDF runs into visual lines by shared baseline, within one block.

    Merging must not cross a block boundary. PyMuPDF splits a line at every
    horizontal jump, so a section heading arrives as "20." plus "Unique Client
    Code" — same block, same baseline — and those must be rejoined or every
    section title in the document is lost.

    But the two columns of a table row also share a baseline while living in
    *different* blocks, and joining those interleaves them: the appendix's
    "MIRSD/SE/CIR-19/2009 dated December 03, 2009" comes out as
    "MIRSD/SE/CIR-19/2009 dated | Dealings between a client and a | December 03,
    2009", which severs each reference from its date. Block identity is what
    separates the two cases.
    """
    buckets: list[tuple[int, list[dict]]] = []
    for block_index, runs in blocks:
        for run in sorted(runs, key=lambda r: (round(r["bbox"][1], 1), r["bbox"][0])):
            y0 = run["bbox"][1]
            if (
                buckets
                and buckets[-1][0] == block_index
                and abs(buckets[-1][1][0]["bbox"][1] - y0) <= _BASELINE_TOL
            ):
                buckets[-1][1].append(run)
            else:
                buckets.append((block_index, [run]))

    lines: list[Line] = []
    for block_index, bucket in buckets:
        bucket.sort(key=lambda r: r["bbox"][0])
        spans = [s for run in bucket for s in run["spans"]]
        if not spans:
            continue

        raw_parts: list[str] = []
        clean_parts: list[str] = []
        markers: list[int] = []
        for span in spans:
            raw_parts.append(span["text"])
            if (span["flags"] & _SUPERSCRIPT_BIT) and re.fullmatch(r"\s*\d{1,3}\s*", span["text"]):
                # A footnote reference mark. Recorded, then dropped from the
                # text that will be hashed: the digits are page furniture, not
                # part of the regulation's sentence.
                markers.append(int(span["text"].strip()))
            else:
                clean_parts.append(span["text"])

        raw_text = "".join(raw_parts)
        clean_text = "".join(clean_parts)
        if not raw_text.strip():
            continue

        lines.append(
            Line(
                page=page_no,
                block=block_index,
                x0=min(r["bbox"][0] for r in bucket),
                y0=min(r["bbox"][1] for r in bucket),
                y1=max(r["bbox"][3] for r in bucket),
                raw_text=raw_text,
                text=clean_text,
                max_size=max(s["size"] for s in spans),
                is_bold=any(s["flags"] & _BOLD_BIT for s in spans),
                first_size=spans[0]["size"],
                markers=markers,
                fonts=tuple(sorted({s["font"] for s in spans})),
            )
        )
    # Page-wide reading order: down the page, then left to right. Deterministic,
    # and correct for the flowing numbered text that carries the obligations.
    lines.sort(key=lambda ln: (round(ln.y0, 1), ln.x0, ln.block))
    return lines


def _separator_y(page: fitz.Page) -> float | None:
    """Find the footnote separator rule, if this page has footnotes."""
    best: float | None = None
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        if (
            abs(rect.x0 - _SEP_X0) < _SEP_TOL
            and abs(rect.width - _SEP_WIDTH) < _SEP_TOL
            and rect.height < _SEP_MAX_HEIGHT
        ):
            if best is None or rect.y0 < best:
                best = rect.y0
    return best


def load_document(pdf_path: str | Path) -> Document:
    """Load and classify the whole PDF. Pure function of the file's bytes."""
    path = Path(pdf_path)
    doc = fitz.open(path)

    pages: list[Page] = []
    chunks: list[str] = []
    page_starts: dict[int, int] = {}
    cursor = 0
    separator_pages = 0

    try:
        for index in range(doc.page_count):
            fitz_page = doc[index]
            number = index + 1
            sep_y = _separator_y(fitz_page)
            if sep_y is not None:
                separator_pages += 1

            runs = [
                (index, block["lines"])
                for index, block in enumerate(fitz_page.get_text("dict")["blocks"])
                if block.get("type") == 0
            ]
            page = Page(number=number, separator_y=sep_y)

            for line in _merge_runs(runs, number):
                stripped = line.text.strip()
                if not stripped:
                    continue

                # Page number: centred, numeric, at the very foot.
                if line.y0 >= _PAGE_NUMBER_MIN_Y:
                    if re.fullmatch(r"\d{1,4}", stripped):
                        page.page_label = stripped
                    continue

                # Footnote block: anything at or below the separator rule.
                if sep_y is not None and line.y0 >= sep_y:
                    page.footnotes.append(line)
                    continue

                # Running part header: centred roman banner at the page top.
                if (
                    line.y0 <= _PART_HEADER_MAX_Y
                    and line.x0 >= _PART_HEADER_MIN_X
                    and _PART_HEADER_RE.match(stripped)
                ):
                    page.part_header = stripped
                    continue

                page.body.append(line)

            page_starts[number] = cursor
            for line in page.body:
                rendered = line.text.rstrip() + "\n"
                chunks.append(rendered)
                line.start = cursor
                # The span covers the line's characters, not the newline the
                # renderer adds, so a clause hash and its span agree exactly.
                line.end = cursor + len(rendered) - 1
                cursor += len(rendered)
            pages.append(page)

        metadata = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        page_count = doc.page_count
    finally:
        doc.close()

    # Measure what this document is actually set in, then tell every line.
    #
    # Deliberately after the page walk rather than during it, because the mode
    # is a property of the whole document and no single page knows it. Safe to
    # do here because nothing above consults ``is_body``: which lines are body
    # rather than furniture or footnotes is decided by the separator rule and
    # page geometry, so ``Document.text`` and every character offset in it are
    # already fixed and this pass cannot move them.
    floor = body_min_size(
        [line.max_size for page in pages for line in page.body]
    )
    for page in pages:
        for line in page.body:
            line.body_floor = floor
        for line in page.footnotes:
            line.body_floor = floor

    return Document(
        path=str(path),
        page_count=page_count,
        pages=pages,
        text="".join(chunks),
        page_starts=page_starts,
        separator_pages=separator_pages,
        pdf_metadata=metadata,
    )

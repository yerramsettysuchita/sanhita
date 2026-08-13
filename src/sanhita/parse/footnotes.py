"""Footnote provenance extraction.

This master circular carries no internal amendment history — no "Inserted vide",
no "Substituted vide". Building an amendment model against it would be inventing
structure the document does not contain. What it *does* carry, richly, is
clause-to-source-circular provenance:

    body:      "21. Simplification and Rationalization of Trading Account Opening Process²⁸"
    footnote:  "28  Reference: Circular CIR/MIRSD/16/2011 dated August 22, 2011"

That binding is worth more than an amendment log. It tells a reviewer that a
paragraph printed in a 2025 consolidation is really a 2011 rule that has never
changed, and it gives every compiled obligation a dated regulatory lineage.

Two independent signals are used, and they are cross-checked against each other:

  definitions   numbered entries below the footnote separator rule
  markers       superscript-flagged digit spans in the body text

Where the two disagree the discrepancy is reported, never patched. A marker with
no definition is `unresolved`; a definition no marker points at is `orphaned`.
Guessing either way would fabricate provenance, which is the one thing this
module exists to prevent.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

from sanhita.parse.layout import Document, Line  # noqa: F401  (Line used in annotations)

__all__ = [
    "BodyCircularRef",
    "FootnoteDef",
    "FootnoteRef",
    "FootnoteReport",
    "extract_footnotes",
    "parse_circular_date",
    "read_issue_date",
]


_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_DATE_RE = re.compile(
    r"\b(?P<month>[A-Z][a-z]{2,8})\.?\s+(?P<day>\d{1,2})\s*,?\s*(?P<year>(?:19|20)\d{2})\b"
)

_DATED_RE = re.compile(r"\bdated\s+(?P<date>[A-Z][a-z]{2,8}\.?\s+\d{1,2}\s*,?\s*(?:19|20)\d{2})\b")

#: A single whitespace-delimited token that may form part of a circular
#: reference. References in this corpus contain slashes, hyphens, underscores,
#: dots, parentheses and Roman numerals, e.g. "SMD/POLICY(BRK.REG.)/CIR-18/98"
#: and "SEBI/MIRSD/MASTER CIR-04/2010".
_REF_TOKEN_RE = re.compile(r"^[A-Za-z0-9()._/\-]+$")

#: How far back from "dated" to look, and how many tokens a reference may span.
_REF_LOOKBACK = 140
_REF_MAX_TOKENS = 6

_MARKER_LINE_RE = re.compile(r"^\s*(\d{1,3})\s*(?=\D|$)")


def parse_circular_date(text: str) -> _dt.date | None:
    """Parse 'August 22, 2011' or 'Dec 3, 2009' into a date. None if unparseable."""
    match = _DATE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").lower().rstrip("."))
    if month is None:
        return None
    try:
        return _dt.date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        # A malformed day such as "February 31" is reported as unparseable
        # rather than being coerced to a nearby valid date.
        return None


def read_issue_date(text: str) -> tuple[_dt.date, str] | None:
    """The date a circular was issued, read off its own first page.

    Returns the date and the exact characters it came from, because a date this
    product sorts editions by has to be checkable against the document rather
    than trusted.

    **The first parseable date on page one, and nothing cleverer.** SEBI prints
    the issue date in the header block, above the subject line and above every
    reference to an earlier circular. Verified against all six master circulars
    in ``corpus/``: in each one the first date on page one is the issue date and
    matches the date in the filename. A rule that searched further, or preferred
    the latest date it found, would pick up the "dated August 09, 2024" of a
    consolidated circular and report a 2025 master circular as a 2024 one.

    Returns ``None`` where no date is found, and the caller must then leave the
    edition undated rather than guess. An edition sorted by a wrong date is
    worse than one that cannot be sorted, because the wrong order is invisible.
    """
    head = text[:900]
    match = _DATE_RE.search(head)
    if match is None:
        return None
    parsed = parse_circular_date(match.group(0))
    if parsed is None:
        return None
    return parsed, match.group(0)


def _tokens_before(window: str) -> list[str]:
    """The trailing run of reference-shaped tokens in `window`.

    Walks backwards from the end, taking tokens while they still look like part
    of a reference, and stopping at the first token that carries sentence
    punctuation such as a comma or a semicolon. A reference may legitimately
    contain an internal space ("SEBI/MIRSD/MASTER CIR-04/2010"), which is why
    this cannot be a single regex over the whole window.
    """
    taken: list[str] = []
    for token in reversed(window.split()):
        if len(taken) >= _REF_MAX_TOKENS:
            break
        if not _REF_TOKEN_RE.match(token):
            break
        taken.append(token)
    taken.reverse()
    return taken


def _clean_ref(tokens: list[str]) -> str:
    """Drop everything before the reference proper, then tidy the spacing.

    A circular number always contains a slash, so the reference begins at the
    first token that has one. Dropping by that rule rather than by a list of
    introducing words also handles the trailing debris of a *previous*
    reference: in "...dated July 11, 2011 and Circular CIR/MIRSD/4/2014 dated
    October 13, 2014" the backward scan reaches "2011", and only the slash rule
    stops "2011 and Circular" from being glued onto the second reference.
    """
    while tokens and "/" not in tokens[0]:
        tokens = tokens[1:]
    text = " ".join(tokens).strip(" ,;:.")
    # "SEBI/HO/ MIRSD/ MIRSD_DPIEA" -> "SEBI/HO/MIRSD/MIRSD_DPIEA"
    text = re.sub(r"\s*/\s*", "/", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,;:.")


def extract_refs(text: str) -> list[tuple[str, _dt.date | None]]:
    """Pull every '<circular ref> dated <date>' pair out of `text`, in order.

    Anchors on the word "dated" and walks backwards, because the reference
    itself has no fixed shape: it may contain spaces, parentheses, dots and
    Roman numerals, so a forward-only pattern either over-matches into the
    preceding sentence or stops short of a reference containing a space.
    """
    out: list[tuple[str, _dt.date | None]] = []
    for dated in _DATED_RE.finditer(text):
        start = max(0, dated.start() - _REF_LOOKBACK)
        window = text[start : dated.start()]
        ref = _clean_ref(_tokens_before(window))
        # Without a slash it is prose, not a circular number. Reporting nothing
        # is correct here: a half-recovered reference is worse than none.
        if "/" not in ref or len(ref) < 5:
            continue
        out.append((ref, parse_circular_date(dated.group("date"))))
    return out


@dataclass(slots=True)
class FootnoteDef:
    """A numbered entry below the separator rule on some page."""

    marker: int
    page: int
    raw_text: str

    @property
    def body(self) -> str:
        return _MARKER_LINE_RE.sub("", self.raw_text, count=1).strip()


@dataclass(slots=True)
class FootnoteRef:
    """A footnote definition bound to the clause whose text carried its marker."""

    marker: int
    clause_id: str | None
    circular_ref: str | None
    dated: _dt.date | None
    raw_text: str
    page: int
    extra_circular_refs: list[str] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.clause_id is not None

    @property
    def all_refs(self) -> list[str]:
        refs = ([self.circular_ref] if self.circular_ref else []) + self.extra_circular_refs
        return sorted(set(refs))


@dataclass(slots=True)
class BodyCircularRef:
    """A dated circular reference occurring in running body text.

    Deliberately kept separate from FootnoteRef and at lower confidence. A
    footnote marker is a typographic binding the document itself asserts; a
    sentence that happens to mention a circular is not. These are never attached
    to a clause unless the attachment is unambiguous.
    """

    circular_ref: str
    dated: _dt.date | None
    page: int
    clause_id: str | None = None
    context: str = ""


@dataclass(slots=True)
class FootnoteReport:
    """Everything recovered, including everything that failed to resolve."""

    footnotes: list[FootnoteRef] = field(default_factory=list)
    body_refs: list[BodyCircularRef] = field(default_factory=list)
    unresolved_markers: list[int] = field(default_factory=list)
    orphan_definitions: list[int] = field(default_factory=list)
    ambiguous_markers: list[tuple[int, str]] = field(default_factory=list)
    definition_count: int = 0
    marker_count: int = 0
    #: Every "dated <Month DD, YYYY>" in body text, including the ones with no
    #: recoverable circular number ("the Master Circular dated August 09, 2024").
    #: Reported alongside body_refs so the gap between them is visible.
    dated_mentions: int = 0

    @property
    def resolved_count(self) -> int:
        return sum(1 for f in self.footnotes if f.is_resolved)

    def by_clause(self) -> dict[str, list[FootnoteRef]]:
        out: dict[str, list[FootnoteRef]] = {}
        for ref in self.footnotes:
            if ref.clause_id:
                out.setdefault(ref.clause_id, []).append(ref)
        return out


def _collect_definitions(document: Document) -> list[FootnoteDef]:
    """Read the footnote block on every page that has a separator rule.

    A new footnote starts at a line whose first run is the small marker glyph;
    everything after it on smaller type is that footnote's continuation, because
    long references wrap.
    """
    defs: list[FootnoteDef] = []
    for page in document.pages:
        current: FootnoteDef | None = None
        for line in page.footnotes:
            stripped = line.raw_text.strip()
            if not stripped:
                continue
            # A new entry begins only where the small marker glyph does. Testing
            # the whole line's size instead would split a wrapped reference that
            # happens to continue with a digit onto a spurious second footnote.
            starts_entry = bool(_MARKER_LINE_RE.match(stripped)) and line.first_size <= 8.5
            if starts_entry:
                if current is not None:
                    defs.append(current)
                marker = int(_MARKER_LINE_RE.match(stripped).group(1))
                current = FootnoteDef(marker=marker, page=page.number, raw_text=stripped)
            elif current is not None:
                current.raw_text = f"{current.raw_text} {stripped}"
        if current is not None:
            defs.append(current)
    return defs


def _collect_markers(document: Document) -> list[tuple[int, int, Line]]:
    """Every superscript-flagged numeric marker in body text, in reading order."""
    out: list[tuple[int, int, Line]] = []
    for page in document.pages:
        for line in page.body:
            for marker in line.markers:
                out.append((marker, page.number, line))
    return out


def extract_footnotes(
    document: Document,
    clause_of_line: dict[int, str] | None = None,
) -> FootnoteReport:
    """Recover footnote provenance and body-level circular references.

    `clause_of_line` maps ``id(Line)`` to the clause id that line belongs to,
    supplied by the clause parser. Without it, footnotes are still extracted but
    nothing is bound to a clause.
    """
    clause_of_line = clause_of_line or {}
    report = FootnoteReport()

    definitions = _collect_definitions(document)
    report.definition_count = len(definitions)
    by_marker: dict[int, FootnoteDef] = {}
    for definition in definitions:
        # A marker number appearing twice would make binding ambiguous; keep the
        # first and report the collision rather than silently overwriting.
        if definition.marker in by_marker:
            report.ambiguous_markers.append(
                (definition.marker, f"duplicate definition on p{definition.page}")
            )
            continue
        by_marker[definition.marker] = definition

    markers = _collect_markers(document)
    report.marker_count = len(markers)
    clause_by_marker: dict[int, str] = {}
    seen_markers: set[int] = set()
    for marker, _page, line in markers:
        seen_markers.add(marker)
        clause_id = clause_of_line.get(id(line))
        if clause_id is None:
            continue
        if marker in clause_by_marker and clause_by_marker[marker] != clause_id:
            report.ambiguous_markers.append(
                (marker, f"marker appears on both {clause_by_marker[marker]} and {clause_id}")
            )
            continue
        clause_by_marker[marker] = clause_id

    for marker in sorted(by_marker):
        definition = by_marker[marker]
        refs = extract_refs(definition.body)
        primary_ref, primary_date = (refs[0] if refs else (None, None))
        report.footnotes.append(
            FootnoteRef(
                marker=marker,
                clause_id=clause_by_marker.get(marker),
                circular_ref=primary_ref,
                dated=primary_date,
                raw_text=definition.body,
                page=definition.page,
                extra_circular_refs=[r for r, _ in refs[1:]],
            )
        )

    report.unresolved_markers = sorted(seen_markers - set(by_marker))
    report.orphan_definitions = sorted(set(by_marker) - seen_markers)

    # Body-level references: same extraction, lower confidence, never bound to a
    # clause here. Run over the page's text joined into one string rather than
    # line by line, because "...Circular SEBI/HO/MIRSD/2022/44\ndated April 04,
    # 2022..." wraps mid-reference and a per-line scan would miss it entirely.
    # Footnote blocks are excluded so a reference is counted once.
    for page in document.pages:
        for joined in _joined_blocks(page.body):
            report.dated_mentions += len(_DATED_RE.findall(joined))
            for ref, dated in extract_refs(joined):
                report.body_refs.append(
                    BodyCircularRef(
                        circular_ref=ref,
                        dated=dated,
                        page=page.number,
                        clause_id=None,
                        context=_context_for(joined, ref),
                    )
                )
    return report


def _joined_blocks(lines: list[Line]) -> list[str]:
    """Join each block's lines into one string, one entry per block.

    Grouping is by block identity, not by adjacency in the page's line order.
    In the appendix the two table columns are separate blocks whose lines share
    baselines, so page-wide reading order interleaves them; joining only
    *consecutive* same-block lines would therefore shred each cell into
    fragments and cut every reference off from its date. The columns must be
    reassembled per block:

        blk3: "19. MIRSD/DR-1/CIR-16/09 dated" + "November 06, 2009."
        blk4: "Market Access through Authorised" + "Persons."

    Blocks are emitted in order of first appearance, so the result stays
    deterministic.
    """
    grouped: dict[int, list[str]] = {}
    order: list[int] = []
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if line.block not in grouped:
            grouped[line.block] = []
            order.append(line.block)
        grouped[line.block].append(text)
    return [" ".join(grouped[block]) for block in order]


def _context_for(text: str, ref: str) -> str:
    """A short window around the first mention of `ref`, for operator review."""
    probe = ref.split("/")[0]
    at = text.find(probe)
    if at < 0:
        return text[:160]
    return text[max(0, at - 40) : at + 120].strip()

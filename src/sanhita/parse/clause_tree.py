"""PDF to clause tree.

The stock-broker master circular is **section numbered**, not chapter numbered.
It contains no chapter divisions of its own, so the hierarchy is::

    Section    "21"            bold heading in the numbered body
      Clause   "21.1"
        SubClause "21.1.2"     and deeper: "21.1.2.1"
          Item   "21.1.2(a)"   lettered and roman sub-items
    Annexure   "ANX-7"         a peer of Section, not a child
    Appendix   "APX"           the circular list at the end

Depth comes from the numbering token, which is authoritative. Indentation is
used as a *corroborating* signal only: the document indents each level
consistently, so a numbered line sitting far from its level's usual left edge is
recorded as an indent mismatch for review rather than being silently accepted or
silently dropped.

Three properties this module must hold, and which `sanhita verify` proves:

  **Determinism.** Ids, spans and hashes are a pure function of the PDF bytes.
  Nothing depends on dict ordering, wall-clock time, or filesystem order.

  **Verbatim text.** A clause's hash covers the regulation's own characters.
  The only thing removed is a superscript footnote marker's digits, which are
  typographic apparatus rather than part of the sentence, and those are recorded
  on the node so nothing is lost.

  **Sequence is observed, not assumed.** Section numbers are not reordered.
  Gaps, repeats and out-of-sequence numbering are reported.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from sanhita.parse.anchors import NonAsciiReport, clause_sha256, scan_non_ascii
from sanhita.parse.footnotes import _joined_blocks
from sanhita.parse.layout import Document, Line, load_document

__all__ = [
    "ClauseNode",
    "ClauseTree",
    "ParseStats",
    "parse_clause_tree",
]


# --------------------------------------------------------------------------
# Recognisers
# --------------------------------------------------------------------------

#: The trailing dot is optional. SEBI's drafting is inconsistent about it —
#: "21.1. SEBI has devised..." but "15.1.1 Uniform nomenclature..." — and
#: requiring it makes the greedy number group stop one component short, so
#: "15.1.1 Uniform" parses as clause "15.1" titled "1 Uniform". That silently
#: collapses every dotless sub-clause in the document onto its parent's id.
_NUMBERED_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?\s+(\S.*)$", re.S)
_LETTER_RE = re.compile(r"^\(?([a-z])\)\s+(\S.*)$", re.S)
_ROMAN_RE = re.compile(r"^\(?(x{0,2}(?:ix|iv|v?i{1,3}|v|x))[.)]\s+(\S.*)$", re.S)

_ANNEXURE_HEAD_RE = re.compile(r"^Annexure\s*[-–—]?\s*(\d{1,3}[A-Z]?)\s*$", re.I)
_ANNEXURES_BANNER_RE = re.compile(r"^Annexures?\s*$", re.I)

#: The closing appendix, matched on its full banner. A loose ``^APPENDIX``
#: pattern also catches the right-aligned "Appendix-A" / "Appendix-B" sub-heads
#: inside the cloud-services annexure, which would truncate the annexure region
#: thirty pages early and silently lose half the annexures.
_APPENDIX_HEAD_RE = re.compile(r"^APPENDIX\s*[-–—]\s*LIST OF CIRCULARS\b")
_TOC_BANNER_RE = re.compile(r"^TABLE OF CONTENTS\s*$", re.I)
_ANNEXURE_MENTION_RE = re.compile(r"Annexure\s*[-–—]?\s*\d{1,3}[A-Z]?", re.I)

_ROMAN_SEQUENCE = [
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx",
]

#: Section headings sit at the far left. Anything indented past this is a list
#: item inside a table or annexure, not a section.
_SECTION_MAX_X0 = 115.0

#: A numbered line further than this from its level's median left edge is
#: flagged. Wide enough to tolerate the document's two tab stops per level.
_INDENT_TOLERANCE = 30.0

#: How far a numbered line in a flat circular may sit from that document's own
#: left margin and still open a clause. Tight, because in a document with no
#: headings the margin is the only signal separating a clause from a numbered
#: item inside one.
_FLAT_MARGIN_TOLERANCE = 4.0

#: How far right of a document's own left margin a section heading may sit.
#:
#: Across the eleven circulars tested, section headings sit between 65 and 109
#: points from the page edge while the fixed ceiling above is 115, so every one
#: of them happens to fit. That is luck, not design: a circular typeset with
#: wider margins would put its headings past 115 and none would be recognised.
_SECTION_MARGIN_BAND = 50.0


def _section_ceiling(document) -> float:
    """The x0 above which a bold number is a list item rather than a heading.

    Measured from the document, then combined with the fixed constant so the
    result can only ever be **more** permissive than the constant alone. That
    matters more than elegance: a line that qualified as a section before must
    still qualify, or the stock broker tree moves and the 183 signatures
    anchored to it no longer point at the text they were signed over.

    The margin is the 5th percentile of body indents rather than the minimum,
    because a single table cell hanging off the left edge would otherwise drag
    it far enough to mean nothing.
    """
    indents = sorted(
        line.x0 for page in document.pages for line in page.body if line.is_body
    )
    if not indents:
        return _SECTION_MAX_X0
    margin = indents[max(0, min(len(indents) - 1, int(len(indents) * 0.05)))]
    return max(_SECTION_MAX_X0, margin + _SECTION_MARGIN_BAND)


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ClauseNode:
    """One addressable unit of the regulation."""

    id: str
    kind: str
    number: str
    title: str
    text: str
    page: int
    char_span: tuple[int, int]
    sha256: str
    depth: int
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    section: str = ""
    x0: float = 0.0
    footnote_markers: list[int] = field(default_factory=list)
    indent_mismatch: bool = False
    lines: list[Line] = field(default_factory=list, repr=False)

    @property
    def verbatim_text(self) -> str:
        return self.text

    def __str__(self) -> str:  # pragma: no cover - display only
        head = self.title or self.text[:60]
        return f"{self.id} [{self.kind}] p{self.page} {head!r}"


@dataclass(slots=True)
class ParseStats:
    """What the parse found, including what it could not resolve."""

    pdf_path: str = ""
    page_count: int = 0
    body_page_start: int = 0
    annexure_page_start: int | None = None
    appendix_page_start: int | None = None
    toc_pages: tuple[int, int] | None = None

    sections: int = 0
    depth_counts: Counter = field(default_factory=Counter)
    lettered_items: int = 0
    roman_items: int = 0
    annexure_lettered_items: int = 0
    annexure_roman_items: int = 0
    annexures: int = 0
    annexure_mentions: int = 0
    appendix_entries: int = 0
    local_lists: int = 0
    total_nodes: int = 0

    #: True where the document had no section headings and was read as a flat
    #: list of numbered paragraphs. Ordinary circulars are shaped that way;
    #: master circulars never are. Recorded rather than hidden, because a
    #: reviewer is entitled to know which reading produced the clauses in front
    #: of them.
    flat_parse: bool = False

    section_numbers: list[int] = field(default_factory=list)
    section_gaps: list[int] = field(default_factory=list)
    out_of_sequence: list[tuple[int, int]] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    repeated_item_ids: list[str] = field(default_factory=list)
    indent_mismatches: list[str] = field(default_factory=list)
    rejected_section_candidates: list[str] = field(default_factory=list)
    #: Lines that open with a clause number but are cross-references inside a
    #: sentence ("...as provided at 15.7.3.1 above..."), not new clauses.
    cross_references: list[str] = field(default_factory=list)

    @property
    def lettered_items_total(self) -> int:
        return self.lettered_items + self.annexure_lettered_items

    @property
    def roman_items_total(self) -> int:
        return self.roman_items + self.annexure_roman_items

    separator_pages: int = 0
    document_chars: int = 0
    non_ascii: NonAsciiReport | None = None
    parse_seconds: float = 0.0

    @property
    def clauses_depth_2_plus(self) -> int:
        """Nodes whose number has at least two components (X.Y and deeper).

        Reported because a line-anchored regex such as ``^\\s*\\d+\\.\\d+\\.``
        also matches the prefix of ``21.1.1.``; this is the figure such a count
        actually produces, and it is what external counts should be compared to.
        """
        return sum(count for depth, count in self.depth_counts.items() if depth >= 2)

    @property
    def clauses_depth_3_plus(self) -> int:
        return sum(count for depth, count in self.depth_counts.items() if depth >= 3)


@dataclass(slots=True)
class ClauseTree:
    """The parsed document."""

    document: Document
    nodes: dict[str, ClauseNode] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    stats: ParseStats = field(default_factory=ParseStats)
    clause_of_line: dict[int, str] = field(default_factory=dict, repr=False)

    def get(self, node_id: str) -> ClauseNode | None:
        return self.nodes.get(node_id)

    def section(self, number: int | str) -> list[ClauseNode]:
        """Every node belonging to one top-level section, in document order."""
        key = str(number)
        return [n for n in self.nodes.values() if n.section == key]

    def walk(self, node_id: str | None = None):
        """Depth-first traversal in document order."""
        starts = [node_id] if node_id else self.roots
        stack = list(reversed(starts))
        while stack:
            current = stack.pop()
            node = self.nodes.get(current)
            if node is None:
                continue
            yield node
            stack.extend(reversed(node.children))

    def fingerprint(self) -> str:
        """A single hash over every id and clause hash, in id-sorted order.

        This is what determinism is checked against: two runs agree if and only
        if they agree on every id and every hash.
        """
        import hashlib

        digest = hashlib.sha256()
        for node_id in sorted(self.nodes):
            node = self.nodes[node_id]
            digest.update(node_id.encode("utf-8"))
            digest.update(b"\x1f")
            digest.update(node.sha256.encode("utf-8"))
            digest.update(b"\x1f")
            digest.update(str(node.char_span).encode("utf-8"))
            digest.update(b"\x1e")
        return digest.hexdigest()


# --------------------------------------------------------------------------
# Region detection
# --------------------------------------------------------------------------


def _find_regions(document: Document) -> tuple[int, int | None, int | None, tuple[int, int] | None]:
    """Locate the body, annexure and appendix regions, and the table of contents.

    All four boundaries are read off the document's own typography rather than
    hard-coded page numbers, so a re-issued circular with different pagination
    still parses. The same now goes for its margins.
    """
    ceiling = _section_ceiling(document)
    toc_start: int | None = None
    toc_end: int | None = None
    for page in document.pages:
        for line in page.body:
            if _TOC_BANNER_RE.match(line.text.strip()):
                toc_start = page.number
                break
        if toc_start is not None:
            break

    # The body begins at the first bold, left-aligned "N. Title" heading after
    # the table of contents.
    body_start = 1
    for page in document.pages:
        if toc_start is not None and page.number <= toc_start:
            continue
        found = False
        for line in page.body:
            if not (line.is_bold and line.is_body and line.x0 <= ceiling):
                continue
            match = _NUMBERED_RE.match(line.text.strip())
            if match and "." not in match.group(1) and match.group(2).strip():
                body_start = page.number
                found = True
                break
        if found:
            break
    if toc_start is not None:
        toc_end = body_start - 1

    # Annexures begin where a centred bold "Annexures" banner or "Annexure-N"
    # heading is the first body line on its page.
    annexure_start: int | None = None
    for page in document.pages:
        if page.number <= body_start or not page.body:
            continue
        first = page.body[0].text.strip()
        if not page.body[0].is_bold:
            continue
        if _ANNEXURES_BANNER_RE.match(first) or _ANNEXURE_HEAD_RE.match(first):
            annexure_start = page.number
            break

    appendix_start: int | None = None
    for page in document.pages:
        if page.number <= body_start:
            continue
        for line in page.body:
            if line.is_bold and _APPENDIX_HEAD_RE.match(line.text.strip()):
                appendix_start = page.number
                break
        if appendix_start is not None:
            break

    toc = (toc_start, toc_end) if toc_start and toc_end and toc_end >= toc_start else None
    return body_start, annexure_start, appendix_start, toc


# --------------------------------------------------------------------------
# Item token disambiguation
# --------------------------------------------------------------------------


def _choose_definitions(
    document: Document,
    body_start: int,
    toc: tuple[int, int] | None,
    annexure_start: int | None,
) -> dict[int, str]:
    """Decide which line *defines* each clause number, before building anything.

    Wrapped sentences routinely begin with a clause number:

        "...shall not be eligible under
         15.7.3.1 above shall not be eligible for re-appointment..."

    Read naively, that invents a clause and steals the id from the real 15.7.3.1.
    Deciding as we go does not work either, because a cross-reference often
    appears *before* the clause it points at, so first-come-first-served hands
    the id to the wrong line.

    So the choice is made up front, over every occurrence of each number, using
    the one signal the document is consistent about: a clause is printed at its
    level's left margin, while a wrapped sentence sits at the deeper
    continuation indent. The leftmost occurrence is the definition; the rest are
    cross-references. Ties fall to the earlier occurrence, so the result does
    not depend on iteration order.

    Returns a map from ``id(Line)`` to the number that line defines.
    """
    candidates: dict[str, list[tuple[float, int, Line]]] = {}
    order = 0
    for page in document.pages:
        if page.number < body_start:
            continue
        if toc and toc[0] <= page.number <= toc[1]:
            continue
        if annexure_start is not None and page.number >= annexure_start:
            break
        for line in page.body:
            order += 1
            if not line.is_body:
                continue
            match = _NUMBERED_RE.match(line.text.strip())
            if not match:
                continue
            candidates.setdefault(match.group(1), []).append((line.x0, order, line))

    chosen: dict[int, str] = {}
    for number, occurrences in candidates.items():
        occurrences.sort(key=lambda item: (round(item[0], 1), item[1]))
        chosen[id(occurrences[0][2])] = number
    return chosen


def _classify_item(token: str, previous: str | None) -> str:
    """Decide whether a bare 'i', 'v' or 'x' is a Roman numeral or a letter.

    These three tokens are genuinely ambiguous in isolation. Continuity settles
    it: a list that just produced "iv" continues with "v" as a numeral, whereas
    one that produced "u" continues with "v" as a letter. With no predecessor,
    "i" opens a Roman list and "v"/"x" are read as letters, which is how the
    document actually uses them.
    """
    if token not in {"i", "v", "x"}:
        return "roman" if token in _ROMAN_SEQUENCE else "letter"
    if previous is None:
        return "roman" if token == "i" else "letter"
    if previous in _ROMAN_SEQUENCE:
        index = _ROMAN_SEQUENCE.index(previous)
        if index + 1 < len(_ROMAN_SEQUENCE) and _ROMAN_SEQUENCE[index + 1] == token:
            return "roman"
    if len(previous) == 1 and ord(token) == ord(previous) + 1:
        return "letter"
    return "roman" if token == "i" else "letter"


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class _Builder:
    """Accumulates nodes while walking body lines in reading order."""

    def __init__(self, tree: ClauseTree) -> None:
        self.tree = tree
        self.current: ClauseNode | None = None
        self.numbered_stack: list[ClauseNode] = []
        self.section_id: str = ""
        self.container_prefix: str = ""
        self.last_item_token: dict[str, str] = {}
        self.x0_by_depth: dict[int, list[float]] = {}
        #: Sections in this circular are numbered strictly upwards. A bold
        #: "1." appearing after section 16 is a list restarting inside that
        #: section, not a new section.
        self.last_section_number: int = 0
        self.local_list_seq: int = 0
        #: Inside an annexure, the node a restarted "1., 2., 3." list hangs off.
        #: Annexures reproduce forms whose numbering restarts under every roman
        #: sub-head, so anchoring such lists on the annexure root alone would
        #: make ANX-32.1 refer to a dozen different paragraphs.
        self.annex_list_anchor: ClauseNode | None = None

    # -- node lifecycle ----------------------------------------------------

    def _unique(self, node_id: str, kind: str = "") -> str:
        if node_id not in self.tree.nodes:
            return node_id
        # A clause repeating its dotted number is a defect worth reporting. A
        # repeated (a)/(i) marker is not: a clause may carry two separate lists.
        if kind == "ITEM":
            self.tree.stats.repeated_item_ids.append(node_id)
        else:
            self.tree.stats.duplicate_ids.append(node_id)
        suffix = 2
        while f"{node_id}#{suffix}" in self.tree.nodes:
            suffix += 1
        return f"{node_id}#{suffix}"


    def open(
        self,
        node_id: str,
        kind: str,
        number: str,
        title: str,
        line: Line,
        depth: int,
        parent: ClauseNode | None,
    ) -> ClauseNode:
        self.close()
        node = ClauseNode(
            id=self._unique(node_id, kind),
            kind=kind,
            number=number,
            title=title,
            text=line.text.strip(),
            page=line.page,
            char_span=(line.start, line.end),
            sha256="",
            depth=depth,
            parent_id=parent.id if parent else None,
            section=self.section_id,
            x0=line.x0,
            footnote_markers=list(line.markers),
            lines=[line],
        )
        self.tree.nodes[node.id] = node
        if parent is not None:
            parent.children.append(node.id)
        else:
            self.tree.roots.append(node.id)
        self.tree.clause_of_line[id(line)] = node.id
        self.current = node
        self.x0_by_depth.setdefault(depth, []).append(line.x0)
        return node

    def append(self, line: Line) -> None:
        """Attach a continuation line to the node currently being read."""
        if self.current is None:
            return
        node = self.current
        node.lines.append(line)
        node.text = f"{node.text}\n{line.text.strip()}"
        node.char_span = (node.char_span[0], line.end)
        node.footnote_markers.extend(line.markers)
        self.tree.clause_of_line[id(line)] = node.id

    def close(self) -> None:
        if self.current is not None:
            self.current.sha256 = clause_sha256(self.current.text)
            self.current = None

    # -- stack management --------------------------------------------------

    def parent_for(self, components: list[str]) -> ClauseNode | None:
        """Pop the numbered stack until it holds this node's parent."""
        while self.numbered_stack and self.numbered_stack[-1].depth >= len(components):
            self.numbered_stack.pop()
        return self.numbered_stack[-1] if self.numbered_stack else None

    def push(self, node: ClauseNode) -> None:
        self.numbered_stack.append(node)


def _parse_flat(document, tree: ClauseTree, stats) -> None:
    """Read a circular that has no section headings, only numbered paragraphs.

    The fallback for ordinary circulars. Called only when the structured pass
    found no numbered node at all, so it never sees a document that parsed.

    Two rules, and they are deliberately strict, because this runs on documents
    the main parser could make no sense of and a permissive reading would
    invent structure that is not there:

    * A clause opens on a body line beginning ``N.`` at the document's own left
      margin. The margin is measured, not assumed, for the same reason the body
      font size is: SEBI sets these at 72pt and at 89pt depending on the year.
    * Numbers must not go backwards. A ``1.`` after a ``5.`` is a restarted list
      inside a paragraph, not a sixth clause, and is kept as continuation text.

    Sub-numbering is left alone. These documents are five paragraphs long and
    inventing a hierarchy over them would be a worse answer than a flat list.
    """
    lines = [line for page in document.pages for line in page.body if line.is_body]
    if not lines:
        return

    # The left margin of this document, taken as the smallest indent any
    # numbered line actually uses.
    starts = [
        line.x0 for line in lines if _NUMBERED_RE.match(line.text.strip())
    ]
    if not starts:
        return
    margin = min(starts)

    builder = _Builder(tree)
    builder.section_id = ""
    highest = 0
    opened = 0

    for line in lines:
        stripped = line.text.strip()
        match = _NUMBERED_RE.match(stripped)
        # Top-level only: "3." opens a clause, "3.1" does not appear in these.
        if (
            match
            and "." not in match.group(1)
            and match.group(2).strip()
            and abs(line.x0 - margin) <= _FLAT_MARGIN_TOLERANCE
        ):
            number = int(match.group(1))
            if number > highest:
                highest = number
                builder.open(
                    number=match.group(1),
                    node_id=match.group(1),
                    kind="CLAUSE",
                    title=match.group(2).strip(),
                    line=line,
                    depth=1,
                    parent=None,
                )
                opened += 1
                stats.depth_counts[1] += 1
                continue
        builder.append(line)

    builder.close()
    if opened:
        stats.flat_parse = True


def parse_clause_tree(pdf_path: str | Path) -> ClauseTree:
    """Parse a SEBI master circular PDF into a clause tree."""
    started = time.perf_counter()
    document = load_document(pdf_path)
    tree = ClauseTree(document=document)
    section_ceiling = _section_ceiling(document)
    stats = tree.stats
    stats.pdf_path = str(pdf_path)
    stats.page_count = document.page_count
    stats.separator_pages = document.separator_pages
    stats.document_chars = len(document.text)

    body_start, annexure_start, appendix_start, toc = _find_regions(document)
    stats.body_page_start = body_start
    stats.annexure_page_start = annexure_start
    stats.appendix_page_start = appendix_start
    stats.toc_pages = toc

    definitions = _choose_definitions(document, body_start, toc, annexure_start)

    builder = _Builder(tree)
    annexure_node: ClauseNode | None = None
    appendix_node: ClauseNode | None = None

    for page in document.pages:
        if page.number < body_start:
            continue
        if toc and toc[0] <= page.number <= toc[1]:
            continue

        in_annexures = annexure_start is not None and page.number >= annexure_start
        in_appendix = appendix_start is not None and page.number >= appendix_start

        for line in page.body:
            stripped = line.text.strip()
            if not stripped:
                continue

            # ---------------------------------------------------- appendix
            if in_appendix:
                if appendix_node is None and _APPENDIX_HEAD_RE.match(stripped) and line.is_bold:
                    builder.section_id = "APX"
                    builder.numbered_stack.clear()
                    appendix_node = builder.open(
                        "APX", "APPENDIX", "APX", stripped, line, 0, None
                    )
                    builder.container_prefix = "APX"
                elif appendix_node is not None:
                    match = _NUMBERED_RE.match(stripped)
                    if match and "." not in match.group(1) and line.x0 < 130:
                        stats.appendix_entries += 1
                    builder.append(line)
                continue

            # --------------------------------------------------- annexures
            if in_annexures:
                head = _ANNEXURE_HEAD_RE.match(stripped)
                if head and line.is_bold:
                    number = head.group(1).upper()
                    builder.section_id = f"ANX-{number}"
                    builder.numbered_stack.clear()
                    builder.last_item_token.clear()
                    builder.annex_list_anchor = None
                    annexure_node = builder.open(
                        f"ANX-{number}", "ANNEXURE", number, stripped, line, 0, None
                    )
                    builder.container_prefix = f"ANX-{number}"
                    stats.annexures += 1
                    continue
                if _ANNEXURES_BANNER_RE.match(stripped) and line.is_bold:
                    # The divider page that introduces the annexures. Not a node.
                    continue
                if annexure_node is None:
                    builder.append(line)
                    continue

            # ------------------------------------------------ numbered node
            match = _NUMBERED_RE.match(stripped) if line.is_body else None
            if match:
                number = match.group(1)
                title = match.group(2).strip()
                components = number.split(".")
                depth = len(components)

                looks_like_section = (
                    depth == 1
                    and not in_annexures
                    and line.is_bold
                    and line.x0 <= section_ceiling
                    and bool(title)
                )

                if looks_like_section:
                    if int(number) > builder.last_section_number:
                        builder.numbered_stack.clear()
                        builder.last_item_token.clear()
                        builder.section_id = number
                        builder.container_prefix = ""
                        builder.last_section_number = int(number)
                        node = builder.open(number, "SECTION", number, title, line, 1, None)
                        builder.push(node)
                        stats.sections += 1
                        stats.depth_counts[1] += 1
                        stats.section_numbers.append(int(number))
                        continue

                    # A restarted local list inside the current section. Given a
                    # namespaced id so it stays addressable without colliding
                    # with the real dotted numbering, and reported as rejected
                    # rather than dropped.
                    anchor = builder.numbered_stack[0] if builder.numbered_stack else None
                    builder.local_list_seq += 1
                    node = builder.open(
                        f"{builder.section_id}.L{number}",
                        "CLAUSE",
                        number,
                        title,
                        line,
                        2,
                        anchor,
                    )
                    # Namespace this list's descendants too, otherwise its "1.1"
                    # would claim the id of section 1's first clause.
                    builder.container_prefix = f"{builder.section_id}.L"
                    stats.local_lists += 1
                    stats.rejected_section_candidates.append(
                        f"p{line.page} '{number}. {title[:44]}'"
                    )
                    continue

                if depth >= 2 or (in_annexures and annexure_node is not None):
                    parent = builder.parent_for(components)
                    if parent is None and in_annexures and annexure_node is not None:
                        parent = annexure_node
                    if in_annexures and annexure_node is not None:
                        # Annexures reproduce forms and terms of reference whose
                        # numbering restarts under every sub-head, so a number
                        # is only unique within the sub-head it sits under.
                        # Scoping to that anchor is what keeps ANX-32.1 from
                        # naming a dozen different paragraphs.
                        anchor = builder.annex_list_anchor or annexure_node
                        parent = anchor if depth == 1 else (parent or anchor)
                        node_id = f"{anchor.id}.{number}"
                        kind = "CLAUSE" if depth == 1 else "SUBCLAUSE"
                    else:
                        prefix = f"{builder.container_prefix}." if builder.container_prefix else ""
                        node_id = f"{prefix}{number}"
                        kind = "CLAUSE" if depth == 2 else "SUBCLAUSE"

                    if not in_annexures and definitions.get(id(line)) != number:
                        # Another line, further left, defines this number. This
                        # occurrence is a cross-reference inside a sentence.
                        stats.cross_references.append(f"p{line.page} '{stripped[:60]}'")
                        builder.append(line)
                        continue

                    node = builder.open(node_id, kind, number, title, line, depth, parent)
                    builder.push(node)
                    if not in_annexures:
                        stats.depth_counts[depth] += 1
                    continue

            # -------------------------------------------------- item node
            if line.is_body and builder.current is not None:
                roman = _ROMAN_RE.match(stripped)
                letter = _LETTER_RE.match(stripped)
                token: str | None = None
                body: str = ""
                if roman:
                    token, body = roman.group(1), roman.group(2)
                elif letter:
                    token, body = letter.group(1), letter.group(2)

                if token is not None:
                    anchor = builder.numbered_stack[-1] if builder.numbered_stack else None
                    anchor_id = anchor.id if anchor else builder.section_id
                    previous = builder.last_item_token.get(anchor_id)
                    style = _classify_item(token, previous)
                    builder.last_item_token[anchor_id] = token

                    node = builder.open(
                        f"{anchor_id}({token})",
                        "ITEM",
                        token,
                        body.strip(),
                        line,
                        (anchor.depth + 1) if anchor else 1,
                        anchor,
                    )
                    if in_annexures:
                        builder.annex_list_anchor = node
                        if style == "roman":
                            stats.annexure_roman_items += 1
                        else:
                            stats.annexure_lettered_items += 1
                    elif style == "roman":
                        stats.roman_items += 1
                    else:
                        stats.lettered_items += 1
                    continue

            # ------------------------------------------------ continuation
            builder.append(line)

    builder.close()

    # --------------------------------------------------- flat circulars
    #
    # Everything above assumes a master circular: bold numbered section
    # headings, with dotted clause numbers nested underneath them. That is the
    # shape of an annual consolidation, and it is not the shape of most of what
    # a regulator actually issues.
    #
    # An ordinary SEBI circular is one or two pages. It extends a deadline or
    # amends one requirement, and its body is five numbered paragraphs with no
    # headings at all. Under the rules above, a depth-1 number that is not bold
    # cannot be a section, and the branch that handles clauses only accepts
    # depth 2 or deeper, so every one of those paragraphs fell through to
    # `builder.append` as continuation text and the document parsed to nothing.
    #
    # Those short circulars are the ones that arrive weekly and cause the very
    # problem this product exists to solve, so dropping them was the wrong half
    # to drop.
    #
    # This runs only when the structured pass produced no numbered node at all.
    # A document that parsed normally never reaches it, which is what keeps the
    # stock broker tree, and the signatures anchored to it, exactly as they
    # were.
    if not any(
        node.kind in ("SECTION", "CLAUSE", "SUBCLAUSE") for node in tree.nodes.values()
    ):
        _parse_flat(document, tree, stats)

    # ------------------------------------------------------------- checks
    stats.total_nodes = len(tree.nodes)

    # Annexure cross-references, counted over every page including the table of
    # contents, and over each page's text joined into one string so a reference
    # that wraps ("Annexure-\n10") is still seen. This is a mention count, not a
    # node count: one annexure is referred to from many clauses.
    for page in document.pages:
        for joined in _joined_blocks(page.body):
            stats.annexure_mentions += len(_ANNEXURE_MENTION_RE.findall(joined))

    numbers = stats.section_numbers
    if numbers:
        highest = max(numbers)
        present = set(numbers)
        stats.section_gaps = [n for n in range(1, highest + 1) if n not in present]
        for previous, current in zip(numbers, numbers[1:]):
            if current <= previous:
                stats.out_of_sequence.append((previous, current))

    # Indentation corroboration. Depth is taken from the numbering token, which
    # is authoritative; this only records where the document's own indentation
    # disagrees, so a misdetected level surfaces instead of hiding.
    #
    # Baselines are drawn from the numbered body only. Annexures reproduce forms
    # and tables at their own indents, and mixing those in would drag every
    # median far enough to make the check meaningless.
    numbered = [
        node
        for node in tree.nodes.values()
        if node.kind in ("SECTION", "CLAUSE", "SUBCLAUSE") and not node.section.startswith("ANX-")
    ]
    by_depth: dict[int, list[float]] = {}
    for node in numbered:
        by_depth.setdefault(node.depth, []).append(node.x0)
    medians = {depth: median(values) for depth, values in by_depth.items() if len(values) >= 5}

    for node in numbered:
        expected = medians.get(node.depth)
        if expected is not None and abs(node.x0 - expected) > _INDENT_TOLERANCE:
            node.indent_mismatch = True
            stats.indent_mismatches.append(node.id)

    stats.non_ascii = scan_non_ascii(
        document.text,
        {offset: page for page, offset in document.page_starts.items()},
    )
    stats.parse_seconds = time.perf_counter() - started
    return tree

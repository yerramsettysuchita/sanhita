"""How well did we read this document?

The clause parser was built against one document: the stock broker master
circular. It is deterministic and it is careful, but it is not universal. Point
it at a two page SEBI press release and it will find almost nothing. Point it at
a differently numbered master circular and it will find some of the structure
and miss some of it.

A product that hides that is lying. So before anyone is allowed to compile a
document, this module reads the parse statistics back and states plainly what
was recognised, what was not, and whether the result is worth compiling at all.

Nothing here is a score out of ten. Every concern names the specific thing that
happened and what it means for the rules that come out of the other end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sanhita.parse.clause_tree import ClauseTree

__all__ = ["Verdict", "Concern", "ParseQuality", "assess"]


class Verdict(str, Enum):
    """Whether this document is worth compiling."""

    #: Structure was found and looks like a numbered regulation.
    READABLE = "READABLE"
    #: Some structure was found, but enough is wrong that output needs care.
    PARTIAL = "PARTIAL"
    #: Not enough structure to compile anything meaningful.
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class Concern:
    """One specific thing that happened during the parse."""

    level: str  # "blocker" | "warning" | "note"
    title: str
    detail: str
    count: int | None = None


@dataclass
class ParseQuality:
    verdict: Verdict
    headline: str
    clauses: int = 0
    sections: int = 0
    pages: int = 0
    obligation_bearing: int = 0
    concerns: list[Concern] = field(default_factory=list)

    @property
    def can_compile(self) -> bool:
        return self.verdict is not Verdict.UNREADABLE

    @property
    def blockers(self) -> list[Concern]:
        return [c for c in self.concerns if c.level == "blocker"]

    @property
    def warnings(self) -> list[Concern]:
        return [c for c in self.concerns if c.level == "warning"]

    @property
    def notes(self) -> list[Concern]:
        return [c for c in self.concerns if c.level == "note"]


#: Below this many clauses there is nothing worth putting a human in front of.
_MIN_CLAUSES = 5

#: A numbered regulation that yields fewer than this many clauses per page of
#: body text is almost certainly being read wrongly rather than being short.
_THIN_CLAUSES_PER_PAGE = 0.35


def assess(tree: ClauseTree) -> ParseQuality:
    """Read the parse statistics back as statements a person can act on."""
    stats = tree.stats
    concerns: list[Concern] = []

    body = [
        n
        for n in tree.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
    ]
    clauses = len(body)
    sections = stats.sections
    pages = stats.page_count

    # ---------------------------------------------------------------- blockers

    if clauses == 0:
        concerns.append(
            Concern(
                "blocker",
                "No numbered clauses were found",
                "Sanhita reads regulations that number their clauses, for example "
                "21.1 or 21.1.2. Nothing in this document matched that shape. It "
                "may be a scanned image with no text layer, a press release, or a "
                "circular that uses a numbering style this parser does not know.",
            )
        )
    elif clauses < _MIN_CLAUSES:
        concerns.append(
            Concern(
                "blocker",
                "Almost no structure was found",
                f"Only {clauses} numbered clause(s) came out of {pages} page(s). "
                "That is too little to compile. The document is probably not a "
                "numbered regulation, or its numbering sits inside images.",
                count=clauses,
            )
        )

    if stats.document_chars == 0:
        concerns.append(
            Concern(
                "blocker",
                "The PDF carries no extractable text",
                "Every page came back empty. This is what a scanned document looks "
                "like. Sanhita does not run OCR, because a rule compiled from a "
                "guess about a blurry character cannot be certified.",
            )
        )

    # ---------------------------------------------------------------- warnings

    if clauses and pages:
        density = clauses / max(pages, 1)
        if density < _THIN_CLAUSES_PER_PAGE and clauses >= _MIN_CLAUSES:
            concerns.append(
                Concern(
                    "warning",
                    "Fewer clauses than a document this long usually has",
                    f"{clauses} clauses across {pages} pages. Either the document "
                    "is mostly prose and tables, or parts of its numbering were "
                    "not recognised. Check a few clauses against the PDF before "
                    "you certify anything.",
                    count=clauses,
                )
            )

    if sections == 0 and clauses:
        concerns.append(
            Concern(
                "warning",
                "No section headings were recognised",
                "Clauses were found but they could not be grouped under section "
                "headings, so every clause is filed at the top level. Coverage by "
                "section will not be meaningful for this document.",
            )
        )

    # A clause that runs to thousands of characters is not a clause. In the
    # stock broker circular the median is 206 characters and the 99.5th
    # percentile is 1,463; the three that exceed 3,000 are summary tables the
    # parser flattened into a single node. Rules drawn from them restate
    # obligations that appear properly elsewhere.
    oversized = sorted(
        (n for n in body if len(n.text) > 3000),
        key=lambda n: -len(n.text),
    )
    if oversized:
        listed = ", ".join(f"{n.id} ({len(n.text):,} characters)" for n in oversized[:4])
        concerns.append(
            Concern(
                "warning",
                "Some clauses are far too long to be clauses",
                "These are almost certainly tables that were flattened into one "
                "clause when the text was read off the page: " + listed + ". "
                "Anything compiled from them will repeat obligations that also "
                "appear in their own clauses, so they are excluded from conflict "
                "analysis and should be read with that in mind.",
                count=len(oversized),
            )
        )

    if stats.duplicate_ids:
        concerns.append(
            Concern(
                "warning",
                "The same clause number appears more than once",
                "A clause id must identify exactly one clause, because a certified "
                "rule points back at it. Repeats were kept and suffixed rather "
                "than dropped, so nothing is lost, but the numbering is not clean: "
                + ", ".join(stats.duplicate_ids[:8])
                + ("..." if len(stats.duplicate_ids) > 8 else ""),
                count=len(stats.duplicate_ids),
            )
        )

    if stats.out_of_sequence:
        pairs = ", ".join(f"{a} then {b}" for a, b in stats.out_of_sequence[:6])
        concerns.append(
            Concern(
                "warning",
                "Section numbers do not run in order",
                "Sanhita reports the order it observed rather than sorting it into "
                "the order you might expect, because reordering would hide a "
                "parsing mistake: " + pairs,
                count=len(stats.out_of_sequence),
            )
        )

    # ------------------------------------------------------------------- notes

    if stats.section_gaps:
        gaps = ", ".join(str(g) for g in stats.section_gaps[:10])
        concerns.append(
            Concern(
                "note",
                "Some section numbers are missing",
                f"No heading was found for section(s) {gaps}. That is common and "
                "usually genuine, since regulators withdraw sections without "
                "renumbering the rest.",
                count=len(stats.section_gaps),
            )
        )

    if stats.indent_mismatches:
        concerns.append(
            Concern(
                "note",
                "Some clauses sit at an unexpected indent",
                "Depth is taken from the clause number, which is authoritative. "
                "Indentation is only a corroborating signal, and these clauses "
                "disagreed with it. They were kept and flagged rather than moved.",
                count=len(stats.indent_mismatches),
            )
        )

    if stats.non_ascii is not None and stats.non_ascii.total_non_ascii:
        concerns.append(
            Concern(
                "note",
                "The document contains characters outside plain ASCII",
                "Curly quotes, rupee signs and dashes are preserved exactly as "
                "published, because a clause hash covers the regulation's own "
                "characters.",
                count=stats.non_ascii.total_non_ascii,
            )
        )

    # ---------------------------------------------------------------- verdict

    if any(c.level == "blocker" for c in concerns):
        verdict = Verdict.UNREADABLE
        headline = "Sanhita could not read this document well enough to compile it."
    elif any(c.level == "warning" for c in concerns):
        verdict = Verdict.PARTIAL
        headline = (
            f"{clauses} clauses were read, but some of the structure did not come "
            "through cleanly."
        )
    else:
        verdict = Verdict.READABLE
        headline = (
            f"{clauses} clauses across {sections} sections were read cleanly."
        )

    return ParseQuality(
        verdict=verdict,
        headline=headline,
        clauses=clauses,
        sections=sections,
        pages=pages,
        concerns=concerns,
    )

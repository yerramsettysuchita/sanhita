"""The parser must not be fitted to one PDF's typography.

The first version of this product measured the stock broker circular, found its
body copy set at 12pt, and wrote 11.5 into the source as the definition of body
text. SEBI's June 2025 research analyst circular is set at 11.3pt. Every line in
it fell under the threshold, every line was classified as furniture, and a
document containing 139 well-formed numbered clauses parsed to zero clauses
while reporting "no numbered clauses were found".

That is the failure mode these tests exist to prevent. A compliance officer does
not upload the one circular the parser was tuned against; they upload whatever
the regulator sent them.

The corpus PDFs are gitignored, so every test that needs one is skipped rather
than failed when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import ROOT, requires_corpus

from sanhita.parse.layout import (
    BODY_MIN_SIZE,
    BODY_SIZE_TOLERANCE,
    body_min_size,
    load_document,
)


def _corpus(name: str) -> Path | None:
    found = sorted((ROOT / "corpus").glob(f"{name}*.pdf"))
    return found[0] if found else None


# ═══════════════════════════════════════════════ the measurement itself ══


def test_the_body_floor_is_measured_not_assumed():
    """Whatever the document is mostly set in is the body size."""
    twelve_point = [12.0] * 900 + [10.0] * 60 + [8.0] * 12
    assert body_min_size(twelve_point) == pytest.approx(12.0 - BODY_SIZE_TOLERANCE)

    eleven_three = [11.3] * 900 + [10.4] * 200
    assert body_min_size(eleven_three) == pytest.approx(11.3 - BODY_SIZE_TOLERANCE)


def test_near_identical_float_sizes_do_not_split_the_mode():
    """PyMuPDF reports 11.999999 and 12.0 as different floats.

    Counted raw, one typeface would spread across several buckets and the mode
    could land on a footnote size that happens to be reported consistently.
    """
    noisy = [11.999998, 12.000001, 11.999999, 12.0] * 100 + [10.0] * 150
    assert body_min_size(noisy) == pytest.approx(12.0 - BODY_SIZE_TOLERANCE)


def test_an_empty_document_falls_back_rather_than_dividing_by_nothing():
    assert body_min_size([]) == BODY_MIN_SIZE


def test_a_tie_resolves_to_the_larger_size():
    """If body and footnotes appear equally often, the footnotes are not the body."""
    assert body_min_size([12.0] * 50 + [10.0] * 50) == pytest.approx(
        12.0 - BODY_SIZE_TOLERANCE
    )


def test_the_tolerance_cannot_widen_without_someone_noticing():
    """A wider tolerance changes documents that already parse.

    The stock broker circular carries 821 lines at 11pt that the old fixed
    floor of 11.5 excluded. Its measured floor is 11.3. Raising the tolerance
    past 0.7 pulls those lines into the body, changes a tree that 183
    signatures are anchored to, and invalidates every one of them.
    """
    assert BODY_SIZE_TOLERANCE <= 0.7, (
        "Widening this changes the stock broker parse. See the constant's comment."
    )
    assert 12.0 - BODY_SIZE_TOLERANCE > 11.0


# ═══════════════════════════════════════════ against the real documents ══


@requires_corpus
def test_the_worked_example_parse_is_unchanged(parsed):
    """The one thing this fix was not allowed to do.

    Every certified rule points at a clause in this tree. If the fingerprint
    moves, the rules point at text that is no longer there.
    """
    assert parsed.fingerprint() == (
        "3a0a41f5deee3fd1c909cfa5979eafb497804bc37b0c1b96ad8498d0a0c9e45d"
    )


@pytest.mark.parametrize(
    "name",
    [
        "stock-brokers-master-circular",
        "research-analysts-2025-06",
        "research-analysts-2026-02",
        "investment-advisers-2025",
        "investment-advisers-2026",
        "depositories",
        "mutual-funds",
    ],
)
def test_every_circular_in_the_corpus_yields_body_text(name: str):
    """No document may parse to zero body lines.

    Zero body lines is the signature of this bug: the text is there, the
    numbering is there, and a threshold decided none of it counted.
    """
    pdf = _corpus(name)
    if pdf is None:
        pytest.skip(f"{name} not in corpus/ (gitignored)")

    document = load_document(pdf)
    lines = [line for page in document.pages for line in page.body]
    body = [line for line in lines if line.is_body]

    assert lines, f"{pdf.name} produced no lines at all"
    assert body, (
        f"{pdf.name} produced {len(lines)} lines and classified none as body. "
        "That is the overfitted-threshold bug returning."
    )
    # A substantial share of a circular is body copy. A handful of lines would
    # mean the floor is still in the wrong place, just less obviously.
    #
    # Not a majority, though. The mutual fund circular is 43% body, because it
    # carries page after page of tabular disclosure formats set below body
    # size, and those genuinely are not body copy. It still yields 2,620
    # clauses. The floor is doing its job there; the document is just mostly
    # tables.
    ratio = len(body) / len(lines)
    assert ratio > 0.3, (
        f"{pdf.name}: only {len(body)} of {len(lines)} lines count as body "
        f"({ratio:.0%})"
    )


@pytest.mark.parametrize(
    "name",
    [
        "stock-brokers-master-circular",
        "research-analysts-2025-06",
        "research-analysts-2026-02",
        "investment-advisers-2025",
        "investment-advisers-2026",
        "depositories",
        "mutual-funds",
    ],
)
def test_every_circular_in_the_corpus_yields_clauses(name: str):
    """A circular that parses to no clauses cannot be compiled at all."""
    from sanhita.parse.clause_tree import parse_clause_tree

    pdf = _corpus(name)
    if pdf is None:
        pytest.skip(f"{name} not in corpus/ (gitignored)")

    tree = parse_clause_tree(pdf)
    assert len(tree.nodes) > 50, (
        f"{pdf.name} parsed to {len(tree.nodes)} nodes"
    )


# ═══════════════════════════════════════════ circulars with no headings ══
#
# A master circular arrives once a year. Ordinary circulars arrive weekly, are
# one or two pages, and have no section headings at all: their body is five
# numbered paragraphs. Those are the documents that create the "interpreting a
# new or amended requirement" problem in the first place, so a parser that
# reads only the annual consolidation reads the wrong half of the corpus.

SHORT_CIRCULARS = [
    "short-transmission-of-securities",
    "short-intraday-borrowing-mf",
    "short-sif-certification",
    "short-timeline-extension",
]


@pytest.mark.parametrize("name", SHORT_CIRCULARS)
def test_a_circular_with_no_section_headings_still_yields_clauses(name: str):
    from sanhita.parse.clause_tree import parse_clause_tree

    pdf = _corpus(name)
    if pdf is None:
        pytest.skip(f"{name} not in corpus/ (gitignored)")

    tree = parse_clause_tree(pdf)
    assert tree.nodes, (
        f"{pdf.name} parsed to nothing. A short circular is numbered "
        "paragraphs with no headings, and dropping it drops the case a "
        "compliance officer meets most often."
    )


def test_the_flat_reading_is_recorded_not_hidden():
    """A reviewer is entitled to know which reading produced their clauses."""
    from sanhita.parse.clause_tree import parse_clause_tree

    pdf = _corpus("short-timeline-extension")
    if pdf is None:
        pytest.skip("short timeline extension circular not in corpus/")

    tree = parse_clause_tree(pdf)
    assert tree.stats.flat_parse is True
    # Five numbered paragraphs in the document, five clauses out of it.
    assert len(tree.nodes) == 5


@requires_corpus
def test_a_master_circular_never_takes_the_flat_path(parsed):
    """The fallback must be unreachable for anything that parsed properly.

    This is the property that keeps the stock broker tree, and the 183
    signatures anchored to it, exactly as they were.
    """
    assert parsed.stats.flat_parse is False
    assert parsed.stats.sections > 0


def test_a_restarted_list_does_not_become_a_sixth_clause():
    """Numbers must not go backwards in a flat document.

    A "1." appearing after a "5." is a list restarting inside a paragraph, not
    a new clause, and reading it as one would invent structure that is not in
    the document.
    """
    from sanhita.parse.clause_tree import parse_clause_tree

    pdf = _corpus("short-timeline-extension")
    if pdf is None:
        pytest.skip("short timeline extension circular not in corpus/")

    tree = parse_clause_tree(pdf)
    numbers = [int(node.number) for node in tree.nodes.values() if node.number.isdigit()]
    assert numbers == sorted(numbers), "clause numbers went backwards"
    assert len(numbers) == len(set(numbers)), "a clause number was reused"


# ═══════════════════════════════════════════════════════ margins ══


def test_the_section_ceiling_can_only_relax_never_tighten():
    """A measured ceiling must never exclude a line the constant admitted.

    This is the property that makes the change safe. If measurement could
    tighten the rule, a heading that qualified before might stop qualifying,
    the stock broker tree would move, and every signature anchored to it would
    point at text that is no longer there.
    """
    from types import SimpleNamespace

    from sanhita.parse.clause_tree import _SECTION_MAX_X0, _section_ceiling

    def document(indents: list[float]):
        lines = [SimpleNamespace(x0=x, is_body=True) for x in indents]
        return SimpleNamespace(pages=[SimpleNamespace(body=lines)])

    # A narrow-margin document keeps the original ceiling.
    assert _section_ceiling(document([21.0] * 100)) == _SECTION_MAX_X0
    # A wide-margin document gets a higher one, never a lower one.
    assert _section_ceiling(document([130.0] * 100)) > _SECTION_MAX_X0
    # And an empty document falls back rather than computing over nothing.
    assert _section_ceiling(document([])) == _SECTION_MAX_X0


def test_a_single_stray_indent_cannot_drag_the_margin():
    """One table cell at the page edge must not redefine the document."""
    from types import SimpleNamespace

    from sanhita.parse.clause_tree import _section_ceiling

    from_body = [130.0] * 200
    with_stray = [4.0] + from_body

    assert _section_ceiling(
        SimpleNamespace(
            pages=[
                SimpleNamespace(
                    body=[SimpleNamespace(x0=x, is_body=True) for x in with_stray]
                )
            ]
        )
    ) == _section_ceiling(
        SimpleNamespace(
            pages=[
                SimpleNamespace(
                    body=[SimpleNamespace(x0=x, is_body=True) for x in from_body]
                )
            ]
        )
    )


def test_the_eleven_point_three_circular_is_the_regression_case():
    """Named explicitly, because it is the document that found the bug."""
    pdf = _corpus("research-analysts-2025-06")
    if pdf is None:
        pytest.skip("research analyst 2025 circular not in corpus/")

    document = load_document(pdf)
    sizes = [line.max_size for page in document.pages for line in page.body]
    floor = body_min_size(sizes)

    # Typeset smaller than the circular the parser was built on.
    assert floor < BODY_MIN_SIZE, (
        "This document is set below the old fixed floor. If that stops being "
        "true, this test is no longer testing anything."
    )
    assert any(line.is_body for page in document.pages for line in page.body)

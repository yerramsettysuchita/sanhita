"""Footnote provenance extraction.

The master circular records no amendment history of its own - there is no
"Inserted vide" anywhere in it - so what is tested here is the thing it *does*
carry: the binding from a clause to the circulars it was consolidated from.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.conftest import requires_corpus

from sanhita.parse.footnotes import extract_refs, parse_circular_date


# --------------------------------------------------- unit, no PDF needed


@pytest.mark.parametrize(
    "text, expected",
    [
        ("dated August 22, 2011", _dt.date(2011, 8, 22)),
        ("dated Dec 3, 2009", _dt.date(2009, 12, 3)),
        ("dated September 04, 2023", _dt.date(2023, 9, 4)),
        ("dated March 31, 2015", _dt.date(2015, 3, 31)),
        ("dated June 17, 2025", _dt.date(2025, 6, 17)),
    ],
)
def test_circular_dates_parse(text, expected):
    assert parse_circular_date(text) == expected


def test_impossible_dates_are_reported_as_unparseable_not_coerced():
    assert parse_circular_date("dated February 31, 2020") is None
    assert parse_circular_date("dated Smarch 3, 2020") is None
    assert parse_circular_date("no date here") is None


@pytest.mark.parametrize(
    "line, ref, when",
    [
        (
            "Reference: Circular SMD/POLICY/CIRCULAR/30/97 dated November 25, 1997.",
            "SMD/POLICY/CIRCULAR/30/97",
            _dt.date(1997, 11, 25),
        ),
        (
            "Reference: Circular CIR/MIRSD/16/2011 dated August 22, 2011",
            "CIR/MIRSD/16/2011",
            _dt.date(2011, 8, 22),
        ),
        (
            "Reference: Circular SEBI/HO/MIRSD/DoP/P/CIR/2022/44 dated April 04, 2022",
            "SEBI/HO/MIRSD/DoP/P/CIR/2022/44",
            _dt.date(2022, 4, 4),
        ),
        (
            "Reference: Circular SMD/POLICY(BRK.REG.)/CIR-18/98 dated July 09, 1998.",
            "SMD/POLICY(BRK.REG.)/CIR-18/98",
            _dt.date(1998, 7, 9),
        ),
        (
            "Reference: Circular SMD/DBA-II/CIR-22/2002 dated September 12, 2002",
            "SMD/DBA-II/CIR-22/2002",
            _dt.date(2002, 9, 12),
        ),
        (
            "Vide Letter MRD/DMS/OW/9500/2015 dated March 31, 2015, SEBI informed",
            "MRD/DMS/OW/9500/2015",
            _dt.date(2015, 3, 31),
        ),
    ],
)
def test_real_footnote_lines_yield_reference_and_date(line, ref, when):
    """Six verbatim footnotes from the corpus."""
    refs = extract_refs(line)
    assert refs, line
    assert refs[0] == (ref, when)


def test_internal_spacing_in_a_reference_is_collapsed():
    line = "Reference: Circular SEBI/HO/ MIRSD/ MIRSD_DPIEA/P/CIR/2022/83 dated June 20, 2022"
    assert extract_refs(line)[0][0] == "SEBI/HO/MIRSD/MIRSD_DPIEA/P/CIR/2022/83"


def test_a_reference_containing_a_space_survives():
    line = "Reference: Circular SEBI/MIRSD/MASTER CIR-04/2010 dated March 17, 2010"
    assert extract_refs(line)[0][0] == "SEBI/MIRSD/MASTER CIR-04/2010"


def test_several_references_on_one_footnote_are_all_recovered():
    line = (
        "Reference: Circular CIR/MIRSD/12/2011 dated July 11, 2011 and "
        "Circular CIR/MIRSD/4/2014 dated October 13, 2014"
    )
    refs = extract_refs(line)
    assert [r for r, _ in refs] == ["CIR/MIRSD/12/2011", "CIR/MIRSD/4/2014"]


def test_prose_dates_with_no_circular_number_are_not_invented():
    """Half a reference is worse than none, so nothing is emitted."""
    assert extract_refs("supersedes the Master Circular dated August 09, 2024") == []


# ------------------------------------------------------ against the corpus


@requires_corpus
def test_markers_and_definitions_agree(footnote_report):
    report = footnote_report
    assert report.definition_count > 100
    assert report.marker_count > 100
    # Two independent signals - separator-anchored definitions and the PDF's own
    # superscript flags - must land on the same count.
    assert report.definition_count == report.marker_count
    assert not report.ambiguous_markers


@requires_corpus
def test_almost_every_footnote_binds_to_a_clause(footnote_report):
    report = footnote_report
    assert report.resolved_count >= report.definition_count - 2
    # Whatever fails to bind is reported rather than guessed at.
    assert len(report.unresolved_markers) <= 2
    assert len(report.orphan_definitions) <= 2


@requires_corpus
def test_recovered_lineage_spans_the_documents_real_history(footnote_report):
    dated = sorted(f.dated for f in footnote_report.footnotes if f.dated)
    assert dated
    assert dated[0].year <= 1998
    assert dated[-1].year >= 2024
    assert all(_dt.date(1985, 1, 1) <= d <= _dt.date(2026, 12, 31) for d in dated)


@requires_corpus
def test_most_footnotes_carry_a_circular_number(footnote_report):
    with_ref = [f for f in footnote_report.footnotes if f.circular_ref]
    assert len(with_ref) >= 0.8 * footnote_report.definition_count
    for ref in with_ref:
        assert "/" in ref.circular_ref
        assert not ref.circular_ref.lower().startswith(("circular", "reference"))


@requires_corpus
def test_bound_footnotes_point_at_clauses_that_exist(footnote_report, parsed):
    for ref in footnote_report.footnotes:
        if ref.clause_id is not None:
            assert ref.clause_id in parsed.nodes, ref.clause_id


@requires_corpus
def test_body_references_are_kept_separate_and_unbound(footnote_report):
    """A sentence mentioning a circular is evidence, not a binding."""
    assert footnote_report.body_refs
    assert all(r.clause_id is None for r in footnote_report.body_refs)
    assert footnote_report.dated_mentions >= len(footnote_report.body_refs)


@requires_corpus
def test_no_amendment_vocabulary_exists_in_this_circular(parsed):
    """Guards the decision not to build an amendment model against this corpus."""
    text = parsed.document.text.lower()
    for phrase in ("inserted vide", "substituted vide", "deleted vide", "amended vide"):
        assert phrase not in text, f"{phrase!r} found - revisit the amendment design"

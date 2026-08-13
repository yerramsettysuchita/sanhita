"""A real SEBI amendment, replayed through the diff engine.

The deck claimed "20+ historical amendments replayed". The true number was
zero: the engine was validated against edits applied to a tree by hand, never
against two editions SEBI actually published.

These tests close that. SEBI issued a Master Circular for Investment Advisers
in June 2025 and reissued it in February 2026. Both PDFs are in ``corpus/``,
both came from sebi.gov.in, and the diff below is between them. Nothing here is
a synthetic edit.

The finding worth carrying into the pitch is the renumbering. The February
edition renumbers most of the circular, which is exactly the change a
spreadsheet of clause references survives worst: every row still points at a
number that now means something else, and nothing warns you.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import ROOT

OLD = "investment-advisers-2025-06-27.pdf"
NEW = "investment-advisers-2026-02.pdf"


def _edition(name: str) -> Path | None:
    candidate = ROOT / "corpus" / name
    return candidate if candidate.is_file() else None


needs_both_editions = pytest.mark.skipif(
    _edition(OLD) is None or _edition(NEW) is None,
    reason="both investment adviser editions are needed (corpus/ is gitignored)",
)


@pytest.fixture(scope="module")
def replay():
    """Parse both editions once and diff them."""
    from sanhita.diff import diff_trees
    from sanhita.parse.clause_tree import parse_clause_tree

    old_pdf, new_pdf = _edition(OLD), _edition(NEW)
    if old_pdf is None or new_pdf is None:
        pytest.skip("editions missing")

    before = parse_clause_tree(old_pdf)
    after = parse_clause_tree(new_pdf)
    changes = diff_trees(
        before, after, before_label="IA Jun 2025", after_label="IA Feb 2026"
    )
    return before, after, changes


@needs_both_editions
def test_the_two_editions_are_genuinely_different_documents(replay):
    """Guards against diffing a file against itself and reporting success."""
    before, after, _ = replay

    assert before.fingerprint() != after.fingerprint()
    assert len(before.nodes) != len(after.nodes)


@needs_both_editions
def test_the_diff_finds_real_structural_change(replay):
    """An amendment that changed nothing would mean the diff is not working."""
    _, _, changes = replay

    assert len(changes.added) > 0, "the February edition adds clauses"
    assert len(changes.removed) > 0, "it also drops some"
    assert len(changes.modified) > 0, "and rewrites others"


@needs_both_editions
def test_the_amendment_renumbers_most_of_the_circular(replay):
    """The finding worth showing a supervisor.

    A renumbering is the change a clause-reference spreadsheet survives worst.
    Every row still points at a number, the number still exists, and it now
    means a different thing. Nothing in a manual process detects that.
    """
    _, after, changes = replay

    assert len(changes.renumbered) > 100, (
        "the February edition renumbers heavily; if this stops being true the "
        "claim in the deck has to change with it"
    )
    # Renumbering dominates: more clauses moved than were added or removed.
    assert len(changes.renumbered) > len(changes.added) + len(changes.removed)


@needs_both_editions
def test_the_new_edition_compiles(replay):
    """A diff is only useful if the later edition becomes rules."""
    from sanhita.compile.extract import RuleExtractor

    _, after, _ = replay
    extractor = RuleExtractor(circular_id="IA-2026-02")
    rules = []
    for node in after.nodes.values():
        rules.extend(extractor.extract(node).obligations)

    assert len(rules) > 50, f"only {len(rules)} obligations from the new edition"


@needs_both_editions
def test_impact_is_reported_against_certified_rules_only(replay):
    """Zero affected here is the correct answer, and the report says why.

    No investment adviser rule has been certified: the signed corpus is the
    stock broker circular. So this amendment disturbs no signature, and the
    impact report should say zero rather than inventing an effect.
    """
    from sanhita.analyse import build_graph
    from sanhita.compile.extract import RuleExtractor
    from sanhita.diff import assess_impact

    _, after, changes = replay
    extractor = RuleExtractor(circular_id="IA-2026-02")
    rules = []
    for node in after.nodes.values():
        rules.extend(extractor.extract(node).obligations)

    impact = assess_impact(changes, rules, references=build_graph(after))

    assert impact.certified_before == 0
    assert impact.signatures_lost == 0
    # `new_clauses` is the list of them, not a count.
    assert len(impact.new_clauses) > 0, "new clauses still have to be reported"

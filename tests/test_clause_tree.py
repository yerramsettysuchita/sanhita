"""Clause tree parsing against the real circular.

Every test here is skipped when the corpus PDF is absent, because the parser's
contract is with a real document and asserting it against a synthetic fixture
would prove nothing about the thing we ship.
"""

from __future__ import annotations

import hashlib
import re

from tests.conftest import requires_corpus

from sanhita.parse.clause_tree import _classify_item, parse_clause_tree


@requires_corpus
def test_ids_spans_and_hashes_are_identical_across_two_independent_runs(corpus_pdf):
    """The determinism guarantee. Without it, no signature means anything."""
    first = parse_clause_tree(corpus_pdf)
    second = parse_clause_tree(corpus_pdf)

    assert first.fingerprint() == second.fingerprint()
    assert set(first.nodes) == set(second.nodes)

    for node_id, a in first.nodes.items():
        b = second.nodes[node_id]
        assert a.sha256 == b.sha256, node_id
        assert a.char_span == b.char_span, node_id
        assert a.page == b.page, node_id
        assert a.text == b.text, node_id


@requires_corpus
def test_every_node_hash_matches_its_own_verbatim_text(parsed):
    """The provenance chain is only as good as this equality."""
    for node in parsed.nodes.values():
        expected = hashlib.sha256(node.text.encode("utf-8")).hexdigest()
        assert node.sha256 == expected, node.id


@requires_corpus
def test_section_21_parses_with_its_real_numbering(parsed):
    """A worked example with sub-items four levels deep."""
    section = parsed.get("21")
    assert section is not None
    assert section.kind == "SECTION"
    assert "Trading Account Opening" in section.title

    for clause_id in ("21.1", "21.1.1", "21.1.2", "21.1.2.1", "21.1.2.2", "21.2", "21.3"):
        node = parsed.get(clause_id)
        assert node is not None, f"{clause_id} missing"
        assert node.section == "21"

    assert parsed.get("21.1.2.1").depth == 4
    assert parsed.get("21.1.2").parent_id == "21.1"
    assert parsed.get("21.1").parent_id == "21"


@requires_corpus
def test_dotless_subclause_numbering_is_handled(parsed):
    """SEBI writes both "21.1." and "15.1.1" - the missing dot must not merge levels."""
    node = parsed.get("15.1.1")
    assert node is not None
    assert node.depth == 3
    assert node.number == "15.1.1"
    # The parent must be the real clause, not a fragment titled "1 Uniform...".
    assert parsed.get("15.1").title.startswith("SEBI constituted")


@requires_corpus
def test_annexures_are_peers_of_sections_not_children(parsed):
    annexures = [n for n in parsed.nodes.values() if n.kind == "ANNEXURE"]
    assert annexures, "no annexures parsed"
    for node in annexures:
        assert node.parent_id is None
        assert node.id.startswith("ANX-")
        assert node.id in parsed.roots


@requires_corpus
def test_lettered_and_roman_items_attach_to_their_clause(parsed):
    items = [n for n in parsed.nodes.values() if n.kind == "ITEM"]
    assert len(items) > 100
    for node in items[:200]:
        assert node.parent_id is not None
        assert re.search(r"\([a-z]+\)(#\d+)?$", node.id), node.id


@requires_corpus
def test_running_furniture_never_enters_a_clause_hash(parsed):
    """Page numbers, part banners and footnote blocks must not be in the text."""
    for node in parsed.nodes.values():
        assert not re.match(r"^\s*\d{1,3}\s*$", node.text)
        assert "Reference: Circular" not in node.text.split("\n")[0]


@requires_corpus
def test_footnote_marker_digits_are_stripped_but_recorded(parsed):
    """The clause hash covers the sentence, not the superscript apparatus."""
    section = parsed.get("21")
    assert section.title.endswith("Process")  # not "...Process28"
    assert 28 in section.footnote_markers


@requires_corpus
def test_char_spans_point_at_the_clause_in_the_document_text(parsed):
    text = parsed.document.text
    checked = 0
    for node in parsed.nodes.values():
        start, end = node.char_span
        assert 0 <= start <= end <= len(text), node.id
        first_line = node.text.split("\n")[0]
        assert first_line[:40] in text[start : start + 200], node.id
        checked += 1
        if checked >= 300:
            break
    assert checked


@requires_corpus
def test_section_numbering_is_reported_not_reordered(parsed):
    numbers = parsed.stats.section_numbers
    assert numbers == sorted(numbers), "sections must be emitted in document order"
    assert not parsed.stats.out_of_sequence
    # Gaps, if any, are reported rather than papered over.
    assert isinstance(parsed.stats.section_gaps, list)


@requires_corpus
def test_no_duplicate_clause_ids_in_the_numbered_body(parsed):
    """Annexure form templates may repeat numbering; the obligation-bearing body may not."""
    body = [
        n
        for n in parsed.nodes.values()
        if not n.section.startswith("ANX-") and n.kind in ("SECTION", "CLAUSE", "SUBCLAUSE")
    ]
    ids = [n.id for n in body]
    assert len(ids) == len(set(ids))
    assert not any("#" in i for i in ids)


@requires_corpus
def test_toc_and_preamble_are_excluded(parsed):
    stats = parsed.stats
    assert stats.toc_pages is not None
    assert stats.body_page_start > stats.toc_pages[1]
    for node in parsed.nodes.values():
        assert node.page >= stats.body_page_start


@requires_corpus
def test_walk_visits_every_node_once(parsed):
    seen = [n.id for n in parsed.walk()]
    assert len(seen) == len(set(seen))
    assert len(seen) == len(parsed.nodes)


# ------------------------------------------------------- unit, no PDF needed


def test_ambiguous_item_tokens_are_resolved_by_continuity():
    """'v)' is a Roman five after 'iv)', but a letter after 'u)'."""
    assert _classify_item("iv", "iii") == "roman"
    assert _classify_item("v", "iv") == "roman"
    assert _classify_item("v", "u") == "letter"
    assert _classify_item("i", None) == "roman"
    assert _classify_item("x", None) == "letter"
    assert _classify_item("b", "a") == "letter"
    assert _classify_item("vii", "vi") == "roman"

"""Deterministic extraction of structure and provenance from SEBI PDFs.

Nothing in this package is probabilistic. Given the same PDF bytes, every
module here must produce identical ids, identical spans and identical hashes,
on any machine, on any run. `sanhita verify` exists to prove it.
"""

from sanhita.parse.anchors import (
    KNOWN_TYPOGRAPHY,
    CharacterAnomaly,
    clause_sha256,
    scan_non_ascii,
    span_of,
)
from sanhita.parse.clause_tree import (
    ClauseNode,
    ClauseTree,
    ParseStats,
    parse_clause_tree,
)
from sanhita.parse.footnotes import (
    BodyCircularRef,
    FootnoteRef,
    FootnoteReport,
    extract_footnotes,
)

__all__ = [
    "BodyCircularRef",
    "CharacterAnomaly",
    "ClauseNode",
    "ClauseTree",
    "FootnoteRef",
    "FootnoteReport",
    "KNOWN_TYPOGRAPHY",
    "ParseStats",
    "clause_sha256",
    "extract_footnotes",
    "parse_clause_tree",
    "scan_non_ascii",
    "span_of",
]

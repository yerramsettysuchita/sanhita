"""Shared fixtures.

The corpus PDF is gitignored, so every test that needs it is skipped rather
than failed when it is absent. Tests that exercise the IR do not need it and
always run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(SRC))


#: The circular the suite is written against.
#:
#: Every corpus test asserts something specific about this document: clause
#: 40.1.8's fields, its footnote lineage, its broken cross references, its
#: fingerprint. Once other circulars were added to corpus/ for testing the
#: upload path, picking "the first PDF alphabetically" silently started
#: returning the depositories circular and eighteen tests failed against a
#: document they were never about.
#: Returning some *other* circular when this one is absent is worse than
#: returning nothing. A reviewer unpacking a partial copy then sees
#: `test_the_worked_example_parse_is_unchanged` fail with a fingerprint that
#: does not match, which reads as "the parser is broken and its output is not
#: reproducible" when the truth is "you are missing one PDF". Skipping says
#: that plainly. There is no fallback for this reason.
WORKED_EXAMPLE = "stock-brokers-master-circular"


def _find_corpus_pdf() -> Path | None:
    """The stock broker master circular, whatever else is sitting beside it."""
    for directory in (ROOT / "corpus", ROOT):
        if not directory.is_dir():
            continue
        pdfs = sorted(directory.glob("*.pdf"))
        if not pdfs:
            continue
        for pdf in pdfs:
            if pdf.name.startswith(WORKED_EXAMPLE):
                return pdf
    return None


CORPUS_PDF = _find_corpus_pdf()

requires_corpus = pytest.mark.skipif(
    CORPUS_PDF is None,
    reason="no circular PDF found in corpus/ (it is gitignored)",
)


@pytest.fixture(scope="session")
def corpus_pdf() -> Path:
    if CORPUS_PDF is None:  # pragma: no cover - guarded by the marker
        pytest.skip("no corpus PDF")
    return CORPUS_PDF


@pytest.fixture(scope="session")
def parsed(corpus_pdf: Path):
    """The clause tree, parsed once for the whole session."""
    from sanhita.parse.clause_tree import parse_clause_tree

    return parse_clause_tree(corpus_pdf)


@pytest.fixture(scope="session")
def footnote_report(parsed):
    from sanhita.parse.footnotes import extract_footnotes

    return extract_footnotes(parsed.document, parsed.clause_of_line)


def sign_in(client, *, name: str = "A Named Officer", email: str = "officer@example.com"):
    """Put an authenticated officer behind the requests a test is about to make.

    Every state-changing compliance action, not only certification, records who
    did it and refuses an anonymous caller. That is the point of the audit
    trail, so most tests of the compliance journey need an account before they
    can exercise the thing they are actually about.

    Tests asserting the refusal itself deliberately do not call this.
    """
    return client.post(
        "/signup",
        data={"name": name, "email": email, "password": "a-long-enough-password"},
        follow_redirects=True,
    )

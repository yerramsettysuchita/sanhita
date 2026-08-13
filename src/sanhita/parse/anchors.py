"""Stable identity and integrity for parsed source text.

Two rules govern this module.

**Verbatim means verbatim.** The hash of a clause must be the hash of the
regulation's own characters. Nothing here rewrites a character — not a curly
quote, not a non-breaking space, not an en dash. Normalising before hashing
would make the provenance chain describe our cleanup rather than SEBI's text.

**Unmappable characters are flagged, never fixed.** Several fonts in the source
PDF (Arial, Times New Roman) are referenced but not embedded, so extraction can
occasionally substitute a character. `scan_non_ascii` reports every non-ASCII
codepoint and separates the ones that are ordinary Word typography from the ones
that warrant a human look. It returns a report; it does not edit the text.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

__all__ = [
    "KNOWN_TYPOGRAPHY",
    "CharacterAnomaly",
    "NonAsciiReport",
    "clause_sha256",
    "scan_non_ascii",
    "span_of",
]


#: Non-ASCII characters that Microsoft Word legitimately emits and that carry no
#: risk of being a font-substitution artefact. Listed so the anomaly report can
#: stay quiet about them without any of them being rewritten in the text.
KNOWN_TYPOGRAPHY: dict[str, str] = {
    "‘": "LEFT SINGLE QUOTATION MARK",
    "’": "RIGHT SINGLE QUOTATION MARK",
    "“": "LEFT DOUBLE QUOTATION MARK",
    "”": "RIGHT DOUBLE QUOTATION MARK",
    "–": "EN DASH",
    "—": "EM DASH",
    "…": "HORIZONTAL ELLIPSIS",
    " ": "NO-BREAK SPACE",
    "®": "REGISTERED SIGN",
    "©": "COPYRIGHT SIGN",
    "™": "TRADE MARK SIGN",
    "₹": "INDIAN RUPEE SIGN",
    "°": "DEGREE SIGN",
    "·": "MIDDLE DOT",
    "•": "BULLET",
    "½": "VULGAR FRACTION ONE HALF",
    "é": "LATIN SMALL LETTER E WITH ACUTE",
    "′": "PRIME",
    "″": "DOUBLE PRIME",
    "ﬁ": "LATIN SMALL LIGATURE FI",
    "ﬂ": "LATIN SMALL LIGATURE FL",
}


@dataclass(frozen=True, slots=True)
class CharacterAnomaly:
    """A non-ASCII character that is not recognised Word typography."""

    char: str
    codepoint: str
    name: str
    category: str
    count: int
    first_page: int | None = None

    def __str__(self) -> str:  # pragma: no cover - display only
        where = f" first seen p{self.first_page}" if self.first_page else ""
        return f"{self.codepoint} {self.name} x{self.count}{where}"


@dataclass(slots=True)
class NonAsciiReport:
    """Outcome of a non-ASCII scan. Advisory only; the text is never altered."""

    total_non_ascii: int = 0
    typography: Counter = field(default_factory=Counter)
    anomalies: list[CharacterAnomaly] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.anomalies

    def summary(self) -> str:
        if self.is_clean:
            return (
                f"{self.total_non_ascii} non-ASCII chars, all recognised typography, "
                f"0 anomalies"
            )
        top = ", ".join(str(a) for a in self.anomalies[:5])
        return (
            f"{self.total_non_ascii} non-ASCII chars, "
            f"{len(self.anomalies)} unmapped kinds: {top}"
        )


def scan_non_ascii(text: str, page_of: dict[int, int] | None = None) -> NonAsciiReport:
    """Report every non-ASCII character in `text`, flagging unrecognised ones.

    `page_of` optionally maps a character offset to a page number, used only to
    tell the operator where to go and look.
    """
    report = NonAsciiReport()
    seen: dict[str, int] = {}
    counts: Counter = Counter()

    for offset, ch in enumerate(text):
        if ch.isascii():
            continue
        report.total_non_ascii += 1
        if ch in KNOWN_TYPOGRAPHY:
            report.typography[ch] += 1
            continue
        counts[ch] += 1
        seen.setdefault(ch, offset)

    for ch, count in sorted(counts.items()):
        offset = seen[ch]
        report.anomalies.append(
            CharacterAnomaly(
                char=ch,
                codepoint=f"U+{ord(ch):04X}",
                name=unicodedata.name(ch, "UNNAMED"),
                category=unicodedata.category(ch),
                count=count,
                first_page=_page_for(offset, page_of),
            )
        )
    return report


def _page_for(offset: int, page_of: dict[int, int] | None) -> int | None:
    """Resolve a character offset to a page using a {page_start_offset: page} map."""
    if not page_of:
        return None
    best: int | None = None
    for start in sorted(page_of):
        if start <= offset:
            best = page_of[start]
        else:
            break
    return best


def clause_sha256(verbatim_text: str) -> str:
    """SHA-256 of the clause's own characters, UTF-8 encoded, nothing removed.

    This is the value that appears in `SourceAnchor.sha256` and it is the
    anchor the whole provenance chain hangs from. It must be computed on text
    that has already had footnote marker digits stripped — the marker is our
    reading of the page furniture, not part of the regulation's sentence — but
    on nothing else.
    """
    return hashlib.sha256(verbatim_text.encode("utf-8")).hexdigest()


def span_of(document_text: str, fragment: str, search_from: int = 0) -> tuple[int, int]:
    """Locate `fragment` in `document_text`, returning a [start, end) span.

    Searches forward from `search_from` so that repeated boilerplate resolves to
    the occurrence that belongs to the current clause rather than to the first
    one in the document.
    """
    start = document_text.find(fragment, search_from)
    if start < 0:
        raise ValueError(
            f"fragment not found at or after offset {search_from}: {fragment[:60]!r}"
        )
    return (start, start + len(fragment))

"""Turning provenance spans into highlightable segments.

The workbench's central interaction is: hover a compiled field on the right, see
the exact words that produced it light up on the left. That needs the clause
text split into runs, each tagged with every field whose span covers it.

Spans overlap — `action.recipient` usually sits inside `action.object`, and
`deadline` can nest inside either. So this cannot be a simple wrap-each-span
pass; it splits the text at every span boundary and labels each resulting run
with the full set of fields covering it.

Segmentation happens server-side because the offsets are the provenance record
itself. Recomputing them in the browser would mean the highlight could drift
from the thing that was signed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Segment", "segment_text"]


@dataclass(slots=True)
class Segment:
    """One run of clause text and the fields whose provenance covers it."""

    text: str
    start: int
    end: int
    fields: list[str] = field(default_factory=list)

    @property
    def is_highlighted(self) -> bool:
        return bool(self.fields)

    @property
    def field_attr(self) -> str:
        """Space-separated field list, for the `data-fields` attribute."""
        return " ".join(self.fields)


def segment_text(text: str, spans: dict[str, tuple[int, int]]) -> list[Segment]:
    """Split `text` at every span boundary, labelling each run.

    Returns runs covering the whole string in order, so joining their `text`
    reproduces the input exactly — the clause is rendered verbatim, with markup
    only around it, never inside a character.
    """
    if not text:
        return []

    # Ignore anything that does not land inside the text. A span that cannot be
    # shown is a provenance bug; it must not silently shift the highlighting of
    # its neighbours.
    usable = {
        name: (start, end)
        for name, (start, end) in spans.items()
        if 0 <= start < end <= len(text)
    }
    if not usable:
        return [Segment(text=text, start=0, end=len(text))]

    boundaries = {0, len(text)}
    for start, end in usable.values():
        boundaries.add(start)
        boundaries.add(end)

    ordered = sorted(boundaries)
    segments: list[Segment] = []
    for left, right in zip(ordered, ordered[1:]):
        if left == right:
            continue
        covering = sorted(
            name for name, (start, end) in usable.items() if start <= left and right <= end
        )
        segments.append(
            Segment(text=text[left:right], start=left, end=right, fields=covering)
        )
    return segments

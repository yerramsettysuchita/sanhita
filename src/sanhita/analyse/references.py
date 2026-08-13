"""Which clauses lean on which other clauses.

Regulation is written by reference. "The entity shall follow the procedure as
prescribed in para 7.2.2 above" is not a complete instruction: it is a pointer,
and the duty it creates is whatever 7.2.2 happens to say today.

That matters when the regulator amends 7.2.2. The text of 7.2.3 does not change,
its hash does not change, and a diff that only compares text will report it as
untouched. But the rule compiled from 7.2.3 now means something different from
what the officer signed, because the thing it points at moved underneath it.

This module finds those pointers, so the amendment stage can follow them. It
reads only the regulation's own words, matching the phrasings SEBI actually
uses. A citation it cannot resolve to a real clause is dropped rather than
guessed at, and the count of dropped ones is reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sanhita.parse.clause_tree import ClauseTree

__all__ = ["Citation", "ReferenceGraph", "build_graph"]

#: "para 7.2.2", "paragraph 15.4", "clause 21.1.2", "section 40". The number is
#: a dotted clause path. Trailing punctuation is left out of the capture.
_CITE = re.compile(
    r"\b(?:para(?:graph)?s?|clauses?|sections?)\s+"
    r"(\d{1,3}(?:\.\d{1,3})*)",
    re.IGNORECASE,
)

#: "paras 7.2.1 and 7.2.2", "paras 15.1, 15.2 and 15.3". Once a citation has
#: been seen, further bare numbers joined by "and" or a comma belong to it.
#: Applied with .match(text, pos), which anchors at that position already.
_MORE = re.compile(r"\s*(?:,|and)\s*(\d{1,3}(?:\.\d{1,3})*)", re.IGNORECASE)


@dataclass(frozen=True)
class Citation:
    """One clause pointing at another."""

    source: str
    target: str
    #: The words around the pointer, so a reader can judge it.
    context: str

    def to_json(self) -> dict:
        return {"source": self.source, "target": self.target, "context": self.context}


@dataclass
class ReferenceGraph:
    citations: list[Citation] = field(default_factory=list)
    #: Numbers shaped like a clause path that no clause in this document
    #: answers to. These are broken cross-references in the regulation itself:
    #: the text tells a reader to follow a pointer that leads nowhere.
    broken: list[Citation] = field(default_factory=list)
    #: Numbers the pattern picked up that were never clause references at all,
    #: such as a page number or an amount. Counted, not reported as defects.
    noise: list[str] = field(default_factory=list)

    #: target -> the clauses that point at it.
    _incoming: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for c in self.citations:
            self._incoming.setdefault(c.target, set()).add(c.source)

    @property
    def edges(self) -> int:
        return len(self.citations)

    @property
    def citing_clauses(self) -> set[str]:
        return {c.source for c in self.citations}

    @property
    def cited_clauses(self) -> set[str]:
        return {c.target for c in self.citations}

    def citers_of(self, clause_id: str) -> set[str]:
        """Clauses that point directly at this one."""
        return set(self._incoming.get(clause_id, ()))

    def dependents_of(self, clause_id: str, *, max_depth: int = 4) -> dict[str, int]:
        """Every clause that reaches this one, with how many hops away it is.

        Breadth first, so each clause is recorded at its shortest distance. The
        depth cap is not an optimisation: a chain four references long is
        already beyond what anyone would call "affected by", and reporting it
        as such would make the finding meaningless.
        """
        found: dict[str, int] = {}
        frontier = {clause_id}
        seen = {clause_id}

        for depth in range(1, max_depth + 1):
            nxt: set[str] = set()
            for node in frontier:
                for citer in self.citers_of(node):
                    if citer in seen:
                        continue
                    seen.add(citer)
                    found[citer] = depth
                    nxt.add(citer)
            if not nxt:
                break
            frontier = nxt
        return found

    def to_json(self) -> dict:
        return {
            "edges": self.edges,
            "citing_clauses": len(self.citing_clauses),
            "cited_clauses": len(self.cited_clauses),
            "broken": [c.to_json() for c in self.broken],
            "noise": len(self.noise),
            "citations": [c.to_json() for c in self.citations],
        }


def build_graph(tree: ClauseTree) -> ReferenceGraph:
    """Read every clause and record what it points at.

    Deterministic: the same tree always produces the same graph, in the same
    order.
    """
    body = {
        n.id: n
        for n in tree.nodes.values()
        if not n.section.startswith("ANX-") and n.kind != "APPENDIX"
    }
    # The suffix form "79.3.3(b)#2" exists for repeated numbering. A citation
    # never carries it, so bare numbers resolve to the first clause that owns
    # them, which is the definition rather than a repeat.
    canonical: dict[str, str] = {}
    for node_id in sorted(body):
        canonical.setdefault(node_id.split("#")[0], node_id)

    # The highest section number that exists. A bare citation above it is not a
    # clause reference at all: it is a page number, an amount, or a year that
    # happened to follow the word "para".
    sections = [
        int(n.section) for n in body.values() if n.section.isdigit()
    ]
    highest_section = max(sections, default=0)

    citations: list[Citation] = []
    broken: list[Citation] = []
    noise: list[str] = []
    seen: set[tuple[str, str]] = set()

    for node_id in sorted(body):
        text = body[node_id].text
        for match in _CITE.finditer(text):
            targets = [match.group(1)]

            # Pick up "and 7.2.3" / ", 15.2" trailing the first number.
            pos = match.end()
            while True:
                more = _MORE.match(text, pos)
                if not more:
                    break
                targets.append(more.group(1))
                pos = more.end()

            start = max(0, match.start() - 60)
            context = " ".join(text[start : pos + 40].split())

            for raw in targets:
                target = canonical.get(raw)
                if target is None:
                    # Does it even look like a clause in this document?
                    head = raw.split(".")[0]
                    plausible = head.isdigit() and int(head) <= highest_section
                    if plausible:
                        broken.append(
                            Citation(source=node_id, target=raw, context=context)
                        )
                    else:
                        noise.append(raw)
                    continue
                # A clause citing itself is a cross-reference to its own
                # sub-items, not a dependency.
                if target == node_id or node_id.startswith(target + "."):
                    continue
                key = (node_id, target)
                if key in seen:
                    continue
                seen.add(key)
                citations.append(Citation(source=node_id, target=target, context=context))

    return ReferenceGraph(
        citations=citations, broken=broken, noise=sorted(set(noise))
    )

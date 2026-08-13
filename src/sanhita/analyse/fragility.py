"""Where this regulation is structurally fragile.

Every other screen in this product answers "what must my firm do". This one
answers a question about the regulation itself:

    if the regulator amended one sentence tomorrow, which sentence would
    disturb the most?

Clauses are not equal. Most stand alone. A few are load-bearing: they are cited
by other clauses, which are cited in turn, and a rulebook hangs off them. Amend
one of those and the damage spreads along the citation graph to clauses whose
own words never changed.

That is computable here and nowhere else, because it needs three things at once:
the citation graph, the compiled rules, and the record of which rules a person
signed. A tool that only retrieves text has none of them.

**This is a structural measure, not a judgement about importance.** A clause
nothing cites can still be the most consequential sentence in the document. What
this ranks is blast radius, not significance, and the screen says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sanhita.analyse.references import ReferenceGraph
from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["LoadBearing", "FragilityReport", "assess_fragility"]


@dataclass(frozen=True)
class LoadBearing:
    """One clause and what an amendment to it would cost."""

    clause_id: str
    page: int
    #: Rules compiled from this clause itself.
    own_rules: int
    own_certified: int
    #: Clauses that reach this one through citations, at any depth.
    dependent_clauses: int
    #: Rules on those clauses, which would need re-reading.
    dependent_rules: int
    dependent_certified: int
    #: How far the furthest dependent sits.
    max_depth: int
    excerpt: str

    @property
    def blast_radius(self) -> int:
        """Certified rules that an amendment here would put back on a desk."""
        return self.own_certified + self.dependent_certified

    @property
    def total_rules_touched(self) -> int:
        return self.own_rules + self.dependent_rules

    def to_json(self) -> dict:
        return {
            "clause_id": self.clause_id,
            "page": self.page,
            "own_rules": self.own_rules,
            "own_certified": self.own_certified,
            "dependent_clauses": self.dependent_clauses,
            "dependent_rules": self.dependent_rules,
            "dependent_certified": self.dependent_certified,
            "max_depth": self.max_depth,
            "blast_radius": self.blast_radius,
        }


@dataclass
class FragilityReport:
    clauses_examined: int = 0
    citation_edges: int = 0
    certified_total: int = 0
    ranked: list[LoadBearing] = field(default_factory=list)

    @property
    def load_bearing(self) -> list[LoadBearing]:
        """Clauses that anything at all depends on."""
        return [c for c in self.ranked if c.dependent_clauses]

    @property
    def most_fragile(self) -> LoadBearing | None:
        return self.ranked[0] if self.ranked else None

    @property
    def coupling(self) -> float:
        """Share of rule-bearing clauses that anything else depends on.

        A low number is good news about the document. It means the circular is
        mostly flat: amend a clause and the damage stops there. A consolidated
        rulebook full of cross-dependencies is far harder to change safely, and
        being able to say which one you have is the point of measuring it.
        """
        return (
            len(self.load_bearing) / self.clauses_examined
            if self.clauses_examined
            else 0.0
        )

    def verdict(self) -> str:
        if not self.clauses_examined:
            return "Nothing has been compiled, so there is nothing to rank."
        depended = len(self.load_bearing)
        return (
            f"{depended} of {self.clauses_examined} clauses carrying rules are "
            f"cited by another clause, {self.coupling:.1%}. This circular is "
            "structurally flat: amend almost any clause and the consequence "
            "stops at that clause. That is a good property, and it is not one "
            "you could assert without counting."
        )

    def caveats(self) -> list[str]:
        return [
            f"{self.citation_edges} citations were read from the regulation's "
            "own words, matching the phrasings it uses: para, paragraph, clause "
            "and section, followed by a number.",
            "This ranks blast radius, not importance. A clause nothing cites can "
            "still be the most consequential sentence in the document. What is "
            "measured is how far an amendment would travel, and nothing else.",
            "A dependency is followed at most four references deep. Beyond that "
            "the connection is too remote to call a clause affected.",
            "Only rules compiled from a clause count toward its radius, so a "
            "load-bearing clause in a section nobody has compiled yet will "
            "score low until it is.",
        ]

    def to_json(self) -> dict:
        return {
            "clauses_examined": self.clauses_examined,
            "citation_edges": self.citation_edges,
            "certified_total": self.certified_total,
            "load_bearing": len(self.load_bearing),
            "coupling": self.coupling,
            "verdict": self.verdict(),
            "caveats": self.caveats(),
            "ranked": [c.to_json() for c in self.ranked],
        }


def assess_fragility(
    tree,
    graph: ReferenceGraph,
    obligations: list[Obligation],
    *,
    limit: int = 40,
) -> FragilityReport:
    """Rank clauses by how much an amendment to each would disturb."""
    from sanhita.analyse.conflicts import TABLE_LIKE_CHARS

    live = [
        o
        for o in obligations
        if o.status in (RuleStatus.PROPOSED, RuleStatus.CERTIFIED)
    ]
    # The flattened summary tables would top this ranking on volume alone: one
    # of them yields sixty-four rules, none of which is a real obligation. A
    # clause that scores high because it is a mis-parsed table is not a fragile
    # clause, it is a parsing defect wearing a crown.
    oversized = {
        o.source.clause_id
        for o in live
        if len(o.source.verbatim_text) > TABLE_LIKE_CHARS
    }
    live = [o for o in live if o.source.clause_id not in oversized]

    by_clause: dict[str, list[Obligation]] = {}
    for o in live:
        by_clause.setdefault(o.source.clause_id, []).append(o)

    def counts(clause_ids) -> tuple[int, int]:
        rules = [r for cid in clause_ids for r in by_clause.get(cid, [])]
        return len(rules), sum(
            1 for r in rules if r.status is RuleStatus.CERTIFIED
        )

    report = FragilityReport(
        clauses_examined=len(by_clause),
        citation_edges=graph.edges,
        certified_total=sum(1 for o in live if o.status is RuleStatus.CERTIFIED),
    )

    # Only clauses that something actually depends on, or that carry rules, are
    # worth ranking. Everything else has a radius of its own rules and nothing
    # more, which is the ordinary case and not a finding.
    candidates = graph.cited_clauses | set(by_clause)

    for clause_id in sorted(candidates):
        dependents = graph.dependents_of(clause_id)
        own_rules, own_certified = counts([clause_id])
        dep_rules, dep_certified = counts(dependents)

        if not own_rules and not dep_rules:
            continue

        node = tree.get(clause_id)
        report.ranked.append(
            LoadBearing(
                clause_id=clause_id,
                page=node.page if node else 0,
                own_rules=own_rules,
                own_certified=own_certified,
                dependent_clauses=len(dependents),
                dependent_rules=dep_rules,
                dependent_certified=dep_certified,
                max_depth=max(dependents.values(), default=0),
                excerpt=" ".join(node.text.split())[:220] if node else "",
            )
        )

    # Worst first: what an amendment would cost in signatures, then in rules,
    # then in how far the damage spreads.
    report.ranked.sort(
        key=lambda c: (
            -c.blast_radius,
            -c.total_rules_touched,
            -c.dependent_clauses,
            c.clause_id,
        )
    )
    report.ranked = report.ranked[:limit]
    return report

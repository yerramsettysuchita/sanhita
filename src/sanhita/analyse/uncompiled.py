"""Clauses that carry a duty and produced no rule.

Every demo shows what its extractor found. Almost none show what it missed,
because the tool has no independent idea of what was there to find.

Sanhita does. The classifier that builds the coverage denominator runs without
consulting the extractor, so the two can be set against each other: the
classifier says this clause imposes a duty, the extractor produced nothing from
it, and the difference is a list of specific clauses somebody should read.

That list is the honest reading of a coverage figure. "11.8% covered" invites
the question "of what, and what is in the other 88%". This answers it by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation
from sanhita.metrics.coverage import classify_clause
from sanhita.parse.clause_tree import ClauseTree

__all__ = ["Uncompiled", "MissingReport", "find_uncompiled"]


@dataclass(frozen=True)
class Uncompiled:
    """A clause the classifier says carries a duty, with nothing compiled."""

    clause_id: str
    section: str
    page: int
    title: str
    excerpt: str
    #: True when rules exist but every one was rejected by a person. That is a
    #: decision, not a gap, and it is shown separately.
    all_rejected: bool = False

    def to_json(self) -> dict:
        return {
            "clause_id": self.clause_id,
            "section": self.section,
            "page": self.page,
            "all_rejected": self.all_rejected,
        }


@dataclass
class MissingReport:
    duty_bearing: int = 0
    with_a_rule: int = 0
    missing: list[Uncompiled] = field(default_factory=list)
    rejected_away: list[Uncompiled] = field(default_factory=list)

    @property
    def rate(self) -> float:
        """How much of the duty-bearing text the extractor said nothing about."""
        return len(self.missing) / self.duty_bearing if self.duty_bearing else 0.0

    def by_section(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.missing:
            counts[item.section] = counts.get(item.section, 0) + 1
        return dict(
            sorted(counts.items(), key=lambda kv: (-kv[1], int(kv[0]) if kv[0].isdigit() else 9999))
        )

    def caveats(self) -> list[str]:
        return [
            f"{self.duty_bearing} clauses were judged to carry a duty by a "
            "classifier that never sees the extractor's output, so a weaker "
            "extractor cannot shrink this list by failing more often.",
            "That classifier is itself imperfect. Some of these clauses will "
            "turn out to impose nothing, which is why they are listed for "
            "reading rather than counted as defects.",
            f"{len(self.rejected_away)} further clause(s) had rules that a "
            "person rejected. Those are decisions, not gaps, and are kept "
            "apart below.",
        ]

    def to_json(self) -> dict:
        return {
            "duty_bearing": self.duty_bearing,
            "with_a_rule": self.with_a_rule,
            "missing": len(self.missing),
            "rate": self.rate,
            "by_section": self.by_section(),
            "caveats": self.caveats(),
            "clauses": [u.to_json() for u in self.missing],
        }


def find_uncompiled(tree: ClauseTree, obligations: list[Obligation]) -> MissingReport:
    """Set the classifier against the extractor and list the difference."""
    live = [o for o in obligations if o.status is not RuleStatus.SUPERSEDED]
    by_clause: dict[str, list[Obligation]] = {}
    for o in live:
        by_clause.setdefault(o.source.clause_id, []).append(o)

    report = MissingReport()

    for node in sorted(tree.nodes.values(), key=lambda n: n.id):
        if node.section.startswith("ANX-") or node.kind == "APPENDIX":
            continue
        if not classify_clause(node).in_denominator:
            continue

        report.duty_bearing += 1
        rules = by_clause.get(node.id, [])
        if rules and any(r.status is not RuleStatus.REJECTED for r in rules):
            report.with_a_rule += 1
            continue

        item = Uncompiled(
            clause_id=node.id,
            section=node.section,
            page=node.page,
            title=(node.title or "").strip(),
            excerpt=" ".join(node.text.split())[:260],
            all_rejected=bool(rules),
        )
        if item.all_rejected:
            report.rejected_away.append(item)
        else:
            report.missing.append(item)

    return report

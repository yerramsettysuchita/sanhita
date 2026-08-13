"""DIFF: what an amendment does to a rulebook that has already been signed.

    from sanhita.diff import diff_trees, assess_impact

    changes = diff_trees(before_tree, after_tree)
    impact = assess_impact(changes, registry.all_current())
    print(impact.headline())

Two stages, deliberately separate. ``diff_trees`` compares two parsed trees and
knows nothing about rules. ``assess_impact`` maps those changes onto compiled
rules and knows nothing about PDFs.
"""

from sanhita.diff.impact import (
    AffectedRule,
    Consequence,
    ImpactReport,
    assess_impact,
)
from sanhita.diff.tree_diff import ChangeKind, ClauseChange, TreeDiff, diff_trees

__all__ = [
    "AffectedRule",
    "ChangeKind",
    "ClauseChange",
    "Consequence",
    "ImpactReport",
    "TreeDiff",
    "assess_impact",
    "diff_trees",
]

"""Reasoning across the whole rulebook rather than one rule at a time.

Everything in this package is only possible because the rules are typed. To
find a contradiction by reading, somebody would have to hold 1,377 clauses in
their head at once and do date arithmetic between any two of them. Over an
Obligation IR it is a grouping and a comparison.

Deterministic throughout. No model is consulted.
"""

from sanhita.analyse.conflicts import (
    Confidence,
    Conflict,
    ConflictKind,
    ConflictReport,
    find_conflicts,
)
from sanhita.analyse.burden import ActorBurden, BurdenReport, measure_burden
from sanhita.analyse.calendar import Due, Schedule, build_schedule
from sanhita.analyse.divergence import (
    DivergenceReport,
    DivergenceRisk,
    Signal,
    assess_divergence,
)
from sanhita.analyse.forecast import Forecast, Outlook, UpcomingDuty, build_forecast
from sanhita.analyse.fragility import FragilityReport, LoadBearing, assess_fragility
from sanhita.analyse.impact_assessment import (
    Change,
    ChangeKind,
    ImpactAssessment,
    assess_amendment,
)
from sanhita.analyse.latency import LatencyReport, Milestone, humanise, measure_latency
from sanhita.analyse.receipt import Receipt, build_receipt, verify_receipt
from sanhita.analyse.references import Citation, ReferenceGraph, build_graph
from sanhita.analyse.rego import RegoExport, to_rego
from sanhita.analyse.uncompiled import MissingReport, Uncompiled, find_uncompiled

__all__ = [
    "ActorBurden",
    "BurdenReport",
    "Citation",
    "FragilityReport",
    "LoadBearing",
    "assess_fragility",
    "measure_burden",
    "Due",
    "MissingReport",
    "Receipt",
    "RegoExport",
    "Schedule",
    "Uncompiled",
    "build_receipt",
    "build_schedule",
    "find_uncompiled",
    "to_rego",
    "verify_receipt",
    "Confidence",
    "Conflict",
    "ConflictKind",
    "ConflictReport",
    "ReferenceGraph",
    "build_graph",
    "find_conflicts",
    # -- what the regulation costs, and what a change to it would cost
    "Change",
    "ChangeKind",
    "ImpactAssessment",
    "assess_amendment",
    # -- where the market will read the same clause two ways
    "DivergenceReport",
    "DivergenceRisk",
    "Signal",
    "assess_divergence",
    # -- how long text took to become an operating rule
    "LatencyReport",
    "Milestone",
    "humanise",
    "measure_latency",
    # -- what is about to be missed
    "Forecast",
    "Outlook",
    "UpcomingDuty",
    "build_forecast",
]

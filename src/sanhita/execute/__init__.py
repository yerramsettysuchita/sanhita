"""EXECUTE: running certified rules against evidence.

The stage that turns a certified rulebook into operational action. Everything
here is deterministic and offline. No model is loaded and no network call is
made at evaluation time, by design: a firm being told it is in breach is
entitled to an answer that is reproducible and that cites the regulation.

    from sanhita.execute import RuleEngine, EvidenceStore

    report = RuleEngine(calendar).run(obligations, evidence, as_of=today)
    for finding in report.ranked():
        print(finding.clause_id, finding.page, finding.certified_by)
"""

from sanhita.execute.calendar import WEEKENDS_ONLY, DayCountUnresolved, TradingCalendar
from sanhita.execute.engine import RuleEngine, due_date
from sanhita.execute.evidence import ComplianceEvent, EvidenceStore
from sanhita.execute.report import Finding, GapReport, Outcome, Unevaluable

__all__ = [
    "ComplianceEvent",
    "DayCountUnresolved",
    "EvidenceStore",
    "Finding",
    "GapReport",
    "Outcome",
    "RuleEngine",
    "TradingCalendar",
    "Unevaluable",
    "WEEKENDS_ONLY",
    "due_date",
]

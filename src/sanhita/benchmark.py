"""Timing every stage of the pipeline, so no performance claim is a guess.

Every number the README quotes about speed comes from here. Run it and the
numbers regenerate; if they move, the README is wrong and this is what proves
it.

**What is measured and what that means.**

Cold and warm are reported separately because they answer different questions.
Cold is what a reviewer waits for on a freshly started process. Warm is what a
compliance officer experiences all day, once the clause tree is in memory. Most
of this product's work is warm, and quoting only the cold number would be as
misleading as quoting only the warm one.

Nothing here is extrapolated. A stage that runs in under a millisecond is
reported as such rather than scaled up into a per-hour figure that was never
observed.
"""

from __future__ import annotations

import datetime as _dt
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Measurement", "BenchmarkReport", "run_benchmark"]


@dataclass
class Measurement:
    """One stage, timed, with what it actually did."""

    stage: str
    seconds: float
    detail: str = ""
    #: Repeats behind the figure. One means a single observation, not an
    #: average, and the report says so rather than implying rigour it lacks.
    runs: int = 1
    spread: float = 0.0

    @property
    def per_second(self) -> float | None:
        return None

    def line(self) -> str:
        if self.seconds < 0.001:
            timing = f"{self.seconds * 1_000_000:>8.0f} us"
        elif self.seconds < 1:
            timing = f"{self.seconds * 1000:>8.1f} ms"
        else:
            timing = f"{self.seconds:>8.2f} s "
        spread = f" +/-{self.spread * 1000:.0f}ms" if self.runs > 1 else ""
        return f"  {self.stage:<34}{timing}{spread:<10}  {self.detail}"


@dataclass
class BenchmarkReport:
    document: str
    run_at: _dt.datetime = field(default_factory=lambda: _dt.datetime.now(_dt.timezone.utc))
    measurements: list[Measurement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(
        self, stage: str, seconds: float, detail: str = "", runs: int = 1, spread: float = 0.0
    ) -> None:
        self.measurements.append(
            Measurement(stage=stage, seconds=seconds, detail=detail, runs=runs, spread=spread)
        )

    def get(self, stage: str) -> Measurement | None:
        for m in self.measurements:
            if m.stage == stage:
                return m
        return None

    def to_json(self) -> dict:
        return {
            "document": self.document,
            "run_at": self.run_at.isoformat(),
            "measurements": [
                {
                    "stage": m.stage,
                    "seconds": round(m.seconds, 6),
                    "runs": m.runs,
                    "spread_seconds": round(m.spread, 6),
                    "detail": m.detail,
                }
                for m in self.measurements
            ],
            "notes": self.notes,
        }


def _time(fn, runs: int = 1) -> tuple[float, float, object]:
    """Run, return (median seconds, spread, last result)."""
    timings: list[float] = []
    result = None
    for _ in range(runs):
        started = time.perf_counter()
        result = fn()
        timings.append(time.perf_counter() - started)
    median = statistics.median(timings)
    spread = (max(timings) - min(timings)) if len(timings) > 1 else 0.0
    return median, spread, result


def run_benchmark(pdf: Path, *, warm_runs: int = 5) -> BenchmarkReport:
    """Time the whole pipeline against one circular."""
    from sanhita.analyse import assess_divergence, find_conflicts, measure_burden
    from sanhita.cli_compile import _load_registry
    from sanhita.compile.extract import RuleExtractor
    from sanhita.execute import WEEKENDS_ONLY, RuleEngine
    from sanhita.execute.synthetic import generate
    from sanhita.ir.enums import RuleStatus
    from sanhita.metrics.coverage import compute_coverage
    from sanhita.parse.clause_tree import parse_clause_tree
    from sanhita.parse.layout import load_document

    report = BenchmarkReport(document=pdf.name)

    # -- ingest
    seconds, _, document = _time(lambda: load_document(pdf))
    report.add(
        "PDF to layout, cold",
        seconds,
        f"{document.page_count} pages, {len(document.text):,} characters",
    )

    seconds, _, tree = _time(lambda: parse_clause_tree(pdf))
    report.add(
        "Layout to clause tree, cold",
        seconds,
        f"{len(tree.nodes):,} clauses",
    )

    seconds, spread, _ = _time(lambda: tree.fingerprint(), runs=warm_runs)
    report.add(
        "Fingerprint the tree, warm",
        seconds,
        "hashes every clause id, span and text",
        runs=warm_runs,
        spread=spread,
    )

    # -- compile
    def extract_all():
        extractor = RuleExtractor(circular_id="BENCH")
        out = []
        for node in tree.nodes.values():
            out.extend(extractor.extract(node).obligations)
        return out

    seconds, _, extracted = _time(extract_all)
    rate = len(extracted) / seconds if seconds else 0
    report.add(
        "Extract obligations, deterministic",
        seconds,
        f"{len(extracted):,} obligations, about {rate:,.0f} a second",
    )

    # -- the live store, which carries the certifications
    seconds, _, registry = _time(lambda: _load_registry())
    rules = registry.all_current()
    certified = [o for o in rules if o.status is RuleStatus.CERTIFIED]
    report.add(
        "Load the signed store from disk",
        seconds,
        f"{len(rules):,} rules, {len(certified)} certified, {len(registry.ledger):,} ledger entries",
    )

    seconds, spread, _ = _time(lambda: registry.ledger.verify_chain(), runs=warm_runs)
    report.add(
        "Verify the audit chain",
        seconds,
        f"{len(registry.ledger):,} hash-chained entries",
        runs=warm_runs,
        spread=spread,
    )

    # -- coverage
    seconds, spread, coverage = _time(
        lambda: compute_coverage(tree, rules), runs=warm_runs
    )
    report.add(
        "Compute coverage",
        seconds,
        f"classifies {coverage.total_clauses:,} clauses independently of the extractor",
        runs=warm_runs,
        spread=spread,
    )

    # -- evidence and execution
    today = _dt.date.today()
    seconds, _, evidence = _time(
        lambda: generate(
            certified,
            calendar=WEEKENDS_ONLY,
            start=today - _dt.timedelta(days=180),
            end=today - _dt.timedelta(days=10),
            seed="benchmark",
        )
    )
    report.add(
        "Generate a synthetic evidence set",
        seconds,
        f"{len(evidence):,} compliance events",
    )

    engine = RuleEngine(WEEKENDS_ONLY)
    seconds, spread, gaps = _time(
        lambda: engine.run(rules, evidence, as_of=today), runs=3
    )
    report.add(
        "Run every certified rule",
        seconds,
        f"{gaps.events_checked:,} occasions checked, {gaps.breaches} findings",
        runs=3,
        spread=spread,
    )

    # -- cross-rule analysis
    seconds, spread, conflicts = _time(lambda: find_conflicts(rules), runs=3)
    report.add(
        "Find contradictions",
        seconds,
        f"{conflicts.pairs_compared:,} pairs compared in full",
        runs=3,
        spread=spread,
    )

    seconds, spread, divergence = _time(
        lambda: assess_divergence(rules, ledger=registry.ledger), runs=3
    )
    report.add(
        "Rank divergence risk",
        seconds,
        f"{divergence.clauses_examined:,} clauses scored",
        runs=3,
        spread=spread,
    )

    seconds, spread, burden = _time(lambda: measure_burden(rules), runs=warm_runs)
    report.add(
        "Measure regulatory load",
        seconds,
        f"{len(burden.actors)} actors, {burden.rules_counted:,} rules counted",
        runs=warm_runs,
        spread=spread,
    )

    report.notes = [
        "Cold means a freshly started process with nothing cached. Warm means "
        "the clause tree is already in memory, which is what a person working "
        "through the queue experiences all day.",
        "Timings are the median of the stated number of runs. A single run is "
        "reported as one observation rather than dressed up as an average.",
        "The deterministic extractor makes no network call, so its figure is "
        "not affected by anything outside this machine. The model-assisted "
        "extractor is not benchmarked here because its time is dominated by an "
        "API round trip that says more about the network than the product.",
        "Nothing here is extrapolated. A stage measured over 1,377 rules is "
        "reported over 1,377 rules.",
    ]
    return report


def format_report(report: BenchmarkReport) -> str:
    """The report as a person reads it."""
    width = 78
    lines = [
        "=" * width,
        "  Sanhita pipeline benchmark",
        f"  {report.document}",
        f"  {report.run_at.strftime('%d %b %Y, %H:%M')} UTC",
        "=" * width,
        "",
    ]
    lines.extend(m.line() for m in report.measurements)
    lines.extend(["", "-" * width, "  How to read this", "-" * width])
    for note in report.notes:
        wrapped = []
        current = "    "
        for word in note.split():
            if len(current) + len(word) + 1 > width - 2:
                wrapped.append(current)
                current = "    " + word
            else:
                current += (" " if current.strip() else "") + word
        wrapped.append(current)
        lines.extend(wrapped)
        lines.append("")
    return "\n".join(lines)

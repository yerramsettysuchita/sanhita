"""The benchmark must measure, not estimate.

Its whole purpose is that no performance claim in the README is a guess, so the
thing to defend is that it reports observations rather than extrapolations, and
says how many runs are behind each figure.
"""

from __future__ import annotations


from tests.conftest import requires_corpus

from sanhita.benchmark import BenchmarkReport, Measurement, format_report


def test_a_single_run_is_not_dressed_up_as_an_average():
    one = Measurement(stage="parse", seconds=1.2, runs=1)
    many = Measurement(stage="coverage", seconds=0.14, runs=5, spread=0.006)

    assert "+/-" not in one.line(), "one observation must not display a spread"
    assert "+/-" in many.line()


def test_sub_millisecond_work_is_reported_in_microseconds():
    """Rounding a 200us stage to 0ms would read as free, which it is not."""
    line = Measurement(stage="hash", seconds=0.0002).line()

    assert "us" in line
    assert "0.0 ms" not in line


def test_the_report_carries_its_caveats():
    report = BenchmarkReport(document="x.pdf")
    report.notes = ["cold means something specific"]
    report.add("parse", 1.0, "399 pages")

    text = format_report(report)

    assert "How to read this" in text
    assert "cold means something specific" in text


@requires_corpus
def test_the_benchmark_runs_end_to_end(corpus_pdf):
    """Every stage produces a figure, and none of them are zero or negative."""
    from sanhita.benchmark import run_benchmark

    report = run_benchmark(corpus_pdf, warm_runs=2)

    assert len(report.measurements) >= 10, "the pipeline has more stages than that"
    for measurement in report.measurements:
        assert measurement.seconds > 0, f"{measurement.stage} took no time at all"
        assert measurement.detail, f"{measurement.stage} says nothing about what it did"

    # The stages the README quotes have to exist by these names.
    for stage in (
        "Layout to clause tree, cold",
        "Extract obligations, deterministic",
        "Compute coverage",
        "Run every certified rule",
    ):
        assert report.get(stage) is not None, f"{stage} is missing"


@requires_corpus
def test_the_benchmark_serialises_for_the_readme(corpus_pdf):
    from sanhita.benchmark import run_benchmark

    payload = run_benchmark(corpus_pdf, warm_runs=2).to_json()

    assert payload["document"]
    assert payload["notes"], "a benchmark with no caveats is a marketing number"
    assert all("seconds" in m for m in payload["measurements"])

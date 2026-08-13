"""Evaluation: state extraction quality as a number, not a claim."""

from sanhita.eval.gold import GOLD_SET, GoldLabel, gold_by_clause
from sanhita.eval.harness import (
    EvalResult,
    MetricScore,
    run_eval,
)

__all__ = [
    "GOLD_SET",
    "EvalResult",
    "GoldLabel",
    "MetricScore",
    "gold_by_clause",
    "run_eval",
]

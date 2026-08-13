"""Scoring extraction against the gold set.

Five metrics, each with an explicit denominator:

  obligation detection  did we find a duty where a human found one?
  actor                 of the clauses both agree carry a duty, is the actor right?
  modality              ... is the deontic force right?
  deadline kind         ... is the temporal type right (including "no deadline")?
  evidence presence     ... did we require evidence where a human would?

The last four are **conditional on agreement about detection**. Scoring actor on
a clause we wrongly claimed carries a duty would double-count one mistake, and
would make a precision failure look like an actor failure.

Results are keyed by prompt (or ruleset) version so improvement across
iterations is visible rather than asserted.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from sanhita.compile.extract import ClauseOutcome, ExtractionStatus
from sanhita.eval.gold import GOLD_SET, GOLD_VERSION, GoldLabel, gold_by_clause
from sanhita.metrics.coverage import classify_clause
from sanhita.parse.clause_tree import ClauseTree

__all__ = ["EvalResult", "MetricScore", "run_eval"]


@dataclass(slots=True)
class MetricScore:
    """A confusion count plus the three ratios everyone asks for."""

    name: str
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    #: For the conditional metrics, which are accuracy over an agreed subset.
    correct: int = 0
    total: int = 0
    kind: str = "prf"  # "prf" or "accuracy"

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return round(self.true_positive / denominator, 4) if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return round(self.true_positive / denominator, 4) if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 4) if self.total else 0.0

    def as_dict(self) -> dict:
        if self.kind == "accuracy":
            return {
                "metric": self.name,
                "accuracy": self.accuracy,
                "correct": self.correct,
                "total": self.total,
            }
        return {
            "metric": self.name,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "tp": self.true_positive,
            "fp": self.false_positive,
            "fn": self.false_negative,
            "tn": self.true_negative,
        }


def _wrap(text: str, width: int) -> list[str]:
    """Wrap the gold-set note for a terminal, without pulling in textwrap's
    paragraph handling for one sentence."""
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


@dataclass(slots=True)
class EvalResult:
    engine: str
    version: str
    gold_version: str = GOLD_VERSION
    run_at: _dt.datetime = field(
        default_factory=lambda: _dt.datetime.now(_dt.timezone.utc)
    )
    metrics: dict[str, MetricScore] = field(default_factory=dict)
    #: Every clause where extraction and the human disagree, for inspection.
    disagreements: list[dict] = field(default_factory=list)
    missing_clauses: list[str] = field(default_factory=list)
    classifier_accuracy: float = 0.0
    #: Whether the seven arguable labels have been settled by a person. Until
    #: they are, the per-field figures below are computed but not published:
    #: a score measured against labels the extractor's author could have bent
    #: is circular reasoning with extra steps.
    gold_set_status: str = "AWAITING_HUMAN_RULINGS"
    gold_set_note: str = ""

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "version": self.version,
            "gold_version": self.gold_version,
            "run_at": self.run_at.isoformat(),
            "gold_size": len(GOLD_SET),
            "classifier_accuracy": self.classifier_accuracy,
            "gold_set_status": self.gold_set_status,
            "gold_set_note": self.gold_set_note,
            "publishable": self.publishable,
            "metrics": [m.as_dict() for m in self.metrics.values()],
            "disagreements": self.disagreements,
            "missing_clauses": self.missing_clauses,
        }

    @property
    def publishable(self) -> bool:
        """Whether these figures may appear on a slide."""
        return self.gold_set_status == "COMPLETE"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

    def table(self) -> str:
        """The table `sanhita eval` prints."""
        lines = [
            f"  gold set        {len(GOLD_SET)} clauses "
            f"({sum(1 for g in GOLD_SET if not g.has_obligation)} carry no obligation)",
            f"  engine          {self.engine}",
            f"  version         {self.version}",
            "",
            f"  {'metric'.ljust(24)}{'prec'.rjust(7)}{'rec'.rjust(8)}{'F1'.rjust(8)}"
            f"{'n'.rjust(7)}",
            f"  {'-' * 54}",
        ]
        for metric in self.metrics.values():
            if metric.kind == "accuracy":
                lines.append(
                    f"  {metric.name.ljust(24)}{'':>7}{'':>8}"
                    f"{metric.accuracy:>8.3f}{metric.total:>7}   (accuracy)"
                )
            else:
                lines.append(
                    f"  {metric.name.ljust(24)}{metric.precision:>7.3f}"
                    f"{metric.recall:>8.3f}{metric.f1:>8.3f}"
                    f"{metric.true_positive + metric.false_negative:>7}"
                )
        lines.append("")
        lines.append(
            f"  denominator classifier accuracy on the gold set: "
            f"{self.classifier_accuracy:.1%}"
        )
        lines.append("")
        lines.append(f"  gold set        {self.gold_set_status}")
        if not self.publishable:
            lines.append("")
            for chunk in _wrap(self.gold_set_note, 72):
                lines.append(f"  {chunk}")
            lines.append(
                "  The per-field figures above are computed and are NOT cleared "
                "for publication."
            )
        return "\n".join(lines)


def _primary(outcome: ClauseOutcome):
    """The obligation a metric is scored against: the first in source order.

    Not the highest-confidence one. A human labelling "what does this clause
    require?" reads the leading duty, so scoring against a later, incidental
    "may" buried in the same paragraph measures a different question than the
    one the gold set answers.
    """
    if not outcome.obligations:
        return None
    return outcome.obligations[0]


def run_eval(
    tree: ClauseTree,
    extractor,
    *,
    engine: str | None = None,
    version: str | None = None,
) -> EvalResult:
    """Score `extractor` over the gold set."""
    from sanhita.eval.rulings import read_rulings

    signoff = read_rulings()
    gold = gold_by_clause()
    result = EvalResult(
        gold_set_status=signoff.state,
        gold_set_note=signoff.describe(),
        engine=engine or getattr(extractor, "engine", "unknown"),
        version=version
        or getattr(extractor, "ruleset_version", None)
        or getattr(extractor, "model", "unknown"),
    )

    detection = MetricScore("obligation detection")
    actor = MetricScore("actor", kind="accuracy")
    modality = MetricScore("modality", kind="accuracy")
    deadline = MetricScore("deadline kind", kind="accuracy")
    evidence = MetricScore("evidence presence", kind="accuracy")
    classifier_correct = 0
    classifier_total = 0

    for label in GOLD_SET:
        node = tree.get(label.clause_id)
        if node is None:
            result.missing_clauses.append(label.clause_id)
            continue

        # The coverage denominator's classifier is scored here too — a coverage
        # ratio built on an unmeasured classifier is not a defensible number.
        classifier_total += 1
        if classify_clause(node).in_denominator == label.has_obligation:
            classifier_correct += 1

        outcome = extractor.extract(node)
        found = outcome.status is ExtractionStatus.PROPOSED and bool(outcome.obligations)

        if found and label.has_obligation:
            detection.true_positive += 1
        elif found and not label.has_obligation:
            detection.false_positive += 1
            result.disagreements.append(
                {
                    "clause": label.clause_id,
                    "kind": "false positive",
                    "gold": "no obligation",
                    "got": f"{len(outcome.obligations)} obligation(s)",
                    "note": label.note,
                }
            )
        elif not found and label.has_obligation:
            detection.false_negative += 1
            result.disagreements.append(
                {
                    "clause": label.clause_id,
                    "kind": "false negative",
                    "gold": f"{label.modality.value if label.modality else 'duty'}",
                    "got": outcome.reason,
                    "note": label.note,
                }
            )
        else:
            detection.true_negative += 1

        # Field metrics only where both sides agree a duty exists.
        if not (found and label.has_obligation):
            continue
        proposal = _primary(outcome)
        if proposal is None:
            continue

        if label.actor is not None:
            actor.total += 1
            if proposal.actor is label.actor:
                actor.correct += 1
            else:
                result.disagreements.append(
                    {
                        "clause": label.clause_id,
                        "kind": "actor",
                        "gold": label.actor.value,
                        "got": proposal.actor.value,
                    }
                )

        if label.modality is not None:
            modality.total += 1
            if proposal.modality is label.modality:
                modality.correct += 1
            else:
                result.disagreements.append(
                    {
                        "clause": label.clause_id,
                        "kind": "modality",
                        "gold": label.modality.value,
                        "got": proposal.modality.value,
                    }
                )

        deadline.total += 1
        got_kind = proposal.deadline.kind if proposal.deadline else None
        if got_kind is label.deadline_kind:
            deadline.correct += 1
        else:
            result.disagreements.append(
                {
                    "clause": label.clause_id,
                    "kind": "deadline",
                    "gold": label.deadline_kind.value if label.deadline_kind else "none",
                    "got": got_kind.value if got_kind else "none",
                }
            )

        evidence.total += 1
        if bool(proposal.evidence) == label.expects_evidence:
            evidence.correct += 1
        else:
            result.disagreements.append(
                {
                    "clause": label.clause_id,
                    "kind": "evidence",
                    "gold": label.expects_evidence,
                    "got": bool(proposal.evidence),
                }
            )

    result.metrics = {
        m.name: m for m in (detection, actor, modality, deadline, evidence)
    }
    result.classifier_accuracy = (
        round(classifier_correct / classifier_total, 4) if classifier_total else 0.0
    )
    return result

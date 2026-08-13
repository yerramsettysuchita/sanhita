"""Generated compliance events, for demonstrating the engine.

No firm is going to hand us their books, so the engine needs something to run
against. This module produces that something, under three constraints that keep
it from becoming the kind of seeded data the rest of the product exists to
refuse.

  **It is labelled.** Every store carries a label that names it as generated and
  states the seed. That label is printed on every gap report built from it, so a
  reader can never mistake one of these runs for a real one.

  **It is deterministic.** The same rules and the same seed produce byte
  identical events. There is no wall clock and no unseeded randomness, so a
  reviewer can regenerate the exact set that produced a report.

  **It derives from the rules, not from a wish list.** Events are generated only
  for rules that were actually certified, dated against the deadline the clause
  actually sets. Nothing here invents an obligation, a clause, or a deadline.

The breach rate is an input, not a discovery. Nobody should read "12% late" off
a generated store and think they have learned something about the market.
"""

from __future__ import annotations

import datetime as _dt
import hashlib

from sanhita.execute.calendar import TradingCalendar
from sanhita.execute.engine import due_date
from sanhita.ir.enums import DayCount, DeadlineKind, Modality, RuleStatus
from sanhita.execute.evidence import ComplianceEvent, EvidenceStore
from sanhita.ir.schema import Obligation

__all__ = ["generate"]

#: Entities the events are spread across. Deliberately obvious placeholders.
_ENTITIES = [
    "Demo Broking Pvt Ltd",
    "Example Securities Ltd",
    "Sample Capital Markets Ltd",
]


def _roll(seed: str, *parts: object) -> float:
    """A stable pseudo-random number in [0, 1) from a seed and some inputs.

    A hash rather than ``random``, so the value depends only on its arguments
    and cannot be disturbed by anything else drawing from a shared generator.
    """
    material = "|".join([seed, *(str(p) for p in parts)]).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def generate(
    obligations: list[Obligation],
    *,
    calendar: TradingCalendar,
    start: _dt.date,
    end: _dt.date,
    seed: str = "sanhita-demo",
    events_per_rule: int = 6,
    miss_rate: float = 0.08,
    late_rate: float = 0.14,
) -> EvidenceStore:
    """Build a store of compliance events for the certified rules given.

    ``miss_rate`` and ``late_rate`` are the proportion of events that will be
    unfiled and filed late. They are stated on the store's label so no reader
    mistakes them for a measurement.
    """
    if not 0 <= miss_rate <= 1 or not 0 <= late_rate <= 1:
        raise ValueError("miss_rate and late_rate are proportions between 0 and 1")
    if start > end:
        raise ValueError("the window starts after it ends")

    label = (
        f"generated, not real books. seed={seed!r}, "
        f"{events_per_rule} events per rule over {start.isoformat()} to "
        f"{end.isoformat()}, {miss_rate:.0%} unfiled and {late_rate:.0%} late by construction"
    )
    store = EvidenceStore(label=label)

    usable = [
        o
        for o in obligations
        if o.status is RuleStatus.CERTIFIED
        and o.modality in {Modality.MUST, Modality.SHOULD}
        and o.deadline is not None
        and o.deadline.kind is not DeadlineKind.ON_DEMAND
        and o.evidence
        and not (
            o.deadline.business_days is DayCount.UNSPECIFIED and o.deadline.offset_days
        )
    ]

    span = (end - start).days or 1
    counter = 0

    for obligation in sorted(usable, key=lambda o: o.id):
        artifact = obligation.evidence[0].artifact_type
        for index in range(events_per_rule):
            offset = int(_roll(seed, obligation.id, index, "when") * span)
            occurred = start + _dt.timedelta(days=offset)

            try:
                due = due_date(obligation.deadline, occurred, calendar)
            except Exception:  # noqa: BLE001 - a rule we cannot date, we skip
                continue

            counter += 1
            entity = _ENTITIES[
                int(_roll(seed, obligation.id, index, "who") * len(_ENTITIES))
            ]

            verdict = _roll(seed, obligation.id, index, "outcome")
            if verdict < miss_rate:
                filed: _dt.date | None = None
                reference = None
            elif verdict < miss_rate + late_rate:
                slip = 1 + int(_roll(seed, obligation.id, index, "slip") * 9)
                filed = due + _dt.timedelta(days=slip)
                reference = f"REF-{counter:05d}"
            else:
                early = int(_roll(seed, obligation.id, index, "early") * 2)
                filed = due - _dt.timedelta(days=early)
                if filed < occurred:
                    filed = occurred
                reference = f"REF-{counter:05d}"

            store.add(
                ComplianceEvent(
                    id=f"EV-{counter:05d}",
                    obligation_id=obligation.id,
                    entity=entity,
                    occurred_on=occurred,
                    artifact_type=artifact,
                    filed_on=filed,
                    reference=reference,
                )
            )

    return store

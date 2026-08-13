"""How long it took to get from a published circular to a rule that runs.

Problem Statement 2 sets its own success test in one sentence:

    "It should demonstrably reduce the gap between regulatory issuance and
     operational compliance action."

That is a measurement, and nothing in the product measured it. This module does,
from timestamps that were already being written for other reasons: every
proposal records ``ExtractionMeta.extracted_at``, every signature records
``Certification.certified_at``, and the audit ledger records the moment of every
transition. None of it was added for this screen, which is the point. The
numbers are a by-product of provenance the product already keeps.

**What is measured and what is not.**

The interval from SEBI publishing a circular to Sanhita first reading it is not
a measurement of Sanhita. It is a measurement of when somebody got round to
running it, and on the worked example that is over a year, because the corpus
was downloaded long after it was issued. Reporting that as a latency figure
would be dishonest in the flattering direction, so it is separated out, labelled
as shelf time, and excluded from every headline.

What is measured is the work: from the first rule proposed to the last, and from
the first rule proposed to the first rule a named person signed. Those two
intervals are what a firm would actually experience, and both trace to
timestamps a sceptic can read out of the store.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from sanhita.ir.enums import RuleStatus
from sanhita.ir.schema import Obligation

__all__ = ["Milestone", "LatencyReport", "measure_latency"]


def _aware(moment: _dt.datetime) -> _dt.datetime:
    """Ledger and store timestamps predate the tz-aware rule in places."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_dt.timezone.utc)
    return moment


def humanise(delta: _dt.timedelta | None) -> str:
    """A duration a person can read, at the precision the duration deserves."""
    if delta is None:
        return "not recorded"
    exact = delta.total_seconds()
    if exact < 0:
        return "not recorded"
    # The deterministic extractor finishes a whole circular inside a second, so
    # rounding to integer seconds would print "0 seconds" for the single most
    # striking measurement in the product.
    if exact < 10:
        return f"{exact:.1f} seconds"
    seconds = int(exact)
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        rest = minutes % 60
        head = f"{hours} hour{'s' if hours != 1 else ''}"
        return f"{head} {rest} min" if rest else head
    days = hours // 24
    if days < 90:
        return f"{days} days"
    months = days // 30
    return f"about {months} months"


@dataclass(frozen=True)
class Milestone:
    """One recorded moment in the life of this document."""

    key: str
    label: str
    at: _dt.datetime | None
    detail: str
    #: True where the moment is a fact about the regulation rather than about
    #: anything Sanhita did.
    external: bool = False

    @property
    def recorded(self) -> bool:
        return self.at is not None


@dataclass
class LatencyReport:
    """The pipeline's own clock, with the parts we cannot claim credit for cut out."""

    issued_on: _dt.date | None = None
    milestones: list[Milestone] = field(default_factory=list)

    rules_proposed: int = 0
    rules_certified: int = 0

    #: Which extractor produced these rules, counted. A timing figure means
    #: something different depending on the engine behind it, so the screen
    #: shows this next to the number rather than letting a reader assume.
    engines: dict[str, int] = field(default_factory=dict)

    #: First and last proposal, and first signature.
    first_proposed_at: _dt.datetime | None = None
    last_proposed_at: _dt.datetime | None = None
    first_certified_at: _dt.datetime | None = None
    last_certified_at: _dt.datetime | None = None

    def _gap(
        self, start: _dt.datetime | None, end: _dt.datetime | None
    ) -> _dt.timedelta | None:
        if start is None or end is None:
            return None
        delta = _aware(end) - _aware(start)
        return delta if delta.total_seconds() >= 0 else None

    # -- the two intervals worth quoting

    @property
    def compile_window(self) -> _dt.timedelta | None:
        """First rule proposed to last rule proposed."""
        return self._gap(self.first_proposed_at, self.last_proposed_at)

    @property
    def time_to_first_certified(self) -> _dt.timedelta | None:
        """First rule proposed to the first rule a named person signed.

        This is the headline: the interval between a machine reading the
        regulation and a human making something operational out of it.
        """
        return self._gap(self.first_proposed_at, self.first_certified_at)

    @property
    def certification_window(self) -> _dt.timedelta | None:
        return self._gap(self.first_certified_at, self.last_certified_at)

    # -- the interval that is not ours

    @property
    def shelf_time(self) -> _dt.timedelta | None:
        """Issuance to first read. A fact about us running it late, nothing more."""
        if self.issued_on is None or self.first_proposed_at is None:
            return None
        issued = _dt.datetime.combine(
            self.issued_on, _dt.time(0, 0), tzinfo=_dt.timezone.utc
        )
        return self._gap(issued, self.first_proposed_at)

    @property
    def rules_per_second(self) -> float | None:
        """Extraction throughput.

        Reported per second rather than per minute because the deterministic
        extractor finishes the whole circular in well under a minute, and a
        per-minute figure over a sub-minute window is an extrapolation rather
        than a measurement.
        """
        window = self.compile_window
        if window is None or not self.rules_proposed:
            return None
        elapsed = window.total_seconds()
        if elapsed <= 0:
            return None
        return round(self.rules_proposed / elapsed, 1)

    @property
    def engine_summary(self) -> str:
        """A sentence naming what produced these timings."""
        if not self.engines:
            return "No extraction metadata recorded."
        parts = ", ".join(
            f"{count} by the {name} extractor"
            for name, count in sorted(self.engines.items(), key=lambda kv: -kv[1])
        )
        return parts

    def caveats(self) -> list[str]:
        notes = [
            "Every moment here was recorded when the work happened, for "
            "provenance. Nothing on this screen is timed for the purpose of "
            "being shown on it.",
            "The interval from the circular being issued to Sanhita first "
            "reading it measures when we got round to running it, not how fast "
            "anything is. It is shown separately and kept out of every headline.",
        ]
        if self.engines:
            notes.append(
                "Extraction engine: " + self.engine_summary + ". A deterministic "
                "extraction and a model-drafted one carry very different "
                "warranties, and a timing figure is meaningless without knowing "
                "which one produced it."
            )
        if self.first_certified_at is None:
            notes.append(
                "No rule in this document has been certified, so there is no "
                "time-to-operational figure yet."
            )
        notes.append(
            "Certification is a human act done in working sessions, so the "
            "certification window includes the hours nobody was at the desk. It "
            "is elapsed time, not effort."
        )
        return notes

    def to_json(self) -> dict:
        return {
            "issued_on": self.issued_on.isoformat() if self.issued_on else None,
            "rules_proposed": self.rules_proposed,
            "rules_certified": self.rules_certified,
            "compile_window_seconds": (
                self.compile_window.total_seconds() if self.compile_window else None
            ),
            "time_to_first_certified_seconds": (
                self.time_to_first_certified.total_seconds()
                if self.time_to_first_certified
                else None
            ),
            "rules_per_second": self.rules_per_second,
            "engines": dict(self.engines),
            "milestones": [
                {
                    "key": m.key,
                    "label": m.label,
                    "at": m.at.isoformat() if m.at else None,
                    "detail": m.detail,
                    "external": m.external,
                }
                for m in self.milestones
            ],
            "caveats": self.caveats(),
        }


def measure_latency(
    obligations: list[Obligation],
    *,
    issued_on: _dt.date | None = None,
) -> LatencyReport:
    """Read the pipeline's clock out of the rules it produced."""
    report = LatencyReport(issued_on=issued_on)

    proposed_at: list[_dt.datetime] = []
    certified_at: list[_dt.datetime] = []

    for obligation in obligations:
        # A superseded version is a historical artifact, not a rule that was
        # produced for this rulebook. A rejected one was produced and then
        # refused, so it still counts as work the extractor did.
        if obligation.status is RuleStatus.SUPERSEDED:
            continue
        report.rules_proposed += 1
        if obligation.extraction is not None:
            proposed_at.append(_aware(obligation.extraction.extracted_at))
            engine = obligation.extraction.engine
            report.engines[engine] = report.engines.get(engine, 0) + 1
        if obligation.status is RuleStatus.CERTIFIED and obligation.certification:
            report.rules_certified += 1
            certified_at.append(_aware(obligation.certification.certified_at))

    if proposed_at:
        report.first_proposed_at = min(proposed_at)
        report.last_proposed_at = max(proposed_at)
    if certified_at:
        report.first_certified_at = min(certified_at)
        report.last_certified_at = max(certified_at)

    report.milestones = [
        Milestone(
            key="issued",
            label="SEBI issued the circular",
            at=(
                _dt.datetime.combine(issued_on, _dt.time(0, 0), tzinfo=_dt.timezone.utc)
                if issued_on
                else None
            ),
            detail="The date on the document itself.",
            external=True,
        ),
        Milestone(
            key="first_proposed",
            label="First rule proposed",
            at=report.first_proposed_at,
            detail="Sanhita began reading the regulation.",
        ),
        Milestone(
            key="last_proposed",
            label="Rulebook complete",
            at=report.last_proposed_at,
            detail=f"{report.rules_proposed} rules drawn from the text.",
        ),
        Milestone(
            key="first_certified",
            label="First rule signed",
            at=report.first_certified_at,
            detail="A named person made one rule operational.",
        ),
        Milestone(
            key="last_certified",
            label="Most recent signature",
            at=report.last_certified_at,
            detail=f"{report.rules_certified} rules signed so far.",
        ),
    ]
    return report

"""One row per firm, for the person who has to look at all of them.

The supervisor screen already existed and it counted the wrong thing. It called
each workspace a firm, and a workspace is a document: one broker declaring three
rulebooks appeared as three firms, and a circular nobody had attached a company
to appeared as a firm with no name. Every number on the screen was a fact about
documents wearing a label that said firms.

This module builds the other view, the one the label was promising. A firm here
is a company profile somebody recorded, against a framework, with whatever
position was last taken on it.

**What a supervisor is looking at, said plainly.** These are the firms on this
installation, which is not a market and not SEBI's book of registered
intermediaries. On a machine with one company it says so rather than drawing a
sector. That caveat is not modesty; a supervisory screen that implies coverage
it does not have is worse than no screen, because the gaps in it are invisible.

Nothing here recomputes a position. The number beside a firm is the one that
firm's own recorded assessment produced, carrying its date, and a firm whose
records have moved since shows no number at all rather than a stale one. Two
screens disagreeing about whether a firm is compliant is exactly the failure the
assessment record was introduced to end.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

__all__ = ["FirmRow", "SupervisoryView", "build_view"]


@dataclass(frozen=True)
class FirmRow:
    """One firm, against one framework, as last recorded."""

    firm: str
    intermediary: str
    framework_id: str
    framework_name: str
    certified: int = 0

    #: The recorded assessment, or nothing. ``None`` throughout means this firm
    #: has never been assessed, or has been but its inputs have moved since.
    #:
    #: The three counts are copied from the run rather than recomputed, and
    #: they are the run's own vocabulary: what the engine actually checked, how
    #: much of it was met on time, and how much was not. Renaming them here to
    #: something friendlier would let this screen and the firm's own overview
    #: disagree about what a number means.
    assessed_at: _dt.datetime | None = None
    evaluated: int | None = None
    satisfied: int | None = None
    breaches: int | None = None
    #: True when a run exists but no longer matches the records in front of us.
    stale: bool = False

    open_tasks: int = 0
    records: int = 0
    #: Days since the most recent compliance record. ``None`` where there are
    #: no records at all.
    days_since_record: int | None = None

    @property
    def has_position(self) -> bool:
        return self.assessed_at is not None and not self.stale and self.evaluated is not None

    @property
    def checked(self) -> int:
        """What the run could actually reach a verdict on."""
        return (self.satisfied or 0) + (self.breaches or 0)

    @property
    def position(self) -> int | None:
        """Percentage met on time, over what was checkable. Nothing otherwise.

        The same arithmetic as ``AssessmentRun.compliance_rate``, deliberately,
        so this screen and the firm's own overview cannot disagree.
        """
        if not self.has_position or not self.checked:
            return None
        return round((self.satisfied or 0) / self.checked * 100)

    @property
    def state(self) -> str:
        """One word for what is known about this firm, for sorting and colour."""
        if self.stale:
            return "WITHDRAWN"
        if not self.has_position:
            return "NEVER_ASSESSED"
        if self.breaches:
            return "FAILING"
        return "CLEAN"

    @property
    def rank(self) -> int:
        """Worst first. A firm nobody has assessed outranks a clean one,
        because an unknown position is a supervisory problem and a clean one
        is not."""
        return {"FAILING": 0, "WITHDRAWN": 1, "NEVER_ASSESSED": 2, "CLEAN": 3}[self.state]

    def describe(self) -> str:
        if self.state == "NEVER_ASSESSED":
            return (
                f"{self.firm} has never been assessed against "
                f"{self.framework_name}. No position exists to report."
            )
        if self.state == "WITHDRAWN":
            when = self.assessed_at.date().isoformat() if self.assessed_at else "an earlier date"
            return (
                f"{self.firm} was assessed on {when}, but its rulebook or its "
                "records have changed since, so that result is history rather "
                "than its current position."
            )
        when = self.assessed_at.date().isoformat() if self.assessed_at else ""
        if self.state == "FAILING":
            return (
                f"{self.firm} was assessed on {when} against "
                f"{self.framework_name}: {self.breaches} of {self.checked} "
                "checkable duties had a finding against them."
            )
        return (
            f"{self.firm} was assessed on {when} against {self.framework_name} "
            f"with no findings across {self.checked} checkable duties."
        )


@dataclass
class SupervisoryView:
    """Every firm on this installation, and what is known about each."""

    rows: list[FirmRow] = field(default_factory=list)
    #: Documents in view that carry no company profile at all. Counted rather
    #: than dropped, because a circular nobody attached a firm to is a fact
    #: about this installation that a supervisor should see.
    documents_without_a_firm: int = 0

    @property
    def firms(self) -> int:
        """Distinct companies, not rows. One firm with three frameworks is one
        firm, and the old screen counted it as three."""
        return len({r.firm for r in self.rows})

    @property
    def assessed(self) -> int:
        return sum(1 for r in self.rows if r.has_position)

    @property
    def never_assessed(self) -> int:
        return sum(1 for r in self.rows if r.state == "NEVER_ASSESSED")

    @property
    def withdrawn(self) -> int:
        return sum(1 for r in self.rows if r.stale)

    @property
    def failing(self) -> int:
        return sum(1 for r in self.rows if r.state == "FAILING")

    @property
    def open_tasks(self) -> int:
        return sum(r.open_tasks for r in self.rows)

    @property
    def is_a_single_firm(self) -> bool:
        return self.firms <= 1

    def by_firm(self) -> dict[str, list[FirmRow]]:
        """One firm's frameworks together, which is how a supervisor reads it."""
        grouped: dict[str, list[FirmRow]] = {}
        for row in self.rows:
            grouped.setdefault(row.firm, []).append(row)
        return dict(
            sorted(grouped.items(), key=lambda kv: (min(r.rank for r in kv[1]), kv[0]))
        )

    def headline(self) -> str:
        if not self.rows:
            return "No company profile has been recorded on this installation."
        if self.is_a_single_firm:
            return (
                f"One firm on this installation, across {len(self.rows)} "
                "declared framework(s). This is not a sector view."
            )
        parts = [f"{self.firms} firms"]
        if self.failing:
            parts.append(f"{self.failing} with findings of record")
        if self.never_assessed:
            parts.append(f"{self.never_assessed} never assessed")
        if self.withdrawn:
            parts.append(f"{self.withdrawn} whose position has been withdrawn")
        return ", ".join(parts) + "."


def build_view(entries) -> SupervisoryView:
    """Assemble the view from what each workspace already keeps beside itself.

    ``entries`` is an iterable of dicts, one per visible workspace. Everything
    in them is read from a sidecar file; nothing here parses a PDF or runs the
    engine, because a supervisory screen that re-derives twelve firms' positions
    on every load is a screen that times out.
    """
    view = SupervisoryView()
    for entry in entries:
        company = entry.get("company")
        if company is None or not getattr(company, "name", ""):
            view.documents_without_a_firm += 1
            continue

        run = entry.get("recorded")
        latest = entry.get("latest_run")
        stale = latest is not None and run is None
        source = run or (latest if stale else None)
        view.rows.append(
            FirmRow(
                firm=company.name,
                intermediary=getattr(
                    getattr(company, "intermediary", None), "value", ""
                )
                or "",
                framework_id=entry.get("framework_id", ""),
                framework_name=entry.get("framework_name", ""),
                certified=entry.get("certified", 0),
                assessed_at=getattr(source, "ran_at", None),
                evaluated=getattr(source, "rules_evaluated", None),
                satisfied=getattr(source, "satisfied", None),
                breaches=getattr(source, "breaches", None),
                stale=stale,
                open_tasks=entry.get("open_tasks", 0),
                records=entry.get("records", 0),
                days_since_record=entry.get("days_since_record"),
            )
        )

    view.rows.sort(key=lambda r: (r.rank, r.firm, r.framework_name))
    return view

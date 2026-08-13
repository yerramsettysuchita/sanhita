"""If there are two of us and eighty things, what do we do on Monday?

The problem statement singles out "smaller intermediaries with limited
compliance resources", and the honest reading of that is not a discount or a
simplified mode. It is that the same regulation lands on a team of two as on a
team of forty, and the team of two cannot triage eighty items by reading them.

So this ranks the open work. It is arithmetic over facts the product already
holds, and the formula is written out below rather than tuned until the demo
looks good.

**It is not a risk opinion.** A high score here means several factors line up:
a duty is overdue, nobody owns it, its records stopped arriving, it recurs
often. Whether that is the firm's biggest legal exposure is a judgement this
product does not make and should not imply. Every screen carrying a score says
so.

**It is deterministic.** Same inputs, same order, every time. A priority list
that reshuffles between two page loads teaches people to ignore it, and a
ranking nobody can reproduce is one nobody can be held to.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum

__all__ = ["Band", "Ranked", "WorkList", "FACTORS", "rank_open_work"]


#: The scoring, stated once and used everywhere. Written here rather than
#: buried in the function so a compliance officer can argue with it, which is
#: the only useful thing to do with somebody else's priority formula.
FACTORS: tuple[tuple[str, int, str], ...] = (
    ("Overdue", 40, "the date the firm set for this has passed"),
    ("Falls due within 7 days", 25, "there is a deadline close enough to miss"),
    ("Falls due within 30 days", 10, "far enough away to plan, close enough to start"),
    ("A signature no longer covers the clause", 30,
     "the rule cannot produce a finding until somebody signs it again"),
    ("Marked high priority", 20, "somebody at the firm already said so"),
    ("Records have stopped arriving", 20,
     "the duty keeps falling due and nothing is being recorded against it"),
    ("Never recorded at all", 15, "no record of this duty has ever been uploaded"),
    ("Nobody owns it", 15, "an item with no team is one nobody will pick up"),
    ("Recurs monthly or more often", 10,
     "the cost of leaving it repeats, rather than waiting for you"),
    ("Reopened after a failed re-check", 15,
     "the fix was attempted and the engine still finds a breach"),
)


class Band(str, Enum):
    """Three bands, because a team of two cannot act on eleven."""

    NOW = "NOW"
    SOON = "SOON"
    WHEN_YOU_CAN = "WHEN_YOU_CAN"

    @property
    def label(self) -> str:
        return {
            Band.NOW: "Do this week",
            Band.SOON: "Do this month",
            Band.WHEN_YOU_CAN: "When you can",
        }[self]


def _band(score: int) -> Band:
    if score >= 55:
        return Band.NOW
    if score >= 25:
        return Band.SOON
    return Band.WHEN_YOU_CAN


@dataclass(frozen=True)
class Ranked:
    """One piece of open work, with the reasons it scored what it did."""

    title: str
    score: int
    band: Band
    #: Which factors fired, in the words above. The score is never shown
    #: without them: a number on its own invites being trusted.
    reasons: tuple[str, ...] = ()
    clause_id: str = ""
    obligation_id: str = ""
    owner: str = ""
    team: str = ""
    due_date: _dt.date | None = None
    #: Where to go to act on it.
    link: str = ""
    kind: str = "TASK"

    @property
    def is_owned(self) -> bool:
        return bool(self.owner or self.team)

    def describe(self) -> str:
        who = f" It sits with {self.owner or self.team}." if self.is_owned else (
            " Nobody owns it yet."
        )
        because = "; ".join(self.reasons) if self.reasons else "no factor fired"
        return f"{because.capitalize()}.{who}"


@dataclass
class WorkList:
    """The open work, in the order a small team should take it."""

    as_of: _dt.date
    items: list[Ranked] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    def of(self, band: Band | str) -> list[Ranked]:
        wanted = Band(band)
        return [i for i in self.items if i.band is wanted]

    @property
    def top(self) -> list[Ranked]:
        """The five a team of two can actually hold in their heads."""
        return self.items[:5]

    def headline(self) -> str:
        if not self.items:
            return "There is no open compliance work for this firm."
        now = len(self.of(Band.NOW))
        if now:
            return (
                f"{now} of {self.total} open items are overdue, close to a "
                "deadline, or on a rule nobody can run."
            )
        return f"{self.total} open item(s), none of them urgent this week."


def _score_task(task, today: _dt.date) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if task.is_overdue(today):
        score += 40
        reasons.append("it is overdue")
    else:
        remaining = task.days_remaining(today)
        if remaining is not None and 0 <= remaining <= 7:
            score += 25
            reasons.append(f"it falls due in {remaining} day(s)")
        elif remaining is not None and remaining <= 30:
            score += 10
            reasons.append("it falls due within the month")

    if getattr(task, "change_kind", "") in ("RECERTIFY", "WITHDRAW", "REPOINT"):
        score += 30
        reasons.append("a signature no longer covers the clause it names")
    if getattr(task.priority, "value", "") == "HIGH":
        score += 20
        reasons.append("the firm marked it high priority")
    if getattr(task.status, "value", "") == "REOPENED":
        score += 15
        reasons.append("the fix was attempted and the rule still finds a breach")
    if not (task.owner or task.assigned_team):
        score += 15
        reasons.append("nobody owns it")
    return score, reasons


def _score_health(rule) -> tuple[int, list[str]]:
    from sanhita.health import Signal

    score, reasons = 0, []
    if rule.signal is Signal.GONE_QUIET:
        score += 20
        reasons.append("its records have stopped arriving")
    elif rule.signal is Signal.NEVER_RECORDED:
        score += 15
        reasons.append("no record of this duty has ever been uploaded")
    elif rule.signal is Signal.UNFILED:
        score += 10
        reasons.append("recorded occasions name no filing date")
    if (rule.period or "").upper() in ("DAY", "WEEK", "MONTH"):
        score += 10
        reasons.append("it recurs monthly or more often")
    if not rule.is_owned:
        score += 15
        reasons.append("nobody owns it")
    return score, reasons


def rank_open_work(
    *,
    tasks=(),
    health=None,
    base: str = "",
    as_of: _dt.date | None = None,
) -> WorkList:
    """Put every open item in one list, worst first.

    ``tasks`` are open remediation tasks; ``health`` is an
    :class:`sanhita.health.EvidenceHealth`. Both are things the firm already
    has, deliberately: a priority list built from a separate store is one that
    can disagree with the screens it claims to summarise.
    """
    today = as_of or _dt.date.today()
    work = WorkList(as_of=today)

    for task in tasks:
        if not task.status.is_open:
            continue
        score, reasons = _score_task(task, today)
        work.items.append(
            Ranked(
                title=task.title or f"Task on clause {task.clause_id}",
                score=score,
                band=_band(score),
                reasons=tuple(reasons),
                clause_id=task.clause_id,
                obligation_id=task.obligation_id,
                owner=task.owner,
                team=task.assigned_team,
                due_date=task.due_date,
                link=f"{base}/remediation#{task.task_id}",
                kind="AMENDMENT" if getattr(task, "change_kind", "") else "TASK",
            )
        )

    if health is not None:
        for rule in health.rules:
            if not rule.signal.needs_attention:
                continue
            score, reasons = _score_health(rule)
            work.items.append(
                Ranked(
                    title=rule.requirement or f"Duty on clause {rule.clause_id}",
                    score=score,
                    band=_band(score),
                    reasons=tuple(reasons),
                    clause_id=rule.clause_id,
                    obligation_id=rule.obligation_id,
                    team=rule.function,
                    link=f"{base}/review",
                    kind="EVIDENCE",
                )
            )

    # Deterministic all the way down: score, then clause, then id. A list that
    # reshuffles between two page loads is one nobody can be held to.
    work.items.sort(key=lambda i: (-i.score, i.clause_id, i.obligation_id, i.title))
    return work

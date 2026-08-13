"""Five firms that do not exist, labelled as such on every screen they touch.

A supervisory view over one firm demonstrates the plumbing and not the point.
The point is what a regulator could see that nobody can see today: the same
clause unmet at four firms out of five, or two firms that read one paragraph
differently and both filed accordingly.

Showing that needs several firms. This installation has one, and inventing real
ones would be the exact fabrication the whole product is built against. So the
firms here are synthetic, they are named so nobody could mistake them for real
intermediaries, and every screen that renders them says so before it says
anything else.

**It is never mixed with the firm's own data.** Not written to the store, not
merged into the supervisor's real rows, not counted in any figure the product
publishes. It is generated on demand from the constant below, so there is no
file anywhere that a later reader could find and mistake for records.

**It is reproducible.** The same firms, the same positions, the same divergence
every time, because a demonstration that differs between two runs is one nobody
can check.

**What it demonstrates is the architecture, not a finding.** No claim is made
that 60% of Indian stock brokers miss this clause. The claim is narrower and
true: if five firms were compiled against one certified rulebook, this is the
question a supervisor could ask and the shape of the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "SYNTHETIC_LABEL",
    "DemoFirm",
    "SharedGap",
    "ObservedDivergence",
    "SyntheticMarket",
    "synthetic_market",
]

#: Printed on every screen that shows any of this, before anything else.
SYNTHETIC_LABEL = "Synthetic demonstration data, not real firms and not market data"


@dataclass(frozen=True)
class DemoFirm:
    """One firm that does not exist."""

    name: str
    intermediary: str
    certified: int
    checked: int
    satisfied: int
    open_tasks: int
    days_since_record: int | None

    @property
    def breaches(self) -> int:
        return self.checked - self.satisfied

    @property
    def position(self) -> int | None:
        if not self.checked:
            return None
        return round(self.satisfied / self.checked * 100)

    @property
    def state(self) -> str:
        if self.checked == 0:
            return "NEVER_ASSESSED"
        return "FAILING" if self.breaches else "CLEAN"


@dataclass(frozen=True)
class SharedGap:
    """One duty that several of the firms are failing at once.

    This is the signal that does not exist today. A regulator can see one
    firm's filings; nobody can see that four of five firms read the same
    paragraph and none of them filed against it.
    """

    clause_id: str
    obligation_id: str
    requirement: str
    firms: tuple[str, ...]
    total_firms: int

    @property
    def share(self) -> int:
        return round(len(self.firms) / self.total_firms * 100)

    def describe(self) -> str:
        return (
            f"{len(self.firms)} of {self.total_firms} firms have an open finding "
            f"on clause {self.clause_id}. A gap at one firm is that firm's "
            "problem; the same gap at most of them is usually the clause."
        )


@dataclass(frozen=True)
class ObservedDivergence:
    """One clause two firms mapped to different duties.

    Distinct from the divergence engine, which *predicts* which clauses will be
    read two ways from their own text. This is the other thing: firms that have
    actually recorded different readings. The two must never be presented as
    one, and here it is synthetic, so it is labelled twice.
    """

    clause_id: str
    requirement: str
    readings: tuple[tuple[str, str], ...]

    @property
    def camps(self) -> int:
        return len({reading for _, reading in self.readings})

    def describe(self) -> str:
        return (
            f"Clause {self.clause_id} has been read {self.camps} different ways "
            f"across {len(self.readings)} firms. Every one of them filed "
            "accordingly, so all of them believe they comply."
        )


@dataclass
class SyntheticMarket:
    """The whole demonstration, and the caveat that travels with it."""

    label: str = SYNTHETIC_LABEL
    firms: list[DemoFirm] = field(default_factory=list)
    shared_gaps: list[SharedGap] = field(default_factory=list)
    divergences: list[ObservedDivergence] = field(default_factory=list)

    @property
    def assessed(self) -> int:
        return sum(1 for f in self.firms if f.state != "NEVER_ASSESSED")

    @property
    def failing(self) -> int:
        return sum(1 for f in self.firms if f.state == "FAILING")

    def headline(self) -> str:
        return (
            f"{len(self.firms)} synthetic firms against one certified rulebook. "
            f"{self.failing} carry findings, {len(self.firms) - self.assessed} have "
            f"never been assessed, and {len(self.shared_gaps)} duties are unmet at "
            "more than half of them."
        )

    def caveat(self) -> str:
        return (
            "None of these firms exist. The figures are generated from a fixed "
            "table so the same demonstration appears every time, and none of "
            "them is written to this installation's records or counted in any "
            "number Sanhita publishes. What is being demonstrated is the "
            "question a supervisor could ask once several firms are compiled "
            "against one certified rulebook, not a finding about the market."
        )


#: Deliberately unmistakable. "Firm A" cannot be confused with a registered
#: intermediary; "Meridian Securities" could be, and somebody would eventually
#: screenshot it without the caveat.
_FIRMS = (
    ("Firm A", "STOCK_BROKER", 183, 45, 42, 2, 3),
    ("Firm B", "STOCK_BROKER", 183, 45, 36, 6, 11),
    ("Firm C", "STOCK_BROKER", 183, 0, 0, 0, None),
    ("Firm D", "STOCK_BROKER", 183, 45, 33, 9, 64),
    ("Firm E", "RESEARCH_ANALYST", 183, 45, 40, 3, 7),
)

_SHARED = (
    ("40.1.8", "SB-40.1.8-a", "report short collection of margin",
     ("Firm B", "Firm D", "Firm E")),
    ("15.9.1", "SB-15.9.1-a", "file the monthly compliance certificate",
     ("Firm A", "Firm B", "Firm D")),
    ("54.4.2", "SB-54.4.2-a", "maintain the record of client instructions",
     ("Firm B", "Firm D")),
)

_DIVERGENT = (
    (
        "54.4.2",
        "maintain the record of client instructions",
        (
            ("Firm A", "Telephone recordings retained for five years"),
            ("Firm B", "Written confirmations only, retained for three years"),
            ("Firm D", "Telephone recordings retained for five years"),
            ("Firm E", "Written confirmations only, retained for three years"),
        ),
    ),
    (
        "15.10.1.7",
        "report the client complaint within the stated period",
        (
            ("Firm A", "Working days"),
            ("Firm B", "Calendar days"),
            ("Firm D", "Working days"),
        ),
    ),
)


def synthetic_market() -> SyntheticMarket:
    """Build the demonstration. Same firms, same numbers, every time."""
    market = SyntheticMarket()
    market.firms = [
        DemoFirm(
            name=name,
            intermediary=kind,
            certified=certified,
            checked=checked,
            satisfied=satisfied,
            open_tasks=tasks,
            days_since_record=quiet,
        )
        for name, kind, certified, checked, satisfied, tasks, quiet in _FIRMS
    ]
    total = len(market.firms)
    market.shared_gaps = [
        SharedGap(
            clause_id=clause,
            obligation_id=oid,
            requirement=requirement,
            firms=firms,
            total_firms=total,
        )
        for clause, oid, requirement, firms in _SHARED
    ]
    # Worst first: a duty most of the market is missing is the one worth
    # reading again.
    market.shared_gaps.sort(key=lambda g: (-len(g.firms), g.clause_id))
    market.divergences = [
        ObservedDivergence(clause_id=clause, requirement=requirement, readings=readings)
        for clause, requirement, readings in _DIVERGENT
    ]
    return market

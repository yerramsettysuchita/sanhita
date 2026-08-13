"""Whether the gold set has been signed off, and by whom.

Every accuracy figure this product could publish is measured against forty
clauses labelled by hand. Seven of those labels are ones where the hand and the
machine disagree, and they are genuinely arguable: whether a sentence fragment
in a list carries a duty, whether a clause binding a clearing member binds a
stock exchange too.

Those seven cannot be settled by the person who wrote the extractor and they
cannot be settled by the machine. If the labels bend toward what the extractor
happens to do, every score built on them is circular reasoning with extra steps.
So they are the one thing in this repository left deliberately blank, and this
module exists to make the blankness visible rather than convenient.

**Until they are answered, nothing publishes a per-field number.** The harness
still computes everything it can, because the arithmetic is not in doubt, and
every screen carrying a figure says which of the seven are outstanding. A
number quietly published against an unsigned gold set is worse than no number:
it is a number that will be defended in front of a jury by somebody who does
not know it rests on an open question.

Read with a line scan rather than a YAML parser. The file's shape is fixed and
adding a dependency to read seven strings is not a trade worth making.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Ruling", "GoldSetStatus", "read_rulings", "RULINGS_FILE"]

RULINGS_FILE = "GOLD-SET-RULINGS.yaml"

_CLAUSE = re.compile(r"^\s*-\s*clause:\s*[\"']?([^\"'\n]+)[\"']?\s*$")
_FIELD = re.compile(r"^\s*(ruling|question|note|options):\s*(.*)$")
_TOP = re.compile(r"^(signed_off_by|signed_off_on):\s*[\"']?([^\"'\n#]*)")


@dataclass(frozen=True)
class Ruling:
    """One of the seven decisions, answered or not."""

    clause_id: str
    question: str = ""
    answer: str = ""
    note: str = ""
    options: tuple[str, ...] = ()

    @property
    def answered(self) -> bool:
        return bool(self.answer.strip())

    @property
    def valid(self) -> bool:
        """Answered with one of the options it offered.

        A ruling of "probably keep it" is not a ruling, and accepting it would
        put a free-text string into the provenance of a published figure.
        """
        return self.answered and (not self.options or self.answer in self.options)


@dataclass
class GoldSetStatus:
    """Whether accuracy may be published, and what is missing if not."""

    rulings: list[Ruling] = field(default_factory=list)
    signed_off_by: str = ""
    signed_off_on: str = ""
    #: Set when the file is absent or unreadable, which is not the same as
    #: unanswered and must not be reported as it.
    problem: str = ""

    @property
    def total(self) -> int:
        return len(self.rulings)

    @property
    def answered(self) -> list[Ruling]:
        return [r for r in self.rulings if r.answered]

    @property
    def outstanding(self) -> list[Ruling]:
        return [r for r in self.rulings if not r.answered]

    @property
    def invalid(self) -> list[Ruling]:
        return [r for r in self.rulings if r.answered and not r.valid]

    @property
    def complete(self) -> bool:
        """The only thing that unlocks a published per-field figure."""
        return (
            not self.problem
            and bool(self.rulings)
            and not self.outstanding
            and not self.invalid
            and bool(self.signed_off_by.strip())
        )

    @property
    def state(self) -> str:
        return "COMPLETE" if self.complete else "AWAITING_HUMAN_RULINGS"

    def describe(self) -> str:
        if self.problem:
            return f"The gold-set rulings could not be read: {self.problem}"
        if self.complete:
            return (
                f"All {self.total} gold-set rulings were signed off by "
                f"{self.signed_off_by}"
                + (f" on {self.signed_off_on}" if self.signed_off_on else "")
                + ". Per-field accuracy is measured against a settled gold set."
            )
        parts = []
        if self.outstanding:
            clauses = ", ".join(r.clause_id for r in self.outstanding)
            parts.append(
                f"{len(self.outstanding)} of {self.total} rulings are unanswered "
                f"(clauses {clauses})"
            )
        if self.invalid:
            parts.append(
                f"{len(self.invalid)} carry an answer that is not one of the "
                "options offered"
            )
        if not self.signed_off_by.strip() and not self.outstanding:
            parts.append("nobody has signed the gold set off by name")
        return (
            "Per-field accuracy is awaiting human rulings: "
            + "; ".join(parts)
            + ". Until they are settled, no per-field figure is published, "
            "because a number measured against labels the machine could have "
            "bent is circular reasoning with extra steps."
        )


def read_rulings(path: Path | str = RULINGS_FILE) -> GoldSetStatus:
    """Read the rulings file and say whether accuracy may be published."""
    status = GoldSetStatus()
    target = Path(path)
    if not target.is_file():
        status.problem = f"{target} is not on disk"
        return status
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable file
        status.problem = str(exc)
        return status

    current: dict = {}
    found: list[dict] = []
    for line in text.splitlines():
        top = _TOP.match(line)
        if top:
            if top.group(1) == "signed_off_by":
                status.signed_off_by = top.group(2).strip()
            else:
                status.signed_off_on = top.group(2).strip()
            continue
        clause = _CLAUSE.match(line)
        if clause:
            if current:
                found.append(current)
            current = {"clause_id": clause.group(1).strip()}
            continue
        if not current:
            continue
        field_match = _FIELD.match(line)
        if field_match:
            key, value = field_match.group(1), field_match.group(2).strip()
            current[key] = value.strip("\"'")
    if current:
        found.append(current)

    for row in found:
        raw_options = row.get("options", "")
        options = tuple(
            o.strip() for o in raw_options.strip("[]").split(",") if o.strip()
        )
        status.rulings.append(
            Ruling(
                clause_id=row["clause_id"],
                question=row.get("question", ""),
                answer=row.get("ruling", ""),
                note=row.get("note", ""),
                options=options,
            )
        )
    return status
